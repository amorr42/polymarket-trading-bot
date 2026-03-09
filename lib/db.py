"""PostgreSQL helpers for storing Polymarket market metadata.

This project originally wrote discovered markets to CSV. We now persist them
to PostgreSQL so the alert/watcher layer can reuse the saved token IDs.

Environment variables:
  DATABASE_URL: postgres connection string, e.g.
    postgresql://user:pass@localhost:5432/polymarket
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Example: postgresql://user:pass@host:5432/dbname"
        )
    # Common misconfiguration: people append schema like "/public" to the database name.
    # PostgreSQL DSN format is .../dbname (schema is not part of dbname).
    if dsn.rstrip("/").endswith("/public"):
        raise RuntimeError(
            "DATABASE_URL looks like it ends with '/public'. "
            "PostgreSQL database names cannot contain '/public'. "
            "Use .../polymarket and refer to schema separately (default is 'public')."
        )
    return dsn


def connect():
    """Create a new psycopg2 connection."""
    return psycopg2.connect(get_dsn())


def ensure_schema(conn) -> None:
    """Create tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE EXTENSION IF NOT EXISTS pg_trgm;

            CREATE TABLE IF NOT EXISTS markets (
              id              BIGINT PRIMARY KEY,
              slug            TEXT UNIQUE,
              question        TEXT,
              created_at      TIMESTAMPTZ,
              closed_time     TIMESTAMPTZ,
              accepting_orders BOOLEAN,
              condition_id    TEXT,
              outcomes        JSONB,
              clob_token_ids  JSONB,
              neg_risk        BOOLEAN,
              volume          NUMERIC,
              ticker          TEXT,
              updated_at      TIMESTAMPTZ NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_markets_created_at ON markets(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);
            CREATE INDEX IF NOT EXISTS idx_markets_question_gin ON markets USING GIN (to_tsvector('simple', coalesce(question,'')));

            -- --- Events / tag categorization layer ---
            CREATE TABLE IF NOT EXISTS events (
              id            TEXT PRIMARY KEY,
              slug          TEXT,
              title         TEXT,
              ticker        TEXT,
              description   TEXT,
              active        BOOLEAN,
              closed        BOOLEAN,
              archived      BOOLEAN,
              volume24hr    NUMERIC,
              volume        NUMERIC,
              liquidity     NUMERIC,
              start_date    TIMESTAMPTZ,
              end_date      TIMESTAMPTZ,
              created_at    TIMESTAMPTZ,
              updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_events_created_at_desc ON events(created_at DESC NULLS LAST);
            CREATE INDEX IF NOT EXISTS idx_events_slug ON events(slug);
            CREATE INDEX IF NOT EXISTS idx_events_ticker ON events(ticker);
            CREATE INDEX IF NOT EXISTS idx_events_volume24hr_desc ON events(volume24hr DESC NULLS LAST);

            CREATE TABLE IF NOT EXISTS event_markets (
              event_id  TEXT   NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              market_id BIGINT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
              PRIMARY KEY (event_id, market_id)
            );
            CREATE INDEX IF NOT EXISTS idx_event_markets_market_id ON event_markets(market_id);

            CREATE TABLE IF NOT EXISTS event_tags (
              event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              tag_slug   TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (event_id, tag_slug)
            );
            CREATE INDEX IF NOT EXISTS idx_event_tags_tag_slug ON event_tags(tag_slug);

            CREATE TABLE IF NOT EXISTS market_tags (
              market_id  BIGINT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
              tag_slug   TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (market_id, tag_slug)
            );
            CREATE INDEX IF NOT EXISTS idx_market_tags_tag_slug ON market_tags(tag_slug);
            """
        )
    conn.commit()


def upsert_events(conn, events: Sequence[Dict[str, Any]]) -> int:
    """Upsert Gamma event objects into the events table."""
    if not events:
        return 0

    rows: List[Tuple[Any, ...]] = []
    for ev in events:
        eid = ev.get("id")
        if eid is None:
            continue
        eid = str(eid)
        rows.append(
            (
                eid,
                ev.get("slug"),
                ev.get("title") or ev.get("name"),
                ev.get("ticker"),
                ev.get("description"),
                bool(ev.get("active")) if ev.get("active") is not None else None,
                bool(ev.get("closed")) if ev.get("closed") is not None else None,
                bool(ev.get("archived")) if ev.get("archived") is not None else None,
                ev.get("volume24hr"),
                ev.get("volume"),
                ev.get("liquidity"),
                _parse_dt(ev.get("startDate") or ev.get("start_date")),
                _parse_dt(ev.get("endDate") or ev.get("end_date")),
                _parse_dt(ev.get("createdAt") or ev.get("created_at")),
                _utcnow(),
            )
        )

    sql = """
        INSERT INTO events (
          id, slug, title, ticker, description,
          active, closed, archived,
          volume24hr, volume, liquidity,
          start_date, end_date, created_at, updated_at
        ) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
          slug = EXCLUDED.slug,
          title = EXCLUDED.title,
          ticker = EXCLUDED.ticker,
          description = EXCLUDED.description,
          active = EXCLUDED.active,
          closed = EXCLUDED.closed,
          archived = EXCLUDED.archived,
          volume24hr = EXCLUDED.volume24hr,
          volume = EXCLUDED.volume,
          liquidity = EXCLUDED.liquidity,
          start_date = EXCLUDED.start_date,
          end_date = EXCLUDED.end_date,
          created_at = EXCLUDED.created_at,
          updated_at = EXCLUDED.updated_at;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()
    return len(rows)


def upsert_event_markets(conn, events: Sequence[Dict[str, Any]]) -> int:
    """Upsert event->market relations based on `event['markets']`."""
    rows: List[Tuple[Any, ...]] = []
    for ev in events or []:
        eid = ev.get("id")
        if eid is None:
            continue
        eid = str(eid)
        for m in (ev.get("markets") or []):
            mid = m.get("id")
            try:
                mid = int(mid)
            except Exception:
                continue
            rows.append((eid, mid))
    if not rows:
        return 0
    # Only insert rows where both event_id and market_id already exist in their
    # respective tables (avoids FK violations when keyword filter skips some markets).
    sql = """
        INSERT INTO event_markets (event_id, market_id)
        SELECT v.eid, v.mid
        FROM (VALUES %s) AS v(eid, mid)
        WHERE EXISTS (SELECT 1 FROM events  WHERE id      = v.eid)
          AND EXISTS (SELECT 1 FROM markets WHERE id::text = v.mid::text)
        ON CONFLICT DO NOTHING;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()
    return len(rows)


def upsert_event_tags(conn, events: Sequence[Dict[str, Any]], tag_slug: str) -> int:
    """Associate each event in `events` with a tag_slug."""
    tag_slug = (tag_slug or "").strip()
    if not tag_slug:
        return 0
    rows: List[Tuple[Any, ...]] = []
    for ev in events or []:
        eid = ev.get("id")
        if eid is None:
            continue
        rows.append((str(eid), tag_slug, _utcnow()))
    if not rows:
        return 0
    sql = """
        INSERT INTO event_tags (event_id, tag_slug, updated_at)
        VALUES %s
        ON CONFLICT (event_id, tag_slug) DO UPDATE SET
          updated_at = EXCLUDED.updated_at;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()
    return len(rows)


def upsert_market_tags(conn, events: Sequence[Dict[str, Any]], tag_slug: str) -> int:
    """Associate markets (via events[].markets) with a tag_slug."""
    tag_slug = (tag_slug or "").strip()
    if not tag_slug:
        return 0
    rows: List[Tuple[Any, ...]] = []
    for ev in events or []:
        for m in (ev.get("markets") or []):
            mid = m.get("id")
            try:
                mid = int(mid)
            except Exception:
                continue
            rows.append((mid, tag_slug, _utcnow()))
    if not rows:
        return 0
    sql = """
        INSERT INTO market_tags (market_id, tag_slug, updated_at)
        VALUES %s
        ON CONFLICT (market_id, tag_slug) DO UPDATE SET
          updated_at = EXCLUDED.updated_at;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()
    return len(rows)


def upsert_markets(conn, markets: Sequence[Dict[str, Any]]) -> int:
    """Upsert a list of Gamma market objects into the markets table.

    Returns number of rows inserted/updated (best-effort).
    """
    if not markets:
        return 0

    rows: List[Tuple[Any, ...]] = []
    for m in markets:
        # Gamma fields come as mixed types; normalize a few
        mid = m.get("id")
        try:
            mid = int(mid)
        except Exception:
            # If id isn't numeric, skip (shouldn't happen)
            continue

        slug = m.get("slug")
        question = m.get("question") or m.get("title")
        created_at = _parse_dt(m.get("createdAt"))
        closed_time = _parse_dt(m.get("closedTime"))
        accepting = bool(m.get("acceptingOrders"))
        condition_id = m.get("conditionId")
        outcomes = _parse_jsonish(m.get("outcomes"))
        clob_ids = _parse_jsonish(m.get("clobTokenIds"))
        neg_risk = bool(m.get("negRiskAugmented") or m.get("negRiskOther"))
        volume = m.get("volume")
        ticker = ""
        if m.get("events") and isinstance(m.get("events"), list) and m["events"]:
            ticker = (m["events"][0] or {}).get("ticker", "") or ""

        rows.append(
            (
                mid,
                slug,
                question,
                created_at,
                closed_time,
                accepting,
                condition_id,
                json.dumps(outcomes),
                json.dumps(clob_ids),
                neg_risk,
                volume,
                ticker,
                _utcnow(),
            )
        )

    sql = """
        INSERT INTO markets (
          id, slug, question, created_at, closed_time, accepting_orders,
          condition_id, outcomes, clob_token_ids, neg_risk, volume, ticker, updated_at
        ) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
          slug = EXCLUDED.slug,
          question = EXCLUDED.question,
          created_at = EXCLUDED.created_at,
          closed_time = EXCLUDED.closed_time,
          accepting_orders = EXCLUDED.accepting_orders,
          condition_id = EXCLUDED.condition_id,
          outcomes = EXCLUDED.outcomes,
          clob_token_ids = EXCLUDED.clob_token_ids,
          neg_risk = EXCLUDED.neg_risk,
          volume = EXCLUDED.volume,
          ticker = EXCLUDED.ticker,
          updated_at = EXCLUDED.updated_at;
    """

    with conn.cursor() as cur:
        execute_values(cur, sql, rows)
    conn.commit()
    return len(rows)


def fetch_binary_markets(conn, keyword: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Fetch binary markets matching keyword in question/slug.

    Only returns markets with exactly 2 token ids.
    """
    kw = (keyword or "").strip()
    if not kw:
        raise ValueError("keyword must be non-empty")

    q = """
        SELECT id, slug, question, created_at, closed_time, accepting_orders,
               condition_id, outcomes, clob_token_ids, volume, ticker
        FROM markets
        WHERE (
          slug ILIKE %(kwlike)s OR question ILIKE %(kwlike)s
        )
        AND accepting_orders = TRUE
        ORDER BY created_at DESC NULLS LAST
        LIMIT %(lim)s
    """
    with conn.cursor() as cur:
        cur.execute(q, {"kwlike": f"%{kw}%", "lim": limit})
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            m = dict(zip(cols, row))
            # outcomes/clob_token_ids are already decoded to python by psycopg2?
            # With jsonb, psycopg2 returns dict/list already for some configs.
            # Ensure list.
            clob = m.get("clob_token_ids")
            if isinstance(clob, str):
                try:
                    clob = json.loads(clob)
                except Exception:
                    clob = []
            if not isinstance(clob, list) or len(clob) != 2:
                continue
            m["clob_token_ids"] = clob
            outs = m.get("outcomes")
            if isinstance(outs, str):
                try:
                    outs = json.loads(outs)
                except Exception:
                    outs = []
            m["outcomes"] = outs if isinstance(outs, list) else []
            out.append(m)
        return out


def fetch_binary_markets_filtered(
    conn,
    keyword: Optional[str] = None,
    tag_slug: Optional[str] = None,
    limit: int = 200,
    *,
    open_only: bool = True,
    require_event_open: bool = False,
    allow_unfiltered: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch binary markets filtered by optional keyword and/or tag_slug.

    - keyword filters on markets.slug/question (ILIKE)
    - tag_slug filters via market_tags table
    - open_only filters out closed markets (closed_time <= now) and non-accepting markets
    - require_event_open additionally requires a linked event that is not ended/closed/archived
    """
    kw = (keyword or "").strip()
    tag = (tag_slug or "").strip()
    if not kw and not tag and not allow_unfiltered:
        raise ValueError("At least one of keyword or tag_slug must be provided (or set allow_unfiltered=True)")

    open_clause = ""
    if open_only:
        open_clause = "AND m.accepting_orders = TRUE AND (m.closed_time IS NULL OR m.closed_time > NOW())"
    else:
        open_clause = "AND m.accepting_orders = TRUE"

    # Avoid duplicates by using EXISTS instead of joining events directly.
    event_open_clause = ""
    if require_event_open:
        event_open_clause = """
          AND EXISTS (
            SELECT 1
            FROM event_markets em
            JOIN events e ON e.id = em.event_id
            WHERE em.market_id = m.id
              AND (e.end_date IS NULL OR e.end_date > NOW())
              AND COALESCE(e.closed, FALSE) = FALSE
              AND COALESCE(e.archived, FALSE) = FALSE
          )
        """

    where_kw = ""
    params: Dict[str, Any] = {"lim": limit, "kwlike": f"%{kw}%", "tag_slug": tag}
    if kw:
        where_kw = "AND (m.slug ILIKE %(kwlike)s OR m.question ILIKE %(kwlike)s)"

    if tag:
        q = f"""
            SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                   m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
            FROM markets m
            JOIN market_tags t ON t.market_id = m.id
            WHERE t.tag_slug = %(tag_slug)s
              {open_clause}
              {event_open_clause}
              {where_kw}
            ORDER BY m.created_at DESC NULLS LAST
            LIMIT %(lim)s
        """
    else:
        if kw:
            q = f"""
                SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                       m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
                FROM markets m
                WHERE 1=1
                  {open_clause}
                  {event_open_clause}
                  {where_kw}
                ORDER BY m.created_at DESC NULLS LAST
                LIMIT %(lim)s
            """
        else:
            # Unfiltered mode (used by --token-db watcher): just grab the newest open markets.
            q = f"""
                SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                       m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
                FROM markets m
                WHERE 1=1
                  {open_clause}
                  {event_open_clause}
                ORDER BY m.created_at DESC NULLS LAST
                LIMIT %(lim)s
            """

    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Reuse the same post-processing as fetch_binary_markets
    out: List[Dict[str, Any]] = []
    for m in rows:
        clob = m.get("clob_token_ids")
        if isinstance(clob, str):
            try:
                clob = json.loads(clob)
            except Exception:
                clob = []
        if not isinstance(clob, list) or len(clob) != 2:
            continue
        m["clob_token_ids"] = clob
        outs = m.get("outcomes")
        if isinstance(outs, str):
            try:
                outs = json.loads(outs)
            except Exception:
                outs = []
        m["outcomes"] = outs if isinstance(outs, list) else []
        out.append(m)
    return out


def fetch_markets_filtered(
    conn,
    keyword: Optional[str] = None,
    tag_slug: Optional[str] = None,
    limit: int = 200,
    *,
    open_only: bool = True,
    require_event_open: bool = False,
    allow_unfiltered: bool = False,
    allow_multi: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch markets filtered by optional keyword and/or tag_slug.

    This is a generalized version of :func:`fetch_binary_markets_filtered`.

    Args:
      keyword: filters on markets.slug/question (ILIKE)
      tag_slug: filters via market_tags table
      open_only: require accepting_orders and market closed_time in the future
      require_event_open: require at least one linked event that is not ended/closed/archived
      allow_unfiltered: if True, keyword/tag can be empty and we just return newest open markets
      allow_multi: if False, only binary markets (exactly 2 token ids) are returned.
                   if True, markets with >=2 token ids are allowed.
    """

    kw = (keyword or "").strip()
    tag = (tag_slug or "").strip()
    if not kw and not tag and not allow_unfiltered:
        raise ValueError(
            "At least one of keyword or tag_slug must be provided (or set allow_unfiltered=True)"
        )

    open_clause = ""
    if open_only:
        open_clause = "AND m.accepting_orders = TRUE AND (m.closed_time IS NULL OR m.closed_time > NOW())"
    else:
        open_clause = "AND m.accepting_orders = TRUE"

    event_open_clause = ""
    if require_event_open:
        event_open_clause = """
          AND EXISTS (
            SELECT 1
            FROM event_markets em
            JOIN events e ON e.id = em.event_id
            WHERE em.market_id = m.id
              AND (e.end_date IS NULL OR e.end_date > NOW())
              AND COALESCE(e.closed, FALSE) = FALSE
              AND COALESCE(e.archived, FALSE) = FALSE
          )
        """

    where_kw = ""
    params: Dict[str, Any] = {"lim": limit, "kwlike": f"%{kw}%", "tag_slug": tag}
    if kw:
        where_kw = "AND (m.slug ILIKE %(kwlike)s OR m.question ILIKE %(kwlike)s)"

    if tag:
        q = f"""
            SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                   m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
            FROM markets m
            JOIN market_tags t ON t.market_id = m.id
            WHERE t.tag_slug = %(tag_slug)s
              {open_clause}
              {event_open_clause}
              {where_kw}
            ORDER BY m.created_at DESC NULLS LAST
            LIMIT %(lim)s
        """
    else:
        if kw:
            q = f"""
                SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                       m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
                FROM markets m
                WHERE 1=1
                  {open_clause}
                  {event_open_clause}
                  {where_kw}
                ORDER BY m.created_at DESC NULLS LAST
                LIMIT %(lim)s
            """
        else:
            q = f"""
                SELECT m.id, m.slug, m.question, m.created_at, m.closed_time, m.accepting_orders,
                       m.condition_id, m.outcomes, m.clob_token_ids, m.volume, m.ticker
                FROM markets m
                WHERE 1=1
                  {open_clause}
                  {event_open_clause}
                ORDER BY m.created_at DESC NULLS LAST
                LIMIT %(lim)s
            """

    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    out: List[Dict[str, Any]] = []
    for m in rows:
        clob = m.get("clob_token_ids")
        if isinstance(clob, str):
            try:
                clob = json.loads(clob)
            except Exception:
                clob = []
        if not isinstance(clob, list):
            continue
        if allow_multi:
            if len(clob) < 2:
                continue
        else:
            if len(clob) != 2:
                continue
        m["clob_token_ids"] = clob

        outs = m.get("outcomes")
        if isinstance(outs, str):
            try:
                outs = json.loads(outs)
            except Exception:
                outs = []
        m["outcomes"] = outs if isinstance(outs, list) else []
        out.append(m)

    return out


def fetch_current_market_by_prefix(
    conn,
    slug_prefix: str,
    window_seconds: int = 0,
) -> Optional[Dict[str, Any]]:
    """Return the currently active market whose slug starts with slug_prefix.

    For timed markets (e.g. btc-updown-5m, btc-updown-15m) the slug ends
    with a Unix timestamp that encodes the window start.  We use that
    timestamp to pick the window that is live right now:

      1. If any market has a slug_ts <= now < slug_ts + window_seconds, return it.
      2. If no window is currently active (gap between windows), return the
         next upcoming window (smallest slug_ts > now).
      3. If window_seconds=0 (unknown interval), auto-detect from the prefix:
         "5m" → 300 s, "15m" → 900 s, "1h" → 3600 s, etc.
         Falls back to closed_time ordering if the slug has no numeric suffix.

    Returns a dict with columns: id, slug, question, closed_time,
    accepting_orders, condition_id, outcomes, clob_token_ids.
    clob_token_ids and outcomes are decoded to Python lists.
    Returns None if no matching market is found.
    """
    import re
    import time as _time

    slug_prefix = (slug_prefix or "").strip().rstrip("-")
    if not slug_prefix:
        return None

    # Auto-detect window duration from prefix when not explicitly given.
    if window_seconds <= 0:
        m_obj = re.search(r"(\d+)m(?:-|$)", slug_prefix)
        if m_obj:
            window_seconds = int(m_obj.group(1)) * 60
        else:
            m_obj = re.search(r"(\d+)h(?:-|$)", slug_prefix)
            if m_obj:
                window_seconds = int(m_obj.group(1)) * 3600
            else:
                window_seconds = 900  # sensible default (15 min)

    # Fetch all open markets with this prefix (at most a few hundred rows).
    q = """
        SELECT id, slug, question, closed_time, accepting_orders,
               condition_id, outcomes, clob_token_ids
        FROM markets
        WHERE slug LIKE %(prefix)s
          AND accepting_orders = TRUE
          AND (closed_time IS NULL OR closed_time > NOW())
        ORDER BY closed_time ASC NULLS LAST
        LIMIT 500
    """
    with conn.cursor() as cur:
        cur.execute(q, {"prefix": f"{slug_prefix}-%"})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    if not rows:
        return None

    def _decode(m: Dict[str, Any]) -> Dict[str, Any]:
        for field in ("clob_token_ids", "outcomes"):
            val = m.get(field)
            if isinstance(val, str):
                try:
                    m[field] = json.loads(val)
                except Exception:
                    m[field] = []
            elif val is None:
                m[field] = []
        return m

    # Extract slug timestamp suffix (last numeric segment).
    _ts_re = re.compile(r"-(\d{9,})$")

    def _slug_ts(m: Dict[str, Any]) -> Optional[int]:
        slug = m.get("slug") or ""
        match = _ts_re.search(slug)
        return int(match.group(1)) if match else None

    now_epoch = _time.time()

    # Separate markets that have slug timestamps vs those that don't.
    ts_markets = [(m, ts) for m in rows if (ts := _slug_ts(m)) is not None]
    no_ts_markets = [m for m in rows if _slug_ts(m) is None]

    if ts_markets:
        # 1. Find markets whose window is currently active.
        active = [
            (m, ts)
            for m, ts in ts_markets
            if ts <= now_epoch < ts + window_seconds
        ]
        if active:
            # Pick the one ending soonest (largest ts among currently active).
            active.sort(key=lambda x: x[1], reverse=True)
            return _decode(active[0][0])

        # 2. Fallback: next upcoming window (smallest ts > now).
        upcoming = [(m, ts) for m, ts in ts_markets if ts > now_epoch]
        if upcoming:
            upcoming.sort(key=lambda x: x[1])
            return _decode(upcoming[0][0])

        # 3. Fallback: most recently started (largest ts <= now that is still open).
        past_open = [(m, ts) for m, ts in ts_markets if ts <= now_epoch]
        if past_open:
            past_open.sort(key=lambda x: x[1], reverse=True)
            return _decode(past_open[0][0])

    # No slug-timestamp markets; fall back to closed_time ordering.
    if no_ts_markets:
        return _decode(no_ts_markets[0])

    return None


def get_latest_created_at(conn, keyword: Optional[str] = None) -> Optional[datetime]:
    """Return the newest created_at in DB.

    If keyword is provided, limit to rows whose slug/question match the keyword.
    """
    if keyword:
        q = """
            SELECT MAX(created_at)
            FROM markets
            WHERE slug ILIKE %(kwlike)s OR question ILIKE %(kwlike)s
        """
        params = {"kwlike": f"%{keyword.strip()}%"}
    else:
        q = "SELECT MAX(created_at) FROM markets"
        params = {}

    with conn.cursor() as cur:
        cur.execute(q, params)
        row = cur.fetchone()
        return row[0] if row else None


def get_latest_created_at_filtered(
    conn,
    keyword: Optional[str] = None,
    tag_slug: Optional[str] = None,
) -> Optional[datetime]:
    """Return newest markets.created_at filtered by optional keyword and/or tag_slug."""
    kw = (keyword or "").strip()
    tag = (tag_slug or "").strip()
    if not kw and not tag:
        return get_latest_created_at(conn)

    params: Dict[str, Any] = {"kwlike": f"%{kw}%", "tag_slug": tag}
    where_kw = ""
    if kw:
        where_kw = "AND (m.slug ILIKE %(kwlike)s OR m.question ILIKE %(kwlike)s)"

    if tag:
        q = f"""
            SELECT MAX(m.created_at)
            FROM markets m
            JOIN market_tags t ON t.market_id = m.id
            WHERE t.tag_slug = %(tag_slug)s
            {where_kw}
        """
    else:
        q = """
            SELECT MAX(created_at)
            FROM markets
            WHERE slug ILIKE %(kwlike)s OR question ILIKE %(kwlike)s
        """
    with conn.cursor() as cur:
        cur.execute(q, params)
        row = cur.fetchone()
        return row[0] if row else None


def _parse_jsonish(val: Any) -> Any:
    if val is None:
        return []
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        try:
            return json.loads(s)
        except Exception:
            return []
    return []


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # Gamma usually returns ISO; datetime.fromisoformat handles "Z" with replace
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return None
    return None
