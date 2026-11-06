"""TWAP-native fair value for Polymarket 5m Up/Down markets.

Settlement is the Chainlink 60s TWAP at the window end. At time t:
  * before the final 60s window starts: expected close = current spot,
    variance includes the remaining path to the window start plus the
    averaging over the 60s window.
  * inside the final 60s window: the observed part is known; only the
    remaining seconds are random.

All prices are treated in log space; sigma is per-second log-return vol.
"""
from __future__ import annotations

import math
from typing import Callable, Optional


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_probability(
    *,
    p0: float,
    spot: float,
    sigma: float,
    now: float,
    end_ts: float,
    twap_window: float = 60.0,
    mean_between: Callable[[float, float], Optional[float]],
) -> Optional[dict]:
    """Return {fair, e_close, sd, z, observed, remaining} or None."""
    if p0 is None or spot is None or sigma is None:
        return None
    if not (p0 > 0 and spot > 0 and sigma > 0):
        return None
    if now >= end_ts:
        return None
    window = float(twap_window)
    if window <= 0:
        return None

    settle_start = end_ts - window
    if now <= settle_start:
        e_close = spot
        sd = sigma * math.sqrt((settle_start - now) + window / 3.0)
        observed = 0.0
        remaining = end_ts - now
    else:
        observed = now - settle_start
        remaining = end_ts - now
        m_obs = mean_between(settle_start, now)
        if m_obs is None or observed <= 0:
            return None
        e_close = (m_obs * observed + spot * remaining) / window
        sd = sigma * math.sqrt(remaining**3 / 3.0) / window
        if sd <= 0:
            return None

    if e_close <= 0 or sd <= 0:
        return None
    z = math.log(e_close / p0) / sd
    fair = norm_cdf(z)
    return {
        "fair": min(1.0, max(0.0, fair)),
        "e_close": e_close,
        "sd": sd,
        "z": z,
        "observed_s": observed,
        "remaining_s": remaining,
    }


def per_share_fee(rate: float, price: float) -> float:
    """Official crypto taker fee per share: rate * p * (1-p)."""
    if price <= 0 or price >= 1:
        return 0.0
    return max(0.0, rate) * price * (1.0 - price)
