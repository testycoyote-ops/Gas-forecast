#!/usr/bin/env python3
"""Offline tests for build.py — run with `python test_build.py`.

The scraper is the fragile part of this project, so it is exercised against several
plausible markup shapes (table, div grid, extra whitespace, entity-encoded, 3- vs
4-decimal prices) plus the failure cases that must NOT silently produce a number.
"""
import re, sys, importlib
from pathlib import Path
import build

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------- markup fixtures
TABLE = """
<html><body><h1>Today's AAA National Average <strong>$4.086</strong></h1>
<table class="table-mob"><thead><tr><th></th><th>Regular</th><th>Mid-Grade</th>
<th>Premium</th><th>Diesel</th></tr></thead><tbody>
<tr><td>Current Avg.</td><td>$4.086</td><td>$4.596</td><td>$4.979</td><td>$5.504</td></tr>
<tr><td>Yesterday Avg.</td><td>$4.065</td><td>$4.574</td><td>$4.958</td><td>$5.468</td></tr>
<tr><td>Week Ago Avg.</td><td>$4.036</td><td>$4.548</td><td>$4.933</td><td>$5.358</td></tr>
<tr><td>Month Ago Avg.</td><td>$3.998</td><td>$4.492</td><td>$4.874</td><td>$5.101</td></tr>
<tr><td>Year Ago Avg.</td><td>$3.130</td><td>$3.617</td><td>$3.974</td><td>$3.698</td></tr>
</tbody></table><script>var junk="Current Avg. $9.999";</script></body></html>"""

DIVS = """<div class="prices"><div class="row"><span>Current Avg.</span>
<span>$4.0860</span><span>$4.5958</span><span>$4.9788</span><span>$5.5042</span></div>
<div class="row"><span>Yesterday&nbsp;Avg.</span><span>$4.0654</span><span>$4.5740</span>
<span>$4.9579</span><span>$5.4677</span></div><div class="row"><span>Week Ago Avg</span>
<span>$4.0360</span><span>$4.5484</span><span>$4.9333</span><span>$5.3575</span></div>
<div class="row"><span>Month Ago Avg.</span><span>$3.9975</span><span>$4.49</span>
<span>$4.87</span><span>$5.10</span></div></div>"""

SPACED = TABLE.replace("$4.086", "$ 4.086").replace("Current Avg.", "Current   Avg .")

print("\nAAA parser")
for label, fixture, want_today, want_yest in [
    ("plain table",        TABLE,  4.086,  4.065),
    ("div grid, 4dp, nbsp", DIVS,  4.0860, 4.0654),
]:
    got = build.parse_aaa(fixture)["prices"]
    check(f"{label}: today", abs(got["today"] - want_today) < 1e-9, f"got {got['today']}")
    check(f"{label}: yesterday", abs(got["yesterday"] - want_yest) < 1e-9, f"got {got['yesterday']}")
    check(f"{label}: weekAgo present", "weekAgo" in got, f"got {got.get('weekAgo')}")

g = build.parse_aaa(TABLE)
check("grades: all four grades captured", g["grades"] == [4.086, 4.596, 4.979, 5.504], str(g["grades"]))
check("script tag ignored (no $9.999 leak)", g["prices"]["today"] != 9.999)

# --------------------------------------------------------------- failure handling
print("\nFailure handling (these must exit, not guess)")
for label, fixture in [("empty page", "<html></html>"),
                       ("labels renamed", "<p>Average Today $4.09</p>")]:
    try:
        build.parse_aaa(fixture)
        check(f"{label}: refuses to guess", False, "returned a value!")
    except SystemExit:
        check(f"{label}: refuses to guess", True)

print("\nValidation guards")
for label, price, prev, should_exit in [
    ("sane move accepted",      4.10, 4.086, False),
    ("absurd price rejected",   0.04, 4.086, True),
    ("impossible jump rejected", 4.90, 4.086, True),
    ("no prior history ok",     4.10, None,  False),
]:
    try:
        build.validate(price, prev, "test")
        check(label, not should_exit)
    except SystemExit:
        check(label, should_exit)

# --------------------------------------------------------------- EIA (best effort)
print("\nEIA parser (non-fatal by design)")
EIA = """<h2>Daily Prices</h2><p>as of 8/18/26 Close</p><table>
<tr><td>Crude Oil ($/barrel)</td><td>WTI</td><td>86.48</td></tr>
<tr><td></td><td>Brent</td><td>95.29</td></tr>
<tr><td>Gasoline RBOB ($/gallon)</td><td>New York Harbor</td><td>3.37</td></tr></table>"""
e = build.parse_eia(EIA)
check("wti", e.get("wti") == 86.48, str(e))
check("brent", e.get("brent") == 95.29, str(e))
check("rbob NYH", e.get("rbobNYH") == 3.37, str(e))
check("garbage input does not raise", isinstance(build.parse_eia("<p>hi</p>"), dict))

# --------------------------------------------------------------- template render
print("\nTemplate render")
tpl = build.TEMPLATE.read_text()
check("markers present", "/*__DATA_START__*/" in tpl and "/*__DATA_END__*/" in tpl)
rendered = re.sub(r"/\*__DATA_START__\*/.*?/\*__DATA_END__\*/",
                  "/*__DATA_START__*/\nconst SNAPSHOT={};const HISTORY=[];const BUILT='x';\n/*__DATA_END__*/",
                  tpl, flags=re.S)
check("substitution changes the file", rendered != tpl)
check("no leftover placeholder", "__DATA_START__" in rendered)

# The page must take PHI and SIGMA from the injected PARAMS block, not from its
# own literals. When they were hard-coded in both places, recalibrating the
# Python side left the published page still forecasting with the old numbers.
check("template reads PHI/SIGMA from PARAMS", "PARAMS.phi" in tpl and "PARAMS.sigma" in tpl)
check("template has no bare PHI/SIGMA assignment",
      not re.search(r"const\s+PHI\s*=\s*0\.\d+\s*,\s*SIGMA", tpl))
check("build injects PARAMS alongside SNAPSHOT",
      "const PARAMS = " in Path(build.__file__).read_text())

check("history seed is sorted & unique", (lambda r: [x["date"] for x in r] ==
      sorted({x["date"] for x in r}))(build.read_history()))
check("history seed non-empty", len(build.read_history()) >= 19, f"{len(build.read_history())} rows")

# --------------------------------------------------------------- date resolution
print("\nDate resolution (the midnight-ET trap)")
import datetime as _dt
_UTC=_dt.timezone.utc
_WITH="<p>Today's AAA National Average as of August 19, 2026</p><p>Current Avg. $4.086 Yesterday Avg. $4.065</p>"
_NO="<p>Current Avg. $4.086 Yesterday Avg. $4.065</p>"
for _n,_pg,_now,_want in [
  ("page date wins at 00:30 ET", _WITH, _dt.datetime(2026,8,20,4,30,tzinfo=_UTC), _dt.date(2026,8,19)),
  ("no page date at 00:30 ET falls back a day", _NO, _dt.datetime(2026,8,20,4,30,tzinfo=_UTC), _dt.date(2026,8,19)),
  ("no page date at 07:20 ET is today", _NO, _dt.datetime(2026,8,20,11,20,tzinfo=_UTC), _dt.date(2026,8,20)),
  ("implausible page date ignored", "<p>January 3, 2026</p>"+_NO, _dt.datetime(2026,8,20,16,20,tzinfo=_UTC), _dt.date(2026,8,20)),
]:
    check(_n, build.resolve_date(_pg,_now)==_want, str(build.resolve_date(_pg,_now)))

print("\n" + (f"{len(FAILS)} FAILURE(S): {FAILS}" if FAILS else "All checks passed."))
sys.exit(1 if FAILS else 0)
