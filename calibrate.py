#!/usr/bin/env python3
"""Re-estimate the model's two parameters from the AAA daily history as it grows.

Why this file exists
--------------------
PHI and SIGMA used to be hard-coded constants, derived once and never revisited.
Two problems with that:

  * SIGMA was wrong by construction. 0.0103 is the *unconditional* standard
    deviation of a one-day change in the AAA average. What the forecast needs is
    the *conditional* one -- the spread of what is left over AFTER the model has
    made its prediction. Those are different numbers, and the second is always
    smaller. Inverting the AR(1) temporal-aggregation formula on 36 years of EIA
    weekly data gives an innovation sd of about $0.0042, not $0.0103; the Kalshi
    ladder has been implying $0.0023-$0.0034. The model was quoting a spread
    roughly three times too wide and getting badly beaten on Brier score for it.

  * PHI = 0.8710 was inverted from EIA *weekly* survey data, on the assumption
    that AAA's daily series is exactly AR(1). Fitted directly against the AAA
    daily observations we now actually have, the persistence looks more like
    0.74. That estimate is far too thin to trust on its own, but it is real
    evidence and it points one way.

So both parameters are now estimated from realised forecast errors, shrunk
toward the priors above. When the history file is short the priors dominate and
the model behaves almost exactly as before. As real observations accumulate the
data takes over and the parameters converge on the truth without anyone having
to redo the analysis by hand.

Deliberately conservative: tightening SIGMA while the point forecast is still
biased makes the model confidently wrong, which scores worse than being vaguely
right. The shrinkage weight on SIGMA's prior is therefore heavy.
"""
from __future__ import annotations
import csv, datetime as dt, json, math
from pathlib import Path

ROOT = Path(__file__).parent
PARAMS = ROOT / "params.json"

# ---------------------------------------------------------------------- priors
# PHI: from lag-1 autocorrelation of EIA weekly retail changes (0.5482, n=1862,
# 1990-2026), inverted through the AR(1) temporal-aggregation formula. Its
# SAMPLING error is tiny (~0.02) — but that is not the uncertainty that matters.
# The real uncertainty is the transfer assumption: EIA weekly is a ~1,000-station
# Monday snapshot, AAA daily is a ~120,000-station rolling average, and the
# inversion additionally assumes the daily series is exactly AR(1). If AAA's
# average carries any moving-average structure from its own smoothing window,
# that inflates apparent weekly persistence and biases this prior upward — which
# is the direction the AAA data we do have points. PRIOR_PHI_SD is therefore set
# by that judgement, not by the sample size.
#
# Sizing it matters more than it looks. Daily changes are small and serially
# noisy, so the standard error on a directly fitted phi is around 0.04 even with
# 400 observations. A prior sd of 0.06 would still be outvoting a year and a half
# of real data; 0.08 lets the evidence win at a reasonable pace without letting a
# fortnight of it swing the model around.
PRIOR_PHI    = 0.8710
PRIOR_PHI_SD = 0.08

# SIGMA: the previous hard-coded value, kept as the prior so the model can only
# narrow as evidence arrives -- never widen on a whim. PRIOR_SIGMA_N is the
# weight in pseudo-observations; at 8 it takes roughly a month of daily data
# before realised errors outvote it.
PRIOR_SIGMA   = 0.0103
PRIOR_SIGMA_N = 8.0

# Hard rails. SIGMA_FLOOR sits just below the tightest spread the Kalshi ladder
# has implied, so a run of lucky days cannot collapse the distribution to a
# spike. SIGMA_CEIL catches a corrupted history file.
SIGMA_FLOOR, SIGMA_CEIL = 0.0020, 0.0200
PHI_FLOOR,   PHI_CEIL   = 0.0000, 0.9900

# Minimum usable cases before the data is allowed to move PHI at all.
MIN_CASES_FOR_PHI = 5


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def build_cases(rows: list[dict]) -> list[dict]:
    """Every (t-1, t, t+1) triple in the history where all three days are present.

    These are exactly the situations the model faces: standing on day t knowing
    t and t-1 (and t-7 when available), predict t+1. Gaps in the history simply
    produce no case, which is the correct behaviour -- a "one-day change" that
    actually spans a nine-day hole is not a one-day change.
    """
    px = {r["date"]: float(r["regular"]) for r in rows}
    cases = []
    for ds, v in sorted(px.items()):
        d = _d(ds)
        y = px.get((d - dt.timedelta(days=1)).isoformat())
        n = px.get((d + dt.timedelta(days=1)).isoformat())
        if y is None or n is None:
            continue
        w = px.get((d - dt.timedelta(days=7)).isoformat())
        cases.append({"date": ds, "d1": v - y, "actual": n - v,
                      "drift": (v - w) / 7.0 if w is not None else None})
    return cases


def _err(case: dict, phi: float) -> float:
    """Forecast error of the live model form for one case.

    dhat = drift + phi * (d1 - drift), with drift falling back to 0 when the
    week-ago value is missing -- identical to what model.forecast() does, so
    these errors describe the model as actually deployed.
    """
    mu = case["drift"] or 0.0
    return case["actual"] - (mu + phi * (case["d1"] - mu))


def _fit_phi(cases: list[dict]) -> tuple[float, float] | tuple[None, None]:
    """Least-squares phi on the demeaned momentum term, with its standard error.

    Regressing (actual - drift) on (d1 - drift) through the origin recovers phi
    directly, because the model form is linear in it.
    """
    xs = [c["d1"] - (c["drift"] or 0.0) for c in cases]
    ys = [c["actual"] - (c["drift"] or 0.0) for c in cases]
    sxx = sum(x * x for x in xs)
    if sxx <= 0 or len(cases) < 3:
        return None, None
    phi = sum(x * y for x, y in zip(xs, ys)) / sxx
    dof = len(cases) - 1
    sse = sum((y - phi * x) ** 2 for x, y in zip(xs, ys))
    se = math.sqrt(sse / dof / sxx) if dof > 0 and sse > 0 else None
    return phi, se


def calibrate(rows: list[dict]) -> dict:
    """Return the parameter set the model should use, given the history so far."""
    cases = build_cases(rows)
    n = len(cases)

    phi_hat, phi_se = _fit_phi(cases) if n >= MIN_CASES_FOR_PHI else (None, None)

    # Normal-normal conjugate update: precision-weighted blend of the EIA-derived
    # prior and the direct AAA estimate. With few cases phi_se is large, the
    # prior dominates, and PHI barely moves -- which is what we want.
    if phi_hat is not None and phi_se and phi_se > 0:
        wp, wd = 1.0 / PRIOR_PHI_SD ** 2, 1.0 / phi_se ** 2
        phi = (wp * PRIOR_PHI + wd * phi_hat) / (wp + wd)
        phi_post_sd = math.sqrt(1.0 / (wp + wd))
    else:
        phi, phi_post_sd = PRIOR_PHI, PRIOR_PHI_SD
    phi = min(max(phi, PHI_FLOOR), PHI_CEIL)

    # SIGMA from the residuals *at the chosen phi*, shrunk toward the prior with
    # PRIOR_SIGMA_N pseudo-observations (scaled inverse-chi-squared posterior mean).
    sse = sum(_err(c, phi) ** 2 for c in cases)
    var = (PRIOR_SIGMA_N * PRIOR_SIGMA ** 2 + sse) / (PRIOR_SIGMA_N + n)
    sigma = min(max(math.sqrt(var), SIGMA_FLOOR), SIGMA_CEIL)

    # `if n` not `if rmse` -- a genuinely perfect fit gives rmse 0.0, which is
    # falsy, and reporting that as None would read as "never calibrated".
    rmse = math.sqrt(sse / n) if n else None
    return {
        "phi": round(phi, 4),
        "sigma": round(sigma, 5),
        "n_cases": n,
        "phi_direct": round(phi_hat, 4) if phi_hat is not None else None,
        "phi_direct_se": round(phi_se, 4) if phi_se else None,
        "phi_post_sd": round(phi_post_sd, 4),
        "resid_rmse": round(rmse, 5) if rmse is not None else None,
        "prior_phi": PRIOR_PHI,
        "prior_sigma": PRIOR_SIGMA,
        "last_case": cases[-1]["date"] if cases else None,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def calibrate_file(path: Path | None = None, write: bool = True) -> dict:
    path = path or (ROOT / "history.csv")
    rows = list(csv.DictReader(path.open()))
    p = calibrate(rows)
    if write:
        PARAMS.write_text(json.dumps(p, indent=1) + "\n")
    return p


if __name__ == "__main__":
    p = calibrate_file()
    print(f"[calibrate] cases={p['n_cases']}  "
          f"phi={p['phi']} (direct {p['phi_direct']} +/- {p['phi_direct_se']}, "
          f"prior {p['prior_phi']})  "
          f"sigma=${p['sigma']} (resid rmse {p['resid_rmse']}, prior {p['prior_sigma']})")
