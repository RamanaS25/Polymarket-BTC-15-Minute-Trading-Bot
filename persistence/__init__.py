"""
Persistence Layer for Trading Bot

PostgreSQL-based persistence for trades, signals, market data, and analytics.

Usage:
    from persistence import init_database, get_repository

    # Initialize database (call once at startup)
    await init_database()

    # Get repository for data operations
    repo = get_repository()

    # Save a trade
    await repo.save_trade(
        trade_id="my-trade-123",
        direction="long",
        entry_price=Decimal("0.65"),
        size_usd=Decimal("1.00"),
        ...
    )

Environment Variables:
    POSTGRES_HOST: PostgreSQL host (default: localhost)
    POSTGRES_PORT: PostgreSQL port (default: 5432)
    POSTGRES_DB: Database name (default: trading_bot)
    POSTGRES_USER: Database user (default: trading)
    POSTGRES_PASSWORD: Database password
"""

from .database import (
    DatabaseManager,
    get_database,
    init_database,
    close_database,
)
from .repository import (
    TradingRepository,
    get_repository,
)
from .models import (
    Base,
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
    SignalDirection,
    SignalStrength,
    SignalType,
    TradeOutcome,
    RiskLevel,
    AlertType,
)

__all__ = [
    # Database
    "DatabaseManager",
    "get_database",
    "init_database",
    "close_database",
    # Repository
    "TradingRepository",
    "get_repository",
    # Models
    "Base",
    "MarketSnapshot",
    "TradingSignal",
    "FusedSignal",
    "Trade",
    "OrderEvent",
    "Position",
    "RiskAlert",
    "RiskSnapshot",
    "PerformanceMetrics",
    "DailyPerformance",
    "SignalPerformance",
    "WeightAdjustment",
    "PaperTrade",
    "TickData",
    # Enums
    "SignalDirection",
    "SignalStrength",
    "SignalType",
    "TradeOutcome",
    "RiskLevel",
    "AlertType",
]
