"""
BTC Price Oracle - Real-time BTC price tracking for value-based trading.

Provides:
- Real-time BTC price from Binance/Coinbase
- Historical price lookup
- Price comparison for value calculation
"""

import time
import requests
from typing import Optional, Dict
from dataclasses import dataclass


@dataclass
class BTCPriceData:
    """BTC price snapshot."""
    price: float
    timestamp: int  # Unix epoch seconds
    source: str  # "binance", "coinbase", "average"


class BTCOracle:
    """
    Tracks BTC price in real-time and provides historical lookups.

    Usage:
        oracle = BTCOracle()
        current = oracle.get_current_price()
        print(f"BTC: ${current.price:.2f}")
    """

    def __init__(self, use_cache: bool = True):
        """
        Initialize BTC oracle.

        Args:
            use_cache: Cache prices to avoid excessive API calls
        """
        self.use_cache = use_cache
        self._cache: Dict[str, BTCPriceData] = {}
        self._cache_ttl = 2  # seconds

    def get_current_price(self) -> BTCPriceData:
        """
        Get current BTC/USD price.

        Returns:
            BTCPriceData with current price

        Strategy:
            1. Try Binance (fastest)
            2. Fallback to Coinbase
            3. Return cached if both fail
        """
        now = int(time.time())
        cache_key = f"current_{now // self._cache_ttl}"

        # Check cache
        if self.use_cache and cache_key in self._cache:
            cached = self._cache[cache_key]
            if now - cached.timestamp < self._cache_ttl:
                return cached

        # Try Binance
        try:
            price = self._fetch_binance()
            data = BTCPriceData(price=price, timestamp=now, source="binance")
            self._cache[cache_key] = data
            return data
        except Exception:
            pass

        # Fallback to Coinbase
        try:
            price = self._fetch_coinbase()
            data = BTCPriceData(price=price, timestamp=now, source="coinbase")
            self._cache[cache_key] = data
            return data
        except Exception:
            pass

        # Last resort: return stale cache or raise
        if cache_key in self._cache:
            return self._cache[cache_key]

        raise RuntimeError("Failed to fetch BTC price from all sources")

    def get_price_at_time(self, timestamp: int) -> float:
        """
        Get BTC price at specific timestamp.

        For historical data, we approximate with current price
        (in production, use historical API or database).

        Args:
            timestamp: Unix epoch seconds

        Returns:
            BTC price at that time (approximated)
        """
        # For now, use current price as approximation
        # In production, fetch from historical data source
        current = self.get_current_price()
        return current.price

    def _fetch_binance(self) -> float:
        """Fetch BTC/USDT price from Binance."""
        url = "https://api.binance.com/api/v3/ticker/price"
        params = {"symbol": "BTCUSDT"}

        resp = requests.get(url, params=params, timeout=3)
        resp.raise_for_status()

        data = resp.json()
        return float(data["price"])

    def _fetch_coinbase(self) -> float:
        """Fetch BTC/USD price from Coinbase."""
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

        resp = requests.get(url, timeout=3)
        resp.raise_for_status()

        data = resp.json()
        return float(data["data"]["amount"])


def calculate_value_opportunity(
    price_to_beat: float,
    current_btc: float,
    market_prices: Dict[str, float],
    min_edge_pp: float = 0.05,
) -> Optional[Dict[str, any]]:
    """
    Calculate value opportunity by comparing oracle to market.

    Args:
        price_to_beat: BTC price at market open (from slug timestamp)
        current_btc: Current BTC price (from oracle)
        market_prices: {"up": 0.45, "down": 0.55}
        min_edge_pp: Minimum edge in probability points to trade

    Returns:
        {
            "side": "up" or "down",
            "edge": 0.15,  # probability points
            "confidence": 0.80,  # 0-1 confidence in prediction
            "delta": +50.0,  # $ BTC movement
            "expected_winner": "up"
        }
        or None if no opportunity

    Example:
        price_to_beat = 67267.08
        current_btc = 67300.00  (+$33)
        market = {"up": 0.30, "down": 0.70}

        Result: UP should win (BTC > price_to_beat)
        Fair value: UP ~0.60 (60% chance)
        Edge: 0.60 - 0.30 = 0.30 (30pp edge!)
        → BUY UP aggressively
    """
    delta = current_btc - price_to_beat

    # Determine expected winner
    if abs(delta) < 2:  # Too close, uncertain (lowered from $5 to $2 for sensitivity)
        return None

    expected_winner = "up" if delta > 0 else "down"

    # Calculate confidence based on delta magnitude
    # Larger moves = higher confidence
    # UPDATED: More aggressive formula (2x sensitivity)
    # $100 move = 95% confidence
    # $50 move = 95% confidence (capped)
    # $25 move = 75% confidence
    # $10 move = 60% confidence
    confidence = min(0.95, 0.50 + (abs(delta) / 100))

    # Calculate fair probability for winner
    # confidence=0.80 → fair_prob=0.80 (80% chance to win)
    fair_prob = confidence

    # Current market price
    market_price = market_prices.get(expected_winner, 0.50)

    # Edge = fair value - market price
    edge = fair_prob - market_price

    # Only trade if edge >= threshold
    if edge < min_edge_pp:
        return None

    return {
        "side": expected_winner,
        "edge": edge,
        "confidence": confidence,
        "delta": delta,
        "expected_winner": expected_winner,
        "fair_prob": fair_prob,
        "market_price": market_price,
    }


# Example usage
if __name__ == "__main__":
    oracle = BTCOracle()

    # Get current price
    data = oracle.get_current_price()
    print(f"BTC: ${data.price:.2f} ({data.source})")

    # Simulate value calculation
    price_to_beat = 67267.08
    current_btc = data.price
    market_prices = {"up": 0.30, "down": 0.70}

    opp = calculate_value_opportunity(price_to_beat, current_btc, market_prices)

    if opp:
        print(f"\nVALUE OPPORTUNITY:")
        print(f"  Side: {opp['side'].upper()}")
        print(f"  Edge: {opp['edge']:.2%}")
        print(f"  Confidence: {opp['confidence']:.1%}")
        print(f"  BTC Delta: ${opp['delta']:+.2f}")
    else:
        print("\nNo value opportunity")
