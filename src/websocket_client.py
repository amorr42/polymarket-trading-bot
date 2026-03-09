"""Polymarket CLOB WebSocket client.

Connects to the Polymarket real-time orderbook feed and provides:
  - MarketWebSocket: full orderbook stream with callback-based API
  - OrderbookSnapshot / Level: typed data structures
  - WebSocketClient: lightweight legacy client used by db_alert_watcher

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribe message:
  {"assets_ids": ["<token_id>", ...], "type": "Market"}

Incoming event types:
  book         -> full orderbook snapshot
  price_change -> delta update (changes list)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Awaitable, Union

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Level:
    """Single price level in an orderbook."""
    price: float
    size: float


@dataclass
class OrderbookSnapshot:
    """Point-in-time orderbook state for a single token."""

    asset_id: str
    timestamp: float
    bids: List[Level] = field(default_factory=list)
    asks: List[Level] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2.0
        if self.bids:
            return self.best_bid
        if self.asks:
            return self.best_ask
        return 0.0


# ---------------------------------------------------------------------------
# MarketWebSocket
# ---------------------------------------------------------------------------

BookCallback = Callable[[OrderbookSnapshot], Union[None, Awaitable[None]]]
SimpleCallback = Callable[[], None]


class MarketWebSocket:
    """Real-time Polymarket CLOB WebSocket client.

    Usage:
        ws = MarketWebSocket()

        @ws.on_book
        async def handle(snapshot: OrderbookSnapshot):
            print(snapshot.mid_price)

        await ws.subscribe(["token_yes", "token_no"])
        await ws.run(auto_reconnect=True)
    """

    def __init__(self) -> None:
        self._books: Dict[str, OrderbookSnapshot] = {}
        self._subscribed: List[str] = []
        self._pending: List[str] = []
        self._ws = None
        self._connected = False
        self._running = False

        self._on_book_cbs: List[BookCallback] = []
        self._on_connect_cbs: List[SimpleCallback] = []
        self._on_disconnect_cbs: List[SimpleCallback] = []

    # --- callback decorators ---

    def on_book(self, cb: BookCallback) -> BookCallback:
        self._on_book_cbs.append(cb)
        return cb

    def on_connect(self, cb: SimpleCallback) -> SimpleCallback:
        self._on_connect_cbs.append(cb)
        return cb

    def on_disconnect(self, cb: SimpleCallback) -> SimpleCallback:
        self._on_disconnect_cbs.append(cb)
        return cb

    # --- public API ---

    def get_orderbook(self, token_id: str) -> Optional[OrderbookSnapshot]:
        return self._books.get(token_id)

    async def subscribe(self, token_ids: List[str], replace: bool = False) -> None:
        if replace:
            self._subscribed = []
            self._books = {k: v for k, v in self._books.items() if k in token_ids}

        new = [t for t in token_ids if t not in self._subscribed]
        if not new:
            return

        if self._connected and self._ws is not None:
            await self._send_subscribe(new)
            self._subscribed.extend(new)
        else:
            self._pending.extend(new)

    async def disconnect(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def run(self, auto_reconnect: bool = True) -> None:
        """Connect and stream orderbook data."""
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    backoff = 1.0

                    self._fire_sync(self._on_connect_cbs)

                    all_tokens = list(dict.fromkeys(self._subscribed + self._pending))
                    if all_tokens:
                        await self._send_subscribe(all_tokens)
                        self._subscribed = all_tokens
                        self._pending = []

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            await self._handle_message(raw)
                        except Exception as exc:
                            logger.debug("Message error: %s", exc)

            except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError) as exc:
                logger.debug("WS disconnected: %s", exc)
            except Exception as exc:
                logger.debug("WS error: %s", exc)
            finally:
                self._ws = None
                self._connected = False
                self._fire_sync(self._on_disconnect_cbs)

            if not self._running or not auto_reconnect:
                break

            logger.debug("Reconnecting in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # --- internal helpers ---

    async def _send_subscribe(self, token_ids: List[str]) -> None:
        if self._ws is None:
            return
        try:
            msg = {"assets_ids": token_ids, "type": "Market"}
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            logger.warning("Subscribe send failed: %s", exc)

    def _fire_sync(self, callbacks: List[SimpleCallback]) -> None:
        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

    async def _fire_book(self, snapshot: OrderbookSnapshot) -> None:
        for cb in self._on_book_cbs:
            try:
                result = cb(snapshot)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def _handle_message(self, raw: str) -> None:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._process_event(item)
        elif isinstance(data, dict):
            await self._process_event(data)

    async def _process_event(self, event: dict) -> None:
        event_type = event.get("event_type") or event.get("type") or ""
        asset_id = str(event.get("asset_id") or event.get("market") or "")
        if not asset_id:
            return

        if event_type == "book":
            snap = self._build_snapshot(event, asset_id)
            self._books[asset_id] = snap
            await self._fire_book(snap)

        elif event_type == "price_change":
            existing = self._books.get(asset_id)
            if existing is not None:
                snap = self._apply_deltas(existing, event)
            else:
                snap = self._build_snapshot(event, asset_id)
            self._books[asset_id] = snap
            await self._fire_book(snap)

    @staticmethod
    def _parse_levels(raw_levels) -> List[Level]:
        out: List[Level] = []
        for lvl in (raw_levels or []):
            try:
                if isinstance(lvl, dict):
                    p = float(lvl.get("price", 0))
                    s = float(lvl.get("size", 0))
                else:
                    p, s = float(lvl[0]), float(lvl[1])
                if s > 0:
                    out.append(Level(price=p, size=s))
            except Exception:
                pass
        return out

    def _build_snapshot(self, event: dict, asset_id: str) -> OrderbookSnapshot:
        ts = float(event.get("timestamp", 0) or 0)
        bids = sorted(self._parse_levels(event.get("bids")), key=lambda l: l.price, reverse=True)
        asks = sorted(self._parse_levels(event.get("asks")), key=lambda l: l.price)
        return OrderbookSnapshot(asset_id=asset_id, timestamp=ts, bids=bids, asks=asks)

    def _apply_deltas(self, existing: OrderbookSnapshot, event: dict) -> OrderbookSnapshot:
        ts = float(event.get("timestamp", existing.timestamp) or existing.timestamp)
        bids: Dict[float, float] = {l.price: l.size for l in existing.bids}
        asks: Dict[float, float] = {l.price: l.size for l in existing.asks}

        for change in (event.get("changes") or []):
            try:
                side_str, price_str, size_str = change[0], change[1], change[2]
                p = float(price_str)
                s = float(size_str)
                if side_str.upper() in ("BUY", "BID"):
                    if s == 0:
                        bids.pop(p, None)
                    else:
                        bids[p] = s
                else:
                    if s == 0:
                        asks.pop(p, None)
                    else:
                        asks[p] = s
            except Exception:
                pass

        bid_list = sorted([Level(p, s) for p, s in bids.items() if s > 0], key=lambda l: l.price, reverse=True)
        ask_list = sorted([Level(p, s) for p, s in asks.items() if s > 0], key=lambda l: l.price)
        return OrderbookSnapshot(asset_id=existing.asset_id, timestamp=ts, bids=bid_list, asks=ask_list)


# ---------------------------------------------------------------------------
# Legacy WebSocketClient (used by db_alert_watcher.py)
# ---------------------------------------------------------------------------

class WebSocketClient:
    """Simple WebSocket client used by db_alert_watcher.

    Wraps MarketWebSocket with the older connect/subscribe/listen interface.
    detector.update(token, price) is called on every orderbook update.
    """

    def __init__(self, detector) -> None:
        self.detector = detector
        self.tokens: List[str] = []
        self._ws = MarketWebSocket()

        @self._ws.on_book
        async def _on_book(snapshot: OrderbookSnapshot) -> None:
            try:
                self.detector.update(snapshot.asset_id, snapshot.mid_price)
            except Exception:
                pass

    async def connect(self) -> None:
        """No-op — connection is established in listen()."""
        pass

    async def subscribe(self, tokens: List[str]) -> None:
        self.tokens = list(tokens)
        await self._ws.subscribe(self.tokens, replace=True)

    async def listen(self) -> None:
        """Connect and stream forever (blocks until cancelled)."""
        await self._ws.run(auto_reconnect=True)
