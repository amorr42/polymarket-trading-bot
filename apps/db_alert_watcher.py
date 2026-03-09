"""Alert watcher: reads token IDs from the database and streams real-time
orderbook updates via Polymarket WebSocket. Alerts when momentum changes.

Usage:
  python apps/db_alert_watcher.py --keyword iran
  python apps/db_alert_watcher.py --tag-slug crypto --lookback-seconds 120
  python apps/db_alert_watcher.py --keyword bitcoin --min-abs-pp 0.02

The script:
  1. Reads matching markets from the PostgreSQL database.
  2. Subscribes to their YES/NO token IDs on the CLOB WebSocket.
  3. Runs MomentumDetector on each update and prints alerts.
"""

from __future__ import annotations

import os
import sys
import argparse
import asyncio

# Allow running as "python apps/db_alert_watcher.py" from project root or apps/
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from lib.alerts.momentum_detector import MomentumDetector
from lib.db import connect, ensure_schema, fetch_markets_filtered
from lib.terminal_utils import Colors
from src.websocket_client import WebSocketClient


def parse_args():
    p = argparse.ArgumentParser(description="DB-backed real-time alert watcher")
    p.add_argument("--keyword", type=str, default="", help="Keyword filter on market slug/question")
    p.add_argument("--tag-slug", type=str, default="", help="Tag slug filter (e.g. crypto, sports)")
    p.add_argument("--event-url", type=str, default="", help="(unused, kept for compatibility)")
    p.add_argument("--include-related", action="store_true")
    p.add_argument("--spread-max-pp", type=float, default=0.0)
    p.add_argument("--lookback-seconds", type=int, default=300, help="MomentumDetector lookback window (seconds)")
    p.add_argument("--min-abs-pp", type=float, default=0.01, help="Minimum absolute price-point change for alert")
    p.add_argument("--limit", type=int, default=50, help="Max markets to watch (default: 50)")
    return p.parse_args()


def load_tokens_from_db(keyword: str, tag_slug: str, limit: int) -> list[str]:
    """Fetch token IDs from the database for markets matching the filters."""
    conn = connect()
    try:
        ensure_schema(conn)
        markets = fetch_markets_filtered(
            conn,
            keyword=keyword or None,
            tag_slug=tag_slug or None,
            limit=limit,
            open_only=True,
            allow_unfiltered=(not keyword and not tag_slug),
            allow_multi=False,
        )
        tokens: list[str] = []
        for m in markets:
            for tid in (m.get("clob_token_ids") or []):
                if tid and str(tid) not in tokens:
                    tokens.append(str(tid))
        return tokens
    finally:
        conn.close()


async def main():
    args = parse_args()
    kw = args.keyword.strip()
    tag = args.tag_slug.strip()

    # Load token IDs from database
    print(f"{Colors.CYAN}Loading markets from database...{Colors.RESET}")
    try:
        tokens = load_tokens_from_db(kw, tag, args.limit)
    except Exception as e:
        print(f"{Colors.RED}DB error: {e}{Colors.RESET}")
        print("Run ingest_markets_pg.py first to populate the database.")
        return

    if not tokens:
        print(f"{Colors.YELLOW}No tokens found in DB for keyword='{kw}' tag='{tag}'.{Colors.RESET}")
        print("Run: python apps/ingest_markets_pg.py --keyword <word> or --tag-slug <tag>")
        return

    print(f"{Colors.GREEN}Watching {len(tokens)} token(s){Colors.RESET} | "
          f"lookback={args.lookback_seconds}s min_change={args.min_abs_pp:.3f}")
    print(f"Press Ctrl+C to stop.\n")

    detector = MomentumDetector(
        lookback_seconds=args.lookback_seconds,
        min_abs_pp=args.min_abs_pp,
    )

    ws = WebSocketClient(detector)
    await ws.connect()
    await ws.subscribe(tokens)
    await ws.listen()


if __name__ == "__main__":
    asyncio.run(main())
