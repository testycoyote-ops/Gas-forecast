#!/usr/bin/env python3
"""Kalshi flow + model-vs-market edge, rendered to edge.html.

What this can and cannot do, stated plainly because it shapes everything below:

  * Kalshi's public trade feed carries NO trader identity -- no account, no wallet,
    no pseudonym. "Whale tracking" here therefore means large PRINTS, not named
    traders. A big block hitting the offer is a whale footprint; whose foot it is
    cannot be known from public data.
  * The CFTC panel is the one place real whale identity-ish data exists: the net
    position of the largest 4 and 8 reportable traders in RBOB/WTI futures, and
    hedge-fund ("managed money") net length. Weekly, official, free.

Run by .github/workflows/flow.yml. Non-fatal by design: Kalshi or CFTC being down
leaves the previous page in place rather than publishing a broken one.
"""
from __future__ import annotations
import csv, json, re, sys, time, datetime as dt
from pathlib import Path

import requests

import model

ROOT      = Path(__file__).parent
SNAPSHOT  = ROOT / "snapshot.json"
TEMPLATE  = ROOT / "edge_template.html"
OUTPUT    = ROOT / "edge.html"
FLOWJSON  = ROOT / "flow.json"
MARKETLOG = ROOT / "market_log.csv"

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXAAAGASD"                       # daily "US gas price above X" markets
CFTC   = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
UA     = {"User-Agent": "gas-forecast/1.0 (+github pages hobby project)",
          "Accept": "application/json"}

BIG_TRADE_MIN = 50.0        # contracts; floor for "large print"
BIG_TRADE_PCT = 0.90        # or top decile of the session, whichever is larger
MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]


def log(m: str) -> None:
    print(f"[flow] {m}", flush=True)


def warn(m: str) -> None:
    print(f"::warning::{m}", flush=True)


def get(url: str, params: dict | None = None, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:                    # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


# ------------------------------------------------------------------ price helpers
def cents(m: dict, *keys) -> float | None:
    """Kalshi returns some prices as cent integers and some as dollar strings.
    Normalise whatever is present to a 0-1 probability."""
    for k in keys:
        v = m.get(k)
        if v in (None, "", 0):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == 0.0:                  # an empty side, not a real 0c quote
            continue
        if k.endswith("_dollars"):
            return f
        return f / 100.0 if f > 1.0 else f
    return None


def strike_of(ticker: str) -> float | None:
    m = re.search(r"-(\d+\.\d+)$", ticker)
    return float(m.group(1)) if m else None


def target_date_of(ticker: str) -> str | None:
    """KXAAAGASD-26AUG20-4.1050 -> 2026-08-20"""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in MONTHS:
        return None
    return f"20{yy}-{MONTHS.index(mon)+1:02d}-{dd}"


# ----------------------------------------------------------------------- Kalshi
def fetch_ladder() -> tuple[list[dict], str | None, bool]:
    """Open strike ladder for the live daily event. Falls back to the most recent
    settled event overnight, when nothing is open."""
    live = True
    data = get(f"{KALSHI}/markets", {"series_ticker": SERIES, "status": "open", "limit": 200})
    mkts = data.get("markets", [])
    if not mkts:
        live = False
        log("no open markets (between sessions) - falling back to most recent event")
        data = get(f"{KALSHI}/markets", {"series_ticker": SERIES, "limit": 200})
        mkts = data.get("markets", [])

    dated = [(target_date_of(m.get("ticker", "")), m) for m in mkts]
    dated = [(d, m) for d, m in dated if d]
    if not dated:
        return [], None, live
    target = max(d for d, _ in dated) if live else max(d for d, _ in dated)
    ladder = [m for d, m in dated if d == target]
    ladder.sort(key=lambda m: strike_of(m["ticker"]) or 0)
    return ladder, target, live


def fetch_trades(ticker: str, limit: int = 200) -> list[dict]:
    try:
        return get(f"{KALSHI}/markets/trades", {"ticker": ticker, "limit": limit}).get("trades", [])
    except Exception as e:                        # noqa: BLE001
        warn(f"trades fetch failed for {ticker}: {e}")
        return []


# ------------------------------------------------------------------------- CFTC
def fetch_cftc() -> dict:
    """Largest-trader concentration and hedge-fund net length in RBOB and WTI.

    Socrata's SoQL filters are easy to get subtly wrong, so this asks broadly and
    filters in Python, then reports plainly if it found nothing.
    """
    fields = ("report_date_as_yyyy_mm_dd,contract_market_name,market_and_exchange_names,"
              "open_interest_all,m_money_positions_long_all,m_money_positions_short_all,"
              "change_in_m_money_long_all,change_in_m_money_short_all,"
              "conc_net_le_4_tdr_long_all,conc_net_le_4_tdr_short_all,"
              "conc_net_le_8_tdr_long_all,conc_net_le_8_tdr_short_all,traders_tot_all")
    out: dict = {}
    for label, needles in [("RBOB Gasoline", ("GASOLINE", "RBOB")),
                           ("WTI Crude Oil", ("CRUDE OIL", "WTI"))]:
        try:
            rows = get(CFTC, {
                "$select": fields,
                "$where": " OR ".join(f"upper(contract_market_name) like '%{n}%'" for n in needles),
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 60,
            })
        except Exception as e:                    # noqa: BLE001
            warn(f"CFTC fetch failed for {label}: {e}")
            continue
        rows = [r for r in rows
                if any(n in (r.get("contract_market_name") or "").upper() for n in needles)]
        if not rows:
            warn(f"CFTC returned no rows matching {label}")
            continue
        newest = rows[0]["report_date_as_yyyy_mm_dd"]
        cur = [r for r in rows if r["report_date_as_yyyy_mm_dd"] == newest]
        # pick the largest contract by open interest (avoids mini/spread variants)
        r = max(cur, key=lambda x: float(x.get("open_interest_all") or 0))
        f = lambda k: float(r[k]) if r.get(k) not in (None, "") else None   # noqa: E731
        long_, short_ = f("m_money_positions_long_all"), f("m_money_positions_short_all")
        out[label] = {
            "report_date": newest[:10],
            "contract": r.get("contract_market_name"),
            "open_interest": f("open_interest_all"),
            "mm_long": long_, "mm_short": short_,
            "mm_net": (long_ - short_) if (long_ is not None and short_ is not None) else None,
            "mm_net_chg": ((f("change_in_m_money_long_all") or 0) -
                           (f("change_in_m_money_short_all") or 0)),
            "conc4_long": f("conc_net_le_4_tdr_long_all"),
            "conc4_short": f("conc_net_le_4_tdr_short_all"),
            "conc8_long": f("conc_net_le_8_tdr_long_all"),
            "conc8_short": f("conc_net_le_8_tdr_short_all"),
            "traders": f("traders_tot_all"),
        }
        log(f"CFTC {label}: {r.get('contract_market_name')} @ {newest[:10]}")
    return out


# -------------------------------------------------------------------- scoreboard
def update_market_log(target: str, rows: list[dict]) -> dict:
    """Append today's model-vs-market probabilities, then settle any past rows whose
    outcome is now known from history.csv. Yields running Brier scores -- the only
    honest way to find out which of the two is actually better calibrated."""
    log_rows: list[dict] = []
    if MARKETLOG.exists():
        with MARKETLOG.open() as f:
            log_rows = list(csv.DictReader(f))

    key = {(r["target_date"], r["strike"]) for r in log_rows}
    for r in rows:
        if r["market_prob"] is None:
            continue
        k = (target, f"{r['strike']:.4f}")
        if k in key:
            for lr in log_rows:
                if (lr["target_date"], lr["strike"]) == k:
                    lr["market_prob"] = f"{r['market_prob']:.4f}"
                    lr["model_prob"] = f"{r['model_prob']:.4f}"
        else:
            log_rows.append({"target_date": target, "strike": f"{r['strike']:.4f}",
                             "market_prob": f"{r['market_prob']:.4f}",
                             "model_prob": f"{r['model_prob']:.4f}", "outcome": ""})

    # settle
    actual: dict[str, float] = {}
    hist = ROOT / "history.csv"
    if hist.exists():
        with hist.open() as f:
            actual = {r["date"]: float(r["regular"]) for r in csv.DictReader(f)}
    settled = 0
    for lr in log_rows:
        if lr.get("outcome") in ("", None) and lr["target_date"] in actual:
            lr["outcome"] = "1" if actual[lr["target_date"]] > float(lr["strike"]) else "0"
            settled += 1
    if settled:
        log(f"settled {settled} logged strikes against published AAA values")

    with MARKETLOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["target_date", "strike", "market_prob",
                                          "model_prob", "outcome"])
        w.writeheader()
        w.writerows(log_rows)

    done = [r for r in log_rows if r.get("outcome") in ("0", "1")]
    if not done:
        return {"n": 0}
    brier = lambda k: sum((float(r[k]) - int(r["outcome"])) ** 2 for r in done) / len(done)  # noqa: E731
    return {"n": len(done), "days": len({r["target_date"] for r in done}),
            "brier_model": brier("model_prob"), "brier_market": brier("market_prob")}


# ------------------------------------------------------------------------- build
def main() -> None:
    if not SNAPSHOT.exists():
        log("snapshot.json missing - run build.py first; nothing to compare against")
        sys.exit(0)
    snap = json.loads(SNAPSHOT.read_text())
    fc = snap["forecast"]
    mu, sigma = fc["pred_price"], fc["sigma"]

    try:
        ladder, target, live = fetch_ladder()
    except Exception as e:                        # noqa: BLE001
        warn(f"Kalshi unreachable: {e}. Leaving the existing page in place.")
        sys.exit(0)
    if not ladder:
        warn("no Kalshi gas markets found; leaving the existing page in place.")
        sys.exit(0)
    log(f"ladder: {len(ladder)} strikes for {target} (live={live})")

    rows, trades = [], []
    for m in ladder:
        t = m["ticker"]
        k = strike_of(t)
        if k is None:
            continue
        bid, ask = cents(m, "yes_bid_dollars", "yes_bid"), cents(m, "yes_ask_dollars", "yes_ask")
        last = cents(m, "last_price_dollars", "last_price")
        # A settled or untended market shows an empty book: bid 0 / ask 1 midpoints to
        # a meaningless 50c. Only trust the mid when the spread is actually tight.
        wide = bid is None or ask is None or ask <= bid or (ask - bid) > 0.25
        mkt = last if wide else (bid + ask) / 2
        mp = model.prob_above(k, mu, sigma)
        vol = float(m.get("volume_fp") or m.get("volume") or 0)
        rows.append({
            "ticker": t, "strike": k, "market_prob": mkt, "model_prob": mp,
            "edge": (mp - mkt) if mkt is not None else None,
            "bid": bid, "ask": ask, "last": last, "volume": vol,
            "open_interest": float(m.get("open_interest_fp") or m.get("open_interest") or 0),
        })
        if vol > 0:
            for tr in fetch_trades(t):
                trades.append({
                    "ticker": t, "strike": k,
                    "count": float(tr.get("count_fp") or tr.get("count") or 0),
                    "yes_price": cents(tr, "yes_price_dollars", "yes_price"),
                    "side": tr.get("taker_outcome_side") or tr.get("taker_side"),
                    "block": bool(tr.get("is_block_trade")),
                    "time": tr.get("created_time"),
                })

    # large prints: top decile of the session, but never below the floor
    # Percentiles are meaningless on a thin session -- with 7 trades the "top decile"
    # is a single print. Only switch to a percentile once the sample can support one.
    sizes = sorted(t["count"] for t in trades)
    thr = BIG_TRADE_MIN
    if len(sizes) >= 50:
        idx = min(int(len(sizes) * BIG_TRADE_PCT), len(sizes) - 1)
        thr = max(BIG_TRADE_MIN, sizes[idx])
    big = sorted([t for t in trades if t["count"] >= thr or t["block"]],
                 key=lambda t: t["time"] or "", reverse=True)[:40]
    log(f"{len(trades)} trades scanned, {len(big)} large prints (threshold {thr:.0f})")

    priced = [r for r in rows if r["market_prob"] is not None]

    # The headline divergence must come from a strike that actually trades. Deep
    # in/out-of-the-money strikes carry stale quotes -- one showed 50c on a contract
    # the model puts at 100% -- and those are quote noise, not disagreement. Require
    # real volume and a price off the 1c/99c pins before a strike can be the headline.
    vmax = max((r["volume"] for r in priced), default=0.0)
    for r in priced:
        r["liquid"] = bool(r["volume"] >= max(25.0, 0.05 * vmax)
                           and 0.02 <= r["market_prob"] <= 0.98)
    liquid = [r for r in priced if r["liquid"]]
    # If nothing is liquid -- which is exactly what a settled session looks like, every
    # strike pinned to 0 or 1 -- there is no divergence worth a headline. Say so rather
    # than promoting quote noise to the top of the page.
    best = max(liquid, key=lambda r: abs(r["edge"])) if liquid else None
    log(f"{len(liquid)}/{len(priced)} strikes liquid; headline = "
        f"{best['strike'] if best else 'n/a'}")
    score = update_market_log(target, priced)
    cftc = fetch_cftc()

    payload = {
        "target": target, "live": live,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%b %-d, %Y at %H:%M UTC"),
        "forecast": fc, "snapshot": {k: snap.get(k) for k in ("asOf", "today")},
        "rows": rows, "best": best, "trades": big, "trade_threshold": thr,
        "n_trades_scanned": len(trades), "cftc": cftc, "score": score,
    }
    FLOWJSON.write_text(json.dumps(payload, indent=1))

    tpl = TEMPLATE.read_text()
    out = re.sub(r"/\*__FLOW_START__\*/.*?/\*__FLOW_END__\*/",
                 lambda _: "/*__FLOW_START__*/\nconst FLOW = " + json.dumps(payload) + ";\n/*__FLOW_END__*/",
                 tpl, flags=re.S)
    if out == tpl:
        print("::error::edge_template.html markers not found", flush=True)
        sys.exit(1)
    OUTPUT.write_text(out)
    log(f"wrote {OUTPUT.name} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
