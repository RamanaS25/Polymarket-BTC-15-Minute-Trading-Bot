"""
Trading Bot Repository Layer

Provides high-level data access methods for persisting and querying
trade data, signals, market snapshots, and performance metrics.
"""

import json
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from typing import Optional, List, Dict, Any, Union
from dataclasses import dataclass

from sqlalchemy import select, func, and_, or_, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from .database import DatabaseManager, get_database
from .models import (
    MarketSnapshot,
    TradingSignal,
    FusedSignal,
    Trade,
    OrderEvent,
    Position,
    RiskAlert,
    RiskSnapshot,
    PerformanceMetrics,
    DailyPerformance,
    SignalPerformance,
    WeightAdjustment,
    PaperTrade,
    TickData,
)


def _to_decimal(value) -> Optional[Decimal]:
    """Convert value to Decimal safely."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _utc_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)


class TradingRepository:
    """
    Repository for all trading-related database operations.

    Usage:
        repo = TradingRepository(db)

        # Save a trade
        trade_id = await repo.save_trade(...)

        # Query trades
        trades = await repo.get_trades_by_date(date.today())
    """

    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize repository.

        Args:
            db: DatabaseManager instance. Uses global instance if not provided.
        """
        self._db = db or get_database()

    # =========================================================================
    # MARKET SNAPSHOTS
    # =========================================================================

    async def save_market_snapshot(
        self,
        market_slug: str,
        instrument_id: str,
        bid_price: Decimal,
        ask_price: Decimal,
        timestamp: Optional[datetime] = None,
        yes_token_id: Optional[str] = None,
        no_token_id: Optional[str] = None,
        market_start_time: Optional[datetime] = None,
        market_end_time: Optional[datetime] = None,
        btc_spot_price: Optional[Decimal] = None,
        btc_spot_source: Optional[str] = None,
        fear_greed_index: Optional[float] = None,
        fear_greed_classification: Optional[str] = None,
        orderbook_bid_volume_usd: Optional[Decimal] = None,
        orderbook_ask_volume_usd: Optional[Decimal] = None,
        orderbook_imbalance: Optional[float] = None,
        orderbook_bid_wall_usd: Optional[Decimal] = None,
        orderbook_ask_wall_usd: Optional[Decimal] = None,
        price_sma_20: Optional[Decimal] = None,
        price_deviation_pct: Optional[float] = None,
        momentum_5p: Optional[float] = None,
        volatility: Optional[float] = None,
        tick_velocity_60s: Optional[float] = None,
        tick_velocity_30s: Optional[float] = None,
        tick_acceleration: Optional[float] = None,
        deribit_pcr: Optional[float] = None,
        deribit_overall_pcr: Optional[float] = None,
        deribit_short_put_oi: Optional[Decimal] = None,
        deribit_short_call_oi: Optional[Decimal] = None,
        extra_data: Optional[Dict] = None,
    ) -> int:
        """
        Save a market snapshot.

        Returns:
            The ID of the created snapshot.
        """
        mid_price = (_to_decimal(bid_price) + _to_decimal(ask_price)) / 2
        spread = _to_decimal(ask_price) - _to_decimal(bid_price)

        snapshot = MarketSnapshot(
            timestamp=timestamp or _utc_now(),
            market_slug=market_slug,
            instrument_id=instrument_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_start_time=market_start_time,
            market_end_time=market_end_time,
            bid_price=_to_decimal(bid_price),
            ask_price=_to_decimal(ask_price),
            mid_price=mid_price,
            spread=spread,
            btc_spot_price=_to_decimal(btc_spot_price),
            btc_spot_source=btc_spot_source,
            fear_greed_index=fear_greed_index,
            fear_greed_classification=fear_greed_classification,
            orderbook_bid_volume_usd=_to_decimal(orderbook_bid_volume_usd),
            orderbook_ask_volume_usd=_to_decimal(orderbook_ask_volume_usd),
            orderbook_imbalance=orderbook_imbalance,
            orderbook_bid_wall_usd=_to_decimal(orderbook_bid_wall_usd),
            orderbook_ask_wall_usd=_to_decimal(orderbook_ask_wall_usd),
            price_sma_20=_to_decimal(price_sma_20),
            price_deviation_pct=price_deviation_pct,
            momentum_5p=momentum_5p,
            volatility=volatility,
            tick_velocity_60s=tick_velocity_60s,
            tick_velocity_30s=tick_velocity_30s,
            tick_acceleration=tick_acceleration,
            deribit_pcr=deribit_pcr,
            deribit_overall_pcr=deribit_overall_pcr,
            deribit_short_put_oi=_to_decimal(deribit_short_put_oi),
            deribit_short_call_oi=_to_decimal(deribit_short_call_oi),
            extra_data=extra_data,
        )

        async with self._db.session() as session:
            session.add(snapshot)
            await session.flush()
            return snapshot.id

    async def get_market_snapshot(self, snapshot_id: int) -> Optional[MarketSnapshot]:
        """Get a market snapshot by ID."""
        async with self._db.session() as session:
            result = await session.get(MarketSnapshot, snapshot_id)
            return result

    # =========================================================================
    # SIGNALS
    # =========================================================================

    async def save_trading_signal(
        self,
        source: str,
        signal_type: str,
        direction: str,
        score: float,
        confidence: float,
        current_price: Decimal,
        timestamp: Optional[datetime] = None,
        strength: Optional[str] = None,
        target_price: Optional[Decimal] = None,
        stop_loss_price: Optional[Decimal] = None,
        market_snapshot_id: Optional[int] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Save a trading signal.

        Returns:
            The ID of the created signal.
        """
        signal = TradingSignal(
            timestamp=timestamp or _utc_now(),
            source=source,
            signal_type=signal_type,
            direction=direction,
            strength=strength,
            score=score,
            confidence=confidence,
            current_price=_to_decimal(current_price),
            target_price=_to_decimal(target_price),
            stop_loss_price=_to_decimal(stop_loss_price),
            market_snapshot_id=market_snapshot_id,
            signal_metadata=metadata,
        )

        async with self._db.session() as session:
            session.add(signal)
            await session.flush()
            return signal.id

    async def save_signals_batch(
        self,
        signals: List[Dict[str, Any]],
        market_snapshot_id: Optional[int] = None,
    ) -> List[int]:
        """
        Save multiple signals in a batch.

        Args:
            signals: List of signal dictionaries
            market_snapshot_id: Optional snapshot to link all signals to

        Returns:
            List of created signal IDs
        """
        signal_ids = []
        timestamp = _utc_now()

        async with self._db.session() as session:
            for sig_data in signals:
                signal = TradingSignal(
                    timestamp=sig_data.get("timestamp", timestamp),
                    source=sig_data["source"],
                    signal_type=sig_data.get("signal_type", "UNKNOWN"),
                    direction=sig_data["direction"],
                    strength=sig_data.get("strength"),
                    score=sig_data["score"],
                    confidence=sig_data["confidence"],
                    current_price=_to_decimal(sig_data["current_price"]),
                    target_price=_to_decimal(sig_data.get("target_price")),
                    stop_loss_price=_to_decimal(sig_data.get("stop_loss_price")),
                    market_snapshot_id=market_snapshot_id,
                    signal_metadata=sig_data.get("metadata"),
                )
                session.add(signal)
                await session.flush()
                signal_ids.append(signal.id)

        return signal_ids

    async def save_fused_signal(
        self,
        direction: str,
        score: float,
        confidence: float,
        num_signals: int,
        timestamp: Optional[datetime] = None,
        bullish_contribution: Optional[float] = None,
        bearish_contribution: Optional[float] = None,
        total_contribution: Optional[float] = None,
        num_bullish: Optional[int] = None,
        num_bearish: Optional[int] = None,
        is_strong: Optional[bool] = None,
        is_actionable: Optional[bool] = None,
        weights_snapshot: Optional[Dict] = None,
        market_snapshot_id: Optional[int] = None,
        current_price: Optional[Decimal] = None,
        contributing_signal_ids: Optional[List[int]] = None,
    ) -> int:
        """
        Save a fused signal.

        Returns:
            The ID of the created fused signal.
        """
        fused = FusedSignal(
            timestamp=timestamp or _utc_now(),
            direction=direction,
            score=score,
            confidence=confidence,
            num_signals=num_signals,
            bullish_contribution=bullish_contribution,
            bearish_contribution=bearish_contribution,
            total_contribution=total_contribution,
            num_bullish=num_bullish,
            num_bearish=num_bearish,
            is_strong=is_strong if is_strong is not None else (score >= 70.0),
            is_actionable=is_actionable if is_actionable is not None else (score >= 60.0 and confidence >= 0.6),
            weights_snapshot=weights_snapshot,
            market_snapshot_id=market_snapshot_id,
            current_price=_to_decimal(current_price),
        )

        async with self._db.session() as session:
            session.add(fused)
            await session.flush()
            fused_id = fused.id

            # Link contributing signals
            if contributing_signal_ids:
                for sig_id in contributing_signal_ids:
                    stmt = (
                        select(TradingSignal)
                        .where(TradingSignal.id == sig_id)
                    )
                    result = await session.execute(stmt)
                    sig = result.scalar_one_or_none()
                    if sig:
                        sig.fused_signal_id = fused_id

        return fused_id

    # =========================================================================
    # TRADES
    # =========================================================================

    async def save_trade(
        self,
        trade_id: str,
        direction: str,
        entry_price: Decimal,
        size_usd: Decimal,
        entry_time: Optional[datetime] = None,
        side: str = "BUY",
        is_simulation: bool = True,
        exit_time: Optional[datetime] = None,
        exit_price: Optional[Decimal] = None,
        target_price: Optional[Decimal] = None,
        stop_loss_price: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        pnl_usd: Optional[Decimal] = None,
        pnl_pct: Optional[float] = None,
        outcome: Optional[str] = None,
        signal_score: Optional[float] = None,
        signal_confidence: Optional[float] = None,
        signal_direction: Optional[str] = None,
        fused_signal_id: Optional[int] = None,
        trend_direction: Optional[str] = None,
        trend_confidence: Optional[float] = None,
        price_at_decision: Optional[Decimal] = None,
        market_slug: Optional[str] = None,
        instrument_id: Optional[str] = None,
        yes_token_id: Optional[str] = None,
        no_token_id: Optional[str] = None,
        market_snapshot_id: Optional[int] = None,
        order_id: Optional[str] = None,
        fill_price: Optional[Decimal] = None,
        slippage_bps: Optional[float] = None,
        execution_latency_ms: Optional[float] = None,
        exit_reason: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Save a trade record.

        Returns:
            The database ID of the created trade.
        """
        entry_time = entry_time or _utc_now()
        duration_seconds = None
        if exit_time:
            duration_seconds = (exit_time - entry_time).total_seconds()

        trade = Trade(
            trade_id=trade_id,
            entry_time=entry_time,
            exit_time=exit_time,
            duration_seconds=duration_seconds,
            direction=direction,
            side=side,
            is_simulation=is_simulation,
            entry_price=_to_decimal(entry_price),
            exit_price=_to_decimal(exit_price),
            target_price=_to_decimal(target_price),
            stop_loss_price=_to_decimal(stop_loss_price),
            size_usd=_to_decimal(size_usd),
            quantity=_to_decimal(quantity),
            pnl_usd=_to_decimal(pnl_usd),
            pnl_pct=pnl_pct,
            outcome=outcome,
            signal_score=signal_score,
            signal_confidence=signal_confidence,
            signal_direction=signal_direction,
            fused_signal_id=fused_signal_id,
            trend_direction=trend_direction,
            trend_confidence=trend_confidence,
            price_at_decision=_to_decimal(price_at_decision),
            market_slug=market_slug,
            instrument_id=instrument_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_snapshot_id=market_snapshot_id,
            order_id=order_id,
            fill_price=_to_decimal(fill_price),
            slippage_bps=slippage_bps,
            execution_latency_ms=execution_latency_ms,
            exit_reason=exit_reason,
            trade_metadata=metadata,
        )

        async with self._db.session() as session:
            session.add(trade)
            await session.flush()
            return trade.id

    async def update_trade_exit(
        self,
        trade_id: str,
        exit_price: Decimal,
        exit_time: Optional[datetime] = None,
        pnl_usd: Optional[Decimal] = None,
        pnl_pct: Optional[float] = None,
        outcome: Optional[str] = None,
        exit_reason: Optional[str] = None,
        fill_price: Optional[Decimal] = None,
        slippage_bps: Optional[float] = None,
    ) -> bool:
        """
        Update a trade with exit information.

        Returns:
            True if trade was found and updated.
        """
        async with self._db.session() as session:
            stmt = select(Trade).where(Trade.trade_id == trade_id)
            result = await session.execute(stmt)
            trade = result.scalar_one_or_none()

            if not trade:
                logger.warning(f"Trade not found for update: {trade_id}")
                return False

            exit_time = exit_time or _utc_now()
            trade.exit_time = exit_time
            trade.exit_price = _to_decimal(exit_price)
            trade.duration_seconds = (exit_time - trade.entry_time).total_seconds()

            if pnl_usd is not None:
                trade.pnl_usd = _to_decimal(pnl_usd)
            if pnl_pct is not None:
                trade.pnl_pct = pnl_pct
            if outcome is not None:
                trade.outcome = outcome
            if exit_reason is not None:
                trade.exit_reason = exit_reason
            if fill_price is not None:
                trade.fill_price = _to_decimal(fill_price)
            if slippage_bps is not None:
                trade.slippage_bps = slippage_bps

            return True

    async def get_trade_by_id(self, trade_id: str) -> Optional[Trade]:
        """Get a trade by its trade_id."""
        async with self._db.session() as session:
            stmt = select(Trade).where(Trade.trade_id == trade_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_trades_by_date(
        self,
        target_date: date,
        simulation_only: Optional[bool] = None,
    ) -> List[Trade]:
        """Get all trades for a specific date."""
        start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)

        async with self._db.session() as session:
            stmt = select(Trade).where(
                and_(
                    Trade.entry_time >= start,
                    Trade.entry_time < end,
                )
            )
            if simulation_only is not None:
                stmt = stmt.where(Trade.is_simulation == simulation_only)
            stmt = stmt.order_by(Trade.entry_time)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_recent_trades(
        self,
        limit: int = 100,
        simulation_only: Optional[bool] = None,
    ) -> List[Trade]:
        """Get the most recent trades."""
        async with self._db.session() as session:
            stmt = select(Trade)
            if simulation_only is not None:
                stmt = stmt.where(Trade.is_simulation == simulation_only)
            stmt = stmt.order_by(desc(Trade.entry_time)).limit(limit)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    # =========================================================================
    # ORDER EVENTS
    # =========================================================================

    async def save_order_event(
        self,
        order_id: str,
        event_type: str,
        timestamp: Optional[datetime] = None,
        trade_id: Optional[int] = None,
        side: Optional[str] = None,
        fill_price: Optional[Decimal] = None,
        fill_quantity: Optional[Decimal] = None,
        fill_usd: Optional[Decimal] = None,
        rejection_reason: Optional[str] = None,
        raw_event: Optional[Dict] = None,
    ) -> int:
        """Save an order event."""
        event = OrderEvent(
            timestamp=timestamp or _utc_now(),
            trade_id=trade_id,
            order_id=order_id,
            event_type=event_type,
            side=side,
            fill_price=_to_decimal(fill_price),
            fill_quantity=_to_decimal(fill_quantity),
            fill_usd=_to_decimal(fill_usd),
            rejection_reason=rejection_reason,
            raw_event=raw_event,
        )

        async with self._db.session() as session:
            session.add(event)
            await session.flush()
            return event.id

    # =========================================================================
    # PAPER TRADES
    # =========================================================================

    async def save_paper_trade(
        self,
        direction: str,
        size_usd: Decimal,
        entry_price: Decimal,
        signal_score: float,
        signal_confidence: float,
        timestamp: Optional[datetime] = None,
        exit_price: Optional[Decimal] = None,
        outcome: Optional[str] = None,
        pnl_usd: Optional[Decimal] = None,
        pnl_pct: Optional[float] = None,
        market_slug: Optional[str] = None,
        btc_spot_price: Optional[Decimal] = None,
        fear_greed_index: Optional[float] = None,
        trend_direction: Optional[str] = None,
        trend_confidence: Optional[float] = None,
        timeframe: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """Save a paper trade."""
        paper = PaperTrade(
            timestamp=timestamp or _utc_now(),
            direction=direction,
            size_usd=_to_decimal(size_usd),
            entry_price=_to_decimal(entry_price),
            exit_price=_to_decimal(exit_price),
            signal_score=signal_score,
            signal_confidence=signal_confidence,
            outcome=outcome,
            pnl_usd=_to_decimal(pnl_usd),
            pnl_pct=pnl_pct,
            market_slug=market_slug,
            btc_spot_price=_to_decimal(btc_spot_price),
            fear_greed_index=fear_greed_index,
            trend_direction=trend_direction,
            trend_confidence=trend_confidence,
            timeframe=timeframe,
            paper_metadata=metadata,
        )

        async with self._db.session() as session:
            session.add(paper)
            await session.flush()
            return paper.id

    async def get_paper_trades(
        self,
        limit: int = 100,
        since: Optional[datetime] = None,
    ) -> List[PaperTrade]:
        """Get paper trades."""
        async with self._db.session() as session:
            stmt = select(PaperTrade)
            if since:
                stmt = stmt.where(PaperTrade.timestamp >= since)
            stmt = stmt.order_by(desc(PaperTrade.timestamp)).limit(limit)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    # =========================================================================
    # RISK MANAGEMENT
    # =========================================================================

    async def save_risk_snapshot(
        self,
        timestamp: Optional[datetime] = None,
        daily_pnl: Optional[Decimal] = None,
        total_pnl: Optional[Decimal] = None,
        realized_pnl: Optional[Decimal] = None,
        unrealized_pnl: Optional[Decimal] = None,
        current_balance: Optional[Decimal] = None,
        peak_balance: Optional[Decimal] = None,
        drawdown_pct: Optional[float] = None,
        total_exposure: Optional[Decimal] = None,
        exposure_utilization_pct: Optional[float] = None,
        open_positions_count: Optional[int] = None,
        max_daily_loss_limit: Optional[Decimal] = None,
        max_position_size: Optional[Decimal] = None,
        max_total_exposure: Optional[Decimal] = None,
        max_drawdown_pct_limit: Optional[float] = None,
        daily_trade_count: Optional[int] = None,
        total_trade_count: Optional[int] = None,
    ) -> int:
        """Save a risk snapshot."""
        snapshot = RiskSnapshot(
            timestamp=timestamp or _utc_now(),
            daily_pnl=_to_decimal(daily_pnl),
            total_pnl=_to_decimal(total_pnl),
            realized_pnl=_to_decimal(realized_pnl),
            unrealized_pnl=_to_decimal(unrealized_pnl),
            current_balance=_to_decimal(current_balance),
            peak_balance=_to_decimal(peak_balance),
            drawdown_pct=drawdown_pct,
            total_exposure=_to_decimal(total_exposure),
            exposure_utilization_pct=exposure_utilization_pct,
            open_positions_count=open_positions_count,
            max_daily_loss_limit=_to_decimal(max_daily_loss_limit),
            max_position_size=_to_decimal(max_position_size),
            max_total_exposure=_to_decimal(max_total_exposure),
            max_drawdown_pct_limit=max_drawdown_pct_limit,
            daily_trade_count=daily_trade_count,
            total_trade_count=total_trade_count,
        )

        async with self._db.session() as session:
            session.add(snapshot)
            await session.flush()
            return snapshot.id

    async def save_risk_alert(
        self,
        alert_type: str,
        risk_level: str,
        message: str,
        timestamp: Optional[datetime] = None,
        current_value: Optional[Decimal] = None,
        threshold_value: Optional[Decimal] = None,
        position_id: Optional[int] = None,
        trade_id: Optional[int] = None,
    ) -> int:
        """Save a risk alert."""
        alert = RiskAlert(
            timestamp=timestamp or _utc_now(),
            alert_type=alert_type,
            risk_level=risk_level,
            message=message,
            current_value=_to_decimal(current_value),
            threshold_value=_to_decimal(threshold_value),
            position_id=position_id,
            trade_id=trade_id,
        )

        async with self._db.session() as session:
            session.add(alert)
            await session.flush()
            return alert.id

    # =========================================================================
    # PERFORMANCE METRICS
    # =========================================================================

    async def save_performance_metrics(
        self,
        timestamp: Optional[datetime] = None,
        total_pnl: Optional[Decimal] = None,
        realized_pnl: Optional[Decimal] = None,
        unrealized_pnl: Optional[Decimal] = None,
        total_trades: Optional[int] = None,
        winning_trades: Optional[int] = None,
        losing_trades: Optional[int] = None,
        win_rate: Optional[float] = None,
        roi_pct: Optional[float] = None,
        sharpe_ratio: Optional[float] = None,
        sortino_ratio: Optional[float] = None,
        max_drawdown_pct: Optional[float] = None,
        open_positions: Optional[int] = None,
        avg_position_size: Optional[Decimal] = None,
        avg_hold_time_seconds: Optional[float] = None,
        total_exposure: Optional[Decimal] = None,
        risk_utilization_pct: Optional[float] = None,
        avg_signal_score: Optional[float] = None,
        avg_signal_confidence: Optional[float] = None,
        long_trades: Optional[int] = None,
        short_trades: Optional[int] = None,
        long_win_rate: Optional[float] = None,
        short_win_rate: Optional[float] = None,
    ) -> int:
        """Save a performance metrics snapshot."""
        metrics = PerformanceMetrics(
            timestamp=timestamp or _utc_now(),
            total_pnl=_to_decimal(total_pnl),
            realized_pnl=_to_decimal(realized_pnl),
            unrealized_pnl=_to_decimal(unrealized_pnl),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            roi_pct=roi_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            max_drawdown_pct=max_drawdown_pct,
            open_positions=open_positions,
            avg_position_size=_to_decimal(avg_position_size),
            avg_hold_time_seconds=avg_hold_time_seconds,
            total_exposure=_to_decimal(total_exposure),
            risk_utilization_pct=risk_utilization_pct,
            avg_signal_score=avg_signal_score,
            avg_signal_confidence=avg_signal_confidence,
            long_trades=long_trades,
            short_trades=short_trades,
            long_win_rate=long_win_rate,
            short_win_rate=short_win_rate,
        )

        async with self._db.session() as session:
            session.add(metrics)
            await session.flush()
            return metrics.id

    async def save_daily_performance(
        self,
        target_date: date,
        daily_pnl: Decimal,
        trades_count: int,
        win_count: int,
        loss_count: int,
        cumulative_pnl: Optional[Decimal] = None,
        daily_roi_pct: Optional[float] = None,
        max_intraday_drawdown_pct: Optional[float] = None,
        start_equity: Optional[Decimal] = None,
        end_equity: Optional[Decimal] = None,
        best_trade_pnl: Optional[Decimal] = None,
        worst_trade_pnl: Optional[Decimal] = None,
        long_pnl: Optional[Decimal] = None,
        short_pnl: Optional[Decimal] = None,
        avg_signal_score: Optional[float] = None,
        avg_signal_confidence: Optional[float] = None,
    ) -> int:
        """Save or update daily performance record."""
        win_rate = (win_count / trades_count * 100) if trades_count > 0 else 0.0
        target_datetime = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)

        async with self._db.session() as session:
            # Check if record exists
            stmt = select(DailyPerformance).where(DailyPerformance.date == target_datetime)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing
                existing.daily_pnl = _to_decimal(daily_pnl)
                existing.cumulative_pnl = _to_decimal(cumulative_pnl)
                existing.trades_count = trades_count
                existing.win_count = win_count
                existing.loss_count = loss_count
                existing.win_rate = win_rate
                existing.daily_roi_pct = daily_roi_pct
                existing.max_intraday_drawdown_pct = max_intraday_drawdown_pct
                existing.start_equity = _to_decimal(start_equity)
                existing.end_equity = _to_decimal(end_equity)
                existing.best_trade_pnl = _to_decimal(best_trade_pnl)
                existing.worst_trade_pnl = _to_decimal(worst_trade_pnl)
                existing.long_pnl = _to_decimal(long_pnl)
                existing.short_pnl = _to_decimal(short_pnl)
                existing.avg_signal_score = avg_signal_score
                existing.avg_signal_confidence = avg_signal_confidence
                return existing.id
            else:
                # Create new
                daily = DailyPerformance(
                    date=target_datetime,
                    daily_pnl=_to_decimal(daily_pnl),
                    cumulative_pnl=_to_decimal(cumulative_pnl),
                    trades_count=trades_count,
                    win_count=win_count,
                    loss_count=loss_count,
                    win_rate=win_rate,
                    daily_roi_pct=daily_roi_pct,
                    max_intraday_drawdown_pct=max_intraday_drawdown_pct,
                    start_equity=_to_decimal(start_equity),
                    end_equity=_to_decimal(end_equity),
                    best_trade_pnl=_to_decimal(best_trade_pnl),
                    worst_trade_pnl=_to_decimal(worst_trade_pnl),
                    long_pnl=_to_decimal(long_pnl),
                    short_pnl=_to_decimal(short_pnl),
                    avg_signal_score=avg_signal_score,
                    avg_signal_confidence=avg_signal_confidence,
                )
                session.add(daily)
                await session.flush()
                return daily.id

    # =========================================================================
    # LEARNING ENGINE
    # =========================================================================

    async def save_signal_performance(
        self,
        source_name: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: Decimal,
        timestamp: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        avg_pnl: Optional[Decimal] = None,
        profit_factor: Optional[float] = None,
        avg_score: Optional[float] = None,
        avg_confidence: Optional[float] = None,
        recommended_weight: Optional[float] = None,
        weight_change_reason: Optional[str] = None,
    ) -> int:
        """Save signal performance analytics."""
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

        perf = SignalPerformance(
            timestamp=timestamp or _utc_now(),
            source_name=source_name,
            lookback_days=lookback_days,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl=_to_decimal(total_pnl),
            avg_pnl=_to_decimal(avg_pnl),
            profit_factor=profit_factor,
            avg_score=avg_score,
            avg_confidence=avg_confidence,
            recommended_weight=recommended_weight,
            weight_change_reason=weight_change_reason,
        )

        async with self._db.session() as session:
            session.add(perf)
            await session.flush()
            return perf.id

    async def save_weight_adjustment(
        self,
        signal_source: str,
        old_weight: float,
        new_weight: float,
        timestamp: Optional[datetime] = None,
        performance_score: Optional[float] = None,
        learning_rate: Optional[float] = None,
        reason: Optional[str] = None,
        adjusted_by: str = "learning_engine",
    ) -> int:
        """Save a weight adjustment record."""
        adj = WeightAdjustment(
            timestamp=timestamp or _utc_now(),
            signal_source=signal_source,
            old_weight=old_weight,
            new_weight=new_weight,
            performance_score=performance_score,
            learning_rate=learning_rate,
            reason=reason,
            adjusted_by=adjusted_by,
        )

        async with self._db.session() as session:
            session.add(adj)
            await session.flush()
            return adj.id

    async def get_signal_performance_history(
        self,
        source_name: str,
        limit: int = 30,
    ) -> List[SignalPerformance]:
        """Get performance history for a signal source."""
        async with self._db.session() as session:
            stmt = (
                select(SignalPerformance)
                .where(SignalPerformance.source_name == source_name)
                .order_by(desc(SignalPerformance.timestamp))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # =========================================================================
    # TICK DATA (optional)
    # =========================================================================

    async def save_tick(
        self,
        market_slug: str,
        bid_price: Decimal,
        ask_price: Decimal,
        timestamp: Optional[datetime] = None,
        instrument_id: Optional[str] = None,
        bid_size: Optional[Decimal] = None,
        ask_size: Optional[Decimal] = None,
    ) -> int:
        """Save a tick (use sparingly - high volume)."""
        mid_price = (_to_decimal(bid_price) + _to_decimal(ask_price)) / 2

        tick = TickData(
            timestamp=timestamp or _utc_now(),
            market_slug=market_slug,
            instrument_id=instrument_id,
            bid_price=_to_decimal(bid_price),
            ask_price=_to_decimal(ask_price),
            mid_price=mid_price,
            bid_size=_to_decimal(bid_size),
            ask_size=_to_decimal(ask_size),
        )

        async with self._db.session() as session:
            session.add(tick)
            await session.flush()
            return tick.id

    # =========================================================================
    # ANALYTICS QUERIES
    # =========================================================================

    async def get_trade_stats(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        simulation_only: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Get aggregated trade statistics.

        Returns:
            Dictionary with trade statistics.
        """
        async with self._db.session() as session:
            # Build base query
            filters = []
            if since:
                filters.append(Trade.entry_time >= since)
            if until:
                filters.append(Trade.entry_time < until)
            if simulation_only is not None:
                filters.append(Trade.is_simulation == simulation_only)

            # Count trades
            count_stmt = select(func.count(Trade.id))
            if filters:
                count_stmt = count_stmt.where(and_(*filters))
            count_result = await session.execute(count_stmt)
            total_trades = count_result.scalar() or 0

            if total_trades == 0:
                return {
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "win_rate": 0.0,
                    "total_pnl": Decimal("0"),
                    "avg_pnl": Decimal("0"),
                    "best_trade": Decimal("0"),
                    "worst_trade": Decimal("0"),
                }

            # Count wins/losses
            win_filters = filters + [Trade.outcome == "WIN"]
            win_stmt = select(func.count(Trade.id)).where(and_(*win_filters))
            win_result = await session.execute(win_stmt)
            winning_trades = win_result.scalar() or 0

            loss_filters = filters + [Trade.outcome == "LOSS"]
            loss_stmt = select(func.count(Trade.id)).where(and_(*loss_filters))
            loss_result = await session.execute(loss_stmt)
            losing_trades = loss_result.scalar() or 0

            # P&L stats
            pnl_stmt = select(
                func.sum(Trade.pnl_usd),
                func.avg(Trade.pnl_usd),
                func.max(Trade.pnl_usd),
                func.min(Trade.pnl_usd),
            )
            if filters:
                pnl_stmt = pnl_stmt.where(and_(*filters))
            pnl_result = await session.execute(pnl_stmt)
            pnl_row = pnl_result.one()

            return {
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": (winning_trades / total_trades * 100) if total_trades > 0 else 0.0,
                "total_pnl": pnl_row[0] or Decimal("0"),
                "avg_pnl": pnl_row[1] or Decimal("0"),
                "best_trade": pnl_row[2] or Decimal("0"),
                "worst_trade": pnl_row[3] or Decimal("0"),
            }

    async def get_signal_effectiveness(
        self,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get effectiveness metrics by signal source.

        Returns:
            List of dictionaries with per-source statistics.
        """
        async with self._db.session() as session:
            # This requires joining signals to trades via fused_signals
            # Simplified version: just get signal counts by source
            stmt = select(
                TradingSignal.source,
                func.count(TradingSignal.id).label("signal_count"),
                func.avg(TradingSignal.score).label("avg_score"),
                func.avg(TradingSignal.confidence).label("avg_confidence"),
            ).group_by(TradingSignal.source)

            if since:
                stmt = stmt.where(TradingSignal.timestamp >= since)

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "source": row[0],
                    "signal_count": row[1],
                    "avg_score": float(row[2]) if row[2] else 0.0,
                    "avg_confidence": float(row[3]) if row[3] else 0.0,
                }
                for row in rows
            ]


# =============================================================================
# SINGLETON
# =============================================================================

_repo_instance: Optional[TradingRepository] = None


def get_repository(db: Optional[DatabaseManager] = None) -> TradingRepository:
    """Get the global repository instance."""
    global _repo_instance
    if _repo_instance is None:
        _repo_instance = TradingRepository(db)
    return _repo_instance
