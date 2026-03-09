from __future__ import annotations

"""Adaptive pump/dump detector for Polymarket binary markets.

Design goals:
  - Works across markets with very different price levels and volatility.
  - Minimizes false positives using spread/liquidity/imbalance filters.
  - Uses a scale-stable transform (logit) + EWMA volatility z-score.

This module is intentionally dependency-free.
"""

from dataclasses import dataclass
from collections import deque
from math import log
from typing import Deque, Dict, Optional, Tuple


def _clamp(p: float, eps: float = 1e-6) -> float:
    if p < eps:
        return eps
    if p > 1.0 - eps:
        return 1.0 - eps
    return p


def logit(p: float) -> float:
    p = _clamp(p)
    return log(p / (1.0 - p))


@dataclass
class PumpAlert:
    direction: str  # "PUMP" or "DUMP"
    window_s: int
    p_from: float
    p_to: float
    dp: float
    z: float
    sigma: float
    spread: float
    imbalance: float
    updates_10s: int
    ts: int

    def format_one_line(self) -> str:
        sign = "+" if self.dp >= 0 else ""
        return (
            f"{self.direction} | {sign}{self.dp*100:.2f}pp/{self.window_s}s "
            f"(z={self.z:.2f}, spread={self.spread:.4f}, imb={self.imbalance:+.2f}, u10s={self.updates_10s})"
        )


class _EwmaStd:
    """EWMA standard deviation estimator for a stream of returns."""

    def __init__(self, half_life_s: float = 600.0):
        self.half_life_s = max(1.0, half_life_s)
        self.var: Optional[float] = None
        self.last_ts: Optional[int] = None

    def update(self, r: float, ts: int) -> float:
        if self.last_ts is None:
            # Initialize with a small variance floor
            self.var = max(r * r, 1e-8)
            self.last_ts = ts
            return (self.var ** 0.5)

        dt = max(1, ts - self.last_ts)
        # Convert half-life to alpha per dt
        alpha = 1.0 - 0.5 ** (dt / self.half_life_s)
        self.last_ts = ts

        v = self.var if self.var is not None else 1e-8
        v = (1.0 - alpha) * v + alpha * (r * r)
        self.var = max(v, 1e-10)
        return (self.var ** 0.5)

    def value(self) -> float:
        if self.var is None:
            return 0.0
        return self.var ** 0.5


class PumpDetector:
    """Detects abnormal rapid moves using adaptive (volatility-scaled) thresholds."""

    SENSITIVITY_PRESETS: Dict[str, Dict[str, float]] = {
        # k = z-score threshold; pp_floor = minimum absolute move in probability points (0..1)
        "low":  {"k": 5.0, "pp_floor_short": 0.006, "pp_floor_long": 0.010},
        "med":  {"k": 4.0, "pp_floor_short": 0.004, "pp_floor_long": 0.008},
        "high": {"k": 3.2, "pp_floor_short": 0.003, "pp_floor_long": 0.006},
    }

    def __init__(
        self,
        windows_s: Tuple[int, int] = (60, 360),
        sensitivity: str = "med",
        spread_max: float = 0.03,
        min_liq_topn: float = 50.0,
        topn: int = 10,
        imbalance_min: float = 0.20,
        ewma_half_life_s: float = 600.0,
        cooldown_s: int = 120,
        beep: bool = False,
    ):
        self.windows_s = windows_s
        self.sensitivity = sensitivity if sensitivity in self.SENSITIVITY_PRESETS else "med"
        self.k = float(self.SENSITIVITY_PRESETS[self.sensitivity]["k"])
        self.pp_floor_short = float(self.SENSITIVITY_PRESETS[self.sensitivity]["pp_floor_short"])
        self.pp_floor_long = float(self.SENSITIVITY_PRESETS[self.sensitivity]["pp_floor_long"])

        self.spread_max = spread_max
        self.min_liq_topn = min_liq_topn
        self.topn = topn
        self.imbalance_min = imbalance_min
        self.cooldown_s = cooldown_s
        self.beep = beep

        self._hist: Deque[Tuple[int, float, float]] = deque(maxlen=6 * 60 * 60)  # (ts, p, x)
        self._updates_10s: Deque[int] = deque(maxlen=5000)
        self._ewma = _EwmaStd(half_life_s=ewma_half_life_s)
        self._last_alert_ts: int = 0
        self._last_down_p: Optional[float] = None

    def update(
        self,
        *,
        ts: int,
        up_p: Optional[float] = None,
        down_p: Optional[float] = None,
        up_best_bid: Optional[float] = None,
        up_best_ask: Optional[float] = None,
        up_bids_sizes: Optional[Tuple[float, ...]] = None,
        up_asks_sizes: Optional[Tuple[float, ...]] = None,
    ) -> Optional[PumpAlert]:
        """Feed the detector.

        Provide `up_p` on UP/YES snapshot updates. Optionally provide `down_p` when DOWN/NO updates arrive
        (used as a light sanity check).

        Also provide UP orderbook summary (best bid/ask, topN sizes) to reduce false positives.
        """

        if down_p is not None:
            self._last_down_p = down_p

        if up_p is None:
            return None

        # record update times for burst-rate estimation
        self._updates_10s.append(ts)
        while self._updates_10s and self._updates_10s[0] < ts - 10:
            self._updates_10s.popleft()
        updates_10s = len(self._updates_10s)

        p = _clamp(float(up_p))
        x = logit(p)
        self._hist.append((ts, p, x))

        if len(self._hist) < 5:
            return None

        # Basic health filters (need bid/ask)
        if up_best_bid is None or up_best_ask is None:
            return None
        spread = float(up_best_ask) - float(up_best_bid)
        if spread <= 0 or spread > self.spread_max:
            return None

        # Liquidity + imbalance filters
        bid_sz = sum(up_bids_sizes) if up_bids_sizes else 0.0
        ask_sz = sum(up_asks_sizes) if up_asks_sizes else 0.0
        liq = bid_sz + ask_sz
        if liq < self.min_liq_topn:
            return None
        imbalance = (bid_sz - ask_sz) / liq if liq > 0 else 0.0

        # Cooldown
        if self._last_alert_ts and ts - self._last_alert_ts < self.cooldown_s:
            return None

        # Evaluate windows, prefer the strongest signal
        best: Optional[PumpAlert] = None
        for w in self.windows_s:
            past = self._get_past_point(ts - w)
            if not past:
                continue
            t0, p0, x0 = past
            dx = x - x0
            dp = p - p0

            # Update EWMA std on dx (return) using the shortest window only for stability
            if w == self.windows_s[0]:
                sigma = self._ewma.update(dx, ts)
            else:
                sigma = max(self._ewma.value(), 1e-6)

            # Floors: short vs long
            pp_floor = self.pp_floor_short if w <= self.windows_s[0] else self.pp_floor_long
            if abs(dp) < pp_floor:
                continue

            sigma_eff = max(sigma, 1e-4)
            z = abs(dx) / sigma_eff
            if z < self.k:
                continue

            # Imbalance direction check
            if dx > 0 and imbalance < self.imbalance_min:
                continue
            if dx < 0 and imbalance > -self.imbalance_min:
                continue

            # Down-side sanity check (optional): should usually move opposite
            if self._last_down_p is not None:
                # We only check sign, not magnitude, to avoid rejecting thin/noisy NO books.
                if dx > 0 and (self._last_down_p - (1.0 - p)) > 0.02:
                    # NO looks "too high" relative to 1-p; ignore as inconsistent book
                    pass

            direction = "PUMP" if dx > 0 else "DUMP"
            alert = PumpAlert(
                direction=direction,
                window_s=w,
                p_from=p0,
                p_to=p,
                dp=dp,
                z=float(z),
                sigma=float(sigma_eff),
                spread=float(spread),
                imbalance=float(imbalance),
                updates_10s=updates_10s,
                ts=ts,
            )
            if (best is None) or (alert.z > best.z):
                best = alert

        if best:
            self._last_alert_ts = ts
        return best

    def _get_past_point(self, target_ts: int) -> Optional[Tuple[int, float, float]]:
        """Return the latest point with ts <= target_ts."""
        # Deque is ordered by time; iterate from right for speed.
        for t, p, x in reversed(self._hist):
            if t <= target_ts:
                return (t, p, x)
        return None
