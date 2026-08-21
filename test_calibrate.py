#!/usr/bin/env python3
"""Offline tests for calibrate.py — run with `python test_calibrate.py`.

Calibration is the one part of this project that can quietly poison the forecast:
it rewrites the model's parameters on every build, from a file that a scraper
appends to. So the tests here are mostly about what it must REFUSE to do —
straddle gaps, blow past the rails, or let a handful of lucky days collapse the
distribution to a spike.
"""
import datetime as dt, math, sys

import calibrate

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def series(start, prices):
    """Consecutive daily rows starting at `start` (ISO date string)."""
    d0 = dt.date.fromisoformat(start)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "regular": str(p)}
            for i, p in enumerate(prices)]


print("\ncase construction")
rows = series("2026-01-01", [4.00, 4.01, 4.02, 4.03])
cases = calibrate.build_cases(rows)
check("n-2 cases from n consecutive days", len(cases) == 2, f"{len(cases)}")
check("d1 and actual are true one-day changes",
      all(abs(c["d1"] - 0.01) < 1e-9 and abs(c["actual"] - 0.01) < 1e-9 for c in cases))
check("drift is None without a week-ago point", cases[0]["drift"] is None)

# The gap trap: 2026-01-04 is missing, so no case may treat 01-03 -> 01-05 as a
# one-day change. This is exactly the bug that corrupted the old daily_log.
gappy = [r for r in series("2026-01-01", [4.00, 4.01, 4.02, 4.03, 4.20, 4.21])
         if r["date"] != "2026-01-04"]
gc = calibrate.build_cases(gappy)
# 01-02 is still a valid case (01-01, 01-02, 01-03 all present). What must NOT
# appear is 01-03 (needs the missing 01-04) or 01-05 (needs it as the day before).
check("gaps produce no case, never a fake one-day change",
      all(c["date"] not in ("2026-01-03", "2026-01-05") for c in gc),
      f"dates={[c['date'] for c in gc]}")
check("the 0.17 jump across the gap never enters as d1",
      all(abs(c["d1"]) < 0.05 and abs(c["actual"]) < 0.05 for c in gc))

full = series("2026-01-01", [4.00 + 0.01 * i for i in range(12)])
fc = calibrate.build_cases(full)
check("drift appears once a week-ago point exists",
      any(c["drift"] is not None for c in fc))
check("drift on a straight ramp equals the daily step",
      all(abs(c["drift"] - 0.01) < 1e-9 for c in fc if c["drift"] is not None))

print("\nphi estimation")
# A pure random walk in the CHANGES: every change is +0.01, so momentum is
# perfectly persistent and phi should be pulled toward 1.
p = calibrate.calibrate(series("2026-01-01", [4.00 + 0.01 * i for i in range(20)]))
check("perfectly persistent changes pull phi up", p["phi"] >= calibrate.PRIOR_PHI,
      f"phi={p['phi']} direct={p['phi_direct']}")

# Alternating changes: momentum reverses every day, so the direct estimate must
# be negative and phi must be dragged below the prior.
alt = [4.00]
for i in range(19):
    alt.append(alt[-1] + (0.02 if i % 2 == 0 else -0.02))
p = calibrate.calibrate(series("2026-01-01", alt))
check("alternating changes drag phi below the prior", p["phi"] < calibrate.PRIOR_PHI,
      f"phi={p['phi']} direct={p['phi_direct']}")
check("phi stays inside its rails", calibrate.PHI_FLOOR <= p["phi"] <= calibrate.PHI_CEIL)

print("\nshrinkage behaviour")
short = calibrate.calibrate(series("2026-01-01", [4.00, 4.01, 4.02, 4.03]))
check("too few cases -> priors stand untouched",
      short["phi"] == calibrate.PRIOR_PHI and short["phi_direct"] is None,
      f"n={short['n_cases']} phi={short['phi']}")
check("empty history does not crash", calibrate.calibrate([])["n_cases"] == 0)
check("empty history returns the priors",
      calibrate.calibrate([])["sigma"] == calibrate.PRIOR_SIGMA)

# A flat series has zero forecast error, so sigma should fall — but only as far
# as the prior's weight allows, never to zero.
flat = calibrate.calibrate(series("2026-01-01", [4.00] * 20))
check("zero-error history narrows sigma", flat["sigma"] < calibrate.PRIOR_SIGMA,
      f"{flat['sigma']}")
check("but the prior keeps it well off the floor", flat["sigma"] > calibrate.SIGMA_FLOOR,
      f"{flat['sigma']}")
check("residual rmse of a flat series is zero", flat["resid_rmse"] == 0.0)

print("\nrails")
# Wild but individually plausible daily moves: sigma must clamp, not run away.
wild = [4.00]
for i in range(30):
    wild.append(round(wild[-1] + (0.20 if i % 2 == 0 else -0.20), 4))
p = calibrate.calibrate(series("2026-01-01", wild))
check("sigma is capped by SIGMA_CEIL", p["sigma"] <= calibrate.SIGMA_CEIL, f"{p['sigma']}")
check("sigma never below SIGMA_FLOOR", p["sigma"] >= calibrate.SIGMA_FLOOR)

print("\nconvergence: the data should out-argue the prior as evidence accumulates")
# A synthetic AR(1) in the changes with a TRUE phi of 0.5 — deliberately far from
# the 0.871 prior. With few cases the prior should win; with many, the truth should.
import random


def ar1_series(n, phi_true=0.5, seed=7):
    rng = random.Random(seed)
    c, prices = 0.0, [4.00]
    for _ in range(n):
        c = phi_true * c + rng.gauss(0, 0.006)
        prices.append(round(prices[-1] + c, 4))
    return series("2026-01-01", prices)


small = calibrate.calibrate(ar1_series(10))
large = calibrate.calibrate(ar1_series(400))
check("with little data phi stays near the prior",
      abs(small["phi"] - calibrate.PRIOR_PHI) < abs(small["phi"] - 0.5),
      f"phi={small['phi']} n={small['n_cases']}")
# Not a tight tolerance, on purpose. Phi is genuinely slow to identify at daily
# frequency — se is still ~0.04 at n=400 — so the honest requirement is that the
# data drags the estimate most of the way from the prior toward the truth, not
# that it lands on it.
_half = (calibrate.PRIOR_PHI + 0.5) / 2
check("with plenty of data phi moves most of the way to the truth",
      large["phi"] < _half, f"phi={large['phi']} (needs < {_half:.3f}) n={large['n_cases']}")
check("posterior uncertainty shrinks as n grows",
      large["phi_post_sd"] < small["phi_post_sd"],
      f"{small['phi_post_sd']} -> {large['phi_post_sd']}")
check("sigma converges on the true innovation sd (~0.006)",
      abs(large["sigma"] - 0.006) < 0.0015, f"{large['sigma']}")

print("\nlive history")
live = calibrate.calibrate_file(write=False)
check("live history yields a usable parameter set",
      0 <= live["phi"] <= 0.99 and calibrate.SIGMA_FLOOR <= live["sigma"] <= calibrate.SIGMA_CEIL,
      f"phi={live['phi']} sigma={live['sigma']} n={live['n_cases']}")
check("calibrated sigma is narrower than the old fixed 0.0103",
      live["sigma"] < 0.0103, f"{live['sigma']}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("All checks passed.")
