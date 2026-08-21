#!/usr/bin/env python3
"""Offline tests for backfill.py — run with `python test_backfill.py`.

No network. Everything here exercises the parts that can corrupt history.csv:
dating an archived page, converting the page's five figures into dated points,
and merging them without downgrading a good number or hiding a disagreement.
"""
import datetime as dt, sys

import backfill, build

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


PAGE = """
<html><body>
  <p>Price as of: 8/21/26</p>
  <table>
    <tr><td>Current Avg.</td><td>$4.1092</td><td>$4.6100</td><td>$4.9900</td><td>$5.5400</td></tr>
    <tr><td>Yesterday Avg.</td><td>$4.1044</td><td>$4.6050</td><td>$4.9850</td><td>$5.5350</td></tr>
    <tr><td>Week Ago Avg.</td><td>$4.0776</td><td>$4.5800</td><td>$4.9600</td><td>$5.5100</td></tr>
    <tr><td>Month Ago Avg.</td><td>$4.0190</td><td>$4.5200</td><td>$4.9000</td><td>$5.4500</td></tr>
    <tr><td>Year Ago Avg.</td><td>$3.1372</td><td>$3.6400</td><td>$4.0200</td><td>$4.5600</td></tr>
  </table>
</body></html>
"""

print("\npage dating")
check("numeric 'Price as of' stamp is parsed",
      dt.date(2026, 8, 21) in build.page_dates(PAGE), f"{build.page_dates(PAGE)}")
check("long-form dates still parse",
      dt.date(2026, 8, 21) in build.page_dates("<p>August 21, 2026</p>"))
check("snapshot dated against its own timestamp, not now",
      backfill.snapshot_date(PAGE, "20260821035959") == dt.date(2026, 8, 21))
check("a page whose date is nowhere near the snapshot is rejected",
      backfill.snapshot_date(PAGE, "20240301120000") is None)
check("undateable page returns None", backfill.snapshot_date("<p>no date</p>", "20260821000000") is None)

print("\nharvest")
import types
backfill.requests = types.SimpleNamespace(
    get=lambda url, **kw: types.SimpleNamespace(text=PAGE, raise_for_status=lambda: None))

got = dict((d, (v, s)) for d, v, s in backfill.harvest("20260821035959", use_fuzzy=False))
check("current filed on the page's own date", got.get("2026-08-21", (None,))[0] == 4.1092)
check("yesterday filed one day back", got.get("2026-08-20", (None,))[0] == 4.1044)
check("week-ago filed seven days back", got.get("2026-08-14", (None,))[0] == 4.0776)
check("month/year ago excluded by default", len(got) == 3, f"{sorted(got)}")

fz = dict((d, v) for d, v, s in backfill.harvest("20260821035959", use_fuzzy=True))
check("--fuzzy adds month-ago on the calendar month", fz.get("2026-07-21") == 4.0190)
check("--fuzzy adds year-ago", fz.get("2025-08-21") == 3.1372)

print("\nmonth arithmetic")
check("month back from the 31st clamps to the 30th",
      backfill.month_back(dt.date(2026, 3, 31)) == dt.date(2026, 2, 28),
      f"{backfill.month_back(dt.date(2026, 3, 31))}")
check("month back across new year",
      backfill.month_back(dt.date(2026, 1, 15)) == dt.date(2025, 12, 15))
check("leap February handled",
      backfill.month_back(dt.date(2024, 3, 31)) == dt.date(2024, 2, 29))

print("\nmerge")
existing = [{"date": "2026-08-20", "regular": "4.1044", "source": "aaa-current"}]

# A weaker source must not overwrite a stronger one, even arriving later.
book, added, conf = backfill.merge(existing, [("2026-08-20", 4.1050, "aaa-week-ago-field")])
check("weaker source cannot overwrite a stronger one",
      book["2026-08-20"]["regular"] == 4.1044 and book["2026-08-20"]["source"] == "aaa-current")
check("the disagreement is still reported", len(conf) == 1 and conf[0][5] is False)

# A stronger source arriving later should win.
weak = [{"date": "2026-08-20", "regular": "4.1050", "source": "aaa-week-ago-field"}]
book, added, conf = backfill.merge(weak, [("2026-08-20", 4.1044, "aaa-current")])
check("stronger source replaces a weaker one",
      book["2026-08-20"]["regular"] == 4.1044, f"{book['2026-08-20']}")
check("that replacement is reported too", len(conf) == 1 and conf[0][5] is True)

# Ordering must not matter: strongest wins regardless of arrival order.
a = backfill.merge([], [("2026-08-20", 4.1050, "aaa-week-ago-field"),
                        ("2026-08-20", 4.1044, "aaa-current")])[0]
b = backfill.merge([], [("2026-08-20", 4.1044, "aaa-current"),
                        ("2026-08-20", 4.1050, "aaa-week-ago-field")])[0]
check("merge is order-independent", a == b and a["2026-08-20"]["regular"] == 4.1044)

book, added, conf = backfill.merge(existing, [("2026-08-20", 4.10442, "aaa-week-ago-field")])
check("sub-tolerance difference is not a conflict", not conf)

book, added, conf = backfill.merge(existing, [("2026-08-19", 4.0860, "aaa-yesterday-field")])
check("genuinely new dates are added", added and book["2026-08-19"]["regular"] == 4.0860)
check("existing rows survive the merge", "2026-08-20" in book)

print("\nrails")
book, added, _ = backfill.merge([], [("2026-08-20", 4.10, "aaa-current")])
check("merge output is a date-keyed book", list(book) == ["2026-08-20"])
check("PRICE rails match build.py",
      backfill.PRICE_MIN == build.PRICE_MIN and backfill.PRICE_MAX == build.PRICE_MAX)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("All checks passed.")
