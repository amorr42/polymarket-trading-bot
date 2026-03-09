#!/usr/bin/env python3
"""Polymarket Orderbook Viewer (Event/Slug)

This is a clean, separate entry-point for monitoring *any* Polymarket market
identified by a slug (typically the URL slug on polymarket.com for simple
binary events) or by a direct YES/NO token pair.

It reuses the same MarketManager + WebSocket stack as the 15-minute coin
viewer, but swaps out the discovery layer via MarketSelector.

Usage
-----
  # Monitor by slug
  python apps/event_orderbook_viewer.py --slug khamenei-public-appearance-by-friday

  # Monitor by direct token IDs (asset_id)
  python apps/event_orderbook_viewer.py --yes-token <YES_ID> --no-token <NO_ID>

Notes
-----
  - WebSocket streaming is token-id based; slug is used only for lookup.
  - This tool is read-only; it does not place trades.
"""

import sys
import asyncio
import argparse
import urllib.parse
import logging
import time
from pathlib import Path

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib import MarketManager, PriceTracker, Colors, SlugMarketSelector, TokenPairSelector
from lib.alerts import PumpDetector

def _normalize_slug(raw: str) -> str:
    """Accept a plain slug or a full polymarket.com URL and return the slug."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    # If it's a URL, extract the path and take the last segment (after /event/ if present)
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            path = urllib.parse.urlparse(raw).path.strip("/")
            parts = path.split("/")
            if "event" in parts:
                i = parts.index("event")
                if i + 1 < len(parts):
                    return parts[i + 1]
            return parts[-1] if parts else raw
        except Exception:
            return raw
    # If user pasted "/event/<slug>" or similar
    raw = raw.strip("/")
    if raw.startswith("event/"):
        return raw.split("/", 1)[1]
    return raw


from lib.terminal_utils import format_countdown


class EventOrderbookTUI:
    """Real-time orderbook viewer for a specific event/slug."""

    def __init__(self, selector, *, enable_alerts: bool = False, sensitivity: str = "med", beep: bool = False):
        self.market = MarketManager(selector=selector, auto_switch_market=False)
        self.prices = PriceTracker()
        self.detector = PumpDetector(sensitivity=sensitivity, beep=beep) if enable_alerts else None
        self.last_alert_line: str | None = None
        self.last_alert_ts: int = 0
        self.beep = beep
        self.running = False

    async def run(self) -> None:
        self.running = True

        @self.market.on_book_update
        async def handle_book(snapshot):  # pyright: ignore[reportUnusedFunction]
            for side, token_id in self.market.token_ids.items():
                if token_id == snapshot.asset_id:
                    self.prices.record(side, snapshot.mid_price)

                    # Adaptive pump/dump alerts (UP side is treated as probability signal)
                    if self.detector:
                        if side == "up":
                            topn = self.detector.topn
                            bid_sizes = tuple(l.size for l in snapshot.bids[:topn])
                            ask_sizes = tuple(l.size for l in snapshot.asks[:topn])
                            alert = self.detector.update(
                                ts=int(snapshot.timestamp),
                                up_p=float(snapshot.mid_price),
                                up_best_bid=float(snapshot.best_bid),
                                up_best_ask=float(snapshot.best_ask),
                                up_bids_sizes=bid_sizes,
                                up_asks_sizes=ask_sizes,
                            )
                        else:
                            alert = self.detector.update(ts=int(snapshot.timestamp), down_p=float(snapshot.mid_price))

                        if alert:
                            self.last_alert_line = alert.format_one_line()
                            self.last_alert_ts = int(snapshot.timestamp)
                            if self.beep:
                                # Terminal bell
                                print("\a", end="", flush=True)
                    break

        if not await self.market.start():
            err = getattr(self.market, "last_error", "") or "Failed to start market manager"
            print(f"{Colors.RED}{err}{Colors.RESET}")

            # If this is a slug-based selector, show a few candidate binary markets to choose from.
            try:
                if isinstance(selector, SlugMarketSelector):
                    cands = selector.list_binary_candidates()
                    if cands:
                        print("\nPossible binary markets under this event (pick one of these slugs):")
                        for m in cands[:10]:
                            slug = str(m.get("slug") or "")
                            q = str(m.get("question") or m.get("title") or "")
                            end = str(m.get("endDate") or "")
                            vol = m.get("volume") or m.get("volumeNum") or ""
                            liq = m.get("liquidity") or m.get("liquidityNum") or ""
                            print(f"- {slug} | end: {end} | vol: {vol} | liq: {liq}\n  {q}")
                        print("\nRun with: python apps/event_orderbook_viewer.py --slug <one-of-the-slugs-above> --alert")
            except Exception:
                pass
            return

        await self.market.wait_for_data(timeout=5.0)

        try:
            while self.running:
                self.render()
                await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.market.stop()

    def render(self) -> None:
        lines = []

        ws_status = f"{Colors.GREEN}Connected{Colors.RESET}" if self.market.is_connected else f"{Colors.RED}Disconnected{Colors.RESET}"
        market = self.market.current_market
        countdown = "--:--"
        if market:
            mins, secs = market.get_countdown()
            countdown = format_countdown(mins, secs)

        up_label = self.market.labels.get("up", "YES")
        down_label = self.market.labels.get("down", "NO")

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")
        lines.append(f"{Colors.CYAN}Event Orderbook TUI{Colors.RESET} | {ws_status} | Ends: {countdown}")
        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Alerts banner (shows for ~20 seconds)
        if self.last_alert_line and market:
            age = int(time.time()) - int(self.last_alert_ts) if self.last_alert_ts else 0
            if age <= 20:
                lines.append(f"{Colors.BOLD}{Colors.YELLOW}ALERT{Colors.RESET} {self.last_alert_line}")
                lines.append("")

        if market:
            lines.append(f"Market: {market.question}")
            lines.append(f"Slug: {market.slug}")
            lines.append("")

        up_ob = self.market.get_orderbook("up")
        down_ob = self.market.get_orderbook("down")

        lines.append(f"{Colors.GREEN}{up_label:^39}{Colors.RESET}|{Colors.RED}{down_label:^39}{Colors.RESET}")
        lines.append(f"{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}|{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}")
        lines.append("-" * 80)

        up_bids = up_ob.bids[:10] if up_ob else []
        up_asks = up_ob.asks[:10] if up_ob else []
        down_bids = down_ob.bids[:10] if down_ob else []
        down_asks = down_ob.asks[:10] if down_ob else []

        for i in range(10):
            up_bid = f"{up_bids[i].price:>9.4f} {up_bids[i].size:>9.1f}" if i < len(up_bids) else f"{'--':>9} {'--':>9}"
            up_ask = f"{up_asks[i].price:>9.4f} {up_asks[i].size:>9.1f}" if i < len(up_asks) else f"{'--':>9} {'--':>9}"
            down_bid = f"{down_bids[i].price:>9.4f} {down_bids[i].size:>9.1f}" if i < len(down_bids) else f"{'--':>9} {'--':>9}"
            down_ask = f"{down_asks[i].price:>9.4f} {down_asks[i].size:>9.1f}" if i < len(down_asks) else f"{'--':>9} {'--':>9}"
            lines.append(f"{up_bid} | {up_ask}|{down_bid} | {down_ask}")

        lines.append("-" * 80)

        up_mid = up_ob.mid_price if up_ob else 0
        down_mid = down_ob.mid_price if down_ob else 0
        up_spread = self.market.get_spread("up")
        down_spread = self.market.get_spread("down")

        lines.append(
            f"Mid: {Colors.GREEN}{up_mid:.4f}{Colors.RESET}  Spread: {up_spread:.4f}           |"
            f"Mid: {Colors.RED}{down_mid:.4f}{Colors.RESET}  Spread: {down_spread:.4f}"
        )

        up_history = self.prices.get_history_count("up")
        down_history = self.prices.get_history_count("down")
        up_vol = self.prices.get_volatility("up", 60)
        down_vol = self.prices.get_volatility("down", 60)

        lines.append("")
        lines.append(
            f"History: {up_label}={up_history} {down_label}={down_history} | "
            f"60s Volatility: {up_label}={up_vol:.4f} {down_label}={down_vol:.4f}"
        )

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")
        lines.append(f"{Colors.DIM}Press Ctrl+C to exit{Colors.RESET}")

        print("\033[H\033[J" + "\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Orderbook TUI for a Polymarket event/market")
    parser.add_argument("--slug", type=str, default=None, help="Polymarket market/event slug")
    parser.add_argument("--yes-token", type=str, default=None, help="YES token_id (asset_id)")
    parser.add_argument("--no-token", type=str, default=None, help="NO token_id (asset_id)")

    parser.add_argument("--alert", action="store_true", help="Enable adaptive pump/dump alerts")
    parser.add_argument(
        "--sensitivity",
        type=str,
        default="med",
        choices=["low", "med", "high"],
        help="Alert sensitivity (low=less noise, high=more sensitive)",
    )
    parser.add_argument("--beep", action="store_true", help="Beep on alert")

    args = parser.parse_args()

    selector = None
    if args.yes_token and args.no_token:
        selector = TokenPairSelector(args.yes_token, args.no_token, labels={"up": "YES", "down": "NO"})
    elif args.slug:
        selector = SlugMarketSelector(_normalize_slug(args.slug))
    else:
        parser.error("Provide --slug or both --yes-token and --no-token")

    tui = EventOrderbookTUI(
        selector=selector,
        enable_alerts=bool(args.alert),
        sensitivity=args.sensitivity,
        beep=bool(args.beep),
    )
    try:
        asyncio.run(tui.run())
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
