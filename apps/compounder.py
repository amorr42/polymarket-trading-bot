#!/usr/bin/env python3
"""
Compounder v5 — Oracle Value Strategy

Mechanism:
  - protected_base ($10): never risked, always preserved
  - trading_capital = balance - protected_base  (only this portion trades)
  - VALUE-BASED: Oracle price vs. market price arbitrage
  - Zone-based sizing: position size determined by price level
  - Settlement-aware exit: high-probability entries held until settlement

Signals:
  1. Swing Bounce/Reject: Technical signal (swing lows/highs)
  2. Oracle Value: Fundamental arbitrage (BTC price vs. market mispricing)

Zone System:
  DEAD    (< 0.10): BLOCK — outcome nearly certain
  LOW     (0.10-0.30): ACCEPT — small position
  MID     (0.30-0.70): NORMAL — mean reversion
  HIGH    (0.70-0.90): GOOD — high probability
  PREMIUM (0.90-0.99): BEST — hold until settlement

Oracle Protection:
  - Last 30s: no new positions opened
  - Last 25s: open positions are force-closed

Usage:
    python apps/compounder.py --balance 12 --protected 10 --target-mult 1.5 --db-prefix btc-updown-5m
    python apps/compounder.py --balance 15 --protected 10 --target-mult 2.0 --db-prefix btc-updown-5m
"""

import math
import os
import re
import sys
import asyncio
import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, IO

logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("src.gamma_client").setLevel(logging.WARNING)

from dotenv import load_dotenv
load_dotenv()

# Windows terminal UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib import MarketManager, CoinIntervalSelector, DbPrefixSelector
from lib.alerts.swing_detector import SwingDetector
from lib.btc_oracle import BTCOracle, calculate_value_opportunity
from lib.terminal_utils import Colors
from src.websocket_client import OrderbookSnapshot

FEE_RATE  = 0.005   # 0.5% taker fee (each leg: entry + exit)
SEP72     = "=" * 72
SEP72D    = "-" * 72

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

# ---------------------------------------------------------------------------
# Zone system
# ---------------------------------------------------------------------------

ZONES = {
    "DEAD":    {"min": 0.00, "max": 0.10, "allowed": False},
    "LOW":     {"min": 0.10, "max": 0.30, "allowed": True,  "win_rate": 0.50, "payoff": 2.5, "kelly_frac": 0.15},  # Small position
    "MID":     {"min": 0.30, "max": 0.70, "allowed": True,  "win_rate": 0.55, "payoff": 2.0, "kelly_frac": 0.25},
    "HIGH":    {"min": 0.70, "max": 0.90, "allowed": True,  "win_rate": 0.70, "payoff": 1.5, "kelly_frac": 0.35},
    "PREMIUM": {"min": 0.90, "max": 0.99, "allowed": True,  "win_rate": 0.85, "payoff": 1.2, "kelly_frac": 0.50},
}

ZONE_COLORS = {
    "DEAD": Colors.RED,
    "LOW": Colors.RED,
    "MID": Colors.CYAN,
    "HIGH": Colors.GREEN,
    "PREMIUM": Colors.GREEN + Colors.BOLD,
}


def get_zone(price: float) -> str:
    """Determine zone based on price level."""
    if price < 0.10: return "DEAD"
    if price < 0.30: return "LOW"
    if price < 0.70: return "MID"
    if price < 0.90: return "HIGH"
    return "PREMIUM"


def kelly_size_pct(zone: str) -> float:
    """Calculate Kelly-optimal position size fraction for the given zone."""
    params = ZONES.get(zone)
    if not params or not params.get("allowed"):
        return 0.0
    w = params["win_rate"]
    r = params["payoff"]
    # Kelly formula: f* = (W * R - (1-W)) / R
    kelly = (w * r - (1 - w)) / r
    kelly = max(0.0, kelly)
    # Fractional Kelly for safety
    return kelly * params["kelly_frac"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CompounderConfig:
    # Money management
    balance: float          = 12.0
    protected_base: float   = 10.0
    min_threshold: float    = 0.30
    target_multiplier: float = 1.5

    # Trade size (fallback — used if zone cannot be determined)
    trade_size_pct: float   = 0.20

    # Entry signal — swing detector
    swing_window_s: int     = 90      # Shorter window (more triggers)
    swing_min_move: float   = 0.008   # Low threshold (0.8pp is sufficient)
    swing_cooldown_s: int   = 10      # Short cooldown

    # Exit
    take_profit_pct: float  = 0.10    # +10% -> take profit (fast)
    stop_loss_pct: float    = 0.05    # -5%  -> stop loss

    # Oracle protection only
    no_trade_last_secs: int  = 30     # No new entries in last 30s
    force_exit_secs: int     = 25     # Force close in last 25s
    cooldown_secs: int       = 5      # Very short cooldown

    # Aggressive mode
    always_enter: bool       = True   # Always enter at market open
    enter_timeout_secs: int  = 120    # Force entry if no signal within 2 minutes

    # Session
    duration_minutes: int   = 60


@dataclass
class SimTrade:
    id: int
    market_slug: str
    side: str
    zone: str        # Entry zone
    signal: str      # Signal type: "swing_bounce", "swing_reject", "always_enter"
    entry_price: float
    entry_time: float
    shares: float
    cost_usdc: float
    entry_fee: float

    exit_price: float  = 0.0
    exit_time: float   = 0.0
    exit_reason: str   = ""
    gross_pnl: float   = 0.0
    exit_fee: float    = 0.0
    net_pnl: float     = 0.0
    status: str        = "open"

    @property
    def hold_secs(self) -> float:
        end = self.exit_time if self.status == "closed" else time.time()
        return end - self.entry_time

    @property
    def return_pct(self) -> float:
        return (self.net_pnl / self.cost_usdc * 100) if self.cost_usdc > 0 else 0.0

    @property
    def total_fees(self) -> float:
        return self.entry_fee + self.exit_fee


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class OracleSafeCompounder:

    def __init__(self, config: CompounderConfig, selector=None, coin: str = "BTC",
                 log_path: Optional[str] = None):
        self.config = config
        self.coin = coin  # Keyword for market preloading
        self.balance = config.balance
        self.initial_trading_capital = max(0.0, config.balance - config.protected_base)
        self.target_trading_capital  = self.initial_trading_capital * config.target_multiplier

        if selector:
            self.market = MarketManager(selector=selector)
        else:
            self.market = MarketManager(coin=coin)

        # Swing detectors (per side)
        self._swing = {
            "up": SwingDetector(
                window_s=config.swing_window_s,
                min_move_pp=config.swing_min_move,
                cooldown_s=config.swing_cooldown_s,
            ),
            "down": SwingDetector(
                window_s=config.swing_window_s,
                min_move_pp=config.swing_min_move,
                cooldown_s=config.swing_cooldown_s,
            ),
        }

        # BTC Oracle for value-based trading
        self._btc_oracle = BTCOracle(use_cache=True)
        self._price_to_beat: Optional[float] = None  # BTC price snapshot at market open

        # Market timing
        self._market_start_time: float = 0.0

        # Market Open Rush strategy
        self._market_open_window: int = 45  # First 45 seconds aggressive
        self._market_open_trades: int = 0   # Rush trades executed in this market
        self._max_rush_trades_per_market: int = 3  # Max rush trades per market

        self.trades: List[SimTrade] = []
        self.open_trade: Optional[SimTrade] = None
        self._trade_counter = 0
        self._last_close_time: float = 0.0
        self._start_time: float = 0.0
        self._last_status_time: float = 0.0
        self._last_file_status_time: float = 0.0
        self._on_new_line: bool = True
        self._session_done: bool = False
        self._stop_reason: str = ""

        # File log
        self._log_file: Optional[IO] = None
        if log_path:
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            self._log_file = open(log_path, "w", encoding="utf-8")

        # Statistics
        self._signals_seen = 0
        self._signals_by_type: Dict[str, int] = {"swing_bounce": 0, "swing_reject": 0, "oracle_value": 0, "market_open_rush": 0, "event_position_holding": 0, "dynamic_order_flow_event": 0, "flash_crash_recovery": 0}
        self._rejected_oracle = 0
        self._rejected_cooldown = 0
        self._rejected_floor = 0
        self._rejected_zone = 0      # DEAD zone block
        self._rejected_no_edge = 0   # Insufficient oracle value edge
        self._zone_entries: Dict[str, int] = {"LOW": 0, "MID": 0, "HIGH": 0, "PREMIUM": 0}

        # Orderbook tracking (Dynamic Order Flow Event Strategy)
        self._orderbook_snapshots: Dict[str, List[OrderbookSnapshot]] = {}
        self._last_orderbook: Dict[str, OrderbookSnapshot] = {}
        self._entry_analysis_complete = False
        self._pre_entry_start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Core calculations
    # ------------------------------------------------------------------

    @property
    def trading_capital(self) -> float:
        return max(0.0, self.balance - self.config.protected_base)

    @property
    def _tc_progress_pct(self) -> float:
        if self.initial_trading_capital <= 0:
            return 0.0
        return self.trading_capital / self.initial_trading_capital * 100

    def _get_countdown(self) -> int:
        market = self.market.current_market
        if not market:
            return 9999
        mins, secs = market.get_countdown()
        if mins < 0:
            return 9999
        return mins * 60 + secs

    def _extract_price_to_beat(self, market_slug: str) -> Optional[float]:
        """
        Extract price_to_beat from market slug.

        Market slug format: btc-updown-15m-1771562700
        Timestamp (1771562700) = market open time
        Price to beat = BTC price NOW (snapshot at market change)

        IMPORTANT: This snapshots the CURRENT BTC price when market opens.
        This becomes the "price to beat" for this market window.
        """
        import re
        match = re.search(r"-(\d{10})$", market_slug)
        if not match:
            # No timestamp in slug, can't determine price_to_beat
            return None

        try:
            # Snapshot current BTC price as the "price to beat"
            # This is the reference price for this market window
            btc_data = self._btc_oracle.get_current_price()
            self._file_log(f"PRICE_TO_BEAT | ${btc_data.price:.2f} ({btc_data.source})")
            return btc_data.price
        except Exception as e:
            self._file_log(f"ORACLE ERROR | Failed to get BTC price: {e}")
            return None

    def _wait_for_next_market_open(self, markets: List[Dict[str, object]]) -> None:
        """
        Find the next market opening from the preloaded list and wait for it.

        Args:
            markets: Market list returned by _preload_daily_markets()
        """
        from datetime import datetime, timezone

        if not markets:
            return

        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        # Extract timestamp from each market slug
        upcoming = []
        for market in markets:
            slug = market.get("slug", "")
            # Slug format: btc-updown-5m-1771623900
            parts = slug.split("-")
            if len(parts) >= 4:
                try:
                    ts = int(parts[-1])
                    # Only include future markets
                    if ts > now_ts:
                        upcoming.append((ts, slug, market))
                except ValueError:
                    continue

        if not upcoming:
            print(f"{Colors.YELLOW}No upcoming market found, starting immediately...{Colors.RESET}")
            return

        # Sort by timestamp, pick nearest
        upcoming.sort(key=lambda x: x[0])
        next_ts, next_slug, next_market = upcoming[0]

        # How many seconds to wait?
        wait_seconds = next_ts - now_ts

        if wait_seconds > 0:
            next_time = datetime.fromtimestamp(next_ts, tz=timezone.utc)
            print(f"{Colors.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}")
            print(f"{Colors.CYAN}  WAITING FOR NEXT MARKET OPEN{Colors.RESET}")
            print(f"{Colors.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}")
            print(f"  Market: {next_slug}")
            print(f"  Opens:  {next_time.strftime('%H:%M:%S')} UTC")
            print(f"  Wait:   {int(wait_seconds)}s ({int(wait_seconds/60)}m {int(wait_seconds%60)}s)")
            print(f"{Colors.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}")
            print()

            self._file_log(f"MARKET WAIT | {next_slug} @ {next_time.strftime('%H:%M:%S')} UTC ({int(wait_seconds)}s)")
            time.sleep(wait_seconds)

            print(f"{Colors.GREEN}Market open! Starting...{Colors.RESET}")
            print()
        else:
            print(f"{Colors.YELLOW}Market already open, starting immediately...{Colors.RESET}")

    def _preload_daily_markets(self, keyword: str) -> List[Dict[str, object]]:
        """
        Fetch all markets for the day (5m, 15m, 30m intervals).

        Slug format: {coin}-updown-{interval}-{timestamp}
        Example: btc-updown-15m-1771567200

        Args:
            keyword: Coin symbol (e.g. "BTC", "ETH")

        Returns:
            List of matching markets
        """
        from src.gamma_client import GammaClient
        from datetime import datetime, timezone, timedelta

        gamma = GammaClient()
        coin = keyword.upper()

        print(f"{Colors.CYAN}Loading markets: {coin} (all intervals, +2 hours){Colors.RESET}")
        self._file_log(f"MARKET LOAD | Coin: {coin} (+2 hours)")

        # Interval definitions (in seconds)
        intervals = {
            "5m": 5 * 60,    # 5 minutes = 300 seconds
            "15m": 15 * 60,  # 15 minutes = 900 seconds
            "30m": 30 * 60,  # 30 minutes = 1800 seconds
        }

        # Time window: now + next N hours (default 2 hours)
        # Note: fetching a full day is too slow (5m interval = 288 markets/day)
        now = datetime.now(timezone.utc)
        time_window_hours = 2  # Next 2 hours
        end_time = now + timedelta(hours=time_window_hours)

        matching_markets = []
        by_interval: Dict[str, int] = {}

        # Fetch markets for each interval
        for interval_name, interval_secs in intervals.items():
            prefix = f"{coin.lower()}-updown-{interval_name}"
            count = 0

            # Round down to interval boundary
            if interval_secs == 300:  # 5m
                minute = (now.minute // 5) * 5
                current_time = now.replace(minute=minute, second=0, microsecond=0)
            elif interval_secs == 900:  # 15m
                minute = (now.minute // 15) * 15
                current_time = now.replace(minute=minute, second=0, microsecond=0)
            else:  # 30m
                minute = (now.minute // 30) * 30
                current_time = now.replace(minute=minute, second=0, microsecond=0)

            # Fetch markets within the time window
            while current_time < end_time:
                ts = int(current_time.timestamp())
                slug = f"{prefix}-{ts}"

                # Fetch market
                market = gamma.get_market_by_slug(slug)

                if market and market.get("acceptingOrders"):
                    matching_markets.append(market)
                    count += 1

                # Advance to next interval
                current_time += timedelta(seconds=interval_secs)

            if count > 0:
                by_interval[interval_name] = count

        # Show results
        if matching_markets:
            total = len(matching_markets)
            breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(by_interval.items()))
            print(f"{Colors.GREEN}  {total} active markets found{Colors.RESET}")
            if breakdown:
                print(f"{Colors.CYAN}    {breakdown}{Colors.RESET}")
            self._file_log(f"MARKET LOAD | {total} markets: {breakdown}")
        else:
            print(f"{Colors.YELLOW}  No active markets found (coin: {coin}){Colors.RESET}")
            self._file_log(f"MARKET LOAD | No markets found")

        return matching_markets

    # ------------------------------------------------------------------
    # File log
    # ------------------------------------------------------------------

    def _file_log(self, msg: str) -> None:
        """Write clean text to the file log (no ANSI codes)."""
        if not self._log_file:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clean = _ANSI_RE.sub("", msg)
        self._log_file.write(f"[{ts}] {clean}\n")
        self._log_file.flush()

    def _file_log_status(self, prices: Dict[str, float]) -> None:
        """Write a status summary to the file log every 5 minutes."""
        if not self._log_file:
            return
        now = time.time()
        if now - self._last_file_status_time < 300:  # 5 minutes
            return
        self._last_file_status_time = now

        tc = self.trading_capital
        elapsed = now - self._start_time
        closed = [t for t in self.trades if t.status == "closed"]
        total_pnl = sum(t.net_pnl for t in closed)
        wins = len([t for t in closed if t.net_pnl > 0])
        losses = len([t for t in closed if t.net_pnl <= 0])
        self._file_log(
            f"STATUS | Balance:${self.balance:.2f} TC:${tc:.2f} PnL:${total_pnl:+.3f} | "
            f"Trades:{len(closed)} Wins:{wins} Losses:{losses} | {elapsed/60:.1f}m"
        )

    def _close_log_file(self) -> None:
        """Close the log file."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------
    # Session controls
    # ------------------------------------------------------------------

    def _check_session_state(self) -> Optional[str]:
        tc = self.trading_capital
        if tc >= self.target_trading_capital and self.initial_trading_capital > 0:
            return "TARGET_HIT"
        if tc < self.config.min_threshold:
            return "FLOOR_PROTECTED"
        return None

    def _should_skip_direction(self, side: str, price: float) -> Optional[str]:
        """
        Filter direction based on price-to-beat logic.

        Logic:
        - Price < 0.50 = BTC below price to beat -> do not buy UP (decline continues)
        - Price > 0.50 = BTC above price to beat -> do not buy DOWN (rally continues)

        Returns:
            Skip reason string or None
        """
        # If price is well below 0.50 (< 0.35) -> BTC too low -> don't buy UP
        if price < 0.35 and side == "up":
            return "price_too_low_for_up"

        # If price is well above 0.50 (> 0.65) -> BTC too high -> don't buy DOWN
        if price > 0.65 and side == "down":
            return "price_too_high_for_down"

        # Zone-based restrictions
        zone = get_zone(price)

        # In LOW zone (< 0.30) don't buy UP - bearish signal
        if zone == "LOW" and side == "up":
            return "low_zone_reject_up"

        # In HIGH/PREMIUM zone don't buy DOWN - bullish signal
        if zone in ["HIGH", "PREMIUM"] and side == "down":
            return "high_zone_reject_down"

        return None

    def _check_loss_streak(self, side: str, min_streak: int = 2) -> bool:
        """
        Check for consecutive losses in the same direction.

        Returns True if the last min_streak trades in the given direction all lost.
        """
        if len(self.trades) < min_streak:
            return False

        recent = self.trades[-min_streak:]
        return all(t.side == side and t.net_pnl < 0 for t in recent)

    # ------------------------------------------------------------------
    # Entry / exit logic
    # ------------------------------------------------------------------

    def _check_market_open_rush(self, prices: Dict[str, float],
                                 elapsed: float) -> Optional[Tuple[str, float, str]]:
        """
        Market Open Rush strategy: aggressive entry in the first 45s after open.

        Prices move quickly at market open, creating large opportunities.
        This strategy bypasses normal swing/oracle checks to scalp 5-10% quickly.

        Returns:
            (side, price, zone) or None
        """
        up = prices.get("up", 0)
        dn = prices.get("down", 0)

        if up <= 0 or dn <= 0:
            return None

        # Only trade mid-range prices (not extremes)
        # RUSH ZONE: 0.30-0.70 (normal MID zone)
        # Extreme zones don't have enough profit margin
        up_zone = get_zone(up)
        dn_zone = get_zone(dn)

        # First 15s: always enter in MID zone
        # 15-30s: enter in MID or HIGH zone
        # 30-45s: only enter if there is volatility

        if elapsed <= 15:
            # First 15s: aggressive entry in MID zone
            if up_zone == "MID":
                # Direction check
                if not self._should_skip_direction("up", up):
                    # Loss streak check
                    if not self._check_loss_streak("up", min_streak=2):
                        return ("up", up, "MID")
            elif dn_zone == "MID":
                if not self._should_skip_direction("down", dn):
                    if not self._check_loss_streak("down", min_streak=2):
                        return ("down", dn, "MID")

        elif elapsed <= 30:
            # 15-30s: MID or HIGH
            if up_zone in ["MID", "HIGH"]:
                # Estimate trend: weak DOWN -> UP bias
                if dn < 0.35:  # DOWN weak -> UP strong
                    if not self._should_skip_direction("up", up):
                        if not self._check_loss_streak("up", min_streak=2):
                            return ("up", up, up_zone)
            elif dn_zone in ["MID", "HIGH"]:
                if up < 0.35:  # UP weak -> DOWN strong
                    if not self._should_skip_direction("down", dn):
                        if not self._check_loss_streak("down", min_streak=2):
                            return ("down", dn, dn_zone)

        elif elapsed <= 45:
            # 30-45s: only enter in MID zone if price spread is wide enough
            spread = abs(up - dn)
            if spread > 0.15:  # >15% difference indicates volatility
                if up_zone == "MID" and dn < up:
                    if not self._should_skip_direction("up", up):
                        if not self._check_loss_streak("up", min_streak=2):
                            return ("up", up, "MID")
                elif dn_zone == "MID" and up < dn:
                    if not self._should_skip_direction("down", dn):
                        if not self._check_loss_streak("down", min_streak=2):
                            return ("down", dn, "MID")

        return None

    def _check_entry(self, prices: Dict[str, float],
                     swing_signals: Dict[str, object] = None,
                     ) -> Optional[Tuple[str, float, str, str]]:
        """Check entry conditions. Returns (side, price, zone, signal_type) or None."""
        if self.open_trade:
            return None

        remaining = self._get_countdown()

        # Oracle: no entry within last N seconds
        if remaining < self.config.no_trade_last_secs:
            self._rejected_oracle += 1
            return None

        # Cooldown
        if self._last_close_time > 0:
            if time.time() - self._last_close_time < self.config.cooldown_secs:
                self._rejected_cooldown += 1
                return None

        # Is trading capital sufficient?
        if self.trading_capital < self.config.min_threshold:
            self._rejected_floor += 1
            return None

        # --- AGGRESSIVE: Signal sources ---
        candidates: List[Tuple[str, float, str, str]] = []

        # Signal 1: Swing BOUNCE (reversal from low)
        if swing_signals:
            for side, swing in swing_signals.items():
                if swing and swing.kind == "BOUNCE":
                    current = prices.get(side, 0)
                    if 0 < current < 0.99:
                        candidates.append((side, current, get_zone(current), "swing_bounce"))
                elif swing and swing.kind == "REJECT":
                    # REJECT: reversal from high -> buy opposite side
                    opp = "down" if side == "up" else "up"
                    current = prices.get(opp, 0)
                    if 0 < current < 0.99:
                        candidates.append((opp, current, get_zone(current), "swing_reject"))

        # Signal 2: ORACLE VALUE (fundamental value arbitrage)
        opp = None  # Save for event holding check
        if self._price_to_beat is not None:
            try:
                current_btc = self._btc_oracle.get_current_price().price
                opp = calculate_value_opportunity(
                    price_to_beat=self._price_to_beat,
                    current_btc=current_btc,
                    market_prices=prices,
                    min_edge_pp=0.03,  # 3pp minimum edge (lowered from 5pp for sensitivity)
                )
                if opp:
                    side = opp["side"]
                    current = prices.get(side, 0)
                    if 0 < current < 0.99:
                        candidates.append((side, current, get_zone(current), "oracle_value"))
                else:
                    self._rejected_no_edge += 1
            except Exception:
                # Oracle error - skip
                pass

        # Signal 3: EVENT POSITION HOLDING (strategic holding for MID zone)
        if opp:
            try:
                side = opp["side"]
                current = prices.get(side, 0)
                zone = get_zone(current)
                event_sig = self._check_event_holding_signal(
                    prices=prices,
                    zone=zone,
                    countdown=remaining,
                    oracle_opp=opp
                )
                if event_sig:
                    # Add event holding signal (replaces oracle_value for MID zone)
                    candidates.append(event_sig)
            except Exception:
                # Event holding check failed - skip
                pass

        # (Dynamic Order Flow Event is called directly from the main loop)

        if not candidates:
            return None

        # Filter candidates
        for side, current, zone, signal_type in candidates:
            self._signals_seen += 1

            # 1) Block DEAD zone (< 0.10)
            if zone == "DEAD":
                self._rejected_zone += 1
                continue

            # 2) Direction check (price-to-beat logic)
            skip_reason = self._should_skip_direction(side, current)
            if skip_reason:
                self._file_log(f"SKIP | {signal_type} {side.upper()} @ {current:.3f} - {skip_reason}")
                continue

            # 3) Loss streak check (skip if 2+ losses in same direction)
            if self._check_loss_streak(side, min_streak=2):
                self._file_log(f"SKIP | {signal_type} {side.upper()} @ {current:.3f} - loss_streak")
                continue

            # Valid signal found
            return (side, current, zone, signal_type)

        return None

    def _check_event_holding_signal(
        self,
        prices: Dict[str, float],
        zone: str,
        countdown: int,
        oracle_opp: Optional[Dict]
    ) -> Optional[Tuple[str, float, str, str]]:
        """
        Check conditions for the Event Position Holding strategy.

        Entry criteria (dynamic — based on situation):
        - Oracle edge >= 3pp (directional confirmation)
        - Remaining time >= 120s (enough time to hold)
        - Not in DEAD zone (< 0.10 is too risky)
        - No recent flash crash (market stability)

        Returns:
            Tuple (side, price, zone, "event_position_holding") or None
        """
        # Block DEAD zone (too risky, requires large move)
        if zone == "DEAD":
            return None

        # Need sufficient time (at least 2 minutes to hold)
        if countdown < 120:
            return None

        # Oracle value edge required (>= 3pp directional confidence)
        if not oracle_opp or oracle_opp.get("edge", 0) < 0.03:
            return None

        # Check for recent flash crash (avoid unstable markets)
        try:
            if self.price_tracker.has_recent_flash_crash(self.market.token_id):
                return None
        except Exception:
            # If flash crash check fails, continue anyway
            pass

        # All criteria met - return signal
        side = oracle_opp["side"]
        price = prices.get(side, 0)
        if 0 < price < 0.99:
            return (side, price, zone, "event_position_holding")

        return None

    def _enter_trade(self, side: str, price: float, zone: str,
                     market_slug: str, signal_type: str = "swing_bounce") -> SimTrade:
        self._trade_counter += 1
        tc = self.trading_capital

        # Kelly-based position sizing (by zone)
        size_pct = kelly_size_pct(zone)
        if size_pct <= 0:
            size_pct = self.config.trade_size_pct  # fallback

        # Oracle value: larger position (90% - high edge)
        if signal_type == "oracle_value":
            size_pct *= 0.90
        # Swing signals: medium position (70%)
        elif signal_type.startswith("swing"):
            size_pct *= 0.70

        raw_size = tc * size_pct
        raw_size = max(raw_size, 0.05)  # minimum $0.05

        entry_fee = raw_size * FEE_RATE
        cost = raw_size + entry_fee
        shares = raw_size / price

        trade = SimTrade(
            id=self._trade_counter,
            market_slug=market_slug,
            side=side,
            zone=zone,
            signal=signal_type,
            entry_price=price,
            entry_time=time.time(),
            shares=shares,
            cost_usdc=cost,
            entry_fee=entry_fee,
        )
        self.balance -= cost
        self.open_trade = trade
        self.trades.append(trade)

        # Update statistics
        if zone in self._zone_entries:
            self._zone_entries[zone] += 1
        if signal_type in self._signals_by_type:
            self._signals_by_type[signal_type] += 1

        return trade

    def _close_trade(self, trade: SimTrade, exit_price: float, reason: str) -> float:
        gross   = (exit_price - trade.entry_price) * trade.shares
        exit_fee = (exit_price * trade.shares) * FEE_RATE
        net     = gross - exit_fee

        trade.exit_price  = exit_price
        trade.exit_time   = time.time()
        trade.exit_reason = reason
        trade.gross_pnl   = gross
        trade.exit_fee    = exit_fee
        trade.net_pnl     = net
        trade.status      = "closed"

        proceeds = max(exit_price * trade.shares - exit_fee, 0.0)
        self.balance += proceeds

        self.open_trade = None
        self._last_close_time = time.time()
        return net

    def _check_exits(self, prices: Dict[str, float]) -> Optional[str]:
        if not self.open_trade:
            return None

        trade = self.open_trade
        current = prices.get(trade.side, 0)
        remaining = self._get_countdown()

        if current <= 0:
            return None

        # Force close (all zones)
        if remaining <= self.config.force_exit_secs:
            return "force_exit"

        # PnL percentage
        initial_val = trade.entry_price * trade.shares
        current_val = current * trade.shares
        if initial_val <= 0:
            return None

        pnl_pct = (current_val - initial_val) / initial_val
        zone = trade.zone

        # MARKET OPEN RUSH: Fast exit strategy
        if trade.signal == "market_open_rush":
            # Aggressive TP/SL for rush trades:
            # - Target: 5-8% profit (vs. normal 10%)
            # - Stop: 3% loss (vs. normal 5%)
            # - Max hold: 30 seconds

            trade_duration = time.time() - trade.entry_time

            # Fast take profit (5-8%)
            if pnl_pct >= 0.05:  # 5%+ profit
                return "take_profit"

            # Fast stop loss (3%)
            if pnl_pct <= -0.03:  # 3% loss
                return "stop_loss"

            # Time-based exit: close if 30s elapsed and at break-even or better
            if trade_duration >= 30:
                if pnl_pct >= 0:  # Profitable or flat
                    return "rush_timeout"
                elif pnl_pct >= -0.02:  # Small loss, close it
                    return "rush_timeout"

            # Continue holding
            return None

        # EVENT POSITION HOLDING: Strategic holding with manipulation detection
        if trade.signal == "event_position_holding":
            # Calculate profit multiplier
            if trade.side == "up":
                profit_mult = current / trade.entry_price
            else:  # down
                profit_mult = (2 - current) / (2 - trade.entry_price) if (2 - trade.entry_price) > 0 else 1.0

            # Track peak multiplier for trailing stop
            if not hasattr(trade, 'peak_mult'):
                trade.peak_mult = profit_mult
            else:
                trade.peak_mult = max(trade.peak_mult, profit_mult)

            # 1. Stop loss check (-10%)
            if profit_mult < 0.90:
                return "stop_loss"

            # 2. Settlement countdown check (T-90s)
            if remaining <= 90:
                if profit_mult >= 1.3:
                    return "settlement_hedge_profit"
                else:
                    return "settlement_hedge_loss"

            # 3. Manipulation detection via swing
            try:
                swing = self.swing_detector.detect(
                    token_id=self.market.token_id,
                    direction=trade.side,
                    price_tracker=self.price_tracker
                )
                if swing and hasattr(swing, 'strength') and swing.strength > 15:
                    # Strong reversal detected - exit to protect profit
                    return "manipulation_suspected"
            except Exception:
                # Swing detection failed - continue with other checks
                pass

            # 4. Dynamic trailing stop
            if trade.peak_mult >= 2.0:
                # Hit 2x → tight trailing (5%)
                trailing_threshold = 0.95
            elif trade.peak_mult >= 1.5:
                # Hit 1.5x → medium trailing (10%)
                trailing_threshold = 0.90
            else:
                # Still scaling → wide trailing (15%)
                trailing_threshold = 0.85

            if profit_mult < (trade.peak_mult * trailing_threshold):
                return "trailing_stop_hit"

            # Hold position - let it run
            return None

        # DYNAMIC ORDER FLOW EVENT: Orderbook-based dynamic exit
        if trade.signal == "dynamic_order_flow_event" or trade.signal == "flash_crash_recovery":
            exit_reason = self._check_dynamic_exit(trade, current, remaining)
            if exit_reason:
                return exit_reason

        if zone == "PREMIUM":
            # Hold until settlement — no TP, SL only
            # Targeting $1.00 settlement payout
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            # Profitable or small loss -> hold
            return None

        elif zone == "HIGH":
            # Hold as settlement approaches (don't exit if profitable in last 90s)
            if remaining < 90 and pnl_pct > 0:
                return None  # In profit, wait for settlement
            # Normal TP/SL
            if pnl_pct >= self.config.take_profit_pct:
                return "take_profit"
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            return None

        else:  # MID zone
            # Standart TP/SL
            if pnl_pct >= self.config.take_profit_pct:
                return "take_profit"
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            return None

    # ------------------------------------------------------------------
    # Terminal output
    # ------------------------------------------------------------------

    def _nl(self) -> None:
        if not self._on_new_line:
            print()
            self._on_new_line = True

    def _progress_bar(self, current: float, start: float, target: float, width: int = 24) -> str:
        if target <= start or start < 0:
            return "[" + "?" * width + "]"
        pct = min(1.0, (current - start) / (target - start))
        filled = int(pct * width)
        bar = "#" * filled + "-" * (width - filled)
        return f"[{bar}] {pct*100:.0f}%"

    def _zone_tag(self, zone: str) -> str:
        c = ZONE_COLORS.get(zone, "")
        return f"{c}{zone:7}{Colors.RESET}"

    def _print_open(self, trade: SimTrade, remaining: int) -> None:
        self._nl()
        ts = datetime.now().strftime("%H:%M:%S")
        tc = self.trading_capital
        prog = self._progress_bar(tc, self.initial_trading_capital, self.target_trading_capital)
        kelly_pct = kelly_size_pct(trade.zone) * 100
        sig_tag = {
            "swing_bounce": "SB",
            "swing_reject": "SR",
            "always_enter": "AE",
            "market_open_rush": "RUSH",
            "oracle_value": "OV",
            "event_position_holding": "HOLD",
            "dynamic_order_flow_event": "FLOW",
            "flash_crash_recovery": "CRASH"
        }.get(trade.signal, "??")
        print(
            f"[{ts}] {Colors.CYAN}ENTRY {trade.side.upper():4} #{trade.id:2}{Colors.RESET} "
            f"{self._zone_tag(trade.zone)} [{sig_tag}] "
            f"Price:{trade.entry_price:.3f} | Shares:{trade.shares:.1f} | "
            f"Cost:${trade.cost_usdc:.2f} (Kelly:{kelly_pct:.0f}%) | T-{remaining}s  "
            f"TC:${tc:.2f} {prog}"
        )
        self._file_log(
            f"ENTRY | #{trade.id} {trade.side.upper()} {trade.zone:7} [{sig_tag}] @ {trade.entry_price:.3f} | "
            f"Kelly:{kelly_pct:.0f}% Cost:${trade.cost_usdc:.2f} | T-{remaining}s"
        )

    def _print_close(self, trade: SimTrade) -> None:
        self._nl()
        ts = datetime.now().strftime("%H:%M:%S")
        tc = self.trading_capital
        prog = self._progress_bar(tc, self.initial_trading_capital, self.target_trading_capital)
        c = Colors.GREEN if trade.net_pnl >= 0 else Colors.RED
        r = {
            "take_profit": "TAKE_PROFIT",
            "stop_loss":   "STOP_LOSS  ",
            "force_exit":  "FORCE_EXIT ",
            "session_end": "SESSION_END",
            "settlement":  "SETTLEMENT ",
            "rush_timeout": "R-TIME     ",
            "settlement_hedge_profit": "S-HEDGE+   ",
            "settlement_hedge_loss": "S-HEDGE-   ",
            "manipulation_suspected": "MANIP      ",
            "trailing_stop_hit": "TRAIL      ",
            "dynamic_target_min_trailing": "D-MIN      ",
            "dynamic_target_max_trailing": "D-MAX      ",
            "pump_dump_reversal": "PUMP-REV   ",
            "swing_reversal_detected": "SWING-REV  "
        }.get(trade.exit_reason, trade.exit_reason[:11])
        r_clean = r.strip()
        print(
            f"[{ts}] {c}EXIT  {trade.side.upper():4} #{trade.id:2}{Colors.RESET} "
            f"{self._zone_tag(trade.zone)} "
            f"{trade.entry_price:.3f}->{trade.exit_price:.3f} | {r} | "
            f"Net:{c}${trade.net_pnl:+.3f}{Colors.RESET}({c}{trade.return_pct:+.1f}%{Colors.RESET}) "
            f"Balance:${self.balance:.2f}  TC:${tc:.2f} {prog}"
        )
        self._file_log(
            f"EXIT | #{trade.id} {trade.side.upper()} {trade.zone:7} "
            f"{trade.entry_price:.3f}->{trade.exit_price:.3f} | {r_clean} | "
            f"Net:${trade.net_pnl:+.3f} ({trade.return_pct:+.1f}%) | Balance:${self.balance:.2f} TC:${tc:.2f}"
        )

    def _render_status(self, prices: Dict[str, float]) -> None:
        now = time.time()
        if now - self._last_status_time < 1.0:
            return
        self._last_status_time = now

        remaining = self._get_countdown()
        up = prices.get("up", 0)
        dn = prices.get("down", 0)
        elapsed = now - self._start_time
        tc = self.trading_capital
        tgt = self.target_trading_capital
        prog = self._progress_bar(tc, self.initial_trading_capital, tgt, width=18)

        # Zone goster
        up_zone = get_zone(up) if up > 0 else "?"
        dn_zone = get_zone(dn) if dn > 0 else "?"

        pos_str = ""
        if self.open_trade:
            t = self.open_trade
            cur = prices.get(t.side, t.entry_price)
            unr = (cur - t.entry_price) * t.shares
            c = Colors.GREEN if unr >= 0 else Colors.RED
            pos_str = f" {c}POS {t.side.upper()}({t.zone}) ${unr:+.3f}{Colors.RESET}"

        line = (
            f"  UP={up:.3f}[{up_zone[:3]}] DN={dn:.3f}[{dn_zone[:3]}] T-{remaining:4}s | "
            f"Base:${self.config.protected_base:.0f} TC:${tc:.2f}/${tgt:.2f} "
            f"{prog} | {elapsed/60:.1f}m{pos_str}   "
        )
        print(f"\r{line}", end="", flush=True)
        self._on_new_line = False

    # ------------------------------------------------------------------
    # Dynamic Order Flow Event Strategy Methods
    # ------------------------------------------------------------------

    async def _on_orderbook_update(self, snapshot: OrderbookSnapshot) -> None:
        """Process real-time orderbook updates"""
        try:
            token_id = snapshot.asset_id

            # Save snapshot history (last 100)
            if token_id not in self._orderbook_snapshots:
                self._orderbook_snapshots[token_id] = []

            self._orderbook_snapshots[token_id].append(snapshot)
            if len(self._orderbook_snapshots[token_id]) > 100:
                self._orderbook_snapshots[token_id].pop(0)

            # Update last snapshot
            self._last_orderbook[token_id] = snapshot
        except Exception as e:
            # CRITICAL: must surface this exception!
            print(f"\n[ERROR] _on_orderbook_update failed: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _calculate_order_flow_metrics(self, snapshot: OrderbookSnapshot) -> dict:
        """Calculate order flow metrics from orderbook snapshot."""
        # Bid-ask spread
        spread = snapshot.best_ask - snapshot.best_bid

        # Top-10 liquidity
        bid_liq = sum(level.size for level in snapshot.bids[:10])
        ask_liq = sum(level.size for level in snapshot.asks[:10])

        # Imbalance
        if bid_liq + ask_liq > 0:
            imbalance = (bid_liq - ask_liq) / (bid_liq + ask_liq)
        else:
            imbalance = 0.0

        return {
            "spread": spread,
            "bid_liquidity": bid_liq,
            "ask_liquidity": ask_liq,
            "imbalance": imbalance,
            "mid_price": snapshot.mid_price
        }

    def _calculate_price_velocity(self, side: str) -> float:
        """Price change velocity (points/second)."""
        token_id = self.market.up_token if side == "up" else self.market.down_token
        if token_id not in self._orderbook_snapshots or len(self._orderbook_snapshots[token_id]) < 2:
            return 0.0

        snapshots = self._orderbook_snapshots[token_id]
        first = snapshots[0]
        last = snapshots[-1]

        time_diff = last.timestamp - first.timestamp
        if time_diff <= 0:
            return 0.0

        price_diff = last.mid_price - first.mid_price
        velocity = price_diff / time_diff

        return velocity

    def _check_dynamic_entry_signal(self) -> Optional[Tuple[str, float, str, str]]:
        """
        Entry decision after 60 seconds of orderbook analysis.

        Returns:
            (side, entry_price, zone, signal) tuple or None
        """
        try:
            # Has 60 seconds elapsed?
            if not self._pre_entry_start_time:
                return None

            elapsed = time.time() - self._pre_entry_start_time
            if elapsed < 60:
                return None  # Too early

            if self._entry_analysis_complete:
                return None  # Analysis already done

            # Entry analysis complete flag
            self._entry_analysis_complete = True
            print(f"\n[FLOW] Dynamic entry analysis starting (60s elapsed)", flush=True)
            self._file_log(f"DEBUG | Dynamic entry analysis starting (60s elapsed)")
            return self._do_dynamic_entry_analysis()
        except Exception as e:
            print(f"\n[ERROR] _check_dynamic_entry_signal failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return None

    def _do_dynamic_entry_analysis(self) -> Optional[Tuple[str, float, str, str]]:
        """Actual dynamic entry analysis logic (separated for exception handling)."""
        # Orderbook metrikleri al
        up_token_id = self.market.up_token
        down_token_id = self.market.down_token
        print(f"[FLOW] Token IDs: up={up_token_id[:8]}... down={down_token_id[:8]}...", flush=True)

        up_snapshot = self._last_orderbook.get(up_token_id)
        down_snapshot = self._last_orderbook.get(down_token_id)
        print(f"[FLOW] Snapshots: up={up_snapshot is not None} down={down_snapshot is not None} (total cached: {len(self._last_orderbook)})", flush=True)

        if not up_snapshot or not down_snapshot:
            print(f"\n[FLOW] No orderbook snapshots! up={up_snapshot is not None} down={down_snapshot is not None}", flush=True)
            print(f"[FLOW] Available tokens: {list(self._last_orderbook.keys())[:3]}", flush=True)
            self._file_log(f"DEBUG | No orderbook snapshots: up={up_snapshot is not None} down={down_snapshot is not None}")
            self._file_log(f"DEBUG | Available tokens: {list(self._last_orderbook.keys())}")
            return None

        up_metrics = self._calculate_order_flow_metrics(up_snapshot)
        down_metrics = self._calculate_order_flow_metrics(down_snapshot)

        # Price velocity
        up_velocity = self._calculate_price_velocity("up")
        down_velocity = self._calculate_price_velocity("down")

        # Countdown check
        countdown = self._get_countdown()
        if countdown < 120:
            print(f"[FLOW] SKIP: Countdown too short ({countdown}s < 120s)", flush=True)
            self._file_log(f"DEBUG | Countdown too short: {countdown}s < 120s")
            return None  # Not enough time

        # Spread check
        if up_metrics["spread"] > 0.05 or down_metrics["spread"] > 0.05:
            print(f"[FLOW] SKIP: Spread too wide (up={up_metrics['spread']:.4f} down={down_metrics['spread']:.4f})", flush=True)
            self._file_log(f"DEBUG | Spread too wide: up={up_metrics['spread']:.4f} down={down_metrics['spread']:.4f}")
            return None  # Spread too wide

        # Liquidity check
        total_liq = up_metrics["bid_liquidity"] + up_metrics["ask_liquidity"]
        if total_liq < 100:
            print(f"[FLOW] SKIP: Low liquidity ({total_liq:.0f} < 100)", flush=True)
            self._file_log(f"DEBUG | Low liquidity: {total_liq:.0f} < 100")
            return None  # Low liquidity

        print(f"[FLOW] Passed checks! Imb: up={up_metrics['imbalance']:.2f} down={down_metrics['imbalance']:.2f} Vel: up={up_velocity:.4f} down={down_velocity:.4f}", flush=True)
        self._file_log(f"DEBUG | Passed basic checks. Imbalance: up={up_metrics['imbalance']:.2f} down={down_metrics['imbalance']:.2f} Velocity: up={up_velocity:.4f} down={down_velocity:.4f}")

        # Imbalance + directional bias
        if up_metrics["imbalance"] > 0.20:  # Buy-side pressure
            # Consider UP entry
            if up_velocity > 0.001 or (self._price_to_beat and self._btc_oracle.get_current_price().price > self._price_to_beat):
                entry_price = up_snapshot.best_ask
                zone = get_zone(entry_price)
                print(f"[FLOW] ENTRY SIGNAL! UP @ {entry_price:.3f} (imb={up_metrics['imbalance']:.2f} vel={up_velocity:.4f})", flush=True)
                self._file_log(f"DEBUG | [FLOW] ENTRY SIGNAL: UP @ {entry_price:.3f} (imbalance={up_metrics['imbalance']:.2f} velocity={up_velocity:.4f})")
                return ("up", entry_price, zone, "dynamic_order_flow_event")
            else:
                print(f"[FLOW] SKIP: UP imbalance OK ({up_metrics['imbalance']:.2f}) but velocity too low ({up_velocity:.4f} < 0.001)", flush=True)

        if down_metrics["imbalance"] > 0.20:  # Sell-side pressure (buy DOWN)
            # Consider DOWN entry
            if down_velocity > 0.001 or (self._price_to_beat and self._btc_oracle.get_current_price().price < self._price_to_beat):
                entry_price = down_snapshot.best_ask
                zone = get_zone(entry_price)
                print(f"[FLOW] ENTRY SIGNAL! DOWN @ {entry_price:.3f} (imb={down_metrics['imbalance']:.2f} vel={down_velocity:.4f})", flush=True)
                self._file_log(f"DEBUG | [FLOW] ENTRY SIGNAL: DOWN @ {entry_price:.3f} (imbalance={down_metrics['imbalance']:.2f} velocity={down_velocity:.4f})")
                return ("down", entry_price, zone, "dynamic_order_flow_event")
            else:
                print(f"[FLOW] SKIP: DOWN imbalance OK ({down_metrics['imbalance']:.2f}) but velocity too low ({down_velocity:.4f} < 0.001)", flush=True)

        # Neutral imbalance -> fall back to price velocity
        if abs(up_metrics["imbalance"]) < 0.10:
            if up_velocity > 0.002:  # Fast upward move
                entry_price = up_snapshot.best_ask
                zone = get_zone(entry_price)
                print(f"[FLOW] ENTRY SIGNAL! UP @ {entry_price:.3f} (velocity={up_velocity:.4f})", flush=True)
                self._file_log(f"DEBUG | [FLOW] ENTRY SIGNAL: UP @ {entry_price:.3f} (velocity={up_velocity:.4f})")
                return ("up", entry_price, zone, "dynamic_order_flow_event")
            elif down_velocity > 0.002:  # Fast move on the DOWN side
                entry_price = down_snapshot.best_ask
                zone = get_zone(entry_price)
                print(f"[FLOW] ENTRY SIGNAL! DOWN @ {entry_price:.3f} (velocity={down_velocity:.4f})", flush=True)
                self._file_log(f"DEBUG | [FLOW] ENTRY SIGNAL: DOWN @ {entry_price:.3f} (velocity={down_velocity:.4f})")
                return ("down", entry_price, zone, "dynamic_order_flow_event")

        print(f"[FLOW] SKIP: No entry criteria met (imbalances neutral)", flush=True)
        self._file_log(f"DEBUG | No entry criteria met. Imbalances neutral.")
        return None  # Kriterlere uymuyor

    def _calculate_dynamic_exit_targets(self, entry_price: float, side: str) -> Tuple[float, float]:
        """
        Dynamic exit price levels based on entry price.

        Returns:
            (min_target, max_target)
        """
        if side == "up":
            if entry_price < 0.40:  # LOW zone
                min_target = entry_price + 0.15
                max_target = entry_price + 0.25
            elif entry_price < 0.55:  # MID-LOW
                min_target = entry_price + 0.15
                max_target = entry_price + 0.20
            elif entry_price < 0.65:  # MID range
                min_target = entry_price + 0.10
                max_target = entry_price + 0.15
                # 0.55 → 0.65-0.70
                # 0.60 → 0.70-0.75
            elif entry_price < 0.75:  # MID-HIGH
                min_target = entry_price + 0.15
                max_target = entry_price + 0.20
                # 0.70 → 0.85-0.90
            else:  # HIGH zone
                min_target = entry_price + 0.10
                max_target = entry_price + 0.15
        else:  # DOWN
            if entry_price > 0.60:
                min_target = entry_price - 0.15
                max_target = entry_price - 0.25
            elif entry_price > 0.45:
                min_target = entry_price - 0.10
                max_target = entry_price - 0.15
            else:
                min_target = entry_price - 0.15
                max_target = entry_price - 0.20

        # Clamp 0-1
        min_target = max(0.01, min(0.99, min_target))
        max_target = max(0.01, min(0.99, max_target))

        return (min_target, max_target)

    def _check_dynamic_exit(self, pos: SimTrade, current_price: float, countdown: int) -> Optional[str]:
        """Exit check for the dynamic order flow event strategy."""

        # Dynamic targets hesapla
        min_target, max_target = self._calculate_dynamic_exit_targets(pos.entry_price, pos.side)

        # Have targets been reached?
        if pos.side == "up":
            reached_min = current_price >= min_target
            reached_max = current_price >= max_target
        else:
            reached_min = current_price <= min_target
            reached_max = current_price <= max_target

        # Peak tracking
        if not hasattr(pos, 'peak_price'):
            pos.peak_price = current_price
        else:
            if pos.side == "up":
                pos.peak_price = max(pos.peak_price, current_price)
            else:
                pos.peak_price = min(pos.peak_price, current_price)

        # Max target reached -> tight trailing stop (5%)
        if reached_max:
            if pos.side == "up":
                should_exit = current_price < (pos.peak_price * 0.95)
            else:
                should_exit = current_price > (pos.peak_price / 0.95)

            if should_exit:
                return "dynamic_target_max_trailing"

        # Min target reached -> medium trailing stop (10%)
        elif reached_min:
            if pos.side == "up":
                should_exit = current_price < (pos.peak_price * 0.90)
            else:
                should_exit = current_price > (pos.peak_price / 0.90)

            if should_exit:
                return "dynamic_target_min_trailing"

        # Stop loss (-10%)
        if pos.side == "up":
            loss_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            loss_pct = (pos.entry_price - current_price) / pos.entry_price

        if loss_pct < -0.10:
            return "stop_loss"

        # Settlement countdown
        if countdown <= 90:
            return "settlement_hedge"

        return None  # Hold

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Startup check
        if self.initial_trading_capital < self.config.min_threshold:
            print(f"{Colors.RED}ERROR: Trading capital too low.{Colors.RESET}")
            print(f"  Current: ${self.initial_trading_capital:.2f}")
            print(f"  Minimum: ${self.config.min_threshold:.2f}")
            print(f"  Suggestion: --balance {self.config.protected_base + 2:.0f} --protected {self.config.protected_base:.0f}")
            return

        @self.market.on_book_update
        async def _on_book(snap):
            await self._on_orderbook_update(snap)

        @self.market.on_market_change
        def _on_market_change(old_slug: str, new_slug: str):
            self._nl()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{Colors.YELLOW}MARKET:{Colors.RESET} "
                f"{old_slug[-12:]} -> {new_slug[-12:]}"
            )
            self._file_log(f"MARKET | {old_slug[-20:]} -> {new_slug[-20:]}")
            # Reset swing detectors
            cfg = self.config
            for side in ["up", "down"]:
                self._swing[side] = SwingDetector(
                    window_s=cfg.swing_window_s,
                    min_move_pp=cfg.swing_min_move,
                    cooldown_s=cfg.swing_cooldown_s,
                )
            # Reset market timer
            self._market_start_time = time.time()
            # Reset Market Open Rush counter
            self._market_open_trades = 0
            # Price to beat - BTC snapshot at market open
            self._price_to_beat = self._extract_price_to_beat(new_slug)
            # Start pre-entry analysis (Dynamic Order Flow Event Strategy)
            self._pre_entry_start_time = time.time()
            self._entry_analysis_complete = False
            self._orderbook_snapshots = {}
            self._last_orderbook = {}
            self._on_new_line = True

        # Preload all daily markets
        print()
        markets = self._preload_daily_markets(self.coin)

        # Wait for next market open
        self._wait_for_next_market_open(markets)
        print()

        if not await self.market.start():
            print(f"{Colors.RED}Market failed to start: {self.market.last_error}{Colors.RESET}")
            return

        await self.market.wait_for_data(timeout=10.0)

        # Session timing starts AFTER market wait
        self._start_time = time.time()
        end_time = self._start_time + self.config.duration_minutes * 60

        # Header
        self._nl()
        tgt_mult_pct = (self.config.target_multiplier - 1) * 100
        print(SEP72)
        print(f"  COMPOUNDER v6 — MARKET OPEN RUSH + ORACLE VALUE")
        print(SEP72)
        print(f"  Total balance    : ${self.config.balance:.2f}")
        print(f"  Protected base   : ${self.config.protected_base:.2f}  <- never risked")
        print(f"  Trading capital  : ${self.initial_trading_capital:.2f}  <- only this trades")
        print(f"  Target           : ${self.target_trading_capital:.2f}  (+{tgt_mult_pct:.0f}%  =  {self.config.target_multiplier}x)")
        print(f"  Min threshold    : ${self.config.min_threshold:.2f}  (stop if below this)")
        print()
        print(f"  Zone System:")
        print(f"    DEAD    (< 0.10) : BLOCK")
        print(f"    LOW  (0.10-0.30) : ACCEPT  — small position (Kelly ~{kelly_size_pct('LOW')*100:.0f}%)")
        print(f"    MID  (0.30-0.70) : NORMAL  (Kelly ~{kelly_size_pct('MID')*100:.0f}%)")
        print(f"    HIGH (0.70-0.90) : GOOD    (Kelly ~{kelly_size_pct('HIGH')*100:.0f}%)")
        print(f"    PREMIUM (> 0.90) : BEST    — hold for settlement (Kelly ~{kelly_size_pct('PREMIUM')*100:.0f}%)")
        print()
        print(f"  Take profit / SL : +{self.config.take_profit_pct*100:.0f}% / -{self.config.stop_loss_pct*100:.0f}%")
        print(f"  Oracle protection: no entry in last {self.config.no_trade_last_secs}s, force close in last {self.config.force_exit_secs}s")
        print()
        print(f"  STRATEGIES:")
        print(f"    Market Open Rush : aggressive entry in first {self._market_open_window}s (max {self._max_rush_trades_per_market} trades/market)")
        print(f"           ├─ Target: 5-8% profit, fast scalp")
        print(f"           └─ Exit: 5% TP / 3% SL, max 30s hold")
        print(f"    Signal 1 - Swing        : window={self.config.swing_window_s}s, move>={self.config.swing_min_move:.3f}pp")
        print(f"    Signal 2 - Oracle Value : BTC delta vs. market mispricing, min edge=3pp")
        print()
        print(f"  Session duration : {self.config.duration_minutes} minutes")
        print(f"  Fee rate         : {FEE_RATE*100:.1f}% (taker, each leg)")
        print(SEP72)
        print()

        # Session start log
        self._file_log(
            f"START | Balance:${self.config.balance:.2f} Protected:${self.config.protected_base:.2f} "
            f"TC:${self.initial_trading_capital:.2f} Target:${self.target_trading_capital:.2f} "
            f"Duration:{self.config.duration_minutes}m"
        )
        self._last_file_status_time = time.time()
        self._market_start_time = time.time()

        # Initialize pre-entry analysis timer for first market
        self._pre_entry_start_time = time.time()
        self._entry_analysis_complete = False
        self._orderbook_snapshots = {}
        self._last_orderbook = {}

        try:
            while time.time() < end_time and not self._session_done:
                market = self.market.current_market
                if not market:
                    await asyncio.sleep(0.3)
                    continue

                prices: Dict[str, float] = {}
                ts_int = int(time.time())
                swing_signals: Dict[str, object] = {}
                for side in ["up", "down"]:
                    mid = self.market.get_mid_price(side)
                    if mid > 0:
                        prices[side] = mid
                        # Update swing detector
                        swing = self._swing[side].update(ts=ts_int, p=mid)
                        if swing:
                            swing_signals[side] = swing

                if len(prices) < 2:
                    await asyncio.sleep(0.3)
                    continue

                # Session state check
                stop_reason = self._check_session_state()
                if stop_reason:
                    self._session_done = True
                    self._stop_reason = stop_reason
                    if self.open_trade:
                        ep = prices.get(self.open_trade.side, self.open_trade.entry_price)
                        self._close_trade(self.open_trade, ep, "session_end")
                        self._print_close(self.trades[-1])
                    break

                # Exit check
                reason = self._check_exits(prices)
                if reason and self.open_trade:
                    ep = prices.get(self.open_trade.side, self.open_trade.entry_price)
                    self._close_trade(self.open_trade, ep, reason)
                    self._print_close(self.trades[-1])

                # Dynamic Order Flow Event check (always runs, independent of position state)
                # Performs analysis once at T+60s and produces an entry signal
                dynamic_entry = self._check_dynamic_entry_signal()

                # Entry check (only when flat)
                if not self.open_trade:
                    # 1) Dynamic signal takes priority
                    if dynamic_entry:
                        side, price, zone, signal_type = dynamic_entry
                        trade = self._enter_trade(side, price, zone, market.slug, signal_type)
                        self._print_open(trade, self._get_countdown())
                        continue

                    # 2) Market Open Rush strategy (first 45 seconds)
                    elapsed_since_open = time.time() - self._market_start_time
                    if (elapsed_since_open <= self._market_open_window and
                        self._market_open_trades < self._max_rush_trades_per_market):
                        rush_entry = self._check_market_open_rush(prices, elapsed_since_open)
                        if rush_entry:
                            side, price, zone = rush_entry
                            trade = self._enter_trade(side, price, zone, market.slug, "market_open_rush")
                            self._market_open_trades += 1
                            self._print_open(trade, self._get_countdown())
                            continue  # Skip normal entry check

                    # 3) Normal entry check (swing/oracle)
                    entry = self._check_entry(prices, swing_signals)
                    if entry:
                        side, price, zone, signal_type = entry
                        trade = self._enter_trade(side, price, zone, market.slug, signal_type)
                        self._print_open(trade, self._get_countdown())
                    else:
                        # DEBUG: why no entry?
                        elapsed = time.time() - self._market_start_time
                        if elapsed > 5 and elapsed < 8:  # Log only once
                            self._file_log(
                                f"DEBUG | {elapsed:.0f}s elapsed, no entry. "
                                f"Swing signals: {len(swing_signals)}, "
                                f"Oracle block: {self._get_countdown() < self.config.no_trade_last_secs}, "
                                f"Prices: UP={prices.get('up', 0):.3f} DN={prices.get('down', 0):.3f}"
                            )

                self._render_status(prices)
                self._file_log_status(prices)
                await asyncio.sleep(0.4)

        except KeyboardInterrupt:
            self._nl()
            print(f"\n{Colors.YELLOW}Stopped by user.{Colors.RESET}")

        finally:
            if self.open_trade:
                prices_final: Dict[str, float] = {}
                for side in ["up", "down"]:
                    mid = self.market.get_mid_price(side)
                    if mid > 0:
                        prices_final[side] = mid
                t = self.open_trade
                ep = prices_final.get(t.side, t.entry_price)
                self._close_trade(t, ep, "session_end")
                self._print_close(self.trades[-1])

            await self.market.stop()
            self._print_report()
            self._file_log_report()
            self._close_log_file()

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _print_report(self) -> None:
        self._nl()
        closed = [t for t in self.trades if t.status == "closed"]
        wins   = [t for t in closed if t.net_pnl > 0]
        losses = [t for t in closed if t.net_pnl <= 0]

        total_net  = sum(t.net_pnl for t in closed)
        total_fees = sum(t.total_fees for t in closed)
        gross      = sum(t.gross_pnl for t in closed)
        elapsed    = time.time() - self._start_time

        tc_final   = self.trading_capital
        tc_change  = tc_final - self.initial_trading_capital
        tc_mult    = (tc_final / self.initial_trading_capital) if self.initial_trading_capital > 0 else 0
        win_rate   = len(wins) / len(closed) * 100 if closed else 0
        avg_win    = sum(t.net_pnl for t in wins)   / len(wins)   if wins   else 0
        avg_loss   = sum(t.net_pnl for t in losses) / len(losses) if losses else 0

        # Result header message
        if self._stop_reason == "TARGET_HIT":
            header_color = Colors.GREEN
            header_msg   = f"TARGET REACHED! {tc_mult:.2f}x"
        elif self._stop_reason == "FLOOR_PROTECTED":
            header_color = Colors.YELLOW
            header_msg   = "FLOOR PROTECTION ACTIVE — trading stopped"
        else:
            header_color = Colors.CYAN
            header_msg   = f"Session complete ({elapsed/60:.1f} min)"

        prog = self._progress_bar(tc_final, self.initial_trading_capital, self.target_trading_capital, 30)

        print()
        print(SEP72)
        print(f"  {header_color}COMPOUNDER v4 RESULT — {header_msg}{Colors.RESET}")
        print(SEP72)
        print(f"  Run time          : {elapsed/60:.1f} minutes")
        print()
        print(f"  BALANCE:")
        print(f"    Protected base  : ${self.config.protected_base:.2f}  [PRESERVED]")
        c_tc = Colors.GREEN if tc_change >= 0 else Colors.RED
        print(f"    Trading capital : ${self.initial_trading_capital:.2f} -> {c_tc}${tc_final:.2f}{Colors.RESET}  ({c_tc}{tc_change:+.2f}{Colors.RESET})")
        print(f"    Total balance   : ${self.config.balance:.2f} -> ${self.balance:.2f}")
        print(f"    Base protection : {'OK — never risked' if self.balance >= self.config.protected_base else 'VIOLATED (unexpected)'}")
        print()
        print(f"  COMPOUNDING TARGET:")
        print(f"    Target          : ${self.initial_trading_capital:.2f} x {self.config.target_multiplier}x = ${self.target_trading_capital:.2f}")
        print(f"    Achieved        : ${tc_final:.2f}  ({tc_mult:.2f}x)")
        print(f"    Progress        : {prog}")
        print()
        print(f"  ZONE BREAKDOWN:")
        for z in ["LOW", "MID", "HIGH", "PREMIUM"]:
            cnt = self._zone_entries.get(z, 0)
            z_wins = len([t for t in closed if t.zone == z and t.net_pnl > 0])
            z_total = len([t for t in closed if t.zone == z])
            z_wr = (z_wins / z_total * 100) if z_total > 0 else 0
            z_pnl = sum(t.net_pnl for t in closed if t.zone == z)
            c = ZONE_COLORS.get(z, "")
            print(f"    {c}{z:7}{Colors.RESET}: {cnt} entries, {z_total} closed, {z_wr:.0f}% win rate, ${z_pnl:+.3f}")
        print()
        print(f"  TRADE STATISTICS:")
        print(f"    Total trades    : {len(closed)}")
        print(f"    Wins / Losses   : {len(wins)} / {len(losses)}  ({win_rate:.0f}% win rate)")
        print(f"    Avg win         : ${avg_win:+.3f}")
        print(f"    Avg loss        : ${avg_loss:+.3f}")
        print(f"    Total net PnL   : ${total_net:+.3f}  (gross: ${gross:+.3f})")
        print(f"    Fees paid       : ${total_fees:.3f}")
        print(f"    Total signals   : {self._signals_seen}")
        sb = self._signals_by_type.get("swing_bounce", 0)
        sr = self._signals_by_type.get("swing_reject", 0)
        ae = self._signals_by_type.get("always_enter", 0)
        print(f"    Signal breakdown: SB:{sb}  SR:{sr}  AE:{ae}")
        print(f"    Zone blocked    : {self._rejected_zone}  (DEAD)")
        print(f"    Oracle blocked  : {self._rejected_oracle}")
        print(f"    Cooldown blocked: {self._rejected_cooldown}")
        print(f"    Floor blocked   : {self._rejected_floor}")

        # $10 -> $100K roadmap
        print()
        print(f"  $10 -> $100K ROADMAP:")
        if tc_mult > 1 and self.initial_trading_capital > 0:
            avg_growth = tc_mult  # This session's multiplier
            if avg_growth > 1:
                sessions_needed = math.log(100000 / self.config.balance) / math.log(avg_growth * (self.config.balance / (self.config.balance - self.initial_trading_capital + tc_final)))
                sessions_needed = max(1, sessions_needed)
            else:
                sessions_needed = float('inf')
            print(f"    This session    : {tc_mult:.2f}x")
            print(f"    Est. sessions   : ~{sessions_needed:.0f} sessions (at this rate)")
        elif tc_mult < 1 and closed:
            print(f"    This session    : {tc_mult:.2f}x (loss)")
            print(f"    Strategy optimization required")
        else:
            print(f"    Not enough data yet")

        if closed:
            print()
            print(SEP72D)
            print(f"  {'#':>2} {'Side':>5} {'Zone':>7} {'Entry':>6} {'Exit':>6} {'Reason':>10} {'Sec':>4} {'Net':>8} {'%':>7}")
            print(f"  {'-'*68}")
            for t in closed:
                c = Colors.GREEN if t.net_pnl > 0 else Colors.RED
                r = {
                    "take_profit": "TAKE_PFT",
                    "stop_loss":   "STOP_LOS",
                    "force_exit":  "FORCE",
                    "session_end": "SESSION",
                    "settlement":  "SETTLE",
                    "settlement_hedge_profit": "S-HEDGE+",
                    "settlement_hedge_loss": "S-HEDGE-",
                    "manipulation_suspected": "MANIP",
                    "trailing_stop_hit": "TRAIL",
                    "rush_timeout": "R-TIME",
                }.get(t.exit_reason, t.exit_reason)
                zc = ZONE_COLORS.get(t.zone, "")
                print(
                    f"  {t.id:>2} {t.side.upper():>5} "
                    f"{zc}{t.zone:>7}{Colors.RESET} "
                    f"{t.entry_price:>6.3f} {t.exit_price:>6.3f} "
                    f"{r:>10} {t.hold_secs:>4.0f} "
                    f"{c}{t.net_pnl:>+8.3f}{Colors.RESET} "
                    f"{c}{t.return_pct:>+6.1f}%{Colors.RESET}"
                )
            print(SEP72D)

    def _file_log_report(self) -> None:
        """Write end-of-session report to the file log (plain text)."""
        if not self._log_file:
            return

        closed = [t for t in self.trades if t.status == "closed"]
        wins = [t for t in closed if t.net_pnl > 0]
        losses = [t for t in closed if t.net_pnl <= 0]
        total_net = sum(t.net_pnl for t in closed)
        total_fees = sum(t.total_fees for t in closed)
        elapsed = time.time() - self._start_time
        tc_final = self.trading_capital
        tc_change = tc_final - self.initial_trading_capital
        tc_mult = (tc_final / self.initial_trading_capital) if self.initial_trading_capital > 0 else 0
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0

        lines = [
            "=" * 60,
            "REPORT | SESSION END",
            "=" * 60,
            f"Run time: {elapsed/60:.1f} minutes",
            f"Protected base: ${self.config.protected_base:.2f}",
            f"TC: ${self.initial_trading_capital:.2f} -> ${tc_final:.2f} ({tc_change:+.2f}, {tc_mult:.2f}x)",
            f"Balance: ${self.config.balance:.2f} -> ${self.balance:.2f}",
            "",
            f"Total trades: {len(closed)}",
            f"Wins/Losses: {len(wins)}/{len(losses)} ({win_rate:.0f}%)",
            f"Avg win: ${avg_win:+.3f}  Avg loss: ${avg_loss:+.3f}",
            f"Net PnL: ${total_net:+.3f}  Fees: ${total_fees:.3f}",
            "",
            "ZONE BREAKDOWN:",
        ]
        for z in ["LOW", "MID", "HIGH", "PREMIUM"]:
            cnt = self._zone_entries.get(z, 0)
            z_total = len([t for t in closed if t.zone == z])
            z_wins = len([t for t in closed if t.zone == z and t.net_pnl > 0])
            z_wr = (z_wins / z_total * 100) if z_total > 0 else 0
            z_pnl = sum(t.net_pnl for t in closed if t.zone == z)
            lines.append(f"  {z:7}: {cnt} entries, {z_wr:.0f}% win rate, ${z_pnl:+.3f}")

        lines.append("")
        lines.append("SIGNAL BREAKDOWN:")
        for st, cnt in self._signals_by_type.items():
            st_pnl = sum(t.net_pnl for t in closed if t.signal == st)
            st_total = len([t for t in closed if t.signal == st])
            lines.append(f"  {st:15}: {cnt} entries, {st_total} closed, ${st_pnl:+.3f}")

        if closed:
            lines.append("")
            lines.append(f"{'#':>2} {'Side':>5} {'Zone':>7} {'Sig':>3} {'Entry':>6} {'Exit':>6} {'Reason':>10} {'Sec':>4} {'Net':>8} {'%':>7}")
            lines.append("-" * 64)
            for t in closed:
                r = {"take_profit": "TAKE_PFT", "stop_loss": "STOP_LOS", "force_exit": "FORCE",
                     "rush_timeout": "R-TIME", "session_end": "SESSION", "settlement": "SETTLE",
                     "settlement_hedge_profit": "S-HEDGE+", "settlement_hedge_loss": "S-HEDGE-",
                     "manipulation_suspected": "MANIP", "trailing_stop_hit": "TRAIL"}.get(t.exit_reason, t.exit_reason)
                sig = {"swing_bounce": "SB", "swing_reject": "SR", "market_open_rush": "RUSH", "oracle_value": "OV", "always_enter": "AE", "event_position_holding": "HOLD"}.get(t.signal, "??")
                lines.append(
                    f"{t.id:>2} {t.side.upper():>5} {t.zone:>7} {sig:>3} "
                    f"{t.entry_price:>6.3f} {t.exit_price:>6.3f} {r:>10} "
                    f"{t.hold_secs:>4.0f} {t.net_pnl:>+8.3f} {t.return_pct:>+6.1f}%"
                )

        lines.append("=" * 60)

        for line in lines:
            self._file_log(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compounder v4 — Aggressive Entry Scalping Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--balance", type=float, default=12.0,
                        help="Total starting balance (default: 12)")
    parser.add_argument("--protected", type=float, default=10.0,
                        help="Protected base amount — never risked (default: 10)")
    parser.add_argument("--target-mult", type=float, default=1.5, dest="target_mult",
                        help="Compounding target multiplier (1.3=30%% gain, 2.0=double, default: 1.5)")
    parser.add_argument("--min-threshold", type=float, default=0.30, dest="min_threshold",
                        help="Minimum trading capital — stop below this (default: 0.30)")
    parser.add_argument("--trade-size-pct", type=float, default=0.35, dest="trade_size_pct",
                        help="Fallback trade size fraction (default: 0.35 = 35%%)")
    parser.add_argument("--tp", type=float, default=0.10,
                        help="Take profit percentage (default: 0.10 = 10%%)")
    parser.add_argument("--sl", type=float, default=0.05,
                        help="Stop loss percentage (default: 0.05 = 5%%)")
    parser.add_argument("--swing-window", type=int, default=90, dest="swing_window",
                        help="Swing detector window in seconds (default: 90)")
    parser.add_argument("--swing-move", type=float, default=0.008, dest="swing_move",
                        help="Swing minimum move in pp (default: 0.008 = 0.8pp)")
    parser.add_argument("--no-trade-secs", type=int, default=30, dest="no_trade_secs",
                        help="No new entries within this many seconds of settlement (default: 30)")
    parser.add_argument("--force-exit-secs", type=int, default=25, dest="force_exit_secs",
                        help="Force close positions within this many seconds of settlement (default: 25)")
    parser.add_argument("--enter-timeout", type=int, default=120, dest="enter_timeout",
                        help="Force entry if no signal within this many seconds at market open (default: 120)")
    parser.add_argument("--duration", type=int, default=60,
                        help="Maximum session duration in minutes (default: 60)")
    parser.add_argument("--coin", default="BTC", choices=["BTC", "ETH", "SOL", "XRP"],
                        help="Coin symbol (used if --db-prefix is not set, default: BTC)")
    parser.add_argument("--interval", default="5m", choices=["5m", "15m", "30m"],
                        help="Market interval (default: 5m)")
    parser.add_argument("--db-prefix", default="", dest="db_prefix",
                        help="DB slug prefix (e.g. btc-updown-5m) — overrides --coin and --interval")
    parser.add_argument("--log", default="", dest="log_path",
                        help="Log file path ('auto' = auto-named, or a custom path)")

    args = parser.parse_args()

    # User warning
    if args.balance <= args.protected:
        print(f"{Colors.YELLOW}WARNING: balance ({args.balance}) <= protected ({args.protected}){Colors.RESET}")
        print(f"  Trading capital = $0 -> cannot trade.")
        print(f"  Suggestion: --balance {args.protected + 2:.0f}  (starts with ${(args.protected + 2 - args.protected):.0f} trading capital)")
        print()

    config = CompounderConfig(
        balance=args.balance,
        protected_base=args.protected,
        target_multiplier=args.target_mult,
        min_threshold=args.min_threshold,
        trade_size_pct=args.trade_size_pct,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        swing_window_s=args.swing_window,
        swing_min_move=args.swing_move,
        no_trade_last_secs=args.no_trade_secs,
        force_exit_secs=args.force_exit_secs,
        enter_timeout_secs=args.enter_timeout,
        duration_minutes=args.duration,
    )

    # Log file path
    log_path = None
    if args.log_path:
        if args.log_path.lower() == "auto":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = f"logs/compounder_{ts}.log"
        else:
            log_path = args.log_path
        print(f"  Log file: {log_path}")

    # Build selector: use db_prefix if provided, otherwise coin + interval
    if args.db_prefix.strip():
        selector = DbPrefixSelector(args.db_prefix)
    else:
        # CoinIntervalSelector kullan (database gerektirmez)
        selector = CoinIntervalSelector(coin=args.coin, interval=args.interval)

    compounder = OracleSafeCompounder(config=config, selector=selector, coin=args.coin,
                                       log_path=log_path)

    try:
        asyncio.run(compounder.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
