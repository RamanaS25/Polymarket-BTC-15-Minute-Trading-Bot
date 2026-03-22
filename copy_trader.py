"""
Polymarket Copy Trader
======================
Monitors a target wallet and copies all their Polymarket trades.

Usage:
    python copy_trader.py --target 0xde17f7144fbd0eddb2679132c10ff5e74b120988
"""

import asyncio
import os
import sys
import time
import json
import aiohttp
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, List, Any
from pathlib import Path
from dataclasses import dataclass, field
from collections import deque

from dotenv import load_dotenv
from loguru import logger

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class CopyTraderConfig:
    """Configuration for the copy trader."""
    # Target wallet to copy
    target_wallet: str = "0xde17f7144fbd0eddb2679132c10ff5e74b120988"

    # Your wallet (from .env)
    your_wallet: str = ""

    # Copy settings
    copy_percentage: float = 1.0  # Copy 100% of their trade size (adjust as needed)
    max_trade_size_usd: float = 10.0  # Max USD per copied trade
    min_trade_size_usd: float = 1.0  # Min USD per copied trade

    # Timing
    poll_interval_seconds: float = 2.0  # How often to check for new trades
    max_copy_delay_seconds: float = 30.0  # Don't copy trades older than this

    # Safety
    enable_live_trading: bool = False  # Start in simulation mode
    daily_loss_limit_usd: float = 50.0  # Stop copying if daily loss exceeds this

    # Monitoring
    log_all_activity: bool = True

    def __post_init__(self):
        self.target_wallet = self.target_wallet.lower()


@dataclass
class Trade:
    """Represents a detected trade."""
    tx_hash: str
    timestamp: datetime
    wallet: str
    market_slug: str
    token_id: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "YES" or "NO"
    price: Decimal
    amount: Decimal  # Token amount
    usd_value: Decimal
    raw_data: Dict = field(default_factory=dict)

    @property
    def is_bullish(self) -> bool:
        """Is this a bullish bet (betting UP)?"""
        return (self.side == "BUY" and self.outcome == "YES") or \
               (self.side == "SELL" and self.outcome == "NO")


# ============================================================================
# POLYMARKET API CLIENT
# ============================================================================

class PolymarketClient:
    """Client for interacting with Polymarket APIs."""

    # Polymarket API endpoints
    CLOB_API = "https://clob.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com"

    # Polygon RPC
    POLYGON_RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")

    # Polymarket contract addresses (Polygon)
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Conditional Token Framework Exchange
    NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # Neg Risk Exchange

    def __init__(self, config: CopyTraderConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Add API keys if available
        api_key = os.getenv("POLYMARKET_API_KEY")
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def connect(self):
        """Initialize HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers)
        logger.info("Polymarket client connected")

    async def disconnect(self):
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def get_user_trades(self, wallet: str, limit: int = 50) -> List[Dict]:
        """
        Get recent trades for a wallet from Polymarket CLOB API.
        """
        try:
            url = f"{self.CLOB_API}/trades"
            params = {
                "maker": wallet,
                "limit": limit,
            }

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else data.get("trades", [])
                else:
                    logger.warning(f"Failed to get trades: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return []

    async def get_user_orders(self, wallet: str, limit: int = 50) -> List[Dict]:
        """
        Get recent orders for a wallet from Polymarket CLOB API.
        """
        try:
            url = f"{self.CLOB_API}/orders"
            params = {
                "maker": wallet,
                "limit": limit,
            }

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else data.get("orders", [])
                else:
                    logger.warning(f"Failed to get orders: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching orders: {e}")
            return []

    async def get_market_info(self, token_id: str) -> Optional[Dict]:
        """Get market information for a token ID."""
        try:
            url = f"{self.GAMMA_API}/markets"
            params = {"clob_token_ids": token_id}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0]
                return None
        except Exception as e:
            logger.error(f"Error fetching market info: {e}")
            return None

    async def get_polygon_transactions(self, wallet: str, limit: int = 100) -> List[Dict]:
        """
        Get recent transactions from Polygonscan API.
        This catches all on-chain activity including trades.
        """
        api_key = os.getenv("POLYGONSCAN_API_KEY", "")
        if not api_key:
            logger.warning("No POLYGONSCAN_API_KEY set - using rate-limited public API")

        try:
            url = "https://api.polygonscan.com/api"
            params = {
                "module": "account",
                "action": "txlist",
                "address": wallet,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": limit,
                "sort": "desc",
                "apikey": api_key,
            }

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "1":
                        return data.get("result", [])
                return []
        except Exception as e:
            logger.error(f"Error fetching Polygon transactions: {e}")
            return []

    async def get_token_transfers(self, wallet: str, limit: int = 100) -> List[Dict]:
        """
        Get ERC-1155 token transfers (Polymarket uses ERC-1155 for outcome tokens).
        """
        api_key = os.getenv("POLYGONSCAN_API_KEY", "")

        try:
            url = "https://api.polygonscan.com/api"
            params = {
                "module": "account",
                "action": "token1155tx",
                "address": wallet,
                "page": 1,
                "offset": limit,
                "sort": "desc",
                "apikey": api_key,
            }

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "1":
                        return data.get("result", [])
                return []
        except Exception as e:
            logger.error(f"Error fetching token transfers: {e}")
            return []


# ============================================================================
# TRADE MONITOR
# ============================================================================

class TradeMonitor:
    """Monitors a target wallet for Polymarket trades."""

    def __init__(self, config: CopyTraderConfig, client: PolymarketClient):
        self.config = config
        self.client = client

        # Track seen trades to avoid duplicates
        self._seen_trades: set = set()
        self._seen_tx_hashes: set = set()

        # Recent trades buffer
        self._recent_trades: deque = deque(maxlen=100)

        # Last check timestamp
        self._last_check: Optional[datetime] = None

    async def check_for_new_trades(self) -> List[Trade]:
        """
        Check for new trades by the target wallet.
        Returns list of new trades since last check.
        """
        new_trades = []
        now = datetime.now(timezone.utc)

        # Method 1: Check CLOB API for trades
        clob_trades = await self._check_clob_trades()
        new_trades.extend(clob_trades)

        # Method 2: Check on-chain token transfers
        chain_trades = await self._check_chain_transfers()
        new_trades.extend(chain_trades)

        # Deduplicate
        unique_trades = []
        for trade in new_trades:
            trade_key = f"{trade.tx_hash}_{trade.token_id}_{trade.side}"
            if trade_key not in self._seen_trades:
                self._seen_trades.add(trade_key)
                unique_trades.append(trade)
                self._recent_trades.append(trade)

        self._last_check = now
        return unique_trades

    async def _check_clob_trades(self) -> List[Trade]:
        """Check Polymarket CLOB API for recent trades."""
        trades = []

        try:
            raw_trades = await self.client.get_user_trades(self.config.target_wallet)

            for raw in raw_trades:
                try:
                    # Parse trade timestamp
                    ts_str = raw.get("timestamp") or raw.get("created_at")
                    if ts_str:
                        if isinstance(ts_str, (int, float)):
                            ts = datetime.fromtimestamp(ts_str, tz=timezone.utc)
                        else:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = datetime.now(timezone.utc)

                    # Skip old trades
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > self.config.max_copy_delay_seconds * 10:
                        continue

                    # Parse trade details
                    trade = Trade(
                        tx_hash=raw.get("transaction_hash", raw.get("id", "")),
                        timestamp=ts,
                        wallet=self.config.target_wallet,
                        market_slug=raw.get("market", raw.get("market_slug", "")),
                        token_id=raw.get("asset_id", raw.get("token_id", "")),
                        side=raw.get("side", "BUY").upper(),
                        outcome=raw.get("outcome", "YES"),
                        price=Decimal(str(raw.get("price", 0))),
                        amount=Decimal(str(raw.get("size", raw.get("amount", 0)))),
                        usd_value=Decimal(str(raw.get("price", 0))) * Decimal(str(raw.get("size", 0))),
                        raw_data=raw,
                    )
                    trades.append(trade)

                except Exception as e:
                    logger.warning(f"Failed to parse CLOB trade: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error checking CLOB trades: {e}")

        return trades

    async def _check_chain_transfers(self) -> List[Trade]:
        """Check on-chain for ERC-1155 token transfers (Polymarket outcome tokens)."""
        trades = []

        try:
            transfers = await self.client.get_token_transfers(self.config.target_wallet)

            for tx in transfers:
                try:
                    tx_hash = tx.get("hash", "")
                    if tx_hash in self._seen_tx_hashes:
                        continue
                    self._seen_tx_hashes.add(tx_hash)

                    # Parse timestamp
                    ts = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)

                    # Skip old transactions
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > self.config.max_copy_delay_seconds * 10:
                        continue

                    # Determine if BUY or SELL based on transfer direction
                    target_lower = self.config.target_wallet.lower()
                    is_incoming = tx.get("to", "").lower() == target_lower

                    # Token ID from the transfer
                    token_id = tx.get("tokenID", "")

                    # Try to get market info
                    market_info = await self.client.get_market_info(token_id)
                    market_slug = market_info.get("slug", "") if market_info else ""

                    trade = Trade(
                        tx_hash=tx_hash,
                        timestamp=ts,
                        wallet=self.config.target_wallet,
                        market_slug=market_slug,
                        token_id=token_id,
                        side="BUY" if is_incoming else "SELL",
                        outcome="YES",  # Need to determine from token ID
                        price=Decimal("0.5"),  # Unknown from transfer alone
                        amount=Decimal(tx.get("tokenValue", 0)),
                        usd_value=Decimal("0"),  # Calculate later
                        raw_data=tx,
                    )
                    trades.append(trade)

                except Exception as e:
                    logger.warning(f"Failed to parse chain transfer: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error checking chain transfers: {e}")

        return trades


# ============================================================================
# TRADE EXECUTOR
# ============================================================================

class TradeExecutor:
    """Executes copy trades on your account."""

    def __init__(self, config: CopyTraderConfig):
        self.config = config
        self._daily_pnl = Decimal("0")
        self._daily_trades = 0
        self._last_reset = datetime.now(timezone.utc).date()

        # Track executed copies
        self._executed_copies: List[Dict] = []

    def _reset_daily_if_needed(self):
        """Reset daily counters if new day."""
        today = datetime.now(timezone.utc).date()
        if today > self._last_reset:
            self._daily_pnl = Decimal("0")
            self._daily_trades = 0
            self._last_reset = today
            logger.info("Daily counters reset")

    def _check_risk_limits(self, trade: Trade) -> tuple[bool, str]:
        """Check if we should copy this trade based on risk limits."""
        self._reset_daily_if_needed()

        # Check daily loss limit
        if self._daily_pnl < -Decimal(str(self.config.daily_loss_limit_usd)):
            return False, f"Daily loss limit reached (${self._daily_pnl:.2f})"

        return True, ""

    def calculate_copy_size(self, trade: Trade) -> Decimal:
        """Calculate how much to copy based on config."""
        # Start with their trade size * copy percentage
        copy_usd = trade.usd_value * Decimal(str(self.config.copy_percentage))

        # Apply min/max limits
        copy_usd = max(copy_usd, Decimal(str(self.config.min_trade_size_usd)))
        copy_usd = min(copy_usd, Decimal(str(self.config.max_trade_size_usd)))

        return copy_usd

    async def execute_copy(self, trade: Trade) -> bool:
        """
        Execute a copy of the given trade.
        Returns True if successful.
        """
        # Check risk limits
        allowed, reason = self._check_risk_limits(trade)
        if not allowed:
            logger.warning(f"Trade blocked: {reason}")
            return False

        copy_size = self.calculate_copy_size(trade)

        logger.info("=" * 70)
        logger.info(f"📋 COPYING TRADE")
        logger.info(f"   Target Wallet: {trade.wallet[:10]}...{trade.wallet[-6:]}")
        logger.info(f"   Market: {trade.market_slug}")
        logger.info(f"   Side: {trade.side} {trade.outcome}")
        logger.info(f"   Their Size: ${trade.usd_value:.2f}")
        logger.info(f"   Our Copy Size: ${copy_size:.2f}")
        logger.info(f"   Price: ${trade.price:.4f}")

        if not self.config.enable_live_trading:
            logger.info(f"   [SIMULATION MODE - No real trade executed]")
            self._executed_copies.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "original_trade": trade.tx_hash,
                "market": trade.market_slug,
                "side": trade.side,
                "outcome": trade.outcome,
                "copy_size_usd": float(copy_size),
                "price": float(trade.price),
                "simulated": True,
            })
            self._daily_trades += 1
            logger.info("=" * 70)
            return True

        # === LIVE TRADING ===
        try:
            # Import Nautilus trading components
            from nautilus_trader.model.enums import OrderSide, TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
            from nautilus_trader.model.objects import Quantity, Price

            # This would integrate with your existing bot infrastructure
            # For now, we'll use the Polymarket py-clob-client

            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs

            host = "https://clob.polymarket.com"
            chain_id = 137  # Polygon mainnet

            client = ClobClient(
                host=host,
                key=os.getenv("POLYMARKET_PK"),
                chain_id=chain_id,
                signature_type=1,
                funder=os.getenv("POLYMARKET_FUNDER"),
            )

            # Build order
            order_args = OrderArgs(
                token_id=trade.token_id,
                price=float(trade.price),
                size=float(copy_size / trade.price),  # Convert USD to token quantity
                side=trade.side,
            )

            # Create and submit order
            signed_order = client.create_order(order_args)
            result = client.post_order(signed_order)

            logger.info(f"   Order submitted: {result}")

            self._executed_copies.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "original_trade": trade.tx_hash,
                "market": trade.market_slug,
                "side": trade.side,
                "outcome": trade.outcome,
                "copy_size_usd": float(copy_size),
                "price": float(trade.price),
                "simulated": False,
                "order_result": result,
            })
            self._daily_trades += 1
            logger.info("=" * 70)
            return True

        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            return False
        except Exception as e:
            logger.error(f"Failed to execute copy trade: {e}")
            import traceback
            traceback.print_exc()
            return False


# ============================================================================
# COPY TRADER BOT
# ============================================================================

class CopyTraderBot:
    """Main copy trading bot."""

    def __init__(self, config: CopyTraderConfig):
        self.config = config
        self.client = PolymarketClient(config)
        self.monitor = TradeMonitor(config, self.client)
        self.executor = TradeExecutor(config)

        self._running = False
        self._stats = {
            "trades_detected": 0,
            "trades_copied": 0,
            "trades_skipped": 0,
            "total_copy_volume_usd": Decimal("0"),
        }

    async def start(self):
        """Start the copy trading bot."""
        logger.info("=" * 70)
        logger.info("🤖 POLYMARKET COPY TRADER")
        logger.info("=" * 70)
        logger.info(f"Target Wallet: {self.config.target_wallet}")
        logger.info(f"Copy Percentage: {self.config.copy_percentage * 100:.0f}%")
        logger.info(f"Max Trade Size: ${self.config.max_trade_size_usd:.2f}")
        logger.info(f"Poll Interval: {self.config.poll_interval_seconds}s")
        logger.info(f"Mode: {'LIVE TRADING' if self.config.enable_live_trading else 'SIMULATION'}")
        logger.info("=" * 70)

        await self.client.connect()
        self._running = True

        # Initial check to seed seen trades (don't copy old trades)
        logger.info("Seeding initial trade history (not copying old trades)...")
        initial_trades = await self.monitor.check_for_new_trades()
        logger.info(f"Found {len(initial_trades)} existing trades - will only copy NEW trades from now")

        logger.info("=" * 70)
        logger.info("👀 MONITORING FOR NEW TRADES...")
        logger.info("=" * 70)

        try:
            while self._running:
                await self._check_and_copy()
                await asyncio.sleep(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            await self.stop()

    async def stop(self):
        """Stop the copy trading bot."""
        self._running = False
        await self.client.disconnect()

        # Print final stats
        logger.info("=" * 70)
        logger.info("📊 FINAL STATISTICS")
        logger.info(f"   Trades Detected: {self._stats['trades_detected']}")
        logger.info(f"   Trades Copied: {self._stats['trades_copied']}")
        logger.info(f"   Trades Skipped: {self._stats['trades_skipped']}")
        logger.info(f"   Total Copy Volume: ${self._stats['total_copy_volume_usd']:.2f}")
        logger.info("=" * 70)

    async def _check_and_copy(self):
        """Check for new trades and copy them."""
        try:
            new_trades = await self.monitor.check_for_new_trades()

            for trade in new_trades:
                self._stats["trades_detected"] += 1

                # Check if trade is recent enough to copy
                age = (datetime.now(timezone.utc) - trade.timestamp).total_seconds()
                if age > self.config.max_copy_delay_seconds:
                    logger.info(f"⏭️ Skipping old trade ({age:.0f}s old): {trade.market_slug}")
                    self._stats["trades_skipped"] += 1
                    continue

                # Log detected trade
                logger.info("-" * 70)
                logger.info(f"🎯 NEW TRADE DETECTED!")
                logger.info(f"   Time: {trade.timestamp.strftime('%H:%M:%S')} UTC ({age:.0f}s ago)")
                logger.info(f"   Market: {trade.market_slug}")
                logger.info(f"   Action: {trade.side} {trade.outcome}")
                logger.info(f"   Size: ${trade.usd_value:.2f} @ ${trade.price:.4f}")

                # Copy the trade
                success = await self.executor.execute_copy(trade)

                if success:
                    self._stats["trades_copied"] += 1
                    copy_size = self.executor.calculate_copy_size(trade)
                    self._stats["total_copy_volume_usd"] += copy_size
                else:
                    self._stats["trades_skipped"] += 1

        except Exception as e:
            logger.error(f"Error in check_and_copy: {e}")
            import traceback
            traceback.print_exc()


# ============================================================================
# MAIN
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Copy Trader")
    parser.add_argument(
        "--target", "-t",
        default="0xde17f7144fbd0eddb2679132c10ff5e74b120988",
        help="Target wallet address to copy"
    )
    parser.add_argument(
        "--copy-pct",
        type=float,
        default=1.0,
        help="Percentage of trade size to copy (1.0 = 100%%)"
    )
    parser.add_argument(
        "--max-size",
        type=float,
        default=10.0,
        help="Maximum USD per copied trade"
    )
    parser.add_argument(
        "--min-size",
        type=float,
        default=1.0,
        help="Minimum USD per copied trade"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between checks for new trades"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable LIVE trading (real money!)"
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=30.0,
        help="Maximum seconds delay to still copy a trade"
    )

    args = parser.parse_args()

    config = CopyTraderConfig(
        target_wallet=args.target,
        copy_percentage=args.copy_pct,
        max_trade_size_usd=args.max_size,
        min_trade_size_usd=args.min_size,
        poll_interval_seconds=args.poll_interval,
        enable_live_trading=args.live,
        max_copy_delay_seconds=args.max_delay,
    )

    bot = CopyTraderBot(config)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\nShutdown requested...")


if __name__ == "__main__":
    main()
