#!/usr/bin/env python3
"""Rebuild AAA daily history from archived snapshots of gasprices.aaa.com.

Why this exists
---------------
The model's parameters can only be as good as the history they are fitted to,
and that history is thin: the daily logger only started in June 2026, it has
gaps, and after discarding every pair that straddles a gap there were just NINE
usable next-day observations to estimate PHI and SIGMA from. Nine. Every
conclusion about the model's accuracy currently rests on that.

There is no free bulk download of AAA's daily national average -- that was
checked and confirmed. But the page itself is the archive: every single view of
gasprices.aaa.com prints five separately dated numbers (current, yesterday, week
ago, month ago, year ago). So one archived snapshot is worth several dated
observations, and consecutive snapshots reconstruct consecutive days.

Run this once (or whenever you want to extend the range) and it merges whatever
it can recover into history.csv.

Where it runs
-------------
On a GitHub Actions runner, via .github/workflows/backfill.yml -- push the
button on the Actions tab. It is a one-shot job, not a schedule.

What it will not do
-------------------
Overwrite a better number with a worse one. Sources are ranked, and a value
already recorded from a stronger source is never downgraded. Where two sources
disagree about the same day the conflict is reported rather than silently
resolved, because a disagreement means one of the two readings is wrong and you
want to know which.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, json, re, sys, time
from pathlib import Path

import requests

import build                     # reuse the battle-tested parser and rails

ROOT    = Path(__file__).parent
HISTORY = ROOT / "history.csv"

CDX  = "http://web.archive.org/cdx/search/cdx"
WB   = "http://web.archive.org/web/{ts}id_/https://gasprices.aaa.com/"
SITE = "gasprices.aaa.com"
UA   = {"User-Agent": "gas-forecast-backfill/1.0 (+github pages hobby project)"}

# Highest confidence first. "current" is the number AAA is publishing that day;
# "yesterday" and "week ago" are exact, unambiguous offsets. Month- and year-ago
# are calendar-relative and AAA does not document the convention, so they are
# off by default -- a point filed one day wrong is worse than no point at all,
# because it manufactures two fake daily changes on either side of itself.
SOURCE_RANK = {
    "aaa-current":           0,
    "aaa-yesterday-field":   1,
    "aaa-week-ago-field":    2,
    "aaa-month-ago-field":   3,
    "aaa-year-ago-field":    4,
}
OFFSETS = [                       # (parsed key, days back, source label, exact?)
    ("today",     0,   "aaa-current",         True),
    ("yesterday", 1,   "aaa-yesterday-field", True),
    ("weekAgo",   7,   "aaa-week-ago-field",  True),
    ("monthAgo",  None, "aaa-month-ago-field", False),
    ("yearAgo",   None, "aaa-year-ago-field",  False),
]

# Same rails build.py enforces, for the same reason.
PRICE_MIN, PRICE_MAX = build.PRICE_MIN, build.PRICE_MAX
# Two readings of the same day that differ by more than this are a real conflict,
# not a rounding artefact. AAA publishes to 4dp and occasionally revises the last.
CONFLICT_TOL = 0.0005


def log(m: str) -> None:
    print(f"[backfill] {m}", flush=True)


def month_back(d: dt.date) -> dt.date:
    """AAA's 'Month Ago' is the same day-of-month one month earlier, clamped."""
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)


def list_snapshots(start: str, end: str, per_day: int = 1) -> list[str]:
    """Wayback timestamps for the AAA page over [start, end].

    collapse=timestamp:8 asks the CDX server for at most one capture per calendar
    day, which is all we need -- the page only changes once a day anyway.
    """
    params = {"url": SITE, "from": start, "to": end, "output": "json",
              "fl": "timestamp,statuscode", "collapse": f"timestamp:{8 if per_day == 1 else 10}",
              "filter": "statuscode:200", "limit": "5000"}
    r = requests.get(CDX, params=params, headers=UA, timeout=120)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return []
    return [row[0] for row in rows[1:]]        # row 0 is the header


def snapshot_date(raw: str, ts: str) -> dt.date | None:
    """Date the archived page, anchored on the snapshot timestamp rather than now.

    A capture taken at 03:00 UTC on the 21st is showing the 20th's number, so the
    printed date is authoritative and the timestamp is only a sanity anchor.
    """
    anchor = dt.date(int(ts[0:4]), int(ts[4:6]), int(ts[6:8]))
    for d in build.page_dates(raw):
        if abs((d - anchor).days) <= 3:
            return d
    return None


def harvest(ts: str, use_fuzzy: bool) -> list[tuple[str, float, str]]:
    """One snapshot -> the dated observations it yields."""
    raw = requests.get(WB.format(ts=ts), headers=UA, timeout=90).text
    try:
        parsed = build.parse_aaa(raw)["prices"]
    except SystemExit:                 # parse_aaa calls fail() -> sys.exit on bad markup
        log(f"  {ts}: unparseable, skipped")
        return []
    d = snapshot_date(raw, ts)
    if d is None:
        log(f"  {ts}: no usable page date, skipped")
        return []

    out = []
    for key, back, src, exact in OFFSETS:
        if key not in parsed or (not exact and not use_fuzzy):
            continue
        v = parsed[key]
        if not (PRICE_MIN <= v <= PRICE_MAX):
            continue
        if key == "monthAgo":
            when = month_back(d)
        elif key == "yearAgo":
            when = d.replace(year=d.year - 1) if (d.month, d.day) != (2, 29) else d - dt.timedelta(365)
        else:
            when = d - dt.timedelta(days=back)
        out.append((when.isoformat(), round(v, 4), src))
    return out


def merge(existing: list[dict], found: list[tuple[str, float, str]]) -> tuple[dict, list, list]:
    """Fold harvested points into the history, best source wins, conflicts reported."""
    book = {r["date"]: {"date": r["date"], "regular": float(r["regular"]),
                        "source": r.get("source", "aaa-current")} for r in existing}
    added, conflicts = [], []

    # Strongest sources first, so a weaker one can never claim the slot first.
    for date, val, src in sorted(found, key=lambda x: SOURCE_RANK[x[2]]):
        cur = book.get(date)
        if cur is None:
            book[date] = {"date": date, "regular": val, "source": src}
            added.append((date, val, src))
            continue
        if abs(cur["regular"] - val) > CONFLICT_TOL:
            better = SOURCE_RANK[src] < SOURCE_RANK.get(cur["source"], 9)
            conflicts.append((date, cur["regular"], cur["source"], val, src, better))
            if better:
                book[date] = {"date": date, "regular": val, "source": src}
    return book, added, conflicts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20250101", help="YYYYMMDD")
    ap.add_argument("--end", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--fuzzy", action="store_true",
                    help="also take month-ago/year-ago fields (calendar-relative, "
                         "so a mis-dated point can manufacture fake daily changes)")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between fetches")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    log(f"listing snapshots {a.start}..{a.end}")
    try:
        stamps = list_snapshots(a.start, a.end)
    except Exception as e:                                   # noqa: BLE001
        print(f"::error::CDX listing failed: {e}")
        sys.exit(1)
    log(f"{len(stamps)} snapshots to try")
    if not stamps:
        log("nothing to do")
        return

    found: list[tuple[str, float, str]] = []
    ok = bad = 0
    for i, ts in enumerate(stamps, 1):
        try:
            got = harvest(ts, a.fuzzy)
            found += got
            ok += bool(got)
        except Exception as e:                               # noqa: BLE001
            bad += 1
            log(f"  {ts}: fetch failed ({e})")
        if i % 25 == 0:
            log(f"  ... {i}/{len(stamps)} ({len(found)} observations so far)")
        time.sleep(a.sleep)

    log(f"harvested {len(found)} observations from {ok} snapshots ({bad} failed)")

    rows = list(csv.DictReader(HISTORY.open())) if HISTORY.exists() else []
    before = len(rows)
    book, added, conflicts = merge(rows, found)

    if conflicts:
        log(f"{len(conflicts)} conflicting readings:")
        for d, ov, os_, nv, ns, took in conflicts[:25]:
            log(f"  {d}: had {ov} ({os_}), saw {nv} ({ns}) -> "
                + ("replaced" if took else "kept existing"))

    ordered = sorted(book.values(), key=lambda r: r["date"])
    log(f"history {before} -> {len(ordered)} rows (+{len(added)} new)")

    # The number that actually matters: usable next-day cases for calibration.
    import calibrate
    cases = calibrate.build_cases([{"date": r["date"], "regular": str(r["regular"])}
                                   for r in ordered])
    log(f"usable next-day forecast cases: {len(cases)} (was 9 before backfill)")

    if a.dry_run:
        log("dry run - history.csv not written")
        return

    with HISTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "regular", "source"])
        w.writeheader()
        for r in ordered:
            w.writerow({"date": r["date"], "regular": r["regular"], "source": r["source"]})
    log(f"wrote {HISTORY.name}")

    p = calibrate.calibrate_file(HISTORY)
    log(f"recalibrated: phi={p['phi']} (direct {p['phi_direct']} +/- {p['phi_direct_se']}) "
        f"sigma=${p['sigma']} on {p['n_cases']} cases")


if __name__ == "__main__":
    main()
