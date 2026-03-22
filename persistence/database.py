"""
PostgreSQL Database Connection Manager

Handles async database connections using SQLAlchemy 2.0+ with asyncpg.
Supports connection pooling and automatic reconnection.
"""

import os
import asyncio
from typing import Optional, AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool
from loguru import logger

from .models import Base


class DatabaseManager:
    """
    Manages PostgreSQL connections for the trading bot.

    Usage:
        db = DatabaseManager()
        await db.connect()

        async with db.session() as session:
            # Use session for queries
            pass

        await db.disconnect()
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        pool_size: int = 5,
        max_overflow: int = 10,
        echo: bool = False,
    ):
        """
        Initialize database manager.

        Args:
            host: PostgreSQL host (default from POSTGRES_HOST env)
            port: PostgreSQL port (default from POSTGRES_PORT env or 5432)
            database: Database name (default from POSTGRES_DB env)
            user: Database user (default from POSTGRES_USER env)
            password: Database password (default from POSTGRES_PASSWORD env)
            pool_size: Connection pool size
            max_overflow: Max connections above pool_size
            echo: Echo SQL statements for debugging
        """
        self.host = host or os.getenv("POSTGRES_HOST", "localhost")
        self.port = port or int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = database or os.getenv("POSTGRES_DB", "trading_bot")
        self.user = user or os.getenv("POSTGRES_USER", "trading")
        self.password = password or os.getenv("POSTGRES_PASSWORD", "")

        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.echo = echo

        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker] = None
        self._connected = False

    @property
    def connection_url(self) -> str:
        """Build the async PostgreSQL connection URL."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def sync_connection_url(self) -> str:
        """Build sync PostgreSQL URL (for migrations)."""
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    async def connect(self) -> None:
        """
        Establish database connection and create tables if needed.
        """
        if self._connected:
            logger.debug("Database already connected")
            return

        try:
            logger.info(f"Connecting to PostgreSQL at {self.host}:{self.port}/{self.database}")

            self._engine = create_async_engine(
                self.connection_url,
                echo=self.echo,
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_pre_ping=True,  # Verify connections before use
                pool_recycle=3600,   # Recycle connections after 1 hour
            )

            self._session_factory = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )

            # Test connection
            async with self._engine.begin() as conn:
                await conn.execute(text("SELECT 1"))

            self._connected = True
            logger.info("PostgreSQL connection established")

        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            raise

    async def create_tables(self) -> None:
        """
        Create all tables defined in models.
        Safe to call multiple times (uses CREATE IF NOT EXISTS).
        """
        if not self._engine:
            raise RuntimeError("Database not connected. Call connect() first.")

        try:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, checkfirst=True)
            logger.info("Database tables created/verified")
        except Exception as e:
            logger.error(f"Failed to create tables: {e}")
            raise

    async def disconnect(self) -> None:
        """Close database connection."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._connected = False
            logger.info("PostgreSQL connection closed")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session context manager.

        Usage:
            async with db.session() as session:
                result = await session.execute(query)
        """
        if not self._session_factory:
            raise RuntimeError("Database not connected. Call connect() first.")

        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def session_no_commit(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session without auto-commit.
        Caller is responsible for commit/rollback.
        """
        if not self._session_factory:
            raise RuntimeError("Database not connected. Call connect() first.")

        session = self._session_factory()
        try:
            yield session
        finally:
            await session.close()

    async def execute(self, query, params=None):
        """Execute a raw SQL query."""
        async with self.session() as session:
            result = await session.execute(text(query), params or {})
            return result

    @property
    def is_connected(self) -> bool:
        """Check if database is connected."""
        return self._connected


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_db_instance: Optional[DatabaseManager] = None


def get_database() -> DatabaseManager:
    """
    Get the global database manager instance.
    Creates one if it doesn't exist.
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance


async def init_database(
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    create_tables: bool = True,
) -> DatabaseManager:
    """
    Initialize the global database instance.

    Args:
        host: PostgreSQL host
        port: PostgreSQL port
        database: Database name
        user: Database user
        password: Database password
        create_tables: Whether to create tables on init

    Returns:
        The initialized DatabaseManager
    """
    global _db_instance

    _db_instance = DatabaseManager(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )

    await _db_instance.connect()

    if create_tables:
        await _db_instance.create_tables()

    return _db_instance


async def close_database() -> None:
    """Close the global database connection."""
    global _db_instance
    if _db_instance:
        await _db_instance.disconnect()
        _db_instance = None
