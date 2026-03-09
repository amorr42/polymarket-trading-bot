#!/usr/bin/env python3
"""
Paper Trading Simulator v2 — Zone-Based Oracle-Safe Strategy

Simulates buy/sell trades against live Polymarket data without sending real orders.
Includes the zone system and Kelly criterion position sizing.

Zone System:
  DEAD    (< 0.10): BLOCK — outcome nearly certain
  LOW     (0.10-0.30): BLOCK — low probability range
  MID     (0.30-0.70): NORMAL entry — mean reversion plays
  HIGH    (0.70-0.90): GOOD entry — flash crash opportunity
  PREMIUM (0.90-0.99): BEST entry — hold until settlement

Oracle Protection:
  - Last 45s: no new positions opened
  - Last 30s: open positions are force-closed
  - Last 2min: UP 0.43-0.57 (neutral zone) -> entry blocked

Usage:
    python apps/paper_trader.py --db-prefix btc-updown-5m --balance 100 --duration 30
    python apps/paper_trader.py --coin BTC --balance 100 --duration 30
"""

import math
import os
import sys
import asyncio
import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

from lib import MarketManager, DbPrefixSelector, PriceTracker
from lib.terminal_utils import Colors

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEE_RATE = 0.005
SEP = "=" * 72

# Zone system (shared with compounder)
ZONES = {
    "DEAD":    {"min": 0.00, "max": 0.10, "allowed": False},
    "LOW":     {"min": 0.10, "max": 0.30, "allowed": False},
    "MID":     {"min": 0.30, "max": 0.70, "allowed": True,  "win_rate": 0.55, "payoff": 2.0, "kelly_frac": 1.0},
    "HIGH":    {"min": 0.70, "max": 0.90, "allowed": True,  "win_rate": 0.70, "payoff": 1.5, "kelly_frac": 1.0},
    "PREMIUM": {"min": 0.90, "max": 0.99, "allowed": True,  "win_rate": 0.85, "payoff": 1.2, "kelly_frac": 1.0},
}

ZONE_COLORS = {
    "DEAD": Colors.RED,
    "LOW": Colors.RED,
    "MID": Colors.CYAN,
    "HIGH": Colors.GREEN,
    "PREMIUM": Colors.GREEN + Colors.BOLD,
}


def get_zone(price: float) -> str:
    if price < 0.10: return "DEAD"
    if price < 0.30: return "LOW"
    if price < 0.70: return "MID"
    if price < 0.90: return "HIGH"
    return "PREMIUM"


def kelly_size_pct(zone: str) -> float:
    params = ZONES.get(zone)
    if not params or not params.get("allowed"):
        return 0.0
    w = params["win_rate"]
    r = params["payoff"]
    kelly = (w * r - (1 - w)) / r
    kelly = max(0.0, kelly)
    return 0.50  # Hardcode 50% max position size since we are in degen/high-leverage mode


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SimConfig:
    initial_balance: float = 100.0
    size_pct: float = 0.50           # Default: 50% of balance
    drop_threshold: float = 0.07
    lookback_secs: int = 15
    take_profit_pct: float = 0.20
    stop_loss_pct: float = 0.10
    no_trade_last_secs: int = 45
    force_exit_secs: int = 30
    neutral_zone_lo: float = 0.43
    neutral_zone_hi: float = 0.57
    neutral_block_secs: int = 120
    cooldown_secs: int = 20
    duration_minutes: int = 30
    min_volatility: float = 0.03     # Volatility filter


@dataclass
class SimTrade:
    id: int
    market_slug: str
    side: str
    zone: str
    entry_price: float
    entry_time: float
    shares: float
    cost_usdc: float

    exit_price: float = 0.0
    exit_time: float = 0.0
    exit_reason: str = ""
    gross_pnl: float = 0.0
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    net_pnl: float = 0.0
    status: str = "open"

    @property
    def hold_secs(self) -> float:
        end = self.exit_time if self.status == "closed" else time.time()
        return end - self.entry_time

    @property
    def return_pct(self) -> float:
        if self.cost_usdc <= 0:
            return 0.0
        return self.net_pnl / self.cost_usdc * 100

    @property
    def total_fees(self) -> float:
        return self.entry_fee + self.exit_fee


# ---------------------------------------------------------------------------
# Main simulation class
# ---------------------------------------------------------------------------

class OracleSafePaperTrader:
    def __init__(self, config: SimConfig, selector=None, coin: str = "ETH"):
        self.config = config
        self.balance = config.initial_balance

        if selector:
            self.market = MarketManager(selector=selector)
        else:
            self.market = MarketManager(coin=coin)

        self.prices_tracker = PriceTracker(
            lookback_seconds=config.lookback_secs,
            drop_threshold=config.drop_threshold,
            max_history=500,
        )

        self.trades: List[SimTrade] = []
        self.open_trade: Optional[SimTrade] = None
        self._trade_counter = 0
        self._last_close_time: float = 0.0
        self._start_time: float = 0.0
        self._last_status_time: float = 0.0
        self._on_new_line: bool = True

        # Statistics
        self._signals_seen = 0
        self._rejected_oracle = 0
        self._rejected_neutral = 0
        self._rejected_cooldown = 0
        self._rejected_position = 0
        self._rejected_zone = 0
        self._rejected_low_vol = 0
        self._zone_entries: Dict[str, int] = {"MID": 0, "HIGH": 0, "PREMIUM": 0}

        # Momentum Reversal State
        self._last_closed_trade: Optional[SimTrade] = None
        self._reversal_cooldown_until: float = 0.0

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _check_loss_streak(self, side: str, min_streak: int = 2) -> bool:
        """Check how many consecutive losses exist in the same direction."""
        if len(self.trades) < min_streak:
            return False
        recent = self.trades[-min_streak:]
        return all(t.side == side and t.net_pnl < 0 for t in recent)

    def _get_countdown(self) -> int:
        market = self.market.current_market
        if not market:
            return 9999
        mins, secs = market.get_countdown()
        if mins < 0:
            return 9999
        return mins * 60 + secs

    def _check_entry(self, prices: Dict[str, float]) -> Optional[Tuple[str, float, str]]:
        """Return (side, price, zone) or None."""
        remaining = self._get_countdown()

        if self.open_trade:
            self._rejected_position += 1
            return None

        if remaining < self.config.no_trade_last_secs:
            return None

        # --- MOMENTUM REVERSAL LOGIC (Bypass Cooldown) ---
        # Check before price-drop detection to capture signal quickly.
        if self._last_closed_trade and self._last_closed_trade.exit_reason == "stop_loss":
            now = time.time()
            # If stop loss hit within 45s, immediately reverse direction (if reversal cooldown clear)
            if now - self._last_closed_trade.exit_time <= 45 and now > self._reversal_cooldown_until:
                opposite = "down" if self._last_closed_trade.side == "up" else "up"
                opp_price = prices.get(opposite, 0)

                # Is there a valid trading zone on the opposite side?
                zone = get_zone(opp_price)
                if ZONES[zone].get("allowed"):
                    # Don't persist in the same direction if already on a loss streak
                    if not self._check_loss_streak(opposite, 2):
                        self._last_closed_trade = None  # Consume the signal
                        return (opposite, opp_price, zone)

        if self._last_close_time > 0:
            cooldown_to_use = self.config.cooldown_secs
            
            # Short cooldown: if we closed with take profit, shorten cooldown to re-enter the trend quickly (5s)
            if self._last_closed_trade and self._last_closed_trade.exit_reason == "take_profit":
                cooldown_to_use = min(5, self.config.cooldown_secs)
                
            if time.time() - self._last_close_time < cooldown_to_use:
                self._rejected_cooldown += 1
                return None

        event = self.prices_tracker.detect_flash_crash()
        if not event:
            return None

        self._signals_seen += 1
        current = prices.get(event.side, 0)
        if current <= 0 or current >= 0.99:
            return None

        # Zone check
        zone = get_zone(current)
        if not ZONES[zone].get("allowed"):
            self._rejected_zone += 1
            return None

        # Volatility filter
        vol = self.prices_tracker.get_volatility(event.side, 30)
        if vol < self.config.min_volatility:
            self._rejected_low_vol += 1
            return None

        # Neutral zone filter
        if remaining < self.config.neutral_block_secs:
            lo = self.config.neutral_zone_lo
            hi = self.config.neutral_zone_hi
            if lo <= current <= hi:
                self._rejected_neutral += 1
                return None

        return (event.side, current, zone)

    def _enter_trade(self, side: str, price: float, zone: str, market_slug: str) -> SimTrade:
        self._trade_counter += 1

        # Kelly-based position sizing
        size_pct = kelly_size_pct(zone)
        if size_pct <= 0:
            size_pct = self.config.size_pct

        raw_size = self.balance * size_pct
        entry_fee = raw_size * FEE_RATE
        cost = raw_size + entry_fee
        shares = raw_size / price

        trade = SimTrade(
            id=self._trade_counter,
            market_slug=market_slug,
            side=side,
            zone=zone,
            entry_price=price,
            entry_time=time.time(),
            shares=shares,
            cost_usdc=cost,
            entry_fee=entry_fee,
        )
        self.balance -= cost
        self.open_trade = trade
        self.trades.append(trade)
        self.prices_tracker.clear(side)

        if zone in self._zone_entries:
            self._zone_entries[zone] += 1

        return trade

    def _close_trade(self, trade: SimTrade, exit_price: float, reason: str) -> float:
        gross = (exit_price - trade.entry_price) * trade.shares
        exit_fee = (exit_price * trade.shares) * FEE_RATE
        net = gross - exit_fee

        trade.exit_price = exit_price
        trade.exit_time = time.time()
        trade.exit_reason = reason
        trade.gross_pnl = gross
        trade.exit_fee = exit_fee
        trade.net_pnl = net
        trade.status = "closed"

        proceeds = max(exit_price * trade.shares - exit_fee, 0.0)
        self.balance += proceeds

        self.open_trade = None
        self._last_close_time = time.time()
        self._last_closed_trade = trade

        # Revenge-trading death spiral protection
        if reason == "stop_loss" and self._check_loss_streak(trade.side, 2):
            self._reversal_cooldown_until = time.time() + 120  # wait 2 minutes
            
        return net

    def _check_exits(self, prices: Dict[str, float]) -> Optional[str]:
        if not self.open_trade:
            return None

        trade = self.open_trade
        current = prices.get(trade.side, 0)
        remaining = self._get_countdown()

        if current <= 0:
            return None

        # Force close
        if remaining <= self.config.force_exit_secs:
            return "force_exit"

        initial_val = trade.entry_price * trade.shares
        current_val = current * trade.shares
        if initial_val <= 0:
            return None

        pnl_pct = (current_val - initial_val) / initial_val
        zone = trade.zone

        if zone == "PREMIUM":
            # Hold until settlement — SL only
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            return None

        elif zone == "HIGH":
            # If in profit during last 90s, wait for settlement
            if remaining < 90 and pnl_pct > 0:
                return None
            if pnl_pct >= self.config.take_profit_pct:
                return "take_profit"
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            return None

        else:  # MID
            if pnl_pct >= self.config.take_profit_pct:
                return "take_profit"
            if pnl_pct <= -self.config.stop_loss_pct:
                return "stop_loss"
            return None

    # ------------------------------------------------------------------
    # Terminal output helpers
    # ------------------------------------------------------------------

    def _newline_if_needed(self) -> None:
        if not self._on_new_line:
            print()
            self._on_new_line = True

    def _zone_tag(self, zone: str) -> str:
        c = ZONE_COLORS.get(zone, "")
        return f"{c}{zone:7}{Colors.RESET}"

    def _print_trade_open(self, trade: SimTrade, remaining: int) -> None:
        self._newline_if_needed()
        ts = datetime.now().strftime("%H:%M:%S")
        kelly_pct = kelly_size_pct(trade.zone) * 100
        print(
            f"[{ts}] {Colors.CYAN}ENTRY  {trade.side.upper():4} #{trade.id:3}{Colors.RESET} "
            f"{self._zone_tag(trade.zone)} | "
            f"Price: {trade.entry_price:.3f} | "
            f"Shares: {trade.shares:.1f} | "
            f"Cost: ${trade.cost_usdc:.2f} (Kelly:{kelly_pct:.0f}%) | "
            f"T-{remaining}s | "
            f"{trade.market_slug[-15:]}"
        )

    def _print_trade_close(self, trade: SimTrade) -> None:
        self._newline_if_needed()
        ts = datetime.now().strftime("%H:%M:%S")
        color = Colors.GREEN if trade.net_pnl >= 0 else Colors.RED
        reason_display = {
            "take_profit": "TAKE_PROFIT",
            "stop_loss":   "STOP_LOSS  ",
            "force_exit":  "FORCE_EXIT ",
            "session_end": "SESSION_END",
        }.get(trade.exit_reason, trade.exit_reason[:11])

        print(
            f"[{ts}] {color}EXIT   {trade.side.upper():4} #{trade.id:3}{Colors.RESET} "
            f"{self._zone_tag(trade.zone)} | "
            f"{trade.entry_price:.3f} -> {trade.exit_price:.3f} | "
            f"{reason_display} | "
            f"Net: {color}${trade.net_pnl:+.3f}{Colors.RESET} | "
            f"Return: {color}{trade.return_pct:+.1f}%{Colors.RESET} | "
            f"Balance: ${self.balance:.2f}"
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
        closed_pnl = sum(t.net_pnl for t in self.trades if t.status == "closed")

        up_zone = get_zone(up) if up > 0 else "?"
        dn_zone = get_zone(dn) if dn > 0 else "?"

        pos_str = ""
        if self.open_trade:
            t = self.open_trade
            cur = prices.get(t.side, t.entry_price)
            unrealized = (cur - t.entry_price) * t.shares
            c = Colors.GREEN if unrealized >= 0 else Colors.RED
            pos_str = (
                f" | {c}POS {t.side.upper()}({t.zone[:3]}) "
                f"${unrealized:+.3f}{Colors.RESET}"
            )

        line = (
            f"  UP={up:.3f}[{up_zone[:3]}] DN={dn:.3f}[{dn_zone[:3]}] "
            f"T-{remaining:4}s | "
            f"Balance=${self.balance:.2f} | "
            f"PnL=${closed_pnl:+.2f} | "
            f"Elapsed={elapsed/60:.1f}m"
            f"{pos_str}   "
        )

        print(f"\r{line}", end="", flush=True)
        self._on_new_line = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._start_time = time.time()
        end_time = self._start_time + self.config.duration_minutes * 60

        @self.market.on_book_update
        async def _on_book(snap):
            # TODO: add per-snapshot orderbook analysis if needed
            pass

        @self.market.on_market_change
        def _on_market_change(old_slug: str, new_slug: str):
            self._newline_if_needed()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{Colors.YELLOW}MARKET CHANGED{Colors.RESET}: "
                f"{old_slug[-15:]} -> {new_slug[-15:]}"
            )
            self.prices_tracker.clear()
            self._on_new_line = True

        if not await self.market.start():
            print(f"{Colors.RED}Market failed to start: {self.market.last_error}{Colors.RESET}")
            return

        await self.market.wait_for_data(timeout=10.0)

        self._newline_if_needed()
        print(SEP)
        print(f"  ORACLE-SAFE PAPER TRADER v2 — Zone-Based Simulation")
        print(SEP)
        print(f"  Initial balance    : ${self.config.initial_balance:.2f}")
        print(f"  Simulation duration: {self.config.duration_minutes} minutes")
        print()
        print(f"  Zone System:")
        print(f"    DEAD    (< 0.10) : BLOCK")
        print(f"    LOW  (0.10-0.30) : BLOCK")
        print(f"    MID  (0.30-0.70) : NORMAL  (Size: 50% fixed)")
        print(f"    HIGH (0.70-0.90) : GOOD    (Size: 50% fixed)")
        print(f"    PREMIUM (> 0.90) : BEST    (Size: 50% fixed, hold for settlement)")
        print()
        print(f"  Entry threshold    : {self.config.drop_threshold*100:.0f}% drop / {self.config.lookback_secs}s")
        print(f"  Take profit / SL   : +{self.config.take_profit_pct*100:.0f}% / -{self.config.stop_loss_pct*100:.0f}%")
        print(f"  Oracle protection  : No new entry in last {self.config.no_trade_last_secs}s")
        print(f"  Force close        : Last {self.config.force_exit_secs}s")
        print(f"  Min volatility     : {self.config.min_volatility:.2f}")
        print(f"  Neutral zone       : {self.config.neutral_zone_lo:.2f}-{self.config.neutral_zone_hi:.2f} (last {self.config.neutral_block_secs}s)")
        print(f"  Cooldown           : {self.config.cooldown_secs}s")
        print(f"  Fee rate           : {FEE_RATE*100:.1f}% (taker, each leg)")
        print(SEP)
        print()

        try:
            while time.time() < end_time:
                market = self.market.current_market
                if not market:
                    await asyncio.sleep(0.3)
                    continue

                prices: Dict[str, float] = {}
                for side in ["up", "down"]:
                    mid = self.market.get_mid_price(side)
                    if mid > 0:
                        prices[side] = mid
                        self.prices_tracker.record(side, mid)

                if len(prices) < 2:
                    await asyncio.sleep(0.3)
                    continue

                reason = self._check_exits(prices)
                if reason and self.open_trade:
                    t = self.open_trade
                    exit_price = prices.get(t.side, t.entry_price)
                    self._close_trade(t, exit_price, reason)
                    self._print_trade_close(self.trades[-1])

                if not self.open_trade:
                    entry = self._check_entry(prices)
                    if entry:
                        side, price, zone = entry
                        trade = self._enter_trade(side, price, zone, market.slug)
                        self._print_trade_open(trade, self._get_countdown())

                self._render_status(prices)
                await asyncio.sleep(0.4)

        except KeyboardInterrupt:
            self._newline_if_needed()
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
                self._print_trade_close(self.trades[-1])

            await self.market.stop()
            self._print_report()

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _print_report(self) -> None:
        self._newline_if_needed()
        closed = [t for t in self.trades if t.status == "closed"]
        wins = [t for t in closed if t.net_pnl > 0]
        losses = [t for t in closed if t.net_pnl <= 0]
        total_pnl = sum(t.net_pnl for t in closed)
        total_fees = sum(t.total_fees for t in closed)
        gross_pnl = sum(t.gross_pnl for t in closed)
        elapsed = time.time() - self._start_time

        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
        max_win = max((t.net_pnl for t in closed), default=0)
        max_loss = min((t.net_pnl for t in closed), default=0)

        print()
        print(SEP)
        print(f"  SIMULATION RESULT — v2 Zone-Based")
        print(SEP)
        print(f"  Run time           : {elapsed/60:.1f} minutes")
        print(f"  Initial balance    : ${self.config.initial_balance:.2f}")
        print(f"  Final balance      : ${self.balance:.2f}")
        clr = Colors.GREEN if total_pnl >= 0 else Colors.RED
        print(f"  Total net PnL      : {clr}${total_pnl:+.2f}{Colors.RESET}  (gross: ${gross_pnl:+.2f})")
        print(f"  ROI                : {clr}{total_pnl/self.config.initial_balance*100:+.2f}%{Colors.RESET}")
        print(f"  Fees paid          : ${total_fees:.3f}")
        print()
        print(f"  ZONE BREAKDOWN:")
        for z in ["MID", "HIGH", "PREMIUM"]:
            cnt = self._zone_entries.get(z, 0)
            z_wins = len([t for t in closed if t.zone == z and t.net_pnl > 0])
            z_total = len([t for t in closed if t.zone == z])
            z_wr = (z_wins / z_total * 100) if z_total > 0 else 0
            z_pnl = sum(t.net_pnl for t in closed if t.zone == z)
            zc = ZONE_COLORS.get(z, "")
            print(f"    {zc}{z:7}{Colors.RESET}: {cnt} entries, {z_total} closed, {z_wr:.0f}% win rate, ${z_pnl:+.3f}")
        print()
        print(f"  Total trades       : {len(closed)}")
        print(f"  Wins / Losses      : {len(wins)} / {len(losses)}  ({win_rate:.0f}% win rate)")
        print(f"  Avg win            : ${avg_win:+.3f}")
        print(f"  Avg loss           : ${avg_loss:+.3f}")
        print(f"  Largest win        : ${max_win:+.3f}")
        print(f"  Largest loss       : ${max_loss:+.3f}")
        print()
        print(f"  Total signals      : {self._signals_seen}")
        print(f"  Zone blocked       : {self._rejected_zone}  (DEAD/LOW)")
        print(f"  Volatility blocked : {self._rejected_low_vol}  (< {self.config.min_volatility:.2f})")
        print(f"  Oracle blocked     : {self._rejected_oracle}  (last {self.config.no_trade_last_secs}s rule)")
        print(f"  Neutral zone block : {self._rejected_neutral}  (price {self.config.neutral_zone_lo:.2f}-{self.config.neutral_zone_hi:.2f})")
        print(f"  Cooldown blocked   : {self._rejected_cooldown}  ({self.config.cooldown_secs}s wait)")
        print()

        if closed:
            print(SEP)
            print(
                f"  {'#':>3} {'Side':>5} {'Zone':>7} {'Entry':>6} {'Exit':>6} "
                f"{'Reason':>12} {'Sec':>5} {'Net PnL':>8} {'Return':>8}"
            )
            print(f"  {'-'*72}")
            for t in closed:
                c = Colors.GREEN if t.net_pnl > 0 else Colors.RED
                reason_d = {
                    "take_profit": "TAKE_PROFIT",
                    "stop_loss":   "STOP_LOSS",
                    "force_exit":  "FORCE_EXIT",
                    "session_end": "SESSION_END",
                }.get(t.exit_reason, t.exit_reason)
                zc = ZONE_COLORS.get(t.zone, "")
                print(
                    f"  {t.id:>3} {t.side.upper():>5} "
                    f"{zc}{t.zone:>7}{Colors.RESET} "
                    f"{t.entry_price:>6.3f} {t.exit_price:>6.3f} "
                    f"{reason_d:>12} "
                    f"{t.hold_secs:>5.0f} "
                    f"{c}{t.net_pnl:>+8.3f}{Colors.RESET} "
                    f"{c}{t.return_pct:>+7.1f}%{Colors.RESET}"
                )
            print(SEP)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Oracle-Safe Paper Trading Simulator v2 — Zone-Based",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--coin", default="BTC", choices=["BTC", "ETH", "SOL", "XRP"],
                        help="Coin symbol (via Gamma API, used if --db-prefix is not set)")
    parser.add_argument("--db-prefix", default="btc-updown-5m", dest="db_prefix",
                        help="DB slug prefix (e.g. btc-updown-5m). Overrides --coin.")
    parser.add_argument("--balance", type=float, default=100.0, help="Starting balance in USD (default: 100)")
    parser.add_argument("--duration", type=int, default=30, help="Simulation duration in minutes (default: 30)")
    parser.add_argument("--size-pct", type=float, default=0.50, dest="size_pct",
                        help="Fallback position size as fraction of balance (default: 0.50 = 50%%)")
    parser.add_argument("--drop", type=float, default=0.07,
                        help="Entry trigger drop threshold (default: 0.07 = 7%%)")
    parser.add_argument("--lookback", type=int, default=15,
                        help="Drop detection window in seconds (default: 15)")
    parser.add_argument("--tp", type=float, default=0.20,
                        help="Take profit percentage (default: 0.20 = 20%%)")
    parser.add_argument("--sl", type=float, default=0.10,
                        help="Stop loss percentage (default: 0.10 = 10%%)")
    parser.add_argument("--no-trade-secs", type=int, default=45, dest="no_trade_secs",
                        help="No new entries within this many seconds of settlement (default: 45)")
    parser.add_argument("--force-exit-secs", type=int, default=30, dest="force_exit_secs",
                        help="Force close positions within this many seconds of settlement (default: 30)")
    parser.add_argument("--min-vol", type=float, default=0.03, dest="min_vol",
                        help="Minimum volatility threshold (default: 0.03)")

    args = parser.parse_args()

    config = SimConfig(
        initial_balance=args.balance,
        size_pct=args.size_pct,
        drop_threshold=args.drop,
        lookback_secs=args.lookback,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        no_trade_last_secs=args.no_trade_secs,
        force_exit_secs=args.force_exit_secs,
        duration_minutes=args.duration,
        min_volatility=args.min_vol,
    )

    selector = DbPrefixSelector(args.db_prefix) if args.db_prefix.strip() else None
    trader = OracleSafePaperTrader(config=config, selector=selector, coin=args.coin)

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
