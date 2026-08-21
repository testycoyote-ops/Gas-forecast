"""The forecast model, shared by build.py (page) and flow.py (edge vs market).

  change_tomorrow = drift + PHI * (change_today - drift)
  drift           = (today - week_ago) / 7

PHI and SIGMA are no longer frozen constants. They start from priors derived
from 36 years of EIA weekly retail prices and are then re-estimated from the
model's own realised forecast errors every time the history file grows -- see
calibrate.py for the derivation and the reasoning. params.json holds the current
values; if it is missing or unreadable the priors below are used, so the model
still runs on a fresh checkout.
"""
from __future__ import annotations
import json, math
from pathlib import Path

ROOT = Path(__file__).parent

# Priors / fallbacks. PHI comes from inverting the AR(1) temporal-aggregation
# formula on EIA weekly changes (lag-1 autocorr 0.5482, n=1862). SIGMA is the
# pre-calibration value, kept only as a starting point -- it is the UNCONDITIONAL
# sd of a daily change, which is far wider than the conditional forecast error
# this model actually needs, and calibration narrows it as evidence arrives.
PHI = 0.8709
SIGMA = 0.0103
PARAMS: dict = {"phi": PHI, "sigma": SIGMA, "source": "prior"}

try:
    _p = json.loads((ROOT / "params.json").read_text())
    if 0.0 <= float(_p["phi"]) <= 0.99 and 0.001 <= float(_p["sigma"]) <= 0.05:
        PHI, SIGMA = float(_p["phi"]), float(_p["sigma"])
        PARAMS = {**_p, "source": "calibrated"}
except Exception:                                  # noqa: BLE001 - priors stand
    pass


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
        "sigma": SIGMA, "phi": PHI,
        "params": {k: PARAMS.get(k) for k in
                   ("source", "n_cases", "phi_direct", "phi_direct_se",
                    "resid_rmse", "updated")},
        "lo80": p - 1.2816 * SIGMA, "hi80": p + 1.2816 * SIGMA,
        "lo95": p - 1.9600 * SIGMA, "hi95": p + 1.9600 * SIGMA,
        "p_up": 1.0 - _ncdf((today - p) / SIGMA),
    }


def prob_above(strike: float, mu: float, sigma: float | None = None) -> float:
    """Model probability that the published AAA average lands strictly above `strike`.

    `sigma` defaults to None rather than to SIGMA so the live calibrated value is
    read at call time. Binding it as a default argument would freeze whatever
    SIGMA happened to be when this module was first imported.
    """
    return 1.0 - _ncdf((strike - mu) / (SIGMA if sigma is None else sigma))
