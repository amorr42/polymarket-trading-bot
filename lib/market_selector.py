"""Market selectors (clean discovery layer).

This module introduces a small abstraction that separates *how* we choose a
market (coin-based 15m, slug, direct token IDs, etc.) from *how* we stream and
cache its orderbook (MarketManager + WebSocket).

Design goals:
  - Keep existing coin-based flows working unchanged.
  - Add an event/slug flow without touching WebSocket code.
  - Use a single canonical pair of sides: "up" and "down" internally.
    For non-15m markets (e.g. YES/NO), we map YES->up and NO->down, and store
    display labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from src.gamma_client import GammaClient


@dataclass(frozen=True)
class SelectedMarket:
    """Normalized market payload used by MarketManager."""

    slug: str
    question: str
    end_date: str
    token_ids: Dict[str, str]  # always contains "up" and "down"
    prices: Dict[str, float]
    accepting_orders: bool
    labels: Dict[str, str]  # e.g. {"up": "UP"/"YES", "down": "DOWN"/"NO"}


class MarketSelector(Protocol):
    """Protocol for discovering/selecting a market."""

    def select(self) -> Optional[SelectedMarket]:
        """Return the currently selected market, or None if not available."""

    @property
    def supports_auto_switch(self) -> bool:
        """Whether MarketManager should periodically re-select and switch."""


class CoinIntervalSelector:
    """Select current active market for a coin with configurable interval (5m, 15m, 30m)."""

    def __init__(self, coin: str, interval: str = "15m", gamma: Optional[GammaClient] = None):
        self.coin = coin.upper()
        self.interval = interval  # "5m", "15m", "30m"
        self.gamma = gamma or GammaClient()

    @property
    def supports_auto_switch(self) -> bool:
        return True

    def select(self) -> Optional[SelectedMarket]:
        from datetime import datetime, timezone

        # Calculate current window timestamp based on interval
        now = datetime.now(timezone.utc)

        if self.interval == "5m":
            interval_secs = 5 * 60
            minute = (now.minute // 5) * 5
        elif self.interval == "15m":
            interval_secs = 15 * 60
            minute = (now.minute // 15) * 15
        elif self.interval == "30m":
            interval_secs = 30 * 60
            minute = (now.minute // 30) * 30
        else:
            return None

        current_window = now.replace(minute=minute, second=0, microsecond=0)
        current_ts = int(current_window.timestamp())

        # Build slug: btc-updown-5m-1771630500
        slug = f"{self.coin.lower()}-updown-{self.interval}-{current_ts}"

        # Get market by slug
        market = self.gamma.get_market_by_slug(slug)
        if not market:
            return None

        accepting = bool(market.get("acceptingOrders", False))
        if not accepting:
            return None

        # Parse token IDs and prices
        raw_token_ids = self.gamma.parse_token_ids(market)
        raw_prices = self.gamma.parse_prices(market)

        # Normalize to up/down
        token_ids, labels = _normalize_two_sided_tokens(raw_token_ids)
        prices = _normalize_two_sided_prices(raw_prices)

        if not token_ids or not prices:
            return None

        return SelectedMarket(
            slug=slug,
            question=str(market.get("question") or ""),
            end_date=str(market.get("endDate") or ""),
            token_ids=token_ids,
            prices=prices,
            accepting_orders=accepting,
            labels=labels,
        )


class FifteenMinuteCoinSelector:
    """Select the current active 15-minute UP/DOWN market for a coin."""

    def __init__(self, coin: str, gamma: Optional[GammaClient] = None):
        self.coin = coin.upper()
        self.gamma = gamma or GammaClient()

    @property
    def supports_auto_switch(self) -> bool:  # market windows roll over
        return True

    def select(self) -> Optional[SelectedMarket]:
        info = self.gamma.get_market_info(self.coin)
        if not info:
            return None
        if not info.get("accepting_orders", False):
            return None

        token_ids = info.get("token_ids", {})
        # Ensure we have canonical keys
        if "up" not in token_ids or "down" not in token_ids:
            return None

        return SelectedMarket(
            slug=str(info.get("slug") or ""),
            question=str(info.get("question") or ""),
            end_date=str(info.get("end_date") or ""),
            token_ids={"up": str(token_ids["up"]), "down": str(token_ids["down"])},
            prices={"up": float(info.get("prices", {}).get("up", 0.0)), "down": float(info.get("prices", {}).get("down", 0.0))},
            accepting_orders=bool(info.get("accepting_orders", False)),
            labels={"up": "UP", "down": "DOWN"},
        )


class SlugMarketSelector:
    """Select a specific Polymarket market by slug.

    Notes:
      - Polymarket's website "event" slug often matches Gamma market slug for
        simple binary markets. If it doesn't, you can still use TokenPairSelector.
      - We normalize outcomes to "up"/"down" internally:
          YES -> up, NO -> down
          UP  -> up, DOWN -> down
    """

    def __init__(self, slug: str, gamma: Optional[GammaClient] = None):
        self.slug = slug
        self.gamma = gamma or GammaClient()
        self.last_error: str = ""

    @property
    def supports_auto_switch(self) -> bool:
        # A fixed slug doesn't roll over like 15m markets.
        return False

    def select(self) -> Optional[SelectedMarket]:
        # First, try direct lookup.
        market = self.gamma.get_market_by_slug(self.slug)

        # Some Polymarket "event" slugs represent a group with many outcomes/markets.
        # In that case, try to find a *binary* child market by searching Gamma.
        if market and not _is_two_sided_market(market):
            market = self._select_best_binary_child(self.slug)

        if not market:
            # Final fallback: search and pick best binary match.
            market = self._select_best_binary_child(self.slug)
        if not market:
            self.last_error = (
                "Slug lookup failed. This often happens when the provided slug is an EVENT slug that "
                "maps to multiple markets/outcomes (not a single binary market)."
            )
            return None

        accepting = bool(market.get("acceptingOrders", False))

        # Parse token ids and prices using GammaClient helpers.
        raw_token_ids = self.gamma.parse_token_ids(market)
        raw_prices = self.gamma.parse_prices(market)

        # Normalize to canonical up/down.
        token_ids, labels = _normalize_two_sided_tokens(raw_token_ids)
        prices = _normalize_two_sided_prices(raw_prices)

        # Reject non-binary markets (e.g. multi-outcome date markets) unless we
        # successfully selected a binary child.
        if not token_ids or len(raw_token_ids) != 2:
            self.last_error = (
                "Selected market is not binary (does not have exactly 2 outcomes/tokens). "
                "For multi-outcome events, pick a specific child market slug from the event page "
                "(the longer /event/.../...-<date>-... slug) or use --list to see candidates."
            )
            return None

        return SelectedMarket(
            slug=str(market.get("slug") or self.slug),
            question=str(market.get("question") or market.get("title") or ""),
            end_date=str(market.get("endDate") or ""),
            token_ids=token_ids,
            prices=prices,
            accepting_orders=accepting,
            labels=labels,
        )

    def _select_best_binary_child(self, slug: str) -> Optional[dict]:
        """Search Gamma markets for binary markets that match this slug and pick the best one.

        This is important for Polymarket "event" slugs that represent a collection of
        markets/outcomes (e.g. "...-by" markets with many date outcomes).
        """
        slug = (slug or "").strip().strip("/")
        if not slug:
            return None

        # Try a few query styles; GammaClient.list_markets is tolerant.
        candidates = []
        for params in (
            {"search": slug, "limit": 50, "offset": 0, "order": "volume", "ascending": False},
            {"query": slug, "limit": 50, "offset": 0, "order": "volume", "ascending": False},
            {"term": slug, "limit": 50, "offset": 0, "order": "volume", "ascending": False},
            {"order": "createdAt", "ascending": False, "limit": 50, "offset": 0, "search": slug},
        ):
            res = self.gamma.list_markets(**params)
            if res:
                candidates.extend(res)

        # Keep only binary markets that have exactly 2 outcomes/tokens.
        binary = [m for m in candidates if _is_two_sided_market(m)]
        if not binary:
            self.last_error = (
                "No binary child markets found for this event slug. Try using a specific child market slug "
                "(from the event page URL after the second slash), or use --yes-token/--no-token." 
            )
            return None

        def num(v) -> float:
            try:
                return float(v)
            except Exception:
                return 0.0

        # Score: prefer acceptingOrders, then higher liquidity, then higher volume, then closer slug match.
        def score(m: dict) -> tuple:
            s = str(m.get("slug") or "")
            accepting = 1 if m.get("acceptingOrders") else 0
            liquidity = num(m.get("liquidity") or m.get("liquidityNum") or 0)
            volume = num(m.get("volume") or m.get("volumeNum") or 0)
            contains = 1 if slug in s else 0
            # higher is better
            return (accepting, contains, liquidity, volume)

        binary.sort(key=score, reverse=True)
        return binary[0]

    def list_binary_candidates(self, slug: Optional[str] = None) -> list[dict]:
        """Return a list of binary markets that look related to this slug.

        Useful for multi-outcome event slugs where you need to pick a specific child market.
        """
        slug = (slug or self.slug or "").strip().strip("/")
        if not slug:
            return []

        candidates: list[dict] = []
        for params in (
            {"search": slug, "limit": 100, "offset": 0, "order": "volume", "ascending": False},
            {"query": slug, "limit": 100, "offset": 0, "order": "volume", "ascending": False},
            {"term": slug, "limit": 100, "offset": 0, "order": "volume", "ascending": False},
            {"order": "createdAt", "ascending": False, "limit": 100, "offset": 0, "search": slug},
        ):
            res = self.gamma.list_markets(**params)
            if res:
                candidates.extend(res)

        binary = [m for m in candidates if _is_two_sided_market(m)]
        # Deduplicate by slug
        seen = set()
        out: list[dict] = []
        for m in binary:
            s = str(m.get("slug") or "")
            if s and s not in seen:
                seen.add(s)
                out.append(m)
        return out

def _is_two_sided_market(market: dict) -> bool:
    """Return True if Gamma market appears to be a 2-outcome market."""
    try:
        outcomes = market.get("outcomes", [])
        clob_ids = market.get("clobTokenIds", [])
        # Fields may be JSON strings
        if isinstance(outcomes, str):
            import json

            outcomes = json.loads(outcomes)
        if isinstance(clob_ids, str):
            import json

            clob_ids = json.loads(clob_ids)
        return isinstance(outcomes, list) and isinstance(clob_ids, list) and len(outcomes) == 2 and len(clob_ids) == 2
    except Exception:
        return False


class TokenPairSelector:
    """Select a market from a direct token pair.

    Useful when you already have YES/NO token IDs (asset_ids) and don't want to
    rely on Gamma slug lookup.
    """

    def __init__(
        self,
        up_token_id: str,
        down_token_id: str,
        slug: str = "manual-token-pair",
        question: str = "",
        end_date: str = "",
        labels: Optional[Dict[str, str]] = None,
    ):
        self._market = SelectedMarket(
            slug=slug,
            question=question,
            end_date=end_date,
            token_ids={"up": str(up_token_id), "down": str(down_token_id)},
            prices={"up": 0.0, "down": 0.0},
            accepting_orders=True,
            labels=labels or {"up": "YES", "down": "NO"},
        )

    @property
    def supports_auto_switch(self) -> bool:
        return False

    def select(self) -> Optional[SelectedMarket]:
        return self._market


class DbPrefixSelector:
    """Select the current active market from the local PostgreSQL database by slug prefix.

    Use this when you have ingested all of a day's timed markets (e.g. all
    BTC-5m slugs) and want to automatically follow whichever one is live right
    now, rolling over to the next window automatically.

    Usage:
        selector = DbPrefixSelector("btc-updown-5m")
        manager  = MarketManager(selector=selector)

    Prerequisites:
        Run ``python apps/ingest_markets_pg.py --source markets --keyword btc-updown-5m``
        first to populate the database.
    """

    def __init__(self, slug_prefix: str):
        self.slug_prefix = slug_prefix.strip().rstrip("-")
        self.last_error: str = ""

    @property
    def supports_auto_switch(self) -> bool:
        # Markets roll over; MarketManager should re-select periodically.
        return True

    def select(self) -> Optional[SelectedMarket]:
        try:
            from lib.db import connect, ensure_schema, fetch_current_market_by_prefix
        except ImportError as exc:
            self.last_error = f"DB import failed: {exc}"
            return None

        try:
            conn = connect()
        except Exception as exc:
            self.last_error = f"DB connect failed: {exc}"
            return None

        try:
            ensure_schema(conn)
            m = fetch_current_market_by_prefix(conn, self.slug_prefix)
        except Exception as exc:
            self.last_error = f"DB query failed: {exc}"
            return None
        finally:
            conn.close()

        if not m:
            self.last_error = (
                f"No active market found in DB with slug prefix '{self.slug_prefix}'. "
                "Run: python apps/ingest_markets_pg.py --source markets "
                f"--keyword {self.slug_prefix}"
            )
            return None

        clob_ids: List[str] = m.get("clob_token_ids") or []
        outcomes: List[Any] = m.get("outcomes") or []

        if len(clob_ids) < 2 or len(outcomes) < 2:
            self.last_error = (
                f"Market '{m.get('slug')}' has fewer than 2 token IDs or outcomes."
            )
            return None

        # Map outcomes to up/down using the same normalization helpers.
        raw_tokens: Dict[str, str] = {}
        raw_prices: Dict[str, float] = {}
        for i, label in enumerate(outcomes[:2]):
            key = str(label).lower()
            raw_tokens[key] = str(clob_ids[i])
            raw_prices[key] = 0.0  # DB doesn't store live prices

        token_ids, labels = _normalize_two_sided_tokens(raw_tokens)
        prices = _normalize_two_sided_prices(raw_prices)

        if not token_ids:
            self.last_error = f"Could not normalize token IDs for '{m.get('slug')}'."
            return None

        end_date = ""
        closed_time = m.get("closed_time")
        if closed_time is not None:
            try:
                end_date = closed_time.isoformat()
            except Exception:
                end_date = str(closed_time)

        # If closed_time is missing, derive end_date from slug timestamp + window.
        if not end_date:
            import re as _re
            import datetime as _dt
            slug = str(m.get("slug") or "")
            _ts_match = _re.search(r"-(\d{9,})$", slug)
            if _ts_match:
                slug_ts = int(_ts_match.group(1))
                # Infer window duration from prefix (e.g. "5m" → 300 s)
                _win_match = _re.search(r"-(\d+)m(?:-|$)", self.slug_prefix)
                if _win_match:
                    window_s = int(_win_match.group(1)) * 60
                else:
                    _win_h = _re.search(r"-(\d+)h(?:-|$)", self.slug_prefix)
                    window_s = int(_win_h.group(1)) * 3600 if _win_h else 900
                end_ts = slug_ts + window_s
                end_date = _dt.datetime.fromtimestamp(
                    end_ts, tz=_dt.timezone.utc
                ).isoformat()

        return SelectedMarket(
            slug=str(m.get("slug") or ""),
            question=str(m.get("question") or ""),
            end_date=end_date,
            token_ids=token_ids,
            prices=prices,
            accepting_orders=bool(m.get("accepting_orders", False)),
            labels=labels,
        )


def _normalize_two_sided_tokens(raw: Dict[str, str]) -> tuple[Dict[str, str], Dict[str, str]]:
    """Map common outcome keys to canonical up/down."""
    if not raw:
        return {}, {}

    # Common possibilities
    if "up" in raw and "down" in raw:
        return {"up": str(raw["up"]), "down": str(raw["down"])}, {"up": "UP", "down": "DOWN"}
    if "yes" in raw and "no" in raw:
        return {"up": str(raw["yes"]), "down": str(raw["no"])}, {"up": "YES", "down": "NO"}
    if "true" in raw and "false" in raw:
        return {"up": str(raw["true"]), "down": str(raw["false"])}, {"up": "TRUE", "down": "FALSE"}

    # Fallback: take first two items in deterministic order.
    items = list(raw.items())
    if len(items) < 2:
        return {}, {}
    (k1, v1), (k2, v2) = items[0], items[1]
    return {"up": str(v1), "down": str(v2)}, {"up": str(k1).upper(), "down": str(k2).upper()}


def _normalize_two_sided_prices(raw: Dict[str, float]) -> Dict[str, float]:
    if not raw:
        return {"up": 0.0, "down": 0.0}
    if "up" in raw and "down" in raw:
        return {"up": float(raw.get("up", 0.0)), "down": float(raw.get("down", 0.0))}
    if "yes" in raw and "no" in raw:
        return {"up": float(raw.get("yes", 0.0)), "down": float(raw.get("no", 0.0))}
    if "true" in raw and "false" in raw:
        return {"up": float(raw.get("true", 0.0)), "down": float(raw.get("false", 0.0))}
    vals = list(raw.values())
    if len(vals) >= 2:
        return {"up": float(vals[0]), "down": float(vals[1])}
    if len(vals) == 1:
        return {"up": float(vals[0]), "down": 0.0}
    return {"up": 0.0, "down": 0.0}
