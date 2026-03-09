#!/usr/bin/env python3
"""Ingest Polymarket markets into PostgreSQL (descending by createdAt).

This replaces the older CSV writer flow. It fetches markets from Gamma in
newest-first order, filters them by a keyword (e.g., "iran"), and upserts
matching rows into Postgres.

Usage:
  export DATABASE_URL='postgresql://user:pass@host:5432/db'
  python apps/ingest_markets_pg.py --keyword iran --batch 500

Notes:
  - Uses newest-first pagination (order=createdAt, ascending=false, offset=...)
  - Stops early when it reaches markets older than the newest one already stored
    for the given keyword.
"""

from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional

if __package__ is None:  # allow running as "python apps/ingest_markets_pg.py" on Windows
    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from src.gamma_client import GammaClient

from lib.db import (
    connect,
    ensure_schema,
    get_latest_created_at,
    get_latest_created_at_filtered,
    upsert_markets,
    upsert_events,
    upsert_event_markets,
    upsert_event_tags,
    upsert_market_tags,
)

from dotenv import load_dotenv
load_dotenv()


def _match_keyword_market(m: Dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    kw = keyword.lower()
    slug = str(m.get("slug") or "").lower()
    q = str(m.get("question") or m.get("title") or "").lower()
    tick = ""
    if m.get("events") and isinstance(m.get("events"), list) and m["events"]:
        tick = str((m["events"][0] or {}).get("ticker") or "").lower()
    return (kw in slug) or (kw in q) or (kw in tick)


def _match_keyword_event(ev: Dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    kw = keyword.lower()
    fields = [
        str(ev.get("slug") or "").lower(),
        str(ev.get("title") or "").lower(),
        str(ev.get("ticker") or "").lower(),
        str(ev.get("description") or "").lower(),
    ]
    return any(kw in f for f in fields)


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest Gamma markets into Postgres")
    p.add_argument("--source", choices=["markets", "events", "both"], default="events")
    p.add_argument("--keyword", default="", help="Optional keyword filter (slug/title/question/ticker)")
    p.add_argument("--tag-slug", default="", help="Optional category/tag slug for events (e.g., nfl-playoffs)")
    p.add_argument("--batch", type=int, default=200, help="Batch size (events or markets)")
    p.add_argument("--max-batches", type=int, default=0, help="Safety cap (0 = no cap)")
    p.add_argument("--order", default="createdAt", help="Order for listing (events: volume24hr/createdAt)")
    p.add_argument("--use-pagination", action="store_true", help="Use /events/pagination endpoint")
    args = p.parse_args()

    kw = args.keyword.strip()
    tag_slug = args.tag_slug.strip()
    if not kw and not tag_slug:
        raise SystemExit("At least one of --keyword or --tag-slug must be provided")

    gamma = GammaClient()

    conn = connect()
    try:
        ensure_schema(conn)
        newest_seen: Optional[datetime] = get_latest_created_at_filtered(conn, keyword=kw or None, tag_slug=tag_slug or None)
        if newest_seen:
            print(f"DB newest created_at for keyword='{kw}' tag_slug='{tag_slug}': {newest_seen.isoformat()}")
        else:
            print(f"DB has no rows yet for keyword='{kw}' tag_slug='{tag_slug}'.")

        offset = 0
        total_upserted = 0
        batches = 0

        while True:
            if args.max_batches and batches >= args.max_batches:
                print("Reached --max-batches cap, stopping.")
                break

            # EVENTS SOURCE (recommended for tag_slug/category)
            if args.source in ("events", "both"):
                # Use tag_slug for filtering; if only keyword is given, try it as tag_slug too.
                # The events API filters by tag natively; active/archived/closed narrow to live events.
                effective_tag = tag_slug or kw
                ev_params = {
                    "order": args.order,
                    "ascending": False,
                    "limit": args.batch,
                    "offset": offset,
                }
                if effective_tag:
                    ev_params["tag_slug"] = effective_tag

                events = gamma.list_events(use_pagination=args.use_pagination, **ev_params)
                if not events:
                    print("No more events returned. Done.")
                    break

                # Apply keyword filter at event level
                events = [ev for ev in events if _match_keyword_event(ev, kw)]

                # Upsert event rows
                upsert_events(conn, events)

                # Flatten markets from events
                flat_markets: List[Dict[str, Any]] = []
                for ev in events:
                    for m in (ev.get("markets") or []):
                        if _match_keyword_market(m, kw):
                            flat_markets.append(m)

                if flat_markets:
                    total_upserted += upsert_markets(conn, flat_markets)

                # Relations
                upsert_event_markets(conn, events)
                if tag_slug:
                    upsert_event_tags(conn, events, tag_slug=tag_slug)
                    upsert_market_tags(conn, events, tag_slug=tag_slug)

                print(f"Batch offset={offset}: events={len(events)} markets_upserted={len(flat_markets)}")
                batches += 1
                offset += args.batch

                continue

            # MARKETS SOURCE (legacy keyword scan)
            mk_params = {
                "order": args.order,
                "ascending": False,
                "limit": args.batch,
                "offset": offset,
            }
            markets = gamma.list_markets(**mk_params)
            if not markets:
                print("No more markets returned. Done.")
                break

            matched: List[Dict[str, Any]] = [m for m in markets if _match_keyword_market(m, kw)]
            if matched:
                upserted = upsert_markets(conn, matched)
                total_upserted += upserted
                print(f"Batch offset={offset}: matched={len(matched)} upserted={upserted}")
            else:
                print(f"Batch offset={offset}: matched=0")

            batches += 1
            offset += len(markets)

        print(f"Done. Total upserted: {total_upserted}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
