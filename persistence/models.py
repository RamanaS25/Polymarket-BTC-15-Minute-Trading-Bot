"""
PostgreSQL Models for Trading Bot Persistence

Comprehensive schema capturing all trade data, signals, market context,
risk metrics, and learning engine data for analytics and strategy optimization.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List
from enum import Enum

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime,
    Numeric, Text, ForeignKey, Index, UniqueConstraint, Enum as SQLEnum,
    JSON, func
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()


# =============================================================================
# ENUMS
# =============================================================================

class SignalDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class SignalStrength(str, Enum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"


class SignalType(str, Enum):
    SPIKE_DETECTED = "SPIKE_DETECTED"
    MEAN_REVERSION = "MEAN_REVERSION"
    MOMENTUM = "MOMENTUM"
    SENTIMENT_SHIFT = "SENTIMENT_SHIFT"
    VOLUME_SURGE = "VOLUME_SURGE"
    PRICE_DIVERGENCE = "PRICE_DIVERGENCE"
    ANOMALY = "ANOMALY"
    ORDERBOOK_IMBALANCE = "ORDERBOOK_IMBALANCE"
    TICK_VELOCITY = "TICK_VELOCITY"
    PCR_SIGNAL = "PCR_SIGNAL"


class TradeOutcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    PENDING = "PENDING"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertType(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"


# =============================================================================
# MARKET DATA
# =============================================================================

class MarketSnapshot(Base):
    """
    Point-in-time snapshot of market conditions.
    Captured at each trading decision for full context reconstruction.
    """
    __tablename__ = "market_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # Market identification
    market_slug = Column(String(128), nullable=False, index=True)
    instrument_id = Column(String(256), nullable=False)
    yes_token_id = Column(String(128))
    no_token_id = Column(String(128))
    market_start_time = Column(DateTime(timezone=True))
    market_end_time = Column(DateTime(timezone=True))

    # Polymarket prices
    bid_price = Column(Numeric(10, 6), nullable=False)
    ask_price = Column(Numeric(10, 6), nullable=False)
    mid_price = Column(Numeric(10, 6), nullable=False)
    spread = Column(Numeric(10, 6))

    # External prices
    btc_spot_price = Column(Numeric(16, 2))  # BTC-USD from Coinbase
    btc_spot_source = Column(String(32))  # "coinbase", "binance", etc.

    # Sentiment data
    fear_greed_index = Column(Float)  # 0-100
    fear_greed_classification = Column(String(32))  # "extreme_fear", etc.

    # Order book data (from CLOB API)
    orderbook_bid_volume_usd = Column(Numeric(16, 2))
    orderbook_ask_volume_usd = Column(Numeric(16, 2))
    orderbook_imbalance = Column(Float)  # -1.0 to +1.0
    orderbook_bid_wall_usd = Column(Numeric(16, 2))
    orderbook_ask_wall_usd = Column(Numeric(16, 2))

    # Computed metrics
    price_sma_20 = Column(Numeric(10, 6))
    price_deviation_pct = Column(Float)
    momentum_5p = Column(Float)  # 5-period momentum
    volatility = Column(Float)

    # Tick velocity (60s and 30s)
    tick_velocity_60s = Column(Float)
    tick_velocity_30s = Column(Float)
    tick_acceleration = Column(Float)

    # Deribit PCR data
    deribit_pcr = Column(Float)  # Put/Call ratio
    deribit_overall_pcr = Column(Float)
    deribit_short_put_oi = Column(Numeric(16, 2))
    deribit_short_call_oi = Column(Numeric(16, 2))

    # Raw JSON for anything else
    extra_data = Column(JSONB)

    __table_args__ = (
        Index("ix_market_snapshots_ts_slug", "timestamp", "market_slug"),
    )


# =============================================================================
# SIGNALS
# =============================================================================

class TradingSignal(Base):
    """
    Individual signal from a signal processor.
    """
    __tablename__ = "trading_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # Signal identification
    source = Column(String(64), nullable=False, index=True)  # Processor name
    signal_type = Column(String(32), nullable=False)
    direction = Column(String(16), nullable=False)  # BULLISH, BEARISH, NEUTRAL
    strength = Column(String(16))  # WEAK, MODERATE, STRONG, VERY_STRONG

    # Signal metrics
    score = Column(Float, nullable=False)  # 0-100
    confidence = Column(Float, nullable=False)  # 0.0-1.0
    current_price = Column(Numeric(10, 6), nullable=False)
    target_price = Column(Numeric(10, 6))
    stop_loss_price = Column(Numeric(10, 6))

    # Market context at signal time
    market_snapshot_id = Column(BigInteger, ForeignKey("market_snapshots.id"))

    # Source-specific metadata (JSONB for flexibility)
    signal_metadata = Column(JSONB)

    # Link to fused signal if this was part of fusion
    fused_signal_id = Column(BigInteger, ForeignKey("fused_signals.id"))

    market_snapshot = relationship("MarketSnapshot", backref="signals")

    __table_args__ = (
        Index("ix_trading_signals_source_ts", "source", "timestamp"),
        Index("ix_trading_signals_direction", "direction"),
    )


class FusedSignal(Base):
    """
    Combined signal from the fusion engine.
    Links to individual contributing signals.
    """
    __tablename__ = "fused_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # Fusion result
    direction = Column(String(16), nullable=False)
    score = Column(Float, nullable=False)  # 0-100
    confidence = Column(Float, nullable=False)  # 0.0-1.0
    num_signals = Column(Integer, nullable=False)

    # Fusion breakdown
    bullish_contribution = Column(Float)
    bearish_contribution = Column(Float)
    total_contribution = Column(Float)
    num_bullish = Column(Integer)
    num_bearish = Column(Integer)

    # Quality flags
    is_strong = Column(Boolean)  # score >= 70
    is_actionable = Column(Boolean)  # score >= 60 AND confidence >= 0.6

    # Weights used in fusion
    weights_snapshot = Column(JSONB)  # {"OrderBookImbalance": 0.30, ...}

    # Market context
    market_snapshot_id = Column(BigInteger, ForeignKey("market_snapshots.id"))
    current_price = Column(Numeric(10, 6))

    # Link to trade if this signal triggered one
    trade_id = Column(BigInteger, ForeignKey("trades.id"))

    contributing_signals = relationship(
        "TradingSignal",
        backref="fused_signal",
        foreign_keys="TradingSignal.fused_signal_id"
    )
    market_snapshot = relationship("MarketSnapshot", backref="fused_signals")


# =============================================================================
# TRADES
# =============================================================================

class Trade(Base):
    """
    Complete trade record with full context.
    """
    __tablename__ = "trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_id = Column(String(128), unique=True, nullable=False, index=True)

    # Trade timing
    entry_time = Column(DateTime(timezone=True), nullable=False, index=True)
    exit_time = Column(DateTime(timezone=True))
    duration_seconds = Column(Float)

    # Trade details
    direction = Column(String(16), nullable=False)  # "long" or "short"
    side = Column(String(8), nullable=False)  # "BUY" or "SELL"
    is_simulation = Column(Boolean, nullable=False, default=True)

    # Prices
    entry_price = Column(Numeric(10, 6), nullable=False)
    exit_price = Column(Numeric(10, 6))
    target_price = Column(Numeric(10, 6))
    stop_loss_price = Column(Numeric(10, 6))

    # Position sizing
    size_usd = Column(Numeric(16, 2), nullable=False)
    quantity = Column(Numeric(20, 8))  # Token quantity

    # P&L
    pnl_usd = Column(Numeric(16, 4))
    pnl_pct = Column(Float)
    outcome = Column(String(16))  # WIN, LOSS, BREAKEVEN, PENDING

    # Signal that triggered the trade
    signal_score = Column(Float)
    signal_confidence = Column(Float)
    signal_direction = Column(String(16))
    fused_signal_id = Column(BigInteger, ForeignKey("fused_signals.id"))

    # Trend filter data (late-window trading logic)
    trend_direction = Column(String(16))  # "UP", "DOWN", "NEUTRAL"
    trend_confidence = Column(Float)
    price_at_decision = Column(Numeric(10, 6))

    # Market context
    market_slug = Column(String(128), index=True)
    instrument_id = Column(String(256))
    yes_token_id = Column(String(128))
    no_token_id = Column(String(128))
    market_snapshot_id = Column(BigInteger, ForeignKey("market_snapshots.id"))

    # Order execution details
    order_id = Column(String(256))
    fill_price = Column(Numeric(10, 6))
    slippage_bps = Column(Float)  # Basis points
    execution_latency_ms = Column(Float)

    # Exit details
    exit_reason = Column(String(32))  # "market_close", "stop_loss", "take_profit", "signal"

    # Full trade metadata
    trade_metadata = Column(JSONB)

    fused_signal = relationship("FusedSignal", backref="trades", foreign_keys=[fused_signal_id])
    market_snapshot = relationship("MarketSnapshot", backref="trades")

    __table_args__ = (
        Index("ix_trades_entry_time", "entry_time"),
        Index("ix_trades_outcome", "outcome"),
        Index("ix_trades_direction", "direction"),
    )


class OrderEvent(Base):
    """
    Individual order lifecycle events (placed, filled, rejected, etc.)
    """
    __tablename__ = "order_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    trade_id = Column(BigInteger, ForeignKey("trades.id"), index=True)
    order_id = Column(String(256), nullable=False, index=True)

    event_type = Column(String(32), nullable=False)  # "placed", "filled", "rejected", "cancelled"
    side = Column(String(8))  # "BUY" or "SELL"

    # Fill details (if applicable)
    fill_price = Column(Numeric(10, 6))
    fill_quantity = Column(Numeric(20, 8))
    fill_usd = Column(Numeric(16, 4))

    # Rejection details (if applicable)
    rejection_reason = Column(Text)

    # Raw event data
    raw_event = Column(JSONB)

    trade = relationship("Trade", backref="order_events")


# =============================================================================
# POSITIONS & RISK
# =============================================================================

class Position(Base):
    """
    Active or closed position tracking.
    """
    __tablename__ = "positions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    position_id = Column(String(128), unique=True, nullable=False, index=True)

    # Timing
    opened_at = Column(DateTime(timezone=True), nullable=False, index=True)
    closed_at = Column(DateTime(timezone=True))
    is_open = Column(Boolean, nullable=False, default=True, index=True)

    # Position details
    direction = Column(String(16), nullable=False)
    instrument_id = Column(String(256))
    market_slug = Column(String(128))

    # Sizing
    entry_price = Column(Numeric(10, 6), nullable=False)
    current_price = Column(Numeric(10, 6))
    exit_price = Column(Numeric(10, 6))
    size_usd = Column(Numeric(16, 2), nullable=False)
    quantity = Column(Numeric(20, 8))

    # P&L tracking
    unrealized_pnl = Column(Numeric(16, 4))
    realized_pnl = Column(Numeric(16, 4))

    # Risk management
    stop_loss = Column(Numeric(10, 6))
    take_profit = Column(Numeric(10, 6))
    risk_level = Column(String(16))  # LOW, MEDIUM, HIGH, CRITICAL

    # Link to trade
    trade_id = Column(BigInteger, ForeignKey("trades.id"))

    trade = relationship("Trade", backref="positions")


class RiskAlert(Base):
    """
    Risk management alerts triggered during trading.
    """
    __tablename__ = "risk_alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    alert_type = Column(String(32), nullable=False, index=True)
    risk_level = Column(String(16), nullable=False)
    message = Column(Text, nullable=False)

    # Trigger details
    current_value = Column(Numeric(16, 4))
    threshold_value = Column(Numeric(16, 4))

    # Related position/trade
    position_id = Column(BigInteger, ForeignKey("positions.id"))
    trade_id = Column(BigInteger, ForeignKey("trades.id"))

    # Was it acted upon?
    was_acknowledged = Column(Boolean, default=False)
    action_taken = Column(String(64))

    position = relationship("Position", backref="risk_alerts")
    trade = relationship("Trade", backref="risk_alerts")


class RiskSnapshot(Base):
    """
    Portfolio-level risk metrics snapshot.
    Captured periodically for monitoring/analytics.
    """
    __tablename__ = "risk_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # P&L
    daily_pnl = Column(Numeric(16, 4))
    total_pnl = Column(Numeric(16, 4))
    realized_pnl = Column(Numeric(16, 4))
    unrealized_pnl = Column(Numeric(16, 4))

    # Balance tracking
    current_balance = Column(Numeric(16, 4))
    peak_balance = Column(Numeric(16, 4))
    drawdown_pct = Column(Float)

    # Exposure
    total_exposure = Column(Numeric(16, 4))
    exposure_utilization_pct = Column(Float)
    open_positions_count = Column(Integer)

    # Limits
    max_daily_loss_limit = Column(Numeric(16, 4))
    max_position_size = Column(Numeric(16, 4))
    max_total_exposure = Column(Numeric(16, 4))
    max_drawdown_pct_limit = Column(Float)

    # Trade counts
    daily_trade_count = Column(Integer)
    total_trade_count = Column(Integer)


# =============================================================================
# PERFORMANCE METRICS
# =============================================================================

class PerformanceMetrics(Base):
    """
    Periodic performance snapshots for analytics/Grafana.
    """
    __tablename__ = "performance_metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # P&L
    total_pnl = Column(Numeric(16, 4))
    realized_pnl = Column(Numeric(16, 4))
    unrealized_pnl = Column(Numeric(16, 4))

    # Trade statistics
    total_trades = Column(Integer)
    winning_trades = Column(Integer)
    losing_trades = Column(Integer)
    win_rate = Column(Float)

    # Return metrics
    roi_pct = Column(Float)
    sharpe_ratio = Column(Float)
    sortino_ratio = Column(Float)
    max_drawdown_pct = Column(Float)

    # Position metrics
    open_positions = Column(Integer)
    avg_position_size = Column(Numeric(16, 4))
    avg_hold_time_seconds = Column(Float)

    # Risk metrics
    total_exposure = Column(Numeric(16, 4))
    risk_utilization_pct = Column(Float)

    # Signal quality
    avg_signal_score = Column(Float)
    avg_signal_confidence = Column(Float)

    # By direction
    long_trades = Column(Integer)
    short_trades = Column(Integer)
    long_win_rate = Column(Float)
    short_win_rate = Column(Float)


class DailyPerformance(Base):
    """
    Daily aggregated performance for reporting.
    """
    __tablename__ = "daily_performance"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    date = Column(DateTime(timezone=True), nullable=False, unique=True, index=True)

    # P&L
    daily_pnl = Column(Numeric(16, 4))
    cumulative_pnl = Column(Numeric(16, 4))

    # Trade counts
    trades_count = Column(Integer)
    win_count = Column(Integer)
    loss_count = Column(Integer)
    win_rate = Column(Float)

    # Returns
    daily_roi_pct = Column(Float)
    max_intraday_drawdown_pct = Column(Float)

    # Balance
    start_equity = Column(Numeric(16, 4))
    end_equity = Column(Numeric(16, 4))

    # Best/worst trades
    best_trade_pnl = Column(Numeric(16, 4))
    worst_trade_pnl = Column(Numeric(16, 4))

    # By direction
    long_pnl = Column(Numeric(16, 4))
    short_pnl = Column(Numeric(16, 4))

    # Average signal quality
    avg_signal_score = Column(Float)
    avg_signal_confidence = Column(Float)


# =============================================================================
# LEARNING ENGINE
# =============================================================================

class SignalPerformance(Base):
    """
    Per-signal-source performance analytics for the learning engine.
    """
    __tablename__ = "signal_performance"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    source_name = Column(String(64), nullable=False, index=True)
    lookback_days = Column(Integer)

    # Trade statistics
    total_trades = Column(Integer)
    winning_trades = Column(Integer)
    losing_trades = Column(Integer)
    win_rate = Column(Float)

    # P&L
    total_pnl = Column(Numeric(16, 4))
    avg_pnl = Column(Numeric(16, 4))
    profit_factor = Column(Float)  # Total wins / Total losses

    # Signal quality
    avg_score = Column(Float)
    avg_confidence = Column(Float)

    # Recommendation
    recommended_weight = Column(Float)
    weight_change_reason = Column(Text)


class WeightAdjustment(Base):
    """
    History of fusion engine weight adjustments.
    """
    __tablename__ = "weight_adjustments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    signal_source = Column(String(64), nullable=False, index=True)
    old_weight = Column(Float, nullable=False)
    new_weight = Column(Float, nullable=False)

    # What drove the change
    performance_score = Column(Float)
    learning_rate = Column(Float)
    reason = Column(Text)

    # Who/what made the change
    adjusted_by = Column(String(64))  # "learning_engine", "manual", etc.


# =============================================================================
# PAPER TRADES (for simulation tracking)
# =============================================================================

class PaperTrade(Base):
    """
    Paper/simulation trade records.
    Mirrors the PaperTrade dataclass in bot.py.
    """
    __tablename__ = "paper_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    direction = Column(String(16), nullable=False)
    size_usd = Column(Numeric(16, 2), nullable=False)
    entry_price = Column(Numeric(10, 6), nullable=False)
    exit_price = Column(Numeric(10, 6))

    signal_score = Column(Float)
    signal_confidence = Column(Float)

    outcome = Column(String(16))  # WIN, LOSS, PENDING
    pnl_usd = Column(Numeric(16, 4))
    pnl_pct = Column(Float)

    # Market context
    market_slug = Column(String(128))
    btc_spot_price = Column(Numeric(16, 2))
    fear_greed_index = Column(Float)

    # Trend filter data
    trend_direction = Column(String(16))
    trend_confidence = Column(Float)

    # Timeframe (5sec, 15min, etc.)
    timeframe = Column(String(16))

    # Full paper trade metadata
    paper_metadata = Column(JSONB)


# =============================================================================
# TICK DATA (optional - for backtesting)
# =============================================================================

class TickData(Base):
    """
    Raw tick data for backtesting.
    Only store if needed for strategy development.
    """
    __tablename__ = "tick_data"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    market_slug = Column(String(128), nullable=False, index=True)
    instrument_id = Column(String(256))

    bid_price = Column(Numeric(10, 6))
    ask_price = Column(Numeric(10, 6))
    mid_price = Column(Numeric(10, 6))

    bid_size = Column(Numeric(20, 8))
    ask_size = Column(Numeric(20, 8))

    __table_args__ = (
        Index("ix_tick_data_ts_slug", "timestamp", "market_slug"),
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def utc_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)
