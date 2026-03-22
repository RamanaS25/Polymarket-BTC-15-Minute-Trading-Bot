import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import math
import random

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

try:
    from patch_gamma_markets import apply_gamma_markets_patch, verify_patch
    patch_applied = apply_gamma_markets_patch()
    if patch_applied:
        verify_patch()
    else:
        print("ERROR: Failed to apply gamma_market patch")
        sys.exit(1)
except ImportError as e:
    print(f"ERROR: Could not import patch module: {e}")
    print("Make sure patch_gamma_markets.py is in the same directory")
    sys.exit(1)

from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.adapters.polymarket.factories import (
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.model.data import QuoteTick

from dotenv import load_dotenv
from loguru import logger
import logging

from bot import (
    IntegratedBTCStrategy,
    PaperTrade,
    init_redis,
    QUOTE_STABILITY_REQUIRED,
    MARKET_INTERVAL_SECONDS,
)
from patch_market_orders import apply_market_order_patch

load_dotenv()
patch_applied = apply_market_order_patch()
if patch_applied:
    logger.info("Market order patch applied successfully")
else:
    logger.warning("Market order patch failed - orders may be rejected")


# === SUPPRESS NOISY "Dropping QuoteTick" WARNINGS ===
class DropQuoteTickFilter(logging.Filter):
    """Filter to suppress repetitive 'Dropping QuoteTick' warnings."""

    def __init__(self):
        super().__init__()
        self._drop_count = 0
        self._last_log_time = 0
        self._log_interval = 60  # Log summary every 60 seconds

    def filter(self, record: logging.LogRecord) -> bool:
        # Check if this is a "Dropping QuoteTick" warning
        if "Dropping QuoteTick" in str(record.msg):
            self._drop_count += 1
            current_time = time.time()

            # Log a summary periodically instead of every warning
            if current_time - self._last_log_time >= self._log_interval:
                if self._drop_count > 1:
                    # Replace the message with a summary
                    record.msg = f"Suppressed {self._drop_count} 'Dropping QuoteTick' warnings (illiquid quotes) in the last {self._log_interval}s"
                    self._drop_count = 0
                    self._last_log_time = current_time
                    return True
                self._last_log_time = current_time
                return True  # Let the first one through

            # Suppress all other occurrences
            return False

        return True  # Let all other log messages through


# Apply the filter to the root logger to catch Nautilus warnings
_drop_filter = DropQuoteTickFilter()
logging.getLogger().addFilter(_drop_filter)


class FiveSecondBTCStrategy(IntegratedBTCStrategy):
    def __init__(self, *args, force_simulation: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_fast_trade_ts = None
        self.learning_enabled = True
        self.learning_every_trades = 20
        self.five_sec_mode = True
        self._market_load_attempted = False
        self._spinner_stop = False
        self._spinner_thread = None
        self._force_simulation = force_simulation

        # === AGGRESSIVE MODE: HIGH VOLUME LEARNING ===
        # Prioritize trade volume and learning over caution
        self.AGGRESSIVE_MODE = True  # Enable aggressive trading

        # Price ceiling/floor - WIDENED for more trades
        self.PRICE_CEILING = 0.95  # Allow LONG almost anywhere
        self.PRICE_FLOOR = 0.05    # Allow SHORT almost anywhere

        # Momentum burst detection
        self._burst_trades_count = 0
        self._burst_direction = None
        self._burst_start_price = None
        self.MAX_BURST_TRADES = 10  # Allow many burst trades

        # Consecutive loss protection - DISABLED for learning
        self._consecutive_losses = {"long": 0, "short": 0}
        self._cooldown_start = {"long": None, "short": None}
        self.MAX_CONSECUTIVE_LOSSES = 100  # Effectively disabled
        self.COOLDOWN_SECONDS = 5  # Very short cooldown

        # Directional performance tracking (rolling window)
        self._direction_results = {"long": [], "short": []}
        self.DIRECTION_WINDOW = 10  # Track last 10 trades per direction

        # Fast momentum detection
        self._price_velocity = []  # Track price changes for burst detection
        self.BURST_THRESHOLD = 0.02  # 2% move triggers burst mode

        # Dual token subscription (YES and NO) for liquidity fallback
        self._subscribed_no_id = None
        self._active_token_id = None
        self._illiquid_count = 0

        # === CACHED MARKET DATA (refreshed in background) ===
        self._cached_fear_greed: Optional[float] = None
        self._cached_fear_greed_class: Optional[str] = None
        self._cached_btc_spot: Optional[float] = None
        self._cache_last_refresh: Optional[datetime] = None
        self._cache_refresh_interval = 60  # Refresh every 60 seconds
        self._cache_refresh_task = None

        # === NEW: SPREAD TRACKING (LOOSENED FOR VOLUME) ===
        self._spread_history: list = []  # Track recent spreads
        self.MAX_SPREAD_THRESHOLD = 0.15  # Allow up to 15% spread for learning
        self.SPREAD_WIDENING_THRESHOLD = 5.0  # Very tolerant of spread widening
        self._avg_spread: Optional[float] = None

        # === NEW: TIME-TO-CLOSE TRACKING (LOOSENED) ===
        self.MARKET_DURATION_SECONDS = 15 * 60  # 15-minute markets
        self.EARLY_WINDOW_SECONDS = 30  # Only first 30 sec is "early"
        self.LATE_WINDOW_SECONDS = 30   # Only last 30 sec is "late"
        self.AVOID_LAST_SECONDS = 10    # Only avoid final 10 seconds

        # === NEW: BTC SPOT MOMENTUM ===
        self._btc_spot_history: list = []  # Track BTC price changes
        self.BTC_MOMENTUM_WINDOW = 5  # Track last 5 readings (5 minutes of data)
        self._btc_momentum: Optional[float] = None  # % change in BTC spot

        # === NEW: FILL/SLIPPAGE TRACKING ===
        self._slippage_history: list = []  # Track slippage on fills
        self.MAX_SLIPPAGE_BPS = 50  # Pause trading if avg slippage > 50 bps
        self._avg_slippage_bps: Optional[float] = None
        self._recent_fill_rate: float = 1.0  # Ratio of filled orders

        # === NEW: VOLUME/TRADE FLOW DETECTION ===
        self._quote_volume_history: list = []  # Track quote tick frequency
        self._volume_spike_threshold = 3.0  # 3x normal volume = spike
        self._last_volume_check = None
        self._is_volume_spike: bool = False

        # === STOP LOSS / TAKE PROFIT ===
        self.STOP_LOSS_PCT = 0.05  # 5% stop loss (exit if price moves 5% against us)
        self.TAKE_PROFIT_PCT = 0.08  # 8% take profit (exit if price moves 8% in our favor)
        self.TRAILING_STOP_PCT = 0.03  # 3% trailing stop (lock in profits)
        self.USE_TRAILING_STOP = True  # Enable trailing stop after hitting 5% profit

        # Open position tracking (for live trades)
        self._open_positions: dict = {}  # {order_id: position_data}

        # === PAPER TRADE TRACKING (for real P&L calculation) ===
        # Instead of simulating, we track entry and check actual price later
        self._pending_paper_trades: dict = {}  # {trade_id: trade_data}
        self.PAPER_TRADE_HOLD_SECONDS = 10  # Check P&L after 10 seconds

        # === MAKER ORDER STRATEGY (0% fees!) ===
        self.USE_MAKER_ORDERS = True  # Use limit orders instead of market orders
        self.MAKER_OFFSET_PCT = 0.005  # Place limit 0.5% better than mid price
        self.MAKER_TIMEOUT_SECONDS = 30  # Cancel unfilled orders after 30s

        # === LOSS ANALYSIS TRACKING ===
        # Track patterns in losses to improve strategy
        self._loss_reasons: dict = {}  # {reason: count}
        self._loss_history: list = []  # Recent losses with full context
        self.MAX_LOSS_HISTORY = 50  # Keep last 50 losses for analysis

        # === PRICE INEFFICIENCY DETECTION ===
        # Fair value estimation based on BTC spot momentum
        self._fair_value_estimate: Optional[float] = None
        self.INEFFICIENCY_THRESHOLD = 0.03  # 3% mispricing = strong edge
        self.MIN_INEFFICIENCY = 0.01  # 1% mispricing = minimum edge

        # === ADAPTIVE FEEDBACK SYSTEM (LEARNING MODE) ===
        # In aggressive mode: LEARN but don't restrict trading
        self._adaptive_enabled = True
        self._adaptation_window = 20  # Analyze last 20 trades

        # Direction preference (learned from performance)
        # In aggressive mode: track but don't skip directions
        self._direction_preference: Optional[str] = None
        self._direction_edge: float = 0.0
        self.MIN_DIRECTION_EDGE = 0.30  # High bar - only note strong preferences
        self.SKIP_WEAK_DIRECTION = False  # Don't skip - learn from both directions

        # Adaptive thresholds (start loose, tighten based on data)
        self._adaptive_spread_threshold = self.MAX_SPREAD_THRESHOLD
        self._adaptive_momentum_threshold = 0.001  # Very low - almost any momentum
        self._adaptive_btc_alignment_required = False

        # Strategy performance tracking (don't disable - learn from all)
        self._strategy_stats = {
            "inefficiency": {"wins": 0, "losses": 0, "enabled": True},
            "burst": {"wins": 0, "losses": 0, "enabled": True},
            "mean_reversion": {"wins": 0, "losses": 0, "enabled": True},
        }
        self._last_trade_strategy: Optional[str] = None
        self.DISABLE_LOSING_STRATEGIES = False  # Keep all strategies active for learning

        # Condition-specific performance
        self._condition_performance = {
            "early_phase": {"wins": 0, "losses": 0},
            "mid_phase": {"wins": 0, "losses": 0},
            "late_phase": {"wins": 0, "losses": 0},
            "btc_aligned": {"wins": 0, "losses": 0},
            "btc_misaligned": {"wins": 0, "losses": 0},
            "wide_spread": {"wins": 0, "losses": 0},
            "normal_spread": {"wins": 0, "losses": 0},
            "volume_spike": {"wins": 0, "losses": 0},
            "normal_volume": {"wins": 0, "losses": 0},
        }
        # position_data = {
        #     "direction": "long" or "short",
        #     "entry_price": float,
        #     "quantity": float,
        #     "entry_time": datetime,
        #     "instrument_id": str,
        #     "stop_loss_price": float,
        #     "take_profit_price": float,
        #     "trailing_stop_price": float (optional),
        #     "highest_price": float (for trailing stop),
        #     "lowest_price": float (for trailing stop),
        # }

    def on_start(self):
        super().on_start()
        # In fast mode, cache instruments may not be ready yet. Retry loading.
        if not self.all_btc_instruments:
            try:
                self.run_in_executor(self._await_and_load_markets)
            except Exception as e:
                logger.warning(f"Failed to schedule market load retry: {e}")

        # Start background cache refresh for Fear & Greed + BTC spot
        try:
            self.run_in_executor(self._start_cache_refresh_loop)
        except Exception as e:
            logger.warning(f"Failed to start cache refresh: {e}")

    def _start_cache_refresh_loop(self):
        """Background loop to refresh Fear & Greed and BTC spot price."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._cache_refresh_loop())
        except Exception as e:
            logger.warning(f"Cache refresh loop error: {e}")
        finally:
            loop.close()

    async def _cache_refresh_loop(self):
        """Async loop that refreshes cached market data every 60 seconds."""
        logger.info("Starting background cache refresh for Fear & Greed + BTC spot")
        while True:
            try:
                await self._refresh_market_cache()
            except Exception as e:
                logger.warning(f"Cache refresh failed: {e}")
            await asyncio.sleep(self._cache_refresh_interval)

    async def _refresh_market_cache(self):
        """Fetch Fear & Greed and BTC spot price, store in cache."""
        # Fetch Fear & Greed Index
        try:
            from data_sources.news_social.adapter import NewsSocialDataSource
            news_source = NewsSocialDataSource()
            await news_source.connect()
            fg = await news_source.get_fear_greed_index()
            await news_source.disconnect()
            if fg and "value" in fg:
                self._cached_fear_greed = float(fg["value"])
                self._cached_fear_greed_class = fg.get("classification", "")
                logger.debug(f"Cache updated: Fear & Greed = {self._cached_fear_greed:.0f} ({self._cached_fear_greed_class})")
        except Exception as e:
            logger.debug(f"Fear & Greed fetch failed: {e}")

        # Fetch BTC spot price from Coinbase
        try:
            from data_sources.coinbase.adapter import CoinbaseDataSource
            coinbase = CoinbaseDataSource()
            await coinbase.connect()
            spot = await coinbase.get_current_price()
            await coinbase.disconnect()
            if spot:
                new_spot = float(spot)
                # Track BTC spot history for momentum calculation
                self._btc_spot_history.append(new_spot)
                if len(self._btc_spot_history) > self.BTC_MOMENTUM_WINDOW:
                    self._btc_spot_history.pop(0)
                # Calculate BTC momentum
                if len(self._btc_spot_history) >= 2:
                    self._btc_momentum = (new_spot - self._btc_spot_history[0]) / self._btc_spot_history[0]
                self._cached_btc_spot = new_spot
                logger.debug(f"Cache updated: BTC spot = ${self._cached_btc_spot:,.2f}")
        except Exception as e:
            logger.debug(f"BTC spot fetch failed: {e}")

        self._cache_last_refresh = datetime.now(timezone.utc)
        if self._cached_fear_greed or self._cached_btc_spot:
            logger.info(
                f"Market cache refreshed: F&G={self._cached_fear_greed or 'N/A'}, "
                f"BTC=${self._cached_btc_spot:,.2f}" if self._cached_btc_spot else f"BTC=N/A"
            )

    def _await_and_load_markets(self):
        import time
        self._start_spinner("Waiting for markets")
        for attempt in range(30):  # up to ~60s
            try:
                instruments = self.cache.instruments()
                if attempt % 5 == 0:  # Log every 10 seconds
                    self._stop_spinner()
                    logger.info(f"Checking cache... found {len(instruments)} instruments (attempt {attempt+1}/30)")
                    self._start_spinner("Waiting for markets")

                if instruments:
                    self._stop_spinner()
                    logger.info(f"Found {len(instruments)} instruments in cache, loading BTC markets...")
                    self._load_all_btc_instruments()

                    if self.instrument_id:
                        # Subscribe to YES token
                        self.subscribe_quote_ticks(self.instrument_id)
                        logger.info(f"✓ SUBSCRIBED to YES token: {self.instrument_id}")

                        # Also subscribe to NO token if available (for liquidity fallback)
                        if self.current_instrument_index >= 0:
                            current_market = self.all_btc_instruments[self.current_instrument_index]
                            no_id = current_market.get('no_instrument_id')
                            if no_id:
                                self.subscribe_quote_ticks(no_id)
                                logger.info(f"✓ SUBSCRIBED to NO token: {no_id}")
                                self._subscribed_no_id = no_id
                    else:
                        logger.warning("No BTC 15m instrument_id after loading - check market availability")
                    return
            except Exception as e:
                logger.warning(f"Error checking cache: {e}")
            time.sleep(2)
        self._stop_spinner()
        logger.warning("No instruments available after 60s of retries; fast mode will stay idle")
        logger.warning("This usually means the Gamma API returned no markets matching the slug filters")

    def _start_spinner(self, label: str):
        if self._spinner_thread:
            return
        self._spinner_stop = False

        def _spin():
            frames = ["|", "/", "-", "\\"]
            i = 0
            while not self._spinner_stop:
                frame = frames[i % len(frames)]
                print(f"\r{label} {frame}", end="", flush=True)
                i += 1
                time.sleep(0.2)
            print("\r" + " " * (len(label) + 2) + "\r", end="", flush=True)

        self._spinner_thread = threading.Thread(target=_spin, daemon=True)
        self._spinner_thread.start()

    def _stop_spinner(self):
        if not self._spinner_thread:
            return
        self._spinner_stop = True
        self._spinner_thread = None

    async def check_simulation_mode(self) -> bool:
        """Respect Redis unless force_simulation is enabled."""
        if self._force_simulation:
            return True
        return await super().check_simulation_mode()

    def on_quote_tick(self, tick: QuoteTick):
        """Trade every 5 seconds while the market is open."""
        try:
            # Accept quotes from YES token, NO token, or subscribed NO token
            subscribed_no = getattr(self, '_subscribed_no_id', None)
            valid_ids = [self.instrument_id]
            if subscribed_no:
                valid_ids.append(subscribed_no)

            if self.instrument_id is None or tick.instrument_id not in valid_ids:
                return

            now = datetime.now(timezone.utc)
            bid = tick.bid_price
            ask = tick.ask_price

            # Skip quotes with missing bid or ask (no liquidity on one side)
            if bid is None or ask is None:
                # Track which token is illiquid (for debugging, not spamming logs)
                if not hasattr(self, '_illiquid_count'):
                    self._illiquid_count = 0
                self._illiquid_count += 1
                # Only log every 500 occurrences
                if self._illiquid_count % 500 == 1:
                    logger.warning(f"Waiting for liquidity... ({self._illiquid_count} illiquid quotes so far)")
                return

            try:
                bid_decimal = bid.as_decimal()
                ask_decimal = ask.as_decimal()
            except Exception:
                return

            # Track which token has liquidity (for smarter trading)
            self._active_token_id = tick.instrument_id
            is_no_token = (subscribed_no and tick.instrument_id == subscribed_no)

            # For NO token, price is inverted (NO price = 1 - YES price)
            if is_no_token:
                mid_price = Decimal("1.0") - (bid_decimal + ask_decimal) / 2
            else:
                mid_price = (bid_decimal + ask_decimal) / 2

            self.price_history.append(mid_price)
            if len(self.price_history) > self.max_history:
                self.price_history.pop(0)

            self._last_bid_ask = (bid_decimal, ask_decimal)
            self._tick_buffer.append({'ts': now, 'price': mid_price})

            # === CHECK STOP LOSS / TAKE PROFIT FOR OPEN POSITIONS ===
            if self._open_positions:
                self._check_stop_loss_take_profit(float(mid_price), float(bid_decimal), float(ask_decimal))

            # === CHECK PENDING PAPER TRADES (real P&L tracking) ===
            if self._pending_paper_trades:
                self._check_pending_paper_trades(float(mid_price))

            # === TRACK SPREAD ===
            spread = float(ask_decimal - bid_decimal)
            spread_pct = spread / float(mid_price) if mid_price > 0 else 0
            self._spread_history.append(spread_pct)
            if len(self._spread_history) > 100:
                self._spread_history.pop(0)
            if len(self._spread_history) >= 10:
                self._avg_spread = sum(self._spread_history) / len(self._spread_history)

            # === TRACK VOLUME (quote tick frequency) ===
            self._quote_volume_history.append(now)
            # Keep only last 60 seconds of tick timestamps
            cutoff = now - timedelta(seconds=60)
            self._quote_volume_history = [t for t in self._quote_volume_history if t > cutoff]

            if not self._market_stable:
                self._stable_tick_count += 1
                if self._stable_tick_count >= 1:
                    self._market_stable = True
                else:
                    return

            if self._waiting_for_market_open:
                return

            if (self.current_instrument_index < 0 or
                    self.current_instrument_index >= len(self.all_btc_instruments)):
                return

            current_market = self.all_btc_instruments[self.current_instrument_index]
            market_start_ts = current_market['market_timestamp']

            elapsed_secs = now.timestamp() - market_start_ts
            if elapsed_secs < 0:
                return

            last_ts = self._last_fast_trade_ts
            # AGGRESSIVE: Trade every 2 seconds instead of 5
            trade_interval = 2 if self.AGGRESSIVE_MODE else 5
            if last_ts is None or (now - last_ts).total_seconds() >= trade_interval:
                self._last_fast_trade_ts = now

                # Calculate time remaining in market
                time_remaining = self.MARKET_DURATION_SECONDS - elapsed_secs
                time_phase = self._get_market_phase(elapsed_secs)

                # === EMERGENCY CLOSE: Close all positions before market ends ===
                if time_phase == "FINAL" and self._open_positions:
                    logger.warning("=" * 80)
                    logger.warning("⚠️ MARKET ENDING - CLOSING ALL OPEN POSITIONS")
                    logger.warning("=" * 80)
                    for order_id, position in list(self._open_positions.items()):
                        self.run_in_executor(lambda oid=order_id, pos=position:
                            self._close_position_sync(oid, pos, "MARKET_CLOSE", float(mid_price)))

                # Calculate volume (ticks per minute)
                ticks_per_min = len(self._quote_volume_history)

                logger.info("=" * 80)
                logger.info(f" FIVE-SECOND TRADE: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                logger.info(f"   Market: {current_market['slug']}")
                logger.info(f"   Price: ${float(mid_price):,.4f} | Bid: ${float(bid_decimal):,.4f} | Ask: ${float(ask_decimal):,.4f}")
                logger.info(f"   Spread: {spread_pct*100:.2f}% | Avg: {(self._avg_spread or 0)*100:.2f}%")
                logger.info(f"   Time: {int(elapsed_secs)}s elapsed | {int(time_remaining)}s remaining | Phase: {time_phase}")
                logger.info(f"   Volume: {ticks_per_min} ticks/min")

                # Show open positions status
                if self._open_positions:
                    logger.info(f"   Open Positions: {len(self._open_positions)}")
                    for oid, pos in self._open_positions.items():
                        entry = pos['entry_price']
                        curr = float(mid_price)
                        if pos['direction'] == 'long':
                            pnl = (curr - entry) / entry * 100
                        else:
                            pnl = (entry - curr) / entry * 100
                        logger.info(f"     {pos['direction'].upper()}: entry=${entry:.4f} | P&L: {pnl:+.2f}% | SL=${pos['stop_loss_price']:.4f} | TP=${pos['take_profit_price']:.4f}")
                logger.info("=" * 80)

                # Pass additional context to trading decision
                self.run_in_executor(lambda: self._make_trading_decision_sync(
                    float(mid_price),
                    elapsed_secs=elapsed_secs,
                    time_remaining=time_remaining,
                    current_spread=spread_pct,
                    ticks_per_min=ticks_per_min
                ))

        except Exception as e:
            logger.error(f"Error processing quote tick: {e}")

    def _get_market_phase(self, elapsed_secs: float) -> str:
        """Determine which phase of the market we're in."""
        time_remaining = self.MARKET_DURATION_SECONDS - elapsed_secs
        if elapsed_secs < self.EARLY_WINDOW_SECONDS:
            return "EARLY"
        elif time_remaining < self.AVOID_LAST_SECONDS:
            return "FINAL"
        elif time_remaining < self.LATE_WINDOW_SECONDS:
            return "LATE"
        else:
            return "MID"

    def _calculate_fair_value_diff(self, current_price: float) -> dict:
        """
        Calculate difference between market price and estimated 'fair value'.

        Fair value is estimated based on:
        1. BTC spot momentum - if BTC is moving up, UP probability should increase
        2. Time decay - closer to settlement, price should approach 0 or 1
        3. Order book imbalance (if available)

        Returns dict with:
        - fair_value: estimated fair probability
        - mispricing: current_price - fair_value (positive = overpriced, negative = underpriced)
        - mispricing_pct: as percentage
        - edge_direction: "long" if underpriced, "short" if overpriced, None if no edge
        - confidence: how confident we are in the mispricing estimate
        """
        result = {
            "fair_value": None,
            "mispricing": 0,
            "mispricing_pct": 0,
            "edge_direction": None,
            "confidence": 0,
            "factors": []
        }

        # Start with current price as baseline
        fair_value = current_price
        confidence = 0
        factors = []

        # === FACTOR 1: BTC MOMENTUM ===
        # If BTC spot is rising, UP probability should be higher
        if self._btc_momentum is not None and abs(self._btc_momentum) > 0.0005:
            btc_move_pct = self._btc_momentum * 100  # Convert to percentage

            # BTC momentum impacts fair value
            # A 0.1% BTC move should shift probability by ~2-5%
            momentum_impact = btc_move_pct * 0.3  # 30% of BTC move impacts probability

            if self._btc_momentum > 0:
                # BTC rising = higher UP probability
                fair_value = min(0.95, current_price + abs(momentum_impact) / 100)
                factors.append(f"BTC +{btc_move_pct:.2f}% → UP more likely")
                confidence += 0.2
            else:
                # BTC falling = lower UP probability
                fair_value = max(0.05, current_price - abs(momentum_impact) / 100)
                factors.append(f"BTC {btc_move_pct:.2f}% → DOWN more likely")
                confidence += 0.2

        # === FACTOR 2: TIME DECAY ===
        # As we approach settlement, prices should move toward 0 or 1
        elapsed_secs = getattr(self, '_current_elapsed_secs', 0)
        time_remaining = getattr(self, '_current_time_remaining', 900)

        if time_remaining < 120:  # Last 2 minutes
            # Strong prices (>0.7 or <0.3) become more certain
            if current_price > 0.65:
                # Market says UP - price should approach 1
                time_adjusted = current_price + (1 - current_price) * 0.1 * (120 - time_remaining) / 120
                fair_value = max(fair_value, time_adjusted)
                factors.append(f"Time decay: {int(time_remaining)}s left, UP likely")
                confidence += 0.15
            elif current_price < 0.35:
                # Market says DOWN - price should approach 0
                time_adjusted = current_price - current_price * 0.1 * (120 - time_remaining) / 120
                fair_value = min(fair_value, time_adjusted)
                factors.append(f"Time decay: {int(time_remaining)}s left, DOWN likely")
                confidence += 0.15

        # === FACTOR 3: SPREAD ANALYSIS ===
        # Wide spread = uncertain market = fair value closer to 0.5
        current_spread = getattr(self, '_current_spread', 0)
        if current_spread and current_spread > 0.03:  # >3% spread
            # High uncertainty - pull fair value toward 0.5
            spread_pull = (0.5 - fair_value) * 0.2
            fair_value += spread_pull
            factors.append(f"Wide spread ({current_spread*100:.1f}%) → uncertain")
            confidence -= 0.1

        # === CALCULATE MISPRICING ===
        mispricing = current_price - fair_value
        mispricing_pct = mispricing * 100

        # Determine if there's an exploitable edge
        edge_direction = None
        if abs(mispricing) >= self.MIN_INEFFICIENCY:
            if mispricing > 0:
                # Market price is HIGHER than fair value = OVERPRICED
                # We should SHORT (bet DOWN)
                edge_direction = "short"
            else:
                # Market price is LOWER than fair value = UNDERPRICED
                # We should LONG (bet UP)
                edge_direction = "long"
            confidence += 0.1

        # Strong edge if mispricing exceeds threshold
        if abs(mispricing) >= self.INEFFICIENCY_THRESHOLD:
            confidence += 0.2
            factors.append(f"STRONG EDGE: {abs(mispricing_pct):.1f}% mispricing")

        result["fair_value"] = round(fair_value, 4)
        result["mispricing"] = round(mispricing, 4)
        result["mispricing_pct"] = round(mispricing_pct, 2)
        result["edge_direction"] = edge_direction
        result["confidence"] = min(1.0, max(0, confidence))
        result["factors"] = factors

        self._fair_value_estimate = fair_value

        return result

    def _analyze_loss_reason(self, trade: dict, exit_price: float, exit_reason: str) -> dict:
        """
        Analyze WHY a trade lost and return detailed breakdown.
        This helps us learn from mistakes and improve strategy.
        """
        entry_conditions = trade.get("entry_conditions", {})
        entry_price = trade["entry_price"]
        direction = trade["direction"]

        reasons = []
        primary_reason = "UNKNOWN"
        severity = "medium"  # low, medium, high

        # Calculate what happened
        if direction == "long":
            price_moved = exit_price - entry_price
            needed_direction = "up"
            actual_direction = "up" if price_moved > 0 else "down"
        else:
            price_moved = entry_price - exit_price
            needed_direction = "down"
            actual_direction = "up" if exit_price > entry_price else "down"

        # === ANALYZE ENTRY CONDITIONS ===

        # 1. SPREAD ANALYSIS
        spread_at_entry = entry_conditions.get("spread_pct")
        avg_spread = entry_conditions.get("avg_spread_pct")
        if spread_at_entry and avg_spread:
            if spread_at_entry > avg_spread * 1.5:
                reasons.append(f"WIDE_SPREAD: Entered at {spread_at_entry*100:.2f}% spread (avg: {avg_spread*100:.2f}%)")
                if not primary_reason or primary_reason == "UNKNOWN":
                    primary_reason = "WIDE_SPREAD_ENTRY"

        # 2. MARKET PHASE ANALYSIS
        market_phase = entry_conditions.get("market_phase")
        if market_phase == "EARLY":
            reasons.append("EARLY_PHASE: Market still finding direction, higher uncertainty")
            if not primary_reason or primary_reason == "UNKNOWN":
                primary_reason = "EARLY_PHASE_ENTRY"
        elif market_phase == "LATE":
            reasons.append("LATE_PHASE: Entered late, less time for price to move in our favor")

        # 3. BTC MOMENTUM ANALYSIS
        btc_momentum = entry_conditions.get("btc_momentum")
        if btc_momentum is not None:
            btc_dir = "bullish" if btc_momentum > 0 else "bearish"
            was_aligned = (
                (direction == "long" and btc_momentum > 0) or
                (direction == "short" and btc_momentum < 0)
            )
            if not was_aligned:
                reasons.append(f"BTC_MISALIGNED: Went {direction.upper()} but BTC was {btc_dir} ({btc_momentum*100:+.2f}%)")
                if not primary_reason or primary_reason == "UNKNOWN":
                    primary_reason = "BTC_MOMENTUM_WRONG"
                severity = "high"
            elif abs(btc_momentum) < 0.0005:
                reasons.append("BTC_NO_MOMENTUM: BTC was flat, no clear direction")
                if not primary_reason or primary_reason == "UNKNOWN":
                    primary_reason = "NO_CLEAR_MOMENTUM"

        # 4. PRICE VS FAIR VALUE
        fair_value_diff = entry_conditions.get("price_vs_fair_value")
        if fair_value_diff and fair_value_diff.get("edge_direction"):
            edge_dir = fair_value_diff["edge_direction"]
            if edge_dir != direction:
                mispricing = fair_value_diff.get("mispricing_pct", 0)
                reasons.append(f"WRONG_EDGE: Fair value suggested {edge_dir.upper()}, but went {direction.upper()} (mispricing: {mispricing:+.1f}%)")
                if not primary_reason or primary_reason == "UNKNOWN":
                    primary_reason = "TRADED_AGAINST_FAIR_VALUE"
                severity = "high"

        # 5. VOLUME SPIKE ANALYSIS
        was_volume_spike = entry_conditions.get("is_volume_spike")
        if was_volume_spike:
            reasons.append("VOLUME_SPIKE: High volatility environment, unpredictable")

        # 6. CONSECUTIVE LOSSES
        consec_losses = entry_conditions.get("consecutive_losses", 0)
        if consec_losses >= 1:
            reasons.append(f"REPEATED_FAILURE: Already had {consec_losses} consecutive {direction} losses before this trade")
            if consec_losses >= 2:
                severity = "high"
                if not primary_reason or primary_reason == "UNKNOWN":
                    primary_reason = "REPEATED_SAME_DIRECTION"

        # 7. TIME REMAINING
        time_remaining = entry_conditions.get("time_remaining")
        if time_remaining and time_remaining < 60:
            reasons.append(f"TOO_CLOSE_TO_SETTLEMENT: Only {int(time_remaining)}s remaining")
            if not primary_reason or primary_reason == "UNKNOWN":
                primary_reason = "SETTLEMENT_TOO_CLOSE"

        # 8. EXIT REASON ANALYSIS
        if exit_reason == "STOP_LOSS":
            reasons.append("STOPPED_OUT: Price moved against position by full stop loss amount")
            if primary_reason == "UNKNOWN":
                primary_reason = "MARKET_MOVED_AGAINST"
        elif exit_reason == "TIME_EXIT":
            reasons.append("TIME_EXIT: Position held too long without hitting target")
            if primary_reason == "UNKNOWN":
                primary_reason = "PRICE_STAGNANT"

        # Generate actionable insight
        insight = self._generate_loss_insight(primary_reason, direction, entry_conditions)

        return {
            "primary_reason": primary_reason,
            "all_reasons": reasons,
            "severity": severity,
            "insight": insight,
            "entry_conditions": entry_conditions,
            "price_moved": price_moved,
            "needed_direction": needed_direction,
            "actual_direction": actual_direction,
        }

    def _generate_loss_insight(self, primary_reason: str, direction: str, entry_conditions: dict) -> str:
        """Generate actionable insight from loss reason."""
        insights = {
            "BTC_MOMENTUM_WRONG": f"Consider only going {direction.upper()} when BTC spot is moving in the same direction.",
            "TRADED_AGAINST_FAIR_VALUE": "Wait for price to be undervalued (for LONG) or overvalued (for SHORT) before entry.",
            "WIDE_SPREAD_ENTRY": "Avoid entering when spread is above average - indicates uncertain/illiquid market.",
            "EARLY_PHASE_ENTRY": "Wait for market to stabilize (after first 2 minutes) before entering.",
            "REPEATED_SAME_DIRECTION": f"Take a longer break after consecutive {direction} losses. Market may be trending against us.",
            "NO_CLEAR_MOMENTUM": "Require stronger BTC momentum (>0.1%) before entering trades.",
            "SETTLEMENT_TOO_CLOSE": "Avoid trading in the final 60 seconds - not enough time for edge to play out.",
            "MARKET_MOVED_AGAINST": "Consider tighter position sizing or wider stops based on current volatility.",
            "PRICE_STAGNANT": "Consider shorter hold times or avoid trading in low-volatility conditions.",
            "UNKNOWN": "Review trade manually - no clear pattern detected.",
        }
        return insights.get(primary_reason, insights["UNKNOWN"])

    # ========================================================================
    # ADAPTIVE FEEDBACK LOOP
    # ========================================================================

    def _update_adaptive_feedback(self, trade: dict, is_win: bool):
        """
        Update the adaptive feedback system based on trade result.
        This is the core learning loop that improves strategy over time.
        """
        if not self._adaptive_enabled:
            return

        direction = trade["direction"]
        entry_conditions = trade.get("entry_conditions", {})
        strategy_used = trade.get("strategy_used", "mean_reversion")

        # === 1. UPDATE DIRECTION PREFERENCE ===
        self._update_direction_preference()

        # === 2. UPDATE STRATEGY STATS ===
        if strategy_used in self._strategy_stats:
            if is_win:
                self._strategy_stats[strategy_used]["wins"] += 1
            else:
                self._strategy_stats[strategy_used]["losses"] += 1

            # Check if strategy should be disabled
            self._evaluate_strategy_performance(strategy_used)

        # === 3. UPDATE CONDITION PERFORMANCE ===
        self._update_condition_stats(entry_conditions, is_win)

        # === 4. ADAPTIVE PARAMETER TUNING ===
        self._tune_parameters_from_losses()

        # === 5. LOG ADAPTATION STATE ===
        if (len(self._direction_results["long"]) + len(self._direction_results["short"])) % 10 == 0:
            self._log_adaptation_state()

    def _update_direction_preference(self):
        """Update direction preference based on rolling win rates."""
        long_results = self._direction_results["long"]
        short_results = self._direction_results["short"]

        min_trades = 5  # Need at least 5 trades each to compare

        if len(long_results) >= min_trades and len(short_results) >= min_trades:
            long_wr = sum(long_results) / len(long_results)
            short_wr = sum(short_results) / len(short_results)

            edge = long_wr - short_wr

            if edge > self.MIN_DIRECTION_EDGE:
                self._direction_preference = "long"
                self._direction_edge = edge
            elif edge < -self.MIN_DIRECTION_EDGE:
                self._direction_preference = "short"
                self._direction_edge = abs(edge)
            else:
                self._direction_preference = None
                self._direction_edge = 0

    def _evaluate_strategy_performance(self, strategy: str):
        """Track strategy performance (disable only if DISABLE_LOSING_STRATEGIES is True)."""
        stats = self._strategy_stats[strategy]
        total = stats["wins"] + stats["losses"]

        if total < 10:
            return  # Need more data

        win_rate = stats["wins"] / total

        # In learning mode, just log but don't disable
        if not self.DISABLE_LOSING_STRATEGIES:
            if win_rate < 0.35:
                logger.info(f"📊 LEARNING: '{strategy}' strategy at {win_rate*100:.0f}% win rate - continuing for data")
            return

        # Disable if win rate < 35%
        if win_rate < 0.35:
            if stats["enabled"]:
                stats["enabled"] = False
                logger.warning(f"⚠️ ADAPTIVE: Disabling '{strategy}' strategy (win rate: {win_rate*100:.0f}%)")
        # Re-enable if win rate improves above 45%
        elif win_rate > 0.45 and not stats["enabled"]:
            stats["enabled"] = True
            logger.info(f"✅ ADAPTIVE: Re-enabling '{strategy}' strategy (win rate: {win_rate*100:.0f}%)")

    def _update_condition_stats(self, entry_conditions: dict, is_win: bool):
        """Track performance under different market conditions."""
        # Market phase
        phase = entry_conditions.get("market_phase", "MID").lower()
        if phase in ["early", "mid", "late"]:
            key = f"{phase}_phase"
            if key in self._condition_performance:
                if is_win:
                    self._condition_performance[key]["wins"] += 1
                else:
                    self._condition_performance[key]["losses"] += 1

        # BTC alignment
        btc_momentum = entry_conditions.get("btc_momentum")
        direction = entry_conditions.get("direction") if "direction" in entry_conditions else None
        # Note: We need to pass direction separately, but for now check if BTC was strong
        if btc_momentum is not None and abs(btc_momentum) > 0.0005:
            # We'll track this broadly
            pass

        # Spread
        spread = entry_conditions.get("spread_pct")
        avg_spread = entry_conditions.get("avg_spread_pct")
        if spread and avg_spread:
            is_wide = spread > avg_spread * 1.5
            key = "wide_spread" if is_wide else "normal_spread"
            if is_win:
                self._condition_performance[key]["wins"] += 1
            else:
                self._condition_performance[key]["losses"] += 1

        # Volume
        is_spike = entry_conditions.get("is_volume_spike", False)
        key = "volume_spike" if is_spike else "normal_volume"
        if is_win:
            self._condition_performance[key]["wins"] += 1
        else:
            self._condition_performance[key]["losses"] += 1

    def _tune_parameters_from_losses(self):
        """Automatically tune parameters based on loss patterns."""
        if not self._loss_reasons:
            return

        total_losses = sum(self._loss_reasons.values())
        if total_losses < 10:
            return  # Need more data

        # === TUNE BASED ON MOST COMMON LOSS REASONS ===

        # If BTC misalignment is common, require BTC alignment
        btc_wrong_pct = self._loss_reasons.get("BTC_MOMENTUM_WRONG", 0) / total_losses
        if btc_wrong_pct > 0.25:  # >25% of losses due to BTC misalignment
            if not self._adaptive_btc_alignment_required:
                self._adaptive_btc_alignment_required = True
                logger.info(f"🔧 ADAPTIVE TUNE: Now requiring BTC alignment ({btc_wrong_pct*100:.0f}% of losses were misaligned)")

        # If wide spread is common, tighten spread threshold
        wide_spread_pct = self._loss_reasons.get("WIDE_SPREAD_ENTRY", 0) / total_losses
        if wide_spread_pct > 0.20:  # >20% of losses due to wide spread
            new_threshold = self._adaptive_spread_threshold * 0.8  # Reduce by 20%
            new_threshold = max(0.02, new_threshold)  # Don't go below 2%
            if new_threshold < self._adaptive_spread_threshold:
                self._adaptive_spread_threshold = new_threshold
                logger.info(f"🔧 ADAPTIVE TUNE: Tightened spread threshold to {self._adaptive_spread_threshold*100:.1f}%")

        # If no momentum is common, increase momentum requirement
        no_momentum_pct = self._loss_reasons.get("NO_CLEAR_MOMENTUM", 0) / total_losses
        if no_momentum_pct > 0.15:  # >15% of losses due to weak momentum
            new_threshold = self._adaptive_momentum_threshold * 1.5
            new_threshold = min(0.01, new_threshold)  # Don't go above 1%
            if new_threshold > self._adaptive_momentum_threshold:
                self._adaptive_momentum_threshold = new_threshold
                logger.info(f"🔧 ADAPTIVE TUNE: Increased momentum threshold to {self._adaptive_momentum_threshold*100:.2f}%")

    def _log_adaptation_state(self):
        """Log current state of adaptive system."""
        logger.info("=" * 70)
        logger.info("🧠 ADAPTIVE FEEDBACK STATE")
        logger.info("-" * 70)

        # Direction preference
        if self._direction_preference:
            logger.info(f"   Direction Preference: {self._direction_preference.upper()} (+{self._direction_edge*100:.0f}% edge)")
        else:
            logger.info(f"   Direction Preference: NONE (balanced)")

        # Win rates by direction
        for d in ["long", "short"]:
            results = self._direction_results[d]
            if len(results) >= 3:
                wr = sum(results) / len(results) * 100
                logger.info(f"   {d.upper()} Win Rate: {wr:.0f}% ({len(results)} trades)")

        # Strategy stats
        logger.info("-" * 70)
        logger.info("   Strategy Performance:")
        for strategy, stats in self._strategy_stats.items():
            total = stats["wins"] + stats["losses"]
            if total > 0:
                wr = stats["wins"] / total * 100
                status = "✅" if stats["enabled"] else "❌"
                logger.info(f"   {status} {strategy}: {wr:.0f}% win rate ({total} trades)")

        # Adaptive parameters
        logger.info("-" * 70)
        logger.info("   Adaptive Parameters:")
        logger.info(f"   • Spread threshold: {self._adaptive_spread_threshold*100:.1f}%")
        logger.info(f"   • Momentum threshold: {self._adaptive_momentum_threshold*100:.2f}%")
        logger.info(f"   • BTC alignment required: {self._adaptive_btc_alignment_required}")

        # Top loss reasons
        if self._loss_reasons:
            logger.info("-" * 70)
            logger.info("   Top Loss Reasons:")
            sorted_reasons = sorted(self._loss_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
            for reason, count in sorted_reasons:
                logger.info(f"   • {reason}: {count}")

        logger.info("=" * 70)

    def _should_skip_direction(self, direction: str) -> tuple[bool, str]:
        """
        Check if we should skip this direction based on adaptive feedback.
        Returns (should_skip, reason).
        """
        if not self._adaptive_enabled:
            return False, ""

        # In aggressive/learning mode, never skip directions
        if not self.SKIP_WEAK_DIRECTION:
            # Still log if we have a preference (for learning)
            if self._direction_preference and self._direction_preference != direction:
                if self._direction_edge > 0.20:
                    logger.info(f"📊 NOTE: {self._direction_preference.upper()} has +{self._direction_edge*100:.0f}% edge, but trading {direction.upper()} anyway for learning")
            return False, ""

        # If we have a strong preference for the OTHER direction, skip this one
        if self._direction_preference and self._direction_preference != direction:
            if self._direction_edge > 0.20:  # Only skip if edge is >20%
                return True, f"Adaptive prefers {self._direction_preference.upper()} (+{self._direction_edge*100:.0f}% edge)"

        return False, ""

    def _get_active_strategy(self, has_inefficiency: bool, is_burst: bool) -> str:
        """Determine which strategy to use based on conditions and enabled status."""
        if has_inefficiency and self._strategy_stats["inefficiency"]["enabled"]:
            return "inefficiency"
        elif is_burst and self._strategy_stats["burst"]["enabled"]:
            return "burst"
        elif self._strategy_stats["mean_reversion"]["enabled"]:
            return "mean_reversion"
        else:
            return None  # All strategies disabled - don't trade

    def _check_stop_loss_take_profit(self, current_price: float, bid_price: float, ask_price: float):
        """Check all open positions for stop loss or take profit triggers."""
        positions_to_close = []

        for order_id, position in self._open_positions.items():
            direction = position["direction"]
            entry_price = position["entry_price"]
            stop_loss = position["stop_loss_price"]
            take_profit = position["take_profit_price"]

            # Calculate current P&L percentage
            if direction == "long":
                # Long: profit when price goes UP
                pnl_pct = (current_price - entry_price) / entry_price
                # Update highest price for trailing stop
                if current_price > position.get("highest_price", entry_price):
                    position["highest_price"] = current_price
                    # Update trailing stop if in profit
                    if self.USE_TRAILING_STOP and pnl_pct > 0.05:  # After 5% profit
                        new_trailing_stop = current_price * (1 - self.TRAILING_STOP_PCT)
                        if new_trailing_stop > position.get("trailing_stop_price", 0):
                            position["trailing_stop_price"] = new_trailing_stop
                            logger.info(f"📈 Trailing stop updated: ${new_trailing_stop:.4f} (locked {pnl_pct*100:.1f}% profit)")
            else:
                # Short: profit when price goes DOWN
                pnl_pct = (entry_price - current_price) / entry_price
                # Update lowest price for trailing stop
                if current_price < position.get("lowest_price", entry_price):
                    position["lowest_price"] = current_price
                    # Update trailing stop if in profit
                    if self.USE_TRAILING_STOP and pnl_pct > 0.05:
                        new_trailing_stop = current_price * (1 + self.TRAILING_STOP_PCT)
                        if new_trailing_stop < position.get("trailing_stop_price", float('inf')):
                            position["trailing_stop_price"] = new_trailing_stop
                            logger.info(f"📈 Trailing stop updated: ${new_trailing_stop:.4f} (locked {pnl_pct*100:.1f}% profit)")

            # Check stop loss
            exit_reason = None
            if direction == "long":
                if current_price <= stop_loss:
                    exit_reason = "STOP_LOSS"
                elif position.get("trailing_stop_price") and current_price <= position["trailing_stop_price"]:
                    exit_reason = "TRAILING_STOP"
                elif current_price >= take_profit:
                    exit_reason = "TAKE_PROFIT"
            else:  # short
                if current_price >= stop_loss:
                    exit_reason = "STOP_LOSS"
                elif position.get("trailing_stop_price") and current_price >= position["trailing_stop_price"]:
                    exit_reason = "TRAILING_STOP"
                elif current_price <= take_profit:
                    exit_reason = "TAKE_PROFIT"

            if exit_reason:
                positions_to_close.append((order_id, position, exit_reason, current_price, pnl_pct))

        # Close triggered positions
        for order_id, position, reason, exit_price, pnl_pct in positions_to_close:
            logger.info("=" * 80)
            logger.info(f"🚨 {reason} TRIGGERED!")
            logger.info(f"   Position: {position['direction'].upper()}")
            logger.info(f"   Entry: ${position['entry_price']:.4f} -> Exit: ${exit_price:.4f}")
            logger.info(f"   P&L: {pnl_pct*100:+.2f}%")
            logger.info("=" * 80)

            # Close the position
            self.run_in_executor(lambda oid=order_id, pos=position, r=reason, ep=exit_price:
                self._close_position_sync(oid, pos, r, ep))

    def _close_position_sync(self, order_id: str, position: dict, reason: str, exit_price: float):
        """Synchronous wrapper to close a position."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._close_position(order_id, position, reason, exit_price))
        finally:
            loop.close()

    async def _close_position(self, order_id: str, position: dict, reason: str, exit_price: float):
        """Close an open position by selling the tokens."""
        from nautilus_trader.model.enums import OrderSide, TimeInForce
        from nautilus_trader.model.identifiers import ClientOrderId
        from nautilus_trader.model.objects import Quantity

        try:
            instrument_id = position["instrument_id"]
            quantity = position["quantity"]
            direction = position["direction"]
            entry_price = position["entry_price"]

            instrument = self.cache.instrument(instrument_id)
            if not instrument:
                logger.error(f"Cannot close position - instrument not in cache: {instrument_id}")
                return

            precision = instrument.size_precision
            qty = Quantity(quantity, precision=precision)
            timestamp_ms = int(time.time() * 1000)
            close_order_id = f"CLOSE-{order_id}-{timestamp_ms}"

            # SELL to close the position
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.SELL,
                quantity=qty,
                client_order_id=ClientOrderId(close_order_id),
                quote_quantity=False,
                time_in_force=TimeInForce.IOC,
            )

            self.submit_order(order)

            # Calculate P&L
            if direction == "long":
                pnl_pct = (exit_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - exit_price) / entry_price
            pnl_usd = position.get("size_usd", 1.0) * pnl_pct

            logger.info(f"POSITION CLOSED: {reason}")
            logger.info(f"   Close Order ID: {close_order_id}")
            logger.info(f"   Direction: {direction.upper()}")
            logger.info(f"   Entry: ${entry_price:.4f} -> Exit: ${exit_price:.4f}")
            logger.info(f"   P&L: ${pnl_usd:+.4f} ({pnl_pct*100:+.2f}%)")

            # Remove from open positions
            if order_id in self._open_positions:
                del self._open_positions[order_id]

            # Persist to database
            if self._db_initialized and self._repo:
                try:
                    await self._repo.save_order_event(
                        order_id=close_order_id,
                        event_type="position_closed",
                        side="SELL",
                        fill_price=Decimal(str(exit_price)),
                        fill_quantity=Decimal(str(quantity)),
                        raw_event={
                            "reason": reason,
                            "original_order_id": order_id,
                            "direction": direction,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "pnl_pct": pnl_pct,
                            "pnl_usd": pnl_usd,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist position close: {e}")

            self._track_order_event("position_closed")

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            import traceback
            traceback.print_exc()

    def _make_trading_decision_sync(
        self, current_price: float,
        elapsed_secs: float = 0,
        time_remaining: float = 900,
        current_spread: float = 0,
        ticks_per_min: int = 0
    ):
        """Synchronous wrapper with additional market context."""
        price_decimal = Decimal(str(current_price))

        # Store context for async method to use
        self._current_elapsed_secs = elapsed_secs
        self._current_time_remaining = time_remaining
        self._current_spread = current_spread
        self._current_ticks_per_min = ticks_per_min

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._make_trading_decision(price_decimal))
        finally:
            loop.close()

    async def _fetch_market_context(self, current_price: Decimal) -> dict:
        """Local context + cached external data for five-second mode."""
        current_price_float = float(current_price)
        recent_prices = [float(p) for p in self.price_history[-20:]]
        sma_20 = sum(recent_prices) / len(recent_prices)
        deviation = (current_price_float - sma_20) / sma_20
        momentum = (
            (current_price_float - float(self.price_history[-5])) / float(self.price_history[-5])
            if len(self.price_history) >= 5 else 0.0
        )
        variance = sum((p - sma_20) ** 2 for p in recent_prices) / len(recent_prices)
        volatility = math.sqrt(variance)

        return {
            "deviation": deviation,
            "momentum": momentum,
            "volatility": volatility,
            "tick_buffer": list(self._tick_buffer),
            "yes_token_id": self._yes_token_id,
            # Cached external data (refreshed every 60s in background)
            "sentiment_score": self._cached_fear_greed,
            "sentiment_classification": self._cached_fear_greed_class,
            "spot_price": self._cached_btc_spot,
            # New tracking data
            "btc_momentum": self._btc_momentum,
            "avg_spread": self._avg_spread,
            "current_spread": getattr(self, '_current_spread', None),
            "elapsed_secs": getattr(self, '_current_elapsed_secs', None),
            "time_remaining": getattr(self, '_current_time_remaining', None),
            "market_phase": self._get_market_phase(getattr(self, '_current_elapsed_secs', 0)),
            "ticks_per_min": getattr(self, '_current_ticks_per_min', None),
            "is_volume_spike": self._is_volume_spike,
        }

    async def _make_trading_decision(self, current_price: Decimal):
        is_simulation = await self.check_simulation_mode()
        logger.info("Mode: SIMULATION (five-second)")

        min_history = 10
        if len(self.price_history) < min_history:
            logger.warning(f"Not enough price history ({len(self.price_history)}/{min_history})")
            return

        price_float = float(current_price)
        logger.info(f"Current price: ${price_float:,.4f}")

        # === SWEET SPOT TRADING (WIDENED FOR AGGRESSIVE LEARNING) ===
        # In aggressive mode, trade almost anywhere to gather data
        SWEET_SPOT_LOW = 0.10 if self.AGGRESSIVE_MODE else 0.30
        SWEET_SPOT_HIGH = 0.90 if self.AGGRESSIVE_MODE else 0.70

        if price_float < SWEET_SPOT_LOW:
            logger.info(f"⛔ OUTSIDE RANGE: Price ${price_float:.2f} too extreme — skipping")
            return
        if price_float > SWEET_SPOT_HIGH:
            logger.info(f"⛔ OUTSIDE RANGE: Price ${price_float:.2f} too extreme — skipping")
            return

        logger.info(f"✅ PRICE OK: ${price_float:.2f} — proceeding")

        # === NEW: SPREAD CHECK (using adaptive threshold) ===
        current_spread = getattr(self, '_current_spread', 0)
        spread_threshold = self._adaptive_spread_threshold if self._adaptive_enabled else self.MAX_SPREAD_THRESHOLD
        if current_spread > spread_threshold:
            logger.info(f"⛔ SPREAD TOO WIDE: {current_spread*100:.2f}% > {spread_threshold*100:.1f}% — SKIPPING")
            return

        # Warn if spread is widening (potential volatility incoming)
        if self._avg_spread and current_spread > self._avg_spread * self.SPREAD_WIDENING_THRESHOLD:
            logger.warning(f"⚠️ SPREAD WIDENING: {current_spread*100:.2f}% vs avg {self._avg_spread*100:.2f}% — be cautious")

        # === NEW: TIME-TO-CLOSE CHECK ===
        elapsed_secs = getattr(self, '_current_elapsed_secs', 0)
        time_remaining = getattr(self, '_current_time_remaining', 900)
        market_phase = self._get_market_phase(elapsed_secs)

        # Don't trade in final seconds (too risky, outcome nearly decided)
        if market_phase == "FINAL":
            logger.info(f"⛔ FINAL PHASE: Only {int(time_remaining)}s remaining — SKIPPING (too close to settlement)")
            return

        # Early phase: be more cautious (market still finding direction)
        early_phase_caution = (market_phase == "EARLY")
        if early_phase_caution:
            logger.info(f"⚠️ EARLY PHASE: Market still finding direction — requiring stronger signals")

        # Late phase: price is more predictive (outcome clearer)
        late_phase_confidence = (market_phase == "LATE")
        if late_phase_confidence:
            logger.info(f"✅ LATE PHASE: Price is more predictive — trusting trend")

        # === NEW: BTC MOMENTUM CHECK ===
        btc_momentum_signal = None
        if self._cached_btc_spot and len(self._btc_spot_history) >= 2:
            btc_change = (self._cached_btc_spot - self._btc_spot_history[0]) / self._btc_spot_history[0]
            self._btc_momentum = btc_change
            if abs(btc_change) > 0.001:  # 0.1% move in BTC
                btc_momentum_signal = "bullish" if btc_change > 0 else "bearish"
                logger.info(f"BTC Momentum: {btc_change*100:+.2f}% ({btc_momentum_signal})")

        # === NEW: VOLUME SPIKE DETECTION ===
        ticks_per_min = getattr(self, '_current_ticks_per_min', 0)
        # Establish baseline after first minute
        if not hasattr(self, '_baseline_ticks_per_min'):
            self._baseline_ticks_per_min = None
        if ticks_per_min > 0:
            if self._baseline_ticks_per_min is None:
                self._baseline_ticks_per_min = ticks_per_min
            elif ticks_per_min > self._baseline_ticks_per_min * self._volume_spike_threshold:
                self._is_volume_spike = True
                logger.info(f"📈 VOLUME SPIKE: {ticks_per_min} ticks/min vs baseline {self._baseline_ticks_per_min}")
            else:
                self._is_volume_spike = False
                # Update baseline with EMA
                self._baseline_ticks_per_min = 0.9 * self._baseline_ticks_per_min + 0.1 * ticks_per_min

        # === PRICE INEFFICIENCY DETECTION ===
        # Check if market price differs from our estimated fair value
        fair_value_analysis = self._calculate_fair_value_diff(price_float)
        if fair_value_analysis["fair_value"]:
            mispricing_pct = fair_value_analysis["mispricing_pct"]
            edge_dir = fair_value_analysis["edge_direction"]
            confidence = fair_value_analysis["confidence"]

            logger.info(f"📊 FAIR VALUE ANALYSIS:")
            logger.info(f"   Market: ${price_float:.4f} | Fair Value: ${fair_value_analysis['fair_value']:.4f}")
            logger.info(f"   Mispricing: {mispricing_pct:+.2f}% | Edge: {edge_dir or 'NONE'}")
            if fair_value_analysis["factors"]:
                for factor in fair_value_analysis["factors"]:
                    logger.info(f"     • {factor}")

            # If we have a strong price inefficiency, use it to guide direction
            if edge_dir and abs(mispricing_pct) >= self.INEFFICIENCY_THRESHOLD * 100:
                logger.info(f"✅ STRONG INEFFICIENCY DETECTED: {abs(mispricing_pct):.1f}% → suggests {edge_dir.upper()}")

        metadata = await self._fetch_market_context(current_price)
        momentum = metadata.get("momentum", 0.0)

        # === TRACK PRICE VELOCITY FOR BURST DETECTION ===
        self._price_velocity.append(momentum)
        if len(self._price_velocity) > 5:
            self._price_velocity.pop(0)

        # === DETECT MOMENTUM BURST (rocket/plunge) ===
        is_burst = False
        burst_direction = None
        if len(self._price_velocity) >= 2:
            avg_velocity = sum(self._price_velocity) / len(self._price_velocity)
            if avg_velocity > self.BURST_THRESHOLD:
                is_burst = True
                burst_direction = "long"  # Price rocketing up
                logger.info(f"🚀 MOMENTUM BURST DETECTED: UP (velocity={avg_velocity:.4f})")
            elif avg_velocity < -self.BURST_THRESHOLD:
                is_burst = True
                burst_direction = "short"  # Price plunging down
                logger.info(f"📉 MOMENTUM BURST DETECTED: DOWN (velocity={avg_velocity:.4f})")

        # === DETERMINE DIRECTION ===
        # PRIORITY ORDER:
        # 1. Strong price inefficiency (mispricing > 3%) - highest edge
        # 2. Momentum burst - ride the wave
        # 3. Mean reversion - default strategy

        # Check if we have a price inefficiency edge to exploit
        inefficiency_edge = None
        has_inefficiency = False
        if fair_value_analysis and fair_value_analysis.get("edge_direction"):
            mispricing_pct = abs(fair_value_analysis.get("mispricing_pct", 0))
            if mispricing_pct >= self.INEFFICIENCY_THRESHOLD * 100:
                inefficiency_edge = fair_value_analysis["edge_direction"]
                has_inefficiency = True
                logger.info(f"🎯 PRICE INEFFICIENCY STRATEGY: {inefficiency_edge.upper()} ({mispricing_pct:.1f}% mispricing)")

        # Determine which strategy to use (respects disabled strategies)
        active_strategy = self._get_active_strategy(has_inefficiency, is_burst)
        if active_strategy is None:
            logger.warning("⛔ ALL STRATEGIES DISABLED by adaptive feedback — SKIPPING")
            return

        # Track which strategy we're using
        self._last_trade_strategy = active_strategy

        if inefficiency_edge and active_strategy == "inefficiency":
            # HIGHEST PRIORITY: Exploit price inefficiency
            direction = inefficiency_edge
            logger.info(f"INEFFICIENCY TRADE: Going {direction.upper()} to exploit mispricing")
        elif is_burst and burst_direction and active_strategy == "burst":
            # During burst: ride the momentum (max 3 trades)
            if self._burst_direction == burst_direction:
                self._burst_trades_count += 1
            else:
                # New burst direction
                self._burst_direction = burst_direction
                self._burst_trades_count = 1
                self._burst_start_price = price_float

            if self._burst_trades_count > self.MAX_BURST_TRADES:
                logger.info(f"BURST LIMIT: Already made {self.MAX_BURST_TRADES} trades in this burst — waiting for reversal")
                return

            direction = burst_direction
            logger.info(f"BURST TRADE #{self._burst_trades_count}: Following momentum {direction.upper()}")
        else:
            # Reset burst tracking when momentum calms
            self._burst_trades_count = 0
            self._burst_direction = None

            # === MEAN REVERSION LOGIC (AGGRESSIVE THRESHOLDS) ===
            # In aggressive mode, trade in wider range
            if self.AGGRESSIVE_MODE:
                long_threshold = 0.48   # Go LONG below this (was 0.45)
                short_threshold = 0.52  # Go SHORT above this (was 0.55)
            else:
                long_threshold = 0.45
                short_threshold = 0.55

            # Adjust thresholds based on market phase (minimal adjustment in aggressive mode)
            if early_phase_caution and not self.AGGRESSIVE_MODE:
                long_threshold = 0.40
                short_threshold = 0.60
                logger.debug(f"Early phase: adjusted thresholds to {long_threshold}/{short_threshold}")
            elif late_phase_confidence:
                # Late phase: trust the price more (wider band = take more trades)
                long_threshold = 0.49
                short_threshold = 0.51
                logger.debug(f"Late phase: adjusted thresholds to {long_threshold}/{short_threshold}")

            # Determine direction
            if price_float > short_threshold:
                direction = "short"  # High price = expect drop
            elif price_float < long_threshold:
                direction = "long"   # Low price = expect rise
            else:
                logger.info(f"TREND: NEUTRAL — price {price_float:.2f} in neutral zone [{long_threshold:.2f}-{short_threshold:.2f}], SKIPPING")
                return

            # === BTC MOMENTUM ALIGNMENT CHECK (logging only in aggressive mode) ===
            if btc_momentum_signal:
                btc_aligned = (
                    (direction == "long" and btc_momentum_signal == "bullish") or
                    (direction == "short" and btc_momentum_signal == "bearish")
                )
                if not btc_aligned:
                    if self.AGGRESSIVE_MODE:
                        # In aggressive mode, log but don't skip
                        logger.info(f"📊 BTC MISALIGNED: {direction.upper()} vs BTC {btc_momentum_signal} — trading anyway for learning")
                    elif self._adaptive_btc_alignment_required:
                        logger.info(f"⛔ SKIPPING: BTC misaligned (adaptive learned to require alignment)")
                        return
                    elif not late_phase_confidence:
                        logger.info(f"⚠️ BTC MISALIGNED: Want {direction.upper()} but BTC is {btc_momentum_signal} — extra caution")
                        # In early phase, skip misaligned trades
                        if early_phase_caution:
                            logger.info(f"⛔ SKIPPING: BTC misalignment + early phase")
                            return
                elif btc_aligned:
                    logger.info(f"✅ BTC ALIGNED: {direction.upper()} matches BTC {btc_momentum_signal}")

            # === VOLUME SPIKE HANDLING ===
            if self._is_volume_spike:
                logger.info(f"📈 VOLUME SPIKE: Increased confidence in direction")
                # Volume spikes often precede big moves - if direction is clear, proceed

        # === PRICE CEILING/FLOOR PROTECTION ===
        if direction == "long" and price_float > self.PRICE_CEILING:
            logger.info(f"⛔ PRICE CEILING: Won't go LONG at {price_float:.2f} (ceiling={self.PRICE_CEILING})")
            return
        if direction == "short" and price_float < self.PRICE_FLOOR:
            logger.info(f"⛔ PRICE FLOOR: Won't go SHORT at {price_float:.2f} (floor={self.PRICE_FLOOR})")
            return

        # === ADAPTIVE DIRECTION PREFERENCE CHECK ===
        should_skip, skip_reason = self._should_skip_direction(direction)
        if should_skip:
            logger.info(f"⛔ ADAPTIVE SKIP: {skip_reason}")
            return

        # === CONSECUTIVE LOSS PROTECTION (minimal in aggressive mode) ===
        if self._consecutive_losses[direction] >= self.MAX_CONSECUTIVE_LOSSES:
            if self.AGGRESSIVE_MODE:
                # In aggressive mode, just log and continue
                logger.info(f"📊 LEARNING: {self._consecutive_losses[direction]} consecutive {direction.upper()} losses — trading anyway")
            else:
                cooldown_start = self._cooldown_start.get(direction)
                if cooldown_start is None:
                    # Start cooldown timer
                    self._cooldown_start[direction] = datetime.now(timezone.utc)
                    logger.info(f"⏸️ COOLDOWN STARTED: {self._consecutive_losses[direction]} consecutive {direction.upper()} losses — waiting {self.COOLDOWN_SECONDS}s")
                    return
                else:
                    # Check if cooldown has expired
                    elapsed = (datetime.now(timezone.utc) - cooldown_start).total_seconds()
                    if elapsed < self.COOLDOWN_SECONDS:
                        remaining = int(self.COOLDOWN_SECONDS - elapsed)
                        if remaining % 10 == 0:  # Only log every 10 seconds
                            logger.info(f"⏸️ COOLDOWN: {remaining}s remaining for {direction.upper()}")
                        return
                    else:
                        # Cooldown expired - reset and allow trade
                        logger.info(f"✅ COOLDOWN EXPIRED: Resetting {direction.upper()} losses, allowing trade")
                        self._consecutive_losses[direction] = 0
                        self._cooldown_start[direction] = None

        # === DIRECTIONAL WIN RATE CHECK (disabled in aggressive mode) ===
        dir_results = self._direction_results[direction]
        if len(dir_results) >= 5 and not self.AGGRESSIVE_MODE:
            win_rate = sum(dir_results) / len(dir_results)
            if win_rate < 0.35:  # Less than 35% win rate
                logger.info(f"⚠️ LOW WIN RATE: {direction.upper()} at {win_rate*100:.0f}% — being cautious")
                # Require stronger momentum to override
                if abs(momentum) < 0.01:
                    logger.info(f"Momentum not strong enough to override low win rate — skipping")
                    return
        elif len(dir_results) >= 5:
            # In aggressive mode, just log for learning
            win_rate = sum(dir_results) / len(dir_results)
            logger.info(f"📊 {direction.upper()} win rate: {win_rate*100:.0f}% — continuing for learning")

        # === SIGNAL VALIDATION ===
        signals = self._process_signals(current_price, metadata)
        if not signals:
            logger.info("No signals generated — no trade this interval")
            return

        signal_sources = [s.source for s in signals]

        # AGGRESSIVE: Lower signal score requirement
        min_score = 10.0 if self.AGGRESSIVE_MODE else 30.0
        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=min_score)
        if not fused:
            logger.info("Fusion produced no actionable signal — no trade this interval")
            return

        # AGGRESSIVE: Almost no momentum requirement
        if self.AGGRESSIVE_MODE:
            min_momentum = 0.0001  # Essentially any movement
        else:
            base_momentum = self._adaptive_momentum_threshold if self._adaptive_enabled else 0.003
            min_momentum = 0.001 if is_burst else base_momentum

        if abs(momentum) < min_momentum:
            logger.info(f"Momentum too weak ({momentum:.4f} < {min_momentum:.4f}) — skipping")
            return

        POSITION_SIZE_USD = Decimal("1.00")

        is_valid, error = self.risk_engine.validate_new_position(
            size=POSITION_SIZE_USD,
            direction=direction,
            current_price=current_price,
        )
        if not is_valid:
            logger.warning(f"Risk engine blocked trade: {error}")
            return

        last_tick = getattr(self, '_last_bid_ask', None)
        if last_tick:
            last_bid, last_ask = last_tick
            MIN_LIQUIDITY = Decimal("0.02")
            if direction == "long" and last_ask <= MIN_LIQUIDITY:
                logger.warning("No liquidity for BUY — skipping trade")
                return
            if direction == "short" and last_bid <= MIN_LIQUIDITY:
                logger.warning("No liquidity for SELL — skipping trade")
                return

        logger.info(f"✅ PLACING {direction.upper()} trade | Price: {price_float:.4f} | Momentum: {momentum:.4f}")

        if is_simulation:
            await self._record_paper_trade(fused, POSITION_SIZE_USD, current_price, direction, signal_sources)
        else:
            await self._place_real_order(fused, POSITION_SIZE_USD, current_price, direction, signal_sources)

    async def _record_paper_trade(self, signal, position_size, current_price, direction, signal_sources=None):
        """
        Record paper trade entry - actual P&L calculated when price updates.
        No more simulation - we use REAL price movement!
        """
        entry_price = float(current_price)
        trade_id = f"paper_{int(time.time() * 1000)}"

        # Calculate stop loss and take profit prices
        if direction == "long":
            stop_loss_price = entry_price * (1 - self.STOP_LOSS_PCT)
            take_profit_price = entry_price * (1 + self.TAKE_PROFIT_PCT)
        else:
            stop_loss_price = entry_price * (1 + self.STOP_LOSS_PCT)
            take_profit_price = entry_price * (1 - self.TAKE_PROFIT_PCT)

        # Store pending trade with FULL CONTEXT for loss analysis
        self._pending_paper_trades[trade_id] = {
            "trade_id": trade_id,
            "direction": direction,
            "entry_price": entry_price,
            "entry_time": datetime.now(timezone.utc),
            "position_size": float(position_size),
            "signal_score": signal.score,
            "signal_confidence": signal.confidence,
            "signal_sources": signal_sources or [],
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "resolved": False,
            # Track which strategy was used (for feedback loop)
            "strategy_used": self._last_trade_strategy or "mean_reversion",
            # === ENTRY CONDITIONS (for loss analysis) ===
            "entry_conditions": {
                "spread_pct": getattr(self, '_current_spread', None),
                "avg_spread_pct": self._avg_spread,
                "market_phase": self._get_market_phase(getattr(self, '_current_elapsed_secs', 0)),
                "elapsed_secs": getattr(self, '_current_elapsed_secs', None),
                "time_remaining": getattr(self, '_current_time_remaining', None),
                "btc_spot_price": self._cached_btc_spot,
                "btc_momentum": self._btc_momentum,
                "fear_greed": self._cached_fear_greed,
                "ticks_per_min": getattr(self, '_current_ticks_per_min', None),
                "is_volume_spike": self._is_volume_spike,
                "consecutive_losses": self._consecutive_losses.get(direction, 0),
                "direction": direction,  # Include direction for condition tracking
                # Price inefficiency metrics
                "price_vs_fair_value": self._calculate_fair_value_diff(entry_price),
                # Adaptive state at entry
                "direction_preference": self._direction_preference,
                "adaptive_spread_threshold": self._adaptive_spread_threshold,
                "adaptive_btc_alignment_required": self._adaptive_btc_alignment_required,
            },
        }

        logger.info("=" * 60)
        logger.info(f"📝 PAPER TRADE OPENED (tracking real price movement)")
        logger.info(f"   Trade ID: {trade_id}")
        logger.info(f"   Direction: {direction.upper()}")
        logger.info(f"   Entry: ${entry_price:.4f}")
        logger.info(f"   Stop Loss: ${stop_loss_price:.4f} ({self.STOP_LOSS_PCT*100:.0f}%)")
        logger.info(f"   Take Profit: ${take_profit_price:.4f} ({self.TAKE_PROFIT_PCT*100:.0f}%)")
        logger.info(f"   Pending trades: {len(self._pending_paper_trades)}")
        logger.info("=" * 60)

        # Don't record outcome yet - wait for actual price movement
        return

    def _check_pending_paper_trades(self, current_price: float):
        """Check all pending paper trades against current price for SL/TP hits."""
        trades_to_resolve = []

        for trade_id, trade in self._pending_paper_trades.items():
            if trade["resolved"]:
                continue

            direction = trade["direction"]
            entry_price = trade["entry_price"]
            stop_loss = trade["stop_loss_price"]
            take_profit = trade["take_profit_price"]
            entry_time = trade["entry_time"]

            # Update high/low tracking
            if current_price > trade["highest_price"]:
                trade["highest_price"] = current_price
            if current_price < trade["lowest_price"]:
                trade["lowest_price"] = current_price

            # Check for stop loss or take profit hit
            exit_reason = None
            exit_price = current_price

            if direction == "long":
                if current_price <= stop_loss:
                    exit_reason = "STOP_LOSS"
                    exit_price = stop_loss
                elif current_price >= take_profit:
                    exit_reason = "TAKE_PROFIT"
                    exit_price = take_profit
            else:  # short
                if current_price >= stop_loss:
                    exit_reason = "STOP_LOSS"
                    exit_price = stop_loss
                elif current_price <= take_profit:
                    exit_reason = "TAKE_PROFIT"
                    exit_price = take_profit

            # Also check for time-based exit (market close or max hold time)
            hold_time = (datetime.now(timezone.utc) - entry_time).total_seconds()
            if hold_time > 60 and exit_reason is None:  # 60 second max hold
                exit_reason = "TIME_EXIT"
                exit_price = current_price

            if exit_reason:
                trades_to_resolve.append((trade_id, trade, exit_reason, exit_price))

        # Resolve triggered trades
        for trade_id, trade, exit_reason, exit_price in trades_to_resolve:
            self._resolve_paper_trade(trade_id, trade, exit_reason, exit_price)

    def _resolve_paper_trade(self, trade_id: str, trade: dict, exit_reason: str, exit_price: float):
        """Resolve a paper trade with actual P&L and detailed loss analysis."""
        direction = trade["direction"]
        entry_price = trade["entry_price"]
        position_size = trade["position_size"]

        # Calculate actual P&L
        if direction == "long":
            pnl = position_size * (exit_price - entry_price) / entry_price
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = position_size * (entry_price - exit_price) / entry_price
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        is_win = pnl > 0
        outcome = "WIN" if is_win else "LOSS"

        # Mark as resolved
        trade["resolved"] = True
        trade["exit_price"] = exit_price
        trade["exit_reason"] = exit_reason
        trade["pnl"] = pnl
        trade["pnl_pct"] = pnl_pct
        trade["outcome"] = outcome

        # Update performance tracking
        self._direction_results[direction].append(1 if is_win else 0)
        if len(self._direction_results[direction]) > self.DIRECTION_WINDOW:
            self._direction_results[direction].pop(0)

        # Track consecutive losses
        if is_win:
            self._consecutive_losses[direction] = 0
            self._cooldown_start[direction] = None
        else:
            self._consecutive_losses[direction] += 1

        # Log result
        logger.info("=" * 60)
        if is_win:
            logger.info(f"✅ PAPER TRADE CLOSED - {outcome} ({exit_reason})")
        else:
            logger.info(f"❌ PAPER TRADE CLOSED - {outcome} ({exit_reason})")
        logger.info(f"   Direction: {direction.upper()}")
        logger.info(f"   Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}")
        logger.info(f"   P&L: ${pnl:+.4f} ({pnl_pct:+.2f}%)")
        logger.info(f"   Consecutive {direction} losses: {self._consecutive_losses[direction]}")

        # === DETAILED LOSS ANALYSIS ===
        loss_analysis = None
        if not is_win:
            loss_analysis = self._analyze_loss_reason(trade, exit_price, exit_reason)

            logger.info("-" * 60)
            logger.info("📊 LOSS ANALYSIS:")
            logger.info(f"   Primary Reason: {loss_analysis['primary_reason']}")
            logger.info(f"   Severity: {loss_analysis['severity'].upper()}")
            logger.info(f"   Price moved: {loss_analysis['actual_direction'].upper()} (needed: {loss_analysis['needed_direction'].upper()})")

            if loss_analysis['all_reasons']:
                logger.info("   Contributing Factors:")
                for reason in loss_analysis['all_reasons']:
                    logger.info(f"     • {reason}")

            logger.info(f"   💡 INSIGHT: {loss_analysis['insight']}")
            logger.info("-" * 60)

            # Track loss reasons for pattern analysis
            primary = loss_analysis['primary_reason']
            self._loss_reasons[primary] = self._loss_reasons.get(primary, 0) + 1

            # Store in loss history for pattern detection
            self._loss_history.append({
                "trade_id": trade_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "primary_reason": primary,
                "all_reasons": loss_analysis['all_reasons'],
                "entry_conditions": trade.get("entry_conditions", {}),
            })
            if len(self._loss_history) > self.MAX_LOSS_HISTORY:
                self._loss_history.pop(0)

            # Periodically log loss reason summary
            if len(self._loss_history) % 10 == 0 and self._loss_reasons:
                logger.info("=" * 60)
                logger.info("📈 LOSS PATTERN SUMMARY (last 10+ losses):")
                sorted_reasons = sorted(self._loss_reasons.items(), key=lambda x: x[1], reverse=True)
                for reason, count in sorted_reasons[:5]:
                    pct = count / sum(self._loss_reasons.values()) * 100
                    logger.info(f"   {reason}: {count} ({pct:.0f}%)")
                logger.info("=" * 60)

            # Add loss analysis to trade data
            trade["loss_analysis"] = loss_analysis

        # Log directional performance
        for d in ["long", "short"]:
            results = self._direction_results[d]
            if len(results) >= 3:
                wr = sum(results) / len(results) * 100
                logger.info(f"   {d.upper()} win rate: {wr:.0f}% (last {len(results)} trades)")
        logger.info("=" * 60)

        # === TRIGGER ADAPTIVE FEEDBACK LOOP ===
        self._update_adaptive_feedback(trade, is_win)

        # Record in paper_trades list
        self.paper_trades.append(
            PaperTrade(
                timestamp=datetime.now(timezone.utc),
                direction=direction.upper(),
                size_usd=float(position_size),
                price=float(entry_price),
                signal_score=trade.get("signal_score", 0),
                signal_confidence=trade.get("signal_confidence", 0),
                outcome=outcome,
            )
        )

        # Record with performance tracker
        self.performance_tracker.record_trade(
            trade_id=trade_id,
            direction=direction,
            entry_price=Decimal(str(entry_price)),
            exit_price=Decimal(str(exit_price)),
            size=Decimal(str(position_size)),
            entry_time=trade["entry_time"],
            exit_time=datetime.now(timezone.utc),
            signal_score=trade.get("signal_score", 0),
            signal_confidence=trade.get("signal_confidence", 0),
            metadata={
                "real_pnl": True,  # This is REAL P&L from actual price movement!
                "exit_reason": exit_reason,
                "signal_sources": trade.get("signal_sources", []),
                "consecutive_losses": self._consecutive_losses[direction],
                "loss_analysis": loss_analysis,  # Include loss analysis if present
            }
        )

        self._save_paper_trades()
        self._maybe_optimize_weights()

        # Remove from pending trades
        if trade_id in self._pending_paper_trades:
            del self._pending_paper_trades[trade_id]

        # Persist to PostgreSQL (async - run in executor)
        self.run_in_executor(lambda: self._persist_paper_trade_sync(trade, exit_price, exit_reason, pnl, pnl_pct, outcome))

    def _persist_paper_trade_sync(self, trade: dict, exit_price: float, exit_reason: str, pnl: float, pnl_pct: float, outcome: str):
        """Persist paper trade to database (sync wrapper)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._persist_paper_trade(trade, exit_price, exit_reason, pnl, pnl_pct, outcome))
        finally:
            loop.close()

    async def _persist_paper_trade(self, trade: dict, exit_price: float, exit_reason: str, pnl: float, pnl_pct: float, outcome: str):
        """Persist paper trade with REAL P&L to PostgreSQL."""
        if not self._db_initialized or not self._repo:
            return

        try:
            current_market = None
            market_slug = None
            if (self.current_instrument_index >= 0 and
                    self.current_instrument_index < len(self.all_btc_instruments)):
                current_market = self.all_btc_instruments[self.current_instrument_index]
                market_slug = current_market.get('slug')

            await self._repo.save_paper_trade(
                direction=trade["direction"].upper(),
                size_usd=Decimal(str(trade["position_size"])),
                entry_price=Decimal(str(trade["entry_price"])),
                signal_score=trade.get("signal_score", 0),
                signal_confidence=trade.get("signal_confidence", 0),
                exit_price=Decimal(str(exit_price)),
                outcome=outcome,
                pnl_usd=Decimal(str(pnl)),
                pnl_pct=pnl_pct,
                market_slug=market_slug,
                trend_direction="UP" if trade["direction"] == "long" else "DOWN",
                trend_confidence=trade.get("signal_confidence", 0),
                timeframe="5sec",
                btc_spot_price=Decimal(str(self._cached_btc_spot)) if self._cached_btc_spot else None,
                fear_greed_index=self._cached_fear_greed,
                metadata={
                    "real_pnl": True,  # REAL P&L from actual price movement!
                    "exit_reason": exit_reason,
                    "signal_sources": trade.get("signal_sources", []),
                    "consecutive_losses": self._consecutive_losses.get(trade["direction"], 0),
                    "burst_trade": self._burst_trades_count > 0,
                    "fear_greed_class": self._cached_fear_greed_class,
                    "spread_pct": getattr(self, '_current_spread', None),
                    "avg_spread_pct": self._avg_spread,
                    "elapsed_secs": getattr(self, '_current_elapsed_secs', None),
                    "time_remaining_secs": getattr(self, '_current_time_remaining', None),
                    "market_phase": self._get_market_phase(getattr(self, '_current_elapsed_secs', 0)),
                    "btc_momentum_pct": self._btc_momentum,
                    "stop_loss_price": trade.get("stop_loss_price"),
                    "take_profit_price": trade.get("take_profit_price"),
                    "highest_price": trade.get("highest_price"),
                    "lowest_price": trade.get("lowest_price"),
                    # Loss analysis (only present on losing trades)
                    "loss_analysis": trade.get("loss_analysis"),
                    "entry_conditions": trade.get("entry_conditions"),
                },
            )
            logger.debug(f"5-sec paper trade with REAL P&L persisted to PostgreSQL")
        except Exception as e:
            logger.warning(f"Failed to persist 5-sec paper trade: {e}")

    async def _place_real_order(self, signal, position_size, current_price, direction, signal_sources=None):
        """
        Override parent's _place_real_order to include 5-sec specific data.
        """
        from nautilus_trader.model.enums import OrderSide, TimeInForce
        from nautilus_trader.model.identifiers import ClientOrderId
        from nautilus_trader.model.objects import Quantity

        if not self.instrument_id:
            logger.error("No instrument available")
            return

        try:
            logger.info("=" * 80)
            logger.info("LIVE MODE - PLACING REAL 5-SEC ORDER!")
            logger.info("=" * 80)

            # On Polymarket, both UP and DOWN are BUY orders
            side = OrderSide.BUY

            if direction == "long":
                trade_instrument_id = getattr(self, '_yes_instrument_id', self.instrument_id)
                trade_label = "YES (UP)"
            else:
                no_id = getattr(self, '_no_instrument_id', None)
                if no_id is None:
                    logger.warning("NO token instrument not found — cannot bet DOWN. Skipping.")
                    return
                trade_instrument_id = no_id
                trade_label = "NO (DOWN)"

            instrument = self.cache.instrument(trade_instrument_id)
            if not instrument:
                logger.error(f"Instrument not in cache: {trade_instrument_id}")
                return

            logger.info(f"Buying {trade_label} token: {trade_instrument_id}")

            trade_price = float(current_price)
            max_usd_amount = float(position_size)
            precision = instrument.size_precision

            # Calculate token quantity
            min_qty_val = float(getattr(instrument, 'min_quantity', None) or 5.0)
            token_qty = max(min_qty_val, 5.0)
            token_qty = round(token_qty, precision)

            logger.info(f"BUY {trade_label}: qty={token_qty:.6f} (${max_usd_amount:.2f} USD)")

            qty = Quantity(token_qty, precision=precision)
            timestamp_ms = int(time.time() * 1000)
            unique_id = f"BTC-5SEC-${max_usd_amount:.0f}-{timestamp_ms}"

            if self.USE_MAKER_ORDERS:
                # === MAKER ORDER (0% FEES!) ===
                # Place limit order slightly better than current price
                # This sits on the book and earns maker rebates
                from nautilus_trader.model.objects import Price

                price_precision = instrument.price_precision

                if direction == "long":
                    # Buying YES: bid slightly below mid to be maker
                    limit_price = trade_price * (1 - self.MAKER_OFFSET_PCT)
                else:
                    # Buying NO: the NO price is 1 - YES price
                    # Bid slightly below current NO price
                    limit_price = trade_price * (1 - self.MAKER_OFFSET_PCT)

                limit_price = round(limit_price, price_precision)
                price_obj = Price(limit_price, precision=price_precision)

                order = self.order_factory.limit(
                    instrument_id=trade_instrument_id,
                    order_side=side,
                    quantity=qty,
                    price=price_obj,
                    client_order_id=ClientOrderId(unique_id),
                    time_in_force=TimeInForce.GTC,  # Good Till Cancel
                    post_only=True,  # Ensure we're maker, reject if would be taker
                )
                logger.info(f"📝 MAKER ORDER (0% fees): Limit ${limit_price:.4f}")
            else:
                # === TAKER ORDER (standard market order) ===
                order = self.order_factory.market(
                    instrument_id=trade_instrument_id,
                    order_side=side,
                    quantity=qty,
                    client_order_id=ClientOrderId(unique_id),
                    quote_quantity=False,
                    time_in_force=TimeInForce.IOC,
                )
                logger.info(f"⚡ TAKER ORDER (instant fill): Market order")

            self.submit_order(order)

            # === TRACK POSITION FOR STOP LOSS / TAKE PROFIT ===
            entry_price = trade_price
            if direction == "long":
                stop_loss_price = entry_price * (1 - self.STOP_LOSS_PCT)
                take_profit_price = entry_price * (1 + self.TAKE_PROFIT_PCT)
            else:
                stop_loss_price = entry_price * (1 + self.STOP_LOSS_PCT)
                take_profit_price = entry_price * (1 - self.TAKE_PROFIT_PCT)

            self._open_positions[unique_id] = {
                "direction": direction,
                "entry_price": entry_price,
                "quantity": token_qty,
                "size_usd": max_usd_amount,
                "entry_time": datetime.now(timezone.utc),
                "instrument_id": trade_instrument_id,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "trailing_stop_price": None,
                "highest_price": entry_price,
                "lowest_price": entry_price,
            }

            logger.info(f"REAL 5-SEC ORDER SUBMITTED!")
            logger.info(f"  Order ID: {unique_id}")
            logger.info(f"  Direction: {trade_label}")
            logger.info(f"  Stop Loss: ${stop_loss_price:.4f} ({self.STOP_LOSS_PCT*100:.0f}%)")
            logger.info(f"  Take Profit: ${take_profit_price:.4f} ({self.TAKE_PROFIT_PCT*100:.0f}%)")
            logger.info(f"  Token Quantity: {token_qty:.6f}")
            logger.info(f"  Estimated Cost: ~${max_usd_amount:.2f}")
            logger.info(f"  Price: ${trade_price:.4f}")
            logger.info("=" * 80)

            self._track_order_event("placed")

            # Persist trade to PostgreSQL with 5-sec specific data
            if self._db_initialized and self._repo:
                try:
                    current_market = None
                    market_slug = None
                    if (self.current_instrument_index >= 0 and
                            self.current_instrument_index < len(self.all_btc_instruments)):
                        current_market = self.all_btc_instruments[self.current_instrument_index]
                        market_slug = current_market.get('slug')

                    await self._repo.save_trade(
                        trade_id=unique_id,
                        direction=direction,
                        entry_price=current_price,
                        size_usd=position_size,
                        side="BUY",
                        is_simulation=False,
                        quantity=Decimal(str(token_qty)),
                        signal_score=signal.score,
                        signal_confidence=signal.confidence,
                        signal_direction=str(signal.direction.value) if hasattr(signal.direction, 'value') else str(signal.direction),
                        trend_direction="UP" if direction == "long" else "DOWN",
                        trend_confidence=signal.confidence,
                        price_at_decision=current_price,
                        market_slug=market_slug,
                        instrument_id=str(trade_instrument_id),
                        yes_token_id=self._yes_token_id,
                        no_token_id=str(self._no_instrument_id) if self._no_instrument_id else None,
                        order_id=unique_id,
                        metadata={
                            "timeframe": "5sec",
                            "trade_label": trade_label,
                            "token_qty": float(token_qty),
                            "estimated_cost_usd": max_usd_amount,
                            "spot_price": self._cached_btc_spot,
                            "fear_greed": self._cached_fear_greed,
                            "signal_sources": signal_sources or [],
                            # 5-sec specific tracking
                            "spread_pct": getattr(self, '_current_spread', None),
                            "avg_spread_pct": self._avg_spread,
                            "elapsed_secs": getattr(self, '_current_elapsed_secs', None),
                            "time_remaining_secs": getattr(self, '_current_time_remaining', None),
                            "market_phase": self._get_market_phase(getattr(self, '_current_elapsed_secs', 0)),
                            "btc_momentum_pct": self._btc_momentum,
                            "ticks_per_min": getattr(self, '_current_ticks_per_min', None),
                            "is_volume_spike": self._is_volume_spike,
                            "consecutive_losses": self._consecutive_losses.get(direction, 0),
                            "burst_trade": self._burst_trades_count > 0,
                        },
                    )
                    logger.debug(f"5-sec live trade persisted to PostgreSQL: {unique_id}")
                except Exception as e:
                    logger.warning(f"Failed to persist 5-sec trade: {e}")

        except Exception as e:
            logger.error(f"Error placing 5-sec real order: {e}")
            import traceback
            traceback.print_exc()
            self._track_order_event("rejected")

    def _maybe_optimize_weights(self):
        if not self.learning_enabled:
            return
        if len(self.paper_trades) < self.learning_every_trades:
            return
        if len(self.paper_trades) % self.learning_every_trades != 0:
            return
        try:
            self.run_in_executor(self._optimize_weights_sync)
        except Exception as e:
            logger.warning(f"Failed to schedule learning optimization: {e}")

    def _optimize_weights_sync(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.learning_engine.optimize_weights())
        except Exception as e:
            logger.warning(f"Learning optimization failed: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass


def run_five_sec_bot(enable_grafana: bool = True, enable_api: bool = True, learning_enabled: bool = True, simulation: bool = True, test_mode: bool = False):
    print("=" * 80)
    if simulation:
        print("FIVE-SECOND POLYMARKET BTC BOT - SIMULATION MODE")
        print("🚀 AGGRESSIVE LEARNING MODE - HIGH VOLUME TRADING")
        print("   • Trading every 2 seconds")
        print("   • Wide price range (0.10-0.90)")
        print("   • Minimal filters - learning from ALL trades")
        print("   • No direction skipping")
        print("   • No strategy disabling")
    else:
        print("FIVE-SECOND POLYMARKET BTC BOT - *** LIVE TRADING ***")
        print("🚀 AGGRESSIVE MODE - HIGH VOLUME")
        print("=" * 80)
        print("WARNING: REAL MONEY AT RISK! Trades will execute on Polymarket.")
        print("Press Ctrl+C within 5 seconds to abort...")
        import time as _time
        try:
            _time.sleep(5)
        except KeyboardInterrupt:
            print("\nAborted by user.")
            return
    print("=" * 80)

    redis_client = init_redis()
    if redis_client:
        try:
            mode_value = '1' if simulation else '0'
            redis_client.set('btc_trading:simulation_mode', mode_value)
        except Exception:
            pass

    print(f"\nConfiguration:")
    print(f"  Mode: {'SIMULATION' if simulation else 'LIVE TRADING'}")
    print(f"  Redis Control: {'Enabled' if redis_client else 'Disabled'}")
    print(f"  Grafana: {'Enabled' if enable_grafana else 'Disabled'}")
    print(f"  API Server: {'Enabled' if enable_api else 'Disabled'}")
    print(f"  Max Trade Size: ${os.getenv('MARKET_BUY_USD', '1.00')}")
    print(f"  Quote stability gate: {QUOTE_STABILITY_REQUIRED} valid ticks")
    print()

    now = datetime.now(timezone.utc)

    # Generate slugs for current + next 24 hours (same approach as 15m bot)
    unix_interval_start = (int(now.timestamp()) // MARKET_INTERVAL_SECONDS) * MARKET_INTERVAL_SECONDS
    btc_slugs = []
    for i in range(-1, 97):  # include 1 prior interval (in case we're just after boundary)
        timestamp = unix_interval_start + (i * MARKET_INTERVAL_SECONDS)
        btc_slugs.append(f"btc-updown-15m-{timestamp}")

    filters = {
        "active": True,
        "closed": False,
        "archived": False,
        "slug": tuple(btc_slugs),
        "limit": 100,
    }

    logger.info("=" * 80)
    logger.info("LOADING BTC 15-MIN MARKETS BY SLUG (5-SEC MODE)")
    logger.info(f"  Interval start: {unix_interval_start} | Count: {len(btc_slugs)}")
    logger.info(f"  First: {btc_slugs[0]}  Last: {btc_slugs[-1]}")
    logger.info("=" * 80)

    instrument_cfg = InstrumentProviderConfig(
        load_all=True,
        filters=filters,
        use_gamma_markets=True,
    )

    poly_data_cfg = PolymarketDataClientConfig(
        private_key=os.getenv("POLYMARKET_PK"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    poly_exec_cfg = PolymarketExecClientConfig(
        private_key=os.getenv("POLYMARKET_PK"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    # Note: "Dropping QuoteTick" warnings are normal when markets have no bid liquidity.
    # They reduce once the market gets activity. Doesn't affect trading - just cosmetic spam.
    config = TradingNodeConfig(
        environment="live",
        trader_id="BTC-5SEC-SIM-001",
        logging=LoggingConfig(
            log_level="INFO",
            log_directory="./logs/nautilus",
        ),
        data_engine=LiveDataEngineConfig(
            qsize=6000,
            debug=False,
        ),
        exec_engine=LiveExecEngineConfig(qsize=6000),
        risk_engine=LiveRiskEngineConfig(bypass=True),
        data_clients={POLYMARKET: poly_data_cfg},
        exec_clients={POLYMARKET: poly_exec_cfg},
    )

    strategy = FiveSecondBTCStrategy(
        redis_client=redis_client,
        enable_grafana=enable_grafana,
        test_mode=test_mode,
        enable_api=enable_api,
        simulation_default=simulation,
        force_simulation=simulation,
    )
    strategy.learning_enabled = learning_enabled

    print("\nBuilding Nautilus node...")
    node = TradingNode(config=config)
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
    node.trader.add_strategy(strategy)
    node.build()
    logger.info("Nautilus node built successfully")

    print()
    print("=" * 80)
    print("BOT STARTING")
    print("=" * 80)

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        node.dispose()
        logger.info("Bot stopped")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Five-Second BTC Bot")
    parser.add_argument("--live", action="store_true",
                        help="Run in LIVE mode (real money at risk!). Default is simulation.")
    parser.add_argument("--no-grafana", action="store_true", help="Disable Grafana metrics")
    parser.add_argument("--no-api", action="store_true", help="Disable API server")
    parser.add_argument("--no-learning", action="store_true", help="Disable learning optimization")
    parser.add_argument("--test-mode", action="store_true",
                        help="Run in TEST MODE (trade every minute for faster testing)")
    args = parser.parse_args()

    enable_grafana = not args.no_grafana
    enable_api = not args.no_api

    # --test-mode ALWAYS forces simulation even if --live is also passed
    if args.test_mode:
        simulation = True
    else:
        simulation = not args.live

    run_five_sec_bot(
        enable_grafana=enable_grafana,
        enable_api=enable_api,
        learning_enabled=not args.no_learning,
        simulation=simulation,
        test_mode=args.test_mode,
    )


if __name__ == "__main__":
    main()
