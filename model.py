"""The forecast model, shared by build.py (page) and flow.py (edge vs market).

  change_tomorrow = drift + PHI * (change_today - drift)
  drift           = (today - week_ago) / 7

PHI was derived from 36 years of EIA weekly retail prices by inverting the AR(1)
temporal-aggregation formula; see the repo README for the full derivation.
"""
from __future__ import annotations
import math

PHI = 0.8709      # daily persistence of price CHANGES
SIGMA = 0.0103    # residual sd of the 1-day change, in dollars


def _ncdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def forecast(today: float, yesterday: float, week_ago: float | None) -> dict:
    d1 = today - yesterday
    mu = (today - week_ago) / 7.0 if week_ago is not None else 0.0
    dhat = mu + PHI * (d1 - mu)
    p = today + dhat
    return {
        "today": today, "yesterday": yesterday, "week_ago": week_ago,
        "chg_1d": d1, "drift_7d": mu, "pred_chg": dhat, "pred_price": p,
        "sigma": SIGMA,
        "lo80": p - 1.2816 * SIGMA, "hi80": p + 1.2816 * SIGMA,
        "lo95": p - 1.9600 * SIGMA, "hi95": p + 1.9600 * SIGMA,
        "p_up": 1.0 - _ncdf((today - p) / SIGMA),
    }


def prob_above(strike: float, mu: float, sigma: float = SIGMA) -> float:
    """Model probability that the published AAA average lands strictly above `strike`."""
    return 1.0 - _ncdf((strike - mu) / sigma)
