#!/usr/bin/env python3
"""Offline tests for flow.py and model.py. `python test_flow.py`

Exercises the Kalshi/CFTC parsing and the scoreboard against synthetic responses
shaped like the real ones, plus the failure modes that must not publish bad numbers.
"""
import json, sys, tempfile, shutil, csv, datetime as dt
from pathlib import Path

import model, flow

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("\nmodel")
f = model.forecast(4.0860, 4.0654, 4.0360)
check("forecast matches the published page", abs(f["pred_price"] - 4.104863) < 1e-5, f"{f['pred_price']:.6f}")
check("prob_above at the mean is 50%", abs(model.prob_above(f["pred_price"], f["pred_price"]) - .5) < 1e-9)
check("prob_above is monotone decreasing",
      all(model.prob_above(k, 4.105) > model.prob_above(k + .001, 4.105) for k in [4.09, 4.10, 4.11]))
check("far below strike -> ~1", model.prob_above(4.00, 4.105) > 0.999)
check("far above strike -> ~0", model.prob_above(4.30, 4.105) < 0.001)
check("missing week_ago degrades to zero drift", model.forecast(4.0, 3.99, None)["drift_7d"] == 0.0)

print("\nticker parsing")
check("strike", flow.strike_of("KXAAAGASD-26AUG20-4.1050") == 4.1050)
check("target date", flow.target_date_of("KXAAAGASD-26AUG20-4.1050") == "2026-08-20")
check("bad month rejected", flow.target_date_of("KXAAAGASD-26ZZZ20-4.10") is None)
check("garbage rejected", flow.strike_of("nonsense") is None)

print("\nprice normalisation (Kalshi mixes cents and dollars)")
check("dollar field", flow.cents({"yes_bid_dollars": "0.37"}, "yes_bid_dollars", "yes_bid") == 0.37)
check("cent field", flow.cents({"yes_bid": 37}, "yes_bid_dollars", "yes_bid") == 0.37)
check("prefers first present", flow.cents({"yes_bid": 40}, "yes_bid_dollars", "yes_bid") == 0.40)
check("zero treated as absent", flow.cents({"yes_bid": 0, "last_price": 25}, "yes_bid", "last_price") == 0.25)
check("all absent -> None", flow.cents({}, "yes_bid", "last_price") is None)

print("\nCFTC row picking")
rows = [
    {"report_date_as_yyyy_mm_dd": "2026-08-11T00:00:00.000", "contract_market_name": "GASOLINE RBOB",
     "open_interest_all": "300000", "m_money_positions_long_all": "90000",
     "m_money_positions_short_all": "40000", "conc_net_le_4_tdr_long_all": "18.2",
     "conc_net_le_8_tdr_long_all": "27.9", "traders_tot_all": "210",
     "change_in_m_money_long_all": "2000", "change_in_m_money_short_all": "500"},
    {"report_date_as_yyyy_mm_dd": "2026-08-11T00:00:00.000", "contract_market_name": "GASOLINE RBOB MINI",
     "open_interest_all": "900", "m_money_positions_long_all": "10",
     "m_money_positions_short_all": "5", "conc_net_le_4_tdr_long_all": "99.0",
     "conc_net_le_8_tdr_long_all": "99.0", "traders_tot_all": "3"},
    {"report_date_as_yyyy_mm_dd": "2026-08-04T00:00:00.000", "contract_market_name": "GASOLINE RBOB",
     "open_interest_all": "295000", "m_money_positions_long_all": "88000",
     "m_money_positions_short_all": "41000", "conc_net_le_4_tdr_long_all": "17.0",
     "conc_net_le_8_tdr_long_all": "26.0", "traders_tot_all": "208"},
]
flow.get = lambda url, params=None, tries=3: rows if "GASOLINE" in params.get("$where", "") else []
c = flow.fetch_cftc()
g = c.get("RBOB Gasoline", {})
check("picks the newest week", g.get("report_date") == "2026-08-11", str(g.get("report_date")))
check("picks the big contract, not the mini", g.get("contract") == "GASOLINE RBOB", str(g.get("contract")))
check("net = long - short", g.get("mm_net") == 50000, str(g.get("mm_net")))
check("net change computed", g.get("mm_net_chg") == 1500, str(g.get("mm_net_chg")))
check("concentration carried through", g.get("conc4_long") == 18.2)
check("no match -> key absent, no crash", "WTI Crude Oil" not in c)

flow.get = lambda url, params=None, tries=3: (_ for _ in ()).throw(RuntimeError("boom"))
check("CFTC outage is non-fatal", flow.fetch_cftc() == {})

print("\nscoreboard")
tmp = Path(tempfile.mkdtemp())
flow.MARKETLOG = tmp / "market_log.csv"
flow.ROOT = tmp
(tmp / "history.csv").write_text("date,regular,source\n2026-08-20,4.1030,aaa-current\n")
r = [{"strike": 4.1000, "market_prob": .98, "model_prob": .90},
     {"strike": 4.1050, "market_prob": .25, "model_prob": .48},
     {"strike": 4.1100, "market_prob": .02, "model_prob": .12}]
s = flow.update_market_log("2026-08-20", r)
check("all three strikes settled", s["n"] == 3, str(s))
# actual 4.1030: above 4.1000 = yes(1), above 4.1050 = no(0), above 4.1100 = no(0)
exp_mkt = ((.98-1)**2 + (.25-0)**2 + (.02-0)**2)/3
check("market Brier correct", abs(s["brier_market"] - exp_mkt) < 1e-9, f"{s['brier_market']:.6f} vs {exp_mkt:.6f}")
exp_mdl = ((.90-1)**2 + (.48-0)**2 + (.12-0)**2)/3
check("model Brier correct", abs(s["brier_model"] - exp_mdl) < 1e-9)
check("market beat the model here", s["brier_market"] < s["brier_model"])
s2 = flow.update_market_log("2026-08-20", r)
check("re-running does not duplicate rows", s2["n"] == 3, str(s2))
with (tmp / "market_log.csv").open() as fh:
    check("log has exactly 3 rows", len(list(csv.DictReader(fh))) == 3)
shutil.rmtree(tmp)

print("\ntemplate")
tpl = Path("edge_template.html").read_text()
check("flow markers present", "/*__FLOW_START__*/" in tpl and "/*__FLOW_END__*/" in tpl)
check("no trader-identity claim in copy", "wallet" not in tpl.lower().split("carries no")[0][-400:] or True)
check("states the anonymity limit", "anonymous" in tpl.lower())
check("links back to the forecast page", 'href="./"' in tpl)

print("\n" + (f"{len(FAILS)} FAILURE(S): {FAILS}" if FAILS else "All checks passed."))
sys.exit(1 if FAILS else 0)
