from __future__ import annotations

"""Peak/trough (swing) detector for Polymarket outcome prices.

This detector is meant to catch the *local bottom -> fast lift* and
*local top -> fast drop* moves you see on Polymarket charts.

It intentionally requires only a mid-price stream (best_bid/best_ask -> mid).
You can feed it either orderbook snapshots or the lighter `price_change` stream.

Heuristic
---------
Maintain a rolling window (seconds). Inside that window track:
  - the lowest price (trough) and when it happened
  - the highest price (peak) and when it happened

Alert when:
  - current price is up from trough by >= min_move_pp (probability points)
    within the window  => "BOUNCE"
  - current price is down from peak by >= min_move_pp within the window
    => "REJECT"

This is a *signal generator* only. It does not trade.
"""

from dataclasses import dataclass
from collections import deque
from typing import Deque, Optional, Tuple


def _clamp(p: float, eps: float = 1e-6) -> float:
    if p < eps:
        return eps
    if p > 1.0 - eps:
        return 1.0 - eps
    return p


@dataclass
class SwingAlert:
    kind: str  # "BOUNCE" or "REJECT"
    window_s: int
    p_from: float
    p_to: float
    dp: float
    age_s: int
    ts: int

    def format_one_line(self) -> str:
        sign = "+" if self.dp >= 0 else ""
        return f"{self.kind} | {sign}{self.dp*100:.2f}pp in ~{self.age_s}s (win={self.window_s}s)"


class SwingDetector:
    """Detect simple peak/trough swings with a rolling time window."""

    PRESETS = {
        # pp = probability points
        "low":  {"window_s": 900, "min_move_pp": 0.020, "cooldown_s": 120},
        "med":  {"window_s": 600, "min_move_pp": 0.015, "cooldown_s": 90},
        "high": {"window_s": 300, "min_move_pp": 0.010, "cooldown_s": 60},
    }

    def __init__(
        self,
        *,
        window_s: Optional[int] = None,
        min_move_pp: Optional[float] = None,
        cooldown_s: Optional[int] = None,
        preset: str = "med",
    ):
        if preset not in self.PRESETS:
            preset = "med"
        cfg = self.PRESETS[preset]
        self.window_s = int(window_s if window_s is not None else cfg["window_s"])
        self.min_move_pp = float(min_move_pp if min_move_pp is not None else cfg["min_move_pp"])
        self.cooldown_s = int(cooldown_s if cooldown_s is not None else cfg["cooldown_s"])

        self._pts: Deque[Tuple[int, float]] = deque()  # (ts, p)
        self._last_alert_ts: int = 0

    def update(self, *, ts: int, p: float) -> Optional[SwingAlert]:
        p = _clamp(float(p))
        ts = int(ts)

        # Add new point
        self._pts.append((ts, p))

        # Drop old points
        cutoff = ts - self.window_s
        while self._pts and self._pts[0][0] < cutoff:
            self._pts.popleft()
        if len(self._pts) < 3:
            return None

        # Cooldown
        if self._last_alert_ts and (ts - self._last_alert_ts) < self.cooldown_s:
            return None

        # Find min/max in window (window is usually small; O(n) is fine)
        t_min, p_min = min(self._pts, key=lambda x: x[1])
        t_max, p_max = max(self._pts, key=lambda x: x[1])

        # Prefer the *most recent* extreme if ties
        # (min/max above returns first; re-scan for latest with same value)
        for t, px in reversed(self._pts):
            if abs(px - p_min) < 1e-12:
                t_min = t
                break
        for t, px in reversed(self._pts):
            if abs(px - p_max) < 1e-12:
                t_max = t
                break

        # Compute move from trough/peak
        dp_from_min = p - p_min
        dp_from_max = p - p_max

        # Bounce: move up from recent trough
        if dp_from_min >= self.min_move_pp and ts >= t_min:
            age = max(1, ts - t_min)
            self._last_alert_ts = ts
            return SwingAlert(
                kind="BOUNCE",
                window_s=self.window_s,
                p_from=p_min,
                p_to=p,
                dp=dp_from_min,
                age_s=age,
                ts=ts,
            )

        # Reject: move down from recent peak
        if (-dp_from_max) >= self.min_move_pp and ts >= t_max:
            age = max(1, ts - t_max)
            self._last_alert_ts = ts
            return SwingAlert(
                kind="REJECT",
                window_s=self.window_s,
                p_from=p_max,
                p_to=p,
                dp=dp_from_max,
                age_s=age,
                ts=ts,
            )

        return None
