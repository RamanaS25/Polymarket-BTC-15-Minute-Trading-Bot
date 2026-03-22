-- Trading Bot Database Schema
-- Run this in HeidiSQL connected to trading_bot database

-- Market Snapshots
CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    market_slug VARCHAR(128) NOT NULL,
    instrument_id VARCHAR(256) NOT NULL,
    yes_token_id VARCHAR(128),
    no_token_id VARCHAR(128),
    market_start_time TIMESTAMPTZ,
    market_end_time TIMESTAMPTZ,
    bid_price NUMERIC(10, 6) NOT NULL,
    ask_price NUMERIC(10, 6) NOT NULL,
    mid_price NUMERIC(10, 6) NOT NULL,
    spread NUMERIC(10, 6),
    btc_spot_price NUMERIC(16, 2),
    btc_spot_source VARCHAR(32),
    fear_greed_index DOUBLE PRECISION,
    fear_greed_classification VARCHAR(32),
    orderbook_bid_volume_usd NUMERIC(16, 2),
    orderbook_ask_volume_usd NUMERIC(16, 2),
    orderbook_imbalance DOUBLE PRECISION,
    orderbook_bid_wall_usd NUMERIC(16, 2),
    orderbook_ask_wall_usd NUMERIC(16, 2),
    price_sma_20 NUMERIC(10, 6),
    price_deviation_pct DOUBLE PRECISION,
    momentum_5p DOUBLE PRECISION,
    volatility DOUBLE PRECISION,
    tick_velocity_60s DOUBLE PRECISION,
    tick_velocity_30s DOUBLE PRECISION,
    tick_acceleration DOUBLE PRECISION,
    deribit_pcr DOUBLE PRECISION,
    deribit_overall_pcr DOUBLE PRECISION,
    deribit_short_put_oi NUMERIC(16, 2),
    deribit_short_call_oi NUMERIC(16, 2),
    extra_data JSONB
);

CREATE INDEX IF NOT EXISTS ix_market_snapshots_timestamp ON market_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS ix_market_snapshots_slug ON market_snapshots (market_slug);

-- Fused Signals (create before trading_signals due to FK)
CREATE TABLE IF NOT EXISTS fused_signals (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    direction VARCHAR(16) NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    num_signals INTEGER NOT NULL,
    bullish_contribution DOUBLE PRECISION,
    bearish_contribution DOUBLE PRECISION,
    total_contribution DOUBLE PRECISION,
    num_bullish INTEGER,
    num_bearish INTEGER,
    is_strong BOOLEAN,
    is_actionable BOOLEAN,
    weights_snapshot JSONB,
    market_snapshot_id BIGINT REFERENCES market_snapshots(id),
    current_price NUMERIC(10, 6),
    trade_id BIGINT
);

CREATE INDEX IF NOT EXISTS ix_fused_signals_timestamp ON fused_signals (timestamp);

-- Trading Signals
CREATE TABLE IF NOT EXISTS trading_signals (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    source VARCHAR(64) NOT NULL,
    signal_type VARCHAR(32) NOT NULL,
    direction VARCHAR(16) NOT NULL,
    strength VARCHAR(16),
    score DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    current_price NUMERIC(10, 6) NOT NULL,
    target_price NUMERIC(10, 6),
    stop_loss_price NUMERIC(10, 6),
    market_snapshot_id BIGINT REFERENCES market_snapshots(id),
    signal_metadata JSONB,
    fused_signal_id BIGINT REFERENCES fused_signals(id)
);

CREATE INDEX IF NOT EXISTS ix_trading_signals_timestamp ON trading_signals (timestamp);
CREATE INDEX IF NOT EXISTS ix_trading_signals_source ON trading_signals (source);
CREATE INDEX IF NOT EXISTS ix_trading_signals_direction ON trading_signals (direction);

-- Trades
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id VARCHAR(128) UNIQUE NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    exit_time TIMESTAMPTZ,
    duration_seconds DOUBLE PRECISION,
    direction VARCHAR(16) NOT NULL,
    side VARCHAR(8) NOT NULL,
    is_simulation BOOLEAN NOT NULL DEFAULT TRUE,
    entry_price NUMERIC(10, 6) NOT NULL,
    exit_price NUMERIC(10, 6),
    target_price NUMERIC(10, 6),
    stop_loss_price NUMERIC(10, 6),
    size_usd NUMERIC(16, 2) NOT NULL,
    quantity NUMERIC(20, 8),
    pnl_usd NUMERIC(16, 4),
    pnl_pct DOUBLE PRECISION,
    outcome VARCHAR(16),
    signal_score DOUBLE PRECISION,
    signal_confidence DOUBLE PRECISION,
    signal_direction VARCHAR(16),
    fused_signal_id BIGINT REFERENCES fused_signals(id),
    trend_direction VARCHAR(16),
    trend_confidence DOUBLE PRECISION,
    price_at_decision NUMERIC(10, 6),
    market_slug VARCHAR(128),
    instrument_id VARCHAR(256),
    yes_token_id VARCHAR(128),
    no_token_id VARCHAR(128),
    market_snapshot_id BIGINT REFERENCES market_snapshots(id),
    order_id VARCHAR(256),
    fill_price NUMERIC(10, 6),
    slippage_bps DOUBLE PRECISION,
    execution_latency_ms DOUBLE PRECISION,
    exit_reason VARCHAR(32),
    trade_metadata JSONB
);

CREATE INDEX IF NOT EXISTS ix_trades_trade_id ON trades (trade_id);
CREATE INDEX IF NOT EXISTS ix_trades_entry_time ON trades (entry_time);
CREATE INDEX IF NOT EXISTS ix_trades_outcome ON trades (outcome);
CREATE INDEX IF NOT EXISTS ix_trades_direction ON trades (direction);
CREATE INDEX IF NOT EXISTS ix_trades_market_slug ON trades (market_slug);

-- Order Events
CREATE TABLE IF NOT EXISTS order_events (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    trade_id BIGINT REFERENCES trades(id),
    order_id VARCHAR(256) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    side VARCHAR(8),
    fill_price NUMERIC(10, 6),
    fill_quantity NUMERIC(20, 8),
    fill_usd NUMERIC(16, 4),
    rejection_reason TEXT,
    raw_event JSONB
);

CREATE INDEX IF NOT EXISTS ix_order_events_timestamp ON order_events (timestamp);
CREATE INDEX IF NOT EXISTS ix_order_events_order_id ON order_events (order_id);

-- Positions
CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    position_id VARCHAR(128) UNIQUE NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    is_open BOOLEAN NOT NULL DEFAULT TRUE,
    direction VARCHAR(16) NOT NULL,
    instrument_id VARCHAR(256),
    market_slug VARCHAR(128),
    entry_price NUMERIC(10, 6) NOT NULL,
    current_price NUMERIC(10, 6),
    exit_price NUMERIC(10, 6),
    size_usd NUMERIC(16, 2) NOT NULL,
    quantity NUMERIC(20, 8),
    unrealized_pnl NUMERIC(16, 4),
    realized_pnl NUMERIC(16, 4),
    stop_loss NUMERIC(10, 6),
    take_profit NUMERIC(10, 6),
    risk_level VARCHAR(16),
    trade_id BIGINT REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS ix_positions_opened_at ON positions (opened_at);
CREATE INDEX IF NOT EXISTS ix_positions_is_open ON positions (is_open);

-- Risk Alerts
CREATE TABLE IF NOT EXISTS risk_alerts (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    alert_type VARCHAR(32) NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    message TEXT NOT NULL,
    current_value NUMERIC(16, 4),
    threshold_value NUMERIC(16, 4),
    position_id BIGINT REFERENCES positions(id),
    trade_id BIGINT REFERENCES trades(id),
    was_acknowledged BOOLEAN DEFAULT FALSE,
    action_taken VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS ix_risk_alerts_timestamp ON risk_alerts (timestamp);
CREATE INDEX IF NOT EXISTS ix_risk_alerts_type ON risk_alerts (alert_type);

-- Risk Snapshots
CREATE TABLE IF NOT EXISTS risk_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    daily_pnl NUMERIC(16, 4),
    total_pnl NUMERIC(16, 4),
    realized_pnl NUMERIC(16, 4),
    unrealized_pnl NUMERIC(16, 4),
    current_balance NUMERIC(16, 4),
    peak_balance NUMERIC(16, 4),
    drawdown_pct DOUBLE PRECISION,
    total_exposure NUMERIC(16, 4),
    exposure_utilization_pct DOUBLE PRECISION,
    open_positions_count INTEGER,
    max_daily_loss_limit NUMERIC(16, 4),
    max_position_size NUMERIC(16, 4),
    max_total_exposure NUMERIC(16, 4),
    max_drawdown_pct_limit DOUBLE PRECISION,
    daily_trade_count INTEGER,
    total_trade_count INTEGER
);

CREATE INDEX IF NOT EXISTS ix_risk_snapshots_timestamp ON risk_snapshots (timestamp);

-- Performance Metrics
CREATE TABLE IF NOT EXISTS performance_metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    total_pnl NUMERIC(16, 4),
    realized_pnl NUMERIC(16, 4),
    unrealized_pnl NUMERIC(16, 4),
    total_trades INTEGER,
    winning_trades INTEGER,
    losing_trades INTEGER,
    win_rate DOUBLE PRECISION,
    roi_pct DOUBLE PRECISION,
    sharpe_ratio DOUBLE PRECISION,
    sortino_ratio DOUBLE PRECISION,
    max_drawdown_pct DOUBLE PRECISION,
    open_positions INTEGER,
    avg_position_size NUMERIC(16, 4),
    avg_hold_time_seconds DOUBLE PRECISION,
    total_exposure NUMERIC(16, 4),
    risk_utilization_pct DOUBLE PRECISION,
    avg_signal_score DOUBLE PRECISION,
    avg_signal_confidence DOUBLE PRECISION,
    long_trades INTEGER,
    short_trades INTEGER,
    long_win_rate DOUBLE PRECISION,
    short_win_rate DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS ix_performance_metrics_timestamp ON performance_metrics (timestamp);

-- Daily Performance
CREATE TABLE IF NOT EXISTS daily_performance (
    id BIGSERIAL PRIMARY KEY,
    date TIMESTAMPTZ NOT NULL UNIQUE,
    daily_pnl NUMERIC(16, 4),
    cumulative_pnl NUMERIC(16, 4),
    trades_count INTEGER,
    win_count INTEGER,
    loss_count INTEGER,
    win_rate DOUBLE PRECISION,
    daily_roi_pct DOUBLE PRECISION,
    max_intraday_drawdown_pct DOUBLE PRECISION,
    start_equity NUMERIC(16, 4),
    end_equity NUMERIC(16, 4),
    best_trade_pnl NUMERIC(16, 4),
    worst_trade_pnl NUMERIC(16, 4),
    long_pnl NUMERIC(16, 4),
    short_pnl NUMERIC(16, 4),
    avg_signal_score DOUBLE PRECISION,
    avg_signal_confidence DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS ix_daily_performance_date ON daily_performance (date);

-- Signal Performance
CREATE TABLE IF NOT EXISTS signal_performance (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    source_name VARCHAR(64) NOT NULL,
    lookback_days INTEGER,
    total_trades INTEGER,
    winning_trades INTEGER,
    losing_trades INTEGER,
    win_rate DOUBLE PRECISION,
    total_pnl NUMERIC(16, 4),
    avg_pnl NUMERIC(16, 4),
    profit_factor DOUBLE PRECISION,
    avg_score DOUBLE PRECISION,
    avg_confidence DOUBLE PRECISION,
    recommended_weight DOUBLE PRECISION,
    weight_change_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_signal_performance_timestamp ON signal_performance (timestamp);
CREATE INDEX IF NOT EXISTS ix_signal_performance_source ON signal_performance (source_name);

-- Weight Adjustments
CREATE TABLE IF NOT EXISTS weight_adjustments (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    signal_source VARCHAR(64) NOT NULL,
    old_weight DOUBLE PRECISION NOT NULL,
    new_weight DOUBLE PRECISION NOT NULL,
    performance_score DOUBLE PRECISION,
    learning_rate DOUBLE PRECISION,
    reason TEXT,
    adjusted_by VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS ix_weight_adjustments_timestamp ON weight_adjustments (timestamp);
CREATE INDEX IF NOT EXISTS ix_weight_adjustments_source ON weight_adjustments (signal_source);

-- Paper Trades
CREATE TABLE IF NOT EXISTS paper_trades (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    direction VARCHAR(16) NOT NULL,
    size_usd NUMERIC(16, 2) NOT NULL,
    entry_price NUMERIC(10, 6) NOT NULL,
    exit_price NUMERIC(10, 6),
    signal_score DOUBLE PRECISION,
    signal_confidence DOUBLE PRECISION,
    outcome VARCHAR(16),
    pnl_usd NUMERIC(16, 4),
    pnl_pct DOUBLE PRECISION,
    market_slug VARCHAR(128),
    btc_spot_price NUMERIC(16, 2),
    fear_greed_index DOUBLE PRECISION,
    trend_direction VARCHAR(16),
    trend_confidence DOUBLE PRECISION,
    timeframe VARCHAR(16),
    paper_metadata JSONB
);

CREATE INDEX IF NOT EXISTS ix_paper_trades_timestamp ON paper_trades (timestamp);

-- Tick Data
CREATE TABLE IF NOT EXISTS tick_data (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    market_slug VARCHAR(128) NOT NULL,
    instrument_id VARCHAR(256),
    bid_price NUMERIC(10, 6),
    ask_price NUMERIC(10, 6),
    mid_price NUMERIC(10, 6),
    bid_size NUMERIC(20, 8),
    ask_size NUMERIC(20, 8)
);

CREATE INDEX IF NOT EXISTS ix_tick_data_timestamp ON tick_data (timestamp);
CREATE INDEX IF NOT EXISTS ix_tick_data_slug ON tick_data (market_slug);
