import time

class MomentumDetector:
    def __init__(self, lookback_seconds=300, min_abs_pp=0.01):
        self.lookback_seconds = lookback_seconds
        self.min_abs_pp = min_abs_pp
        self.history = {}

    def update(self, token, price):
        now = time.time()

        if token not in self.history:
            self.history[token] = []

        self.history[token].append((now, price))

        # Keep only recent history
        self.history[token] = [
            (t, p) for t, p in self.history[token]
            if now - t <= self.lookback_seconds
        ]

        if len(self.history[token]) < 2:
            return

        base_price = self.history[token][0][1]
        delta = price - base_price

        if abs(delta) >= self.min_abs_pp:
            direction = "UP" if delta > 0 else "DOWN"
            print(f"ALERT {direction} | token={token} | Δ={delta:.4f}")
