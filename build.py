#!/usr/bin/env python3
"""Fetch today's AAA national average, update the history file, rebuild index.html.

Run by .github/workflows/update.yml every morning. Designed to fail loudly rather
than publish a wrong number: every scraped value is sanity-checked before it is
allowed into the history file, and a parse failure exits non-zero so the GitHub
Actions run goes red and emails you.
"""
from __future__ import annotations
import csv, html, importlib, json, os, re, sys, datetime as dt
from pathlib import Path

import requests

import calibrate
import model

ROOT     = Path(__file__).parent
HISTORY  = ROOT / "history.csv"
TEMPLATE = ROOT / "template.html"
OUTPUT   = ROOT / "index.html"
SNAPOUT  = ROOT / "snapshot.json"

AAA_URL = "https://gasprices.aaa.com/"
EIA_URL = "https://www.eia.gov/todayinenergy/prices.php"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# A published US national average outside this range means we parsed the wrong thing.
PRICE_MIN, PRICE_MAX = 1.00, 12.00
# AAA is a ~120k-station rolling survey; it physically cannot move this much in a day.
MAX_DAILY_JUMP = 0.25


# ----------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def fail(msg: str) -> "None":
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                     timeout=45)
    r.raise_for_status()
    return r.text


def to_text(raw: str) -> str:
    """HTML -> flat text, so parsing does not depend on the page's markup structure."""
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def money(s: str) -> float:
    return float(s.replace("$", "").replace(",", ""))


# ------------------------------------------------------------------------------- AAA
def parse_aaa(raw: str) -> dict:
    """Pull the national-average table. Regular is the first of the four grade columns.

    Strategy 1 walks the labelled rows ("Current Avg.", "Yesterday Avg.", ...) and takes
    the first dollar figure after each -- robust to the table being restructured, since it
    only relies on the labels and the column order, both of which have been stable for
    years. Strategy 2 is a positional fallback if the labels ever change.
    """
    text = to_text(raw)
    out: dict[str, float] = {}

    labels = {
        "today":     r"Current\s+Avg",
        "yesterday": r"Yesterday\s+Avg",
        "weekAgo":   r"Week\s+Ago\s+Avg",
        "monthAgo":  r"Month\s+Ago\s+Avg",
        "yearAgo":   r"Year\s+Ago\s+Avg",
    }
    for key, pat in labels.items():
        m = re.search(pat + r"\.?\s*((?:\$\s?\d+\.\d{2,4}\s*){1,4})", text, re.I)
        if m:
            nums = re.findall(r"\$\s?(\d+\.\d{2,4})", m.group(1))
            if nums:
                out[key] = float(nums[0])          # column 1 = Regular

    # also grab the other three grades of the current row, for the history file
    m = re.search(r"Current\s+Avg\.?\s*((?:\$\s?\d+\.\d{2,4}\s*){4})", text, re.I)
    grades = [float(x) for x in re.findall(r"\$\s?(\d+\.\d{2,4})", m.group(1))] if m else []

    if "today" not in out:                                   # fallback
        m = re.search(r"National\s+Average\D{0,80}?\$\s?(\d+\.\d{2,4})", text, re.I)
        if m:
            out["today"] = float(m.group(1))
            log("used fallback parser for today's price")

    if "today" not in out or "yesterday" not in out:
        fail("could not parse AAA national average (page layout may have changed). "
             f"Parsed keys: {sorted(out)}")

    return {"prices": out,
            "grades": grades if len(grades) == 4 else [out["today"], None, None, None]}


MONTHS = ("January February March April May June July August "
          "September October November December").split()


def page_dates(raw: str) -> list[dt.date]:
    """Every date AAA prints on the page, most specific format first.

    Two formats matter. The page's own "Price as of:" stamp is numeric —
    `8/21/26` — which is what the live site actually serves; the long form
    ("August 21, 2026") turns up in prose elsewhere on the page. The numeric
    stamp is checked first because it is the one attached to the price table.

    Kept separate from resolve_date() so the backfill can date an archived page
    against its snapshot timestamp instead of against the wall clock.
    """
    text = to_text(raw)
    found: list[dt.date] = []

    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|20\d{2})\b", text):
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            found.append(dt.date(yy + 2000 if yy < 100 else yy, mm, dd))
        except ValueError:
            continue

    for m in re.finditer(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})\b", text):
        try:
            found.append(dt.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2))))
        except ValueError:
            continue

    return found


def resolve_date(raw: str, now: dt.datetime | None = None) -> dt.date:
    """Which calendar day does AAA's "Current Avg" actually belong to?

    Do NOT just take the current Eastern date. AAA publishes overnight, so between
    midnight ET and roughly 5am ET the page still shows *yesterday's* figure while
    the ET clock has already rolled over -- taking the clock date there silently
    files yesterday's price under today and corrupts the history.

    So: trust the date AAA prints on its own page when it is present and sane, and
    fall back to a clock rule that only advances the date after 6am ET.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(dt.timezone(dt.timedelta(hours=-4)))

    for found in page_dates(raw):
        if abs((found - et.date()).days) <= 2:          # sane relative to now
            log(f"date from AAA page: {found}")
            return found
        log(f"ignoring implausible page date {found} (ET today is {et.date()})")

    d = et.date() if et.hour >= 6 else et.date() - dt.timedelta(days=1)
    log(f"no usable page date; using clock rule -> {d} (ET now {et:%Y-%m-%d %H:%M})")
    return d


def parse_eia(raw: str) -> dict:
    """Wholesale context. Best-effort -- never fatal, it is display-only."""
    text = to_text(raw)
    out: dict = {}
    try:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})\s*Close", text, re.I)
        if m:
            out["asOf"] = m.group(1)
        for key, pat in [("wti", r"WTI\D{0,40}?(\d{2,3}\.\d{2})"),
                         ("brent", r"Brent\D{0,40}?(\d{2,3}\.\d{2})"),
                         ("rbobNYH", r"New\s*York\s*Harbor\D{0,40}?(\d\.\d{2,3})")]:
            m = re.search(pat, text, re.I)
            if m:
                out[key] = float(m.group(1))
    except Exception as e:                                    # pragma: no cover
        log(f"EIA parse skipped: {e}")
    return out


# --------------------------------------------------------------------------- history
def read_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    with HISTORY.open() as f:
        return [{"date": r["date"], "regular": float(r["regular"]),
                 "source": r.get("source", "")} for r in csv.DictReader(f) if r.get("date")]


def write_history(rows: list[dict]) -> None:
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "regular", "source"])
        w.writeheader()
        w.writerows(rows)


def validate(price: float, prev: float | None, label: str) -> None:
    if not (PRICE_MIN <= price <= PRICE_MAX):
        fail(f"{label} price ${price} is outside the sane range "
             f"${PRICE_MIN}-${PRICE_MAX}; refusing to record it.")
    if prev is not None and abs(price - prev) > MAX_DAILY_JUMP:
        fail(f"{label} price ${price} jumped ${abs(price-prev):.3f} from the last "
             f"recorded ${prev} (limit ${MAX_DAILY_JUMP}); refusing to record it. "
             "If this is a real move, raise MAX_DAILY_JUMP.")


# ----------------------------------------------------------------------------- build
def main() -> None:
    log("fetching AAA…")
    raw_aaa = get(AAA_URL)
    aaa = parse_aaa(raw_aaa)
    p = aaa["prices"]

    try:
        eia = parse_eia(get(EIA_URL))
        log(f"EIA: {eia}")
    except Exception as e:
        eia = {}
        log(f"EIA fetch failed (non-fatal): {e}")

    today = resolve_date(raw_aaa)
    yday = today - dt.timedelta(days=1)

    hist = read_history()
    known = {r["date"]: r for r in hist}
    prev = hist[-1]["regular"] if hist else None

    validate(p["today"], prev, "today's")
    validate(p["yesterday"], prev, "yesterday's")

    # The backfill trick: every page view exposes yesterday's value too, so a single
    # missed run is recoverable on the next one.
    added = []
    for d, v, src in [(yday.isoformat(), p["yesterday"], "aaa-yesterday-field"),
                      (today.isoformat(), p["today"], "aaa-current")]:
        if known.get(d, {}).get("regular") != v:
            known[d] = {"date": d, "regular": v, "source": src}
            added.append(f"{d}=${v}")
    if added:
        log("history updated: " + ", ".join(added))
    else:
        log("history already current")
    write_history(list(known.values()))

    # Re-estimate PHI and SIGMA from the history we just extended, then reload the
    # model so this run's forecast uses the fresh values. Every case feeding the
    # calibration is fully in the past -- it needs day t+1 to score a forecast made
    # on day t -- so there is no look-ahead into the day being predicted.
    try:
        prm = calibrate.calibrate_file(HISTORY)
        importlib.reload(model)
        log(f"calibrated on {prm['n_cases']} cases: phi={model.PHI} sigma=${model.SIGMA} "
            f"(direct phi {prm['phi_direct']}, resid rmse {prm['resid_rmse']})")
    except Exception as e:                          # noqa: BLE001
        log(f"calibration failed (non-fatal, priors stand): {e}")

    rows = sorted(known.values(), key=lambda r: r["date"])
    series = [[r["date"], round(r["regular"], 4)] for r in rows][-90:]

    snapshot = {
        "asOf": today.isoformat(),
        "today": p["today"], "yesterday": p["yesterday"],
        "weekAgo": p.get("weekAgo", p["today"]),
        "monthAgo": p.get("monthAgo"), "yearAgo": p.get("yearAgo"),
        "wholesale": {"asOf": eia.get("asOf", ""), "wti": eia.get("wti"),
                      "brent": eia.get("brent"), "rbobNYH": eia.get("rbobNYH")},
    }
    if "weekAgo" not in p:
        log("WARNING: no week-ago value parsed; drift falls back to zero")

    built = dt.datetime.now(dt.timezone.utc).strftime("%b %-d, %Y at %H:%M UTC")
    data_js = (f"const SNAPSHOT = {json.dumps(snapshot, indent=2)};\n"
               f"const HISTORY = {json.dumps(series)};\n"
               f"const PARAMS = {json.dumps(model.PARAMS, indent=2)};\n"
               f"const BUILT = {json.dumps(built)};\n")

    tpl = TEMPLATE.read_text()
    out = re.sub(r"/\*__DATA_START__\*/.*?/\*__DATA_END__\*/",
                 lambda _: "/*__DATA_START__*/\n" + data_js + "/*__DATA_END__*/",
                 tpl, flags=re.S)
    if out == tpl:
        fail("template markers /*__DATA_START__*/ … /*__DATA_END__*/ not found")

    OUTPUT.write_text(out)
    log(f"wrote {OUTPUT.name}: {len(series)} history points, "
        f"today ${p['today']}, {len(out):,} bytes")

    # snapshot.json is the contract between this script and flow.py: it carries the
    # forecast so the edge page never re-derives (and never disagrees with) the model.
    fc = model.forecast(p["today"], p["yesterday"], p.get("weekAgo"))
    SNAPOUT.write_text(json.dumps({**snapshot, "forecast": fc, "built": built}, indent=1))
    log(f"wrote {SNAPOUT.name}: forecast ${fc['pred_price']:.4f} ± {fc['sigma']}")


if __name__ == "__main__":
    main()
