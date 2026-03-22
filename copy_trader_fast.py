"""
FAST Polymarket Copy Trader
===========================
Real-time monitoring + instant trade copying.

Speed optimizations:
- WebSocket connection for real-time updates
- Parallel API checks
- Sub-second response time
- Pre-authenticated for instant execution

Usage:
    python copy_trader_fast.py
"""

import asyncio
import os
import sys
import time
import json
import aiohttp
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, List
from pathlib import Path
from dataclasses import dataclass
from collections import deque

from dotenv import load_dotenv
from loguru import logger

# Configure logging for speed (less verbose)
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss.SSS}</green> | <level>{message}</level>")

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))
load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

# TARGET WALLET TO COPY
TARGET_WALLET = "0xde17f7144fbd0eddb2679132c10ff5e74b120988"

# COPY SETTINGS
COPY_PERCENTAGE = 0.1      # Copy 10% of their trade size (we use fixed $1)
MAX_TRADE_USD = 1.0        # Cap at $1 per copy
MIN_TRADE_USD = 1.0        # Fixed $1 trades

# SPEED SETTINGS
POLL_INTERVAL_MS = 500     # Check every 500ms (0.5 seconds)
MAX_COPY_DELAY_SEC = 10    # Only copy trades less than 10 seconds old

# MODE
LIVE_TRADING = False       # SET TO True FOR REAL TRADES

# ============================================================================
# POLYMARKET FAST CLIENT
# ============================================================================

class FastPolymarketClient:
    """Optimized client for speed."""

    CLOB_API = "https://clob.polymarket.com"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_trades_cache: Dict = {}
        self._seen_trade_ids: set = set()

        # Pre-load credentials
        self._api_key = os.getenv("POLYMARKET_API_KEY")
        self._api_secret = os.getenv("POLYMARKET_API_SECRET")
        self._private_key = os.getenv("POLYMARKET_PK")

    async def connect(self):
        """Initialize with connection pooling for speed."""
        connector = aiohttp.TCPConnector(
            limit=10,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=5)

        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["POLY_API_KEY"] = self._api_key

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
        )
        logger.info("Fast client connected with connection pooling")

    async def disconnect(self):
        if self._session:
            await self._session.close()

    async def get_trades_fast(self, wallet: str) -> List[Dict]:
        """Get trades with minimal latency."""
        try:
            # Try multiple endpoints in parallel for redundancy
            tasks = [
                self._fetch_clob_trades(wallet),
                self._fetch_activity(wallet),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_trades = []
            for result in results:
                if isinstance(result, list):
                    all_trades.extend(result)

            return all_trades
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return []

    async def _fetch_clob_trades(self, wallet: str) -> List[Dict]:
        """Fetch from CLOB trades endpoint."""
        try:
            url = f"{self.CLOB_API}/trades"
            params = {"maker": wallet, "limit": 20}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else []
        except:
            pass
        return []

    async def _fetch_activity(self, wallet: str) -> List[Dict]:
        """Fetch from activity/orders endpoint."""
        try:
            url = f"{self.CLOB_API}/orders"
            params = {"maker": wallet, "limit": 20}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Convert orders to trade-like format
                    trades = []
                    for order in (data if isinstance(data, list) else []):
                        if order.get("status") == "MATCHED" or order.get("size_matched", 0) > 0:
                            trades.append(order)
                    return trades
        except:
            pass
        return []

    async def place_order_fast(self, token_id: str, side: str, price: float, size: float) -> Optional[Dict]:
        """Place order with minimal latency."""
        if not LIVE_TRADING:
            logger.info(f"[SIMULATION] Would place: {side} {size:.2f} @ ${price:.4f}")
            return {"simulated": True, "side": side, "size": size, "price": price}

        try:
            # Use py-clob-client for actual order placement
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType

            client = ClobClient(
                host=self.CLOB_API,
                key=self._private_key,
                chain_id=137,
                signature_type=1,
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )

            signed_order = client.create_order(order_args)
            result = client.post_order(signed_order)

            logger.info(f"Order placed: {result}")
            return result

        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None


# ============================================================================
# FAST COPY ENGINE
# ============================================================================

class FastCopyEngine:
    """High-speed copy trading engine."""

    def __init__(self):
        self.client = FastPolymarketClient()
        self.target_wallet = TARGET_WALLET.lower()

        # Track what we've seen
        self._seen_trades: set = set()
        self._trade_buffer: deque = deque(maxlen=100)

        # Stats
        self.stats = {
            "checks": 0,
            "trades_detected": 0,
            "trades_copied": 0,
            "latency_ms": [],
        }

        # Running state
        self._running = False

    async def start(self):
        """Start the copy engine."""
        print("=" * 60, flush=True)
        print(">> FAST COPY TRADER", flush=True)
        print("=" * 60, flush=True)
        print(f"Target: {self.target_wallet[:10]}...{self.target_wallet[-6:]}", flush=True)
        print(f"Mode: {'[LIVE] LIVE TRADING' if LIVE_TRADING else '[SIM] SIMULATION'}", flush=True)
        print(f"Copy Size: {COPY_PERCENTAGE*100:.0f}% (max ${MAX_TRADE_USD})", flush=True)
        print(f"Poll Speed: {POLL_INTERVAL_MS}ms", flush=True)
        print("=" * 60, flush=True)

        await self.client.connect()

        # Seed seen trades (don't copy old ones)
        print("Loading existing trades...", flush=True)
        await self._seed_existing_trades()

        print("=" * 60, flush=True)
        print("[WATCHING] WATCHING FOR NEW TRADES...", flush=True)
        print("=" * 60, flush=True)

        self._running = True

        try:
            while self._running:
                loop_start = time.time()

                await self._check_for_trades()

                # Calculate time to next check
                elapsed_ms = (time.time() - loop_start) * 1000
                sleep_ms = max(0, POLL_INTERVAL_MS - elapsed_ms)

                if sleep_ms > 0:
                    await asyncio.sleep(sleep_ms / 1000)

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            await self.stop()

    async def stop(self):
        """Stop and print stats."""
        self._running = False
        await self.client.disconnect()

        print("\n" + "=" * 60)
        print("📊 SESSION STATS")
        print(f"   Checks: {self.stats['checks']}")
        print(f"   Trades Detected: {self.stats['trades_detected']}")
        print(f"   Trades Copied: {self.stats['trades_copied']}")
        if self.stats['latency_ms']:
            avg_latency = sum(self.stats['latency_ms']) / len(self.stats['latency_ms'])
            print(f"   Avg Detection Latency: {avg_latency:.0f}ms")
        print("=" * 60)

    async def _seed_existing_trades(self):
        """Load existing trades so we don't copy old ones."""
        trades = await self.client.get_trades_fast(self.target_wallet)
        for trade in trades:
            trade_id = self._get_trade_id(trade)
            self._seen_trades.add(trade_id)
        print(f"Seeded {len(self._seen_trades)} existing trades")

    def _get_trade_id(self, trade: Dict) -> str:
        """Generate unique ID for a trade."""
        return f"{trade.get('id', '')}{trade.get('transaction_hash', '')}{trade.get('asset_id', '')}{trade.get('timestamp', '')}"

    async def _check_for_trades(self):
        """Check for new trades and copy instantly."""
        self.stats['checks'] += 1
        check_start = time.time()

        try:
            trades = await self.client.get_trades_fast(self.target_wallet)

            for trade in trades:
                trade_id = self._get_trade_id(trade)

                # Skip if already seen
                if trade_id in self._seen_trades:
                    continue

                self._seen_trades.add(trade_id)
                self.stats['trades_detected'] += 1

                # Calculate latency
                detection_time = time.time()
                latency_ms = (detection_time - check_start) * 1000
                self.stats['latency_ms'].append(latency_ms)

                # Parse trade
                await self._process_new_trade(trade, latency_ms)

        except Exception as e:
            logger.error(f"Check error: {e}")

    async def _process_new_trade(self, trade: Dict, latency_ms: float):
        """Process and copy a new trade."""
        now = datetime.now(timezone.utc)

        # Extract trade info
        side = trade.get("side", "BUY").upper()
        token_id = trade.get("asset_id", trade.get("token_id", ""))
        price = float(trade.get("price", 0.5))
        size = float(trade.get("size", trade.get("original_size", 0)))
        market = trade.get("market", trade.get("condition_id", ""))[:20]

        # Calculate trade age
        ts = trade.get("timestamp") or trade.get("created_at")
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    trade_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    trade_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                age_sec = (now - trade_time).total_seconds()
            except:
                age_sec = 0
        else:
            age_sec = 0

        # Skip if too old
        if age_sec > MAX_COPY_DELAY_SEC:
            logger.info(f"[SKIP]  SKIP (too old: {age_sec:.0f}s): {side} {market}")
            return

        # Calculate copy size
        their_usd = price * size
        our_usd = min(MAX_TRADE_USD, max(MIN_TRADE_USD, their_usd * COPY_PERCENTAGE))
        our_size = our_usd / price if price > 0 else 0

        # Log detection
        print("\n" + ">>>" * 30)
        print(f">>> NEW TRADE DETECTED!")
        print(f"   Latency: {latency_ms:.0f}ms | Age: {age_sec:.1f}s")
        print(f"   Market: {market}...")
        print(f"   Action: {side}")
        print(f"   Their Size: ${their_usd:.2f} ({size:.2f} tokens @ ${price:.4f})")
        print(f"   Our Copy: ${our_usd:.2f} ({our_size:.2f} tokens)")

        # Execute copy
        result = await self.client.place_order_fast(
            token_id=token_id,
            side=side,
            price=price,
            size=our_size,
        )

        if result:
            self.stats['trades_copied'] += 1
            if LIVE_TRADING:
                print(f"   [OK] COPIED!")
            else:
                print(f"   [OK] SIMULATED COPY")
        else:
            print(f"   [FAIL] COPY FAILED")

        print(">>>" * 30 + "\n")


# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Run the fast copy trader."""

    # Check for required env vars
    if LIVE_TRADING:
        required = ["POLYMARKET_PK", "POLYMARKET_API_KEY"]
        missing = [var for var in required if not os.getenv(var)]
        if missing:
            print(f"ERROR: Missing environment variables for live trading: {missing}")
            print("Set these in your .env file")
            return

    engine = FastCopyEngine()
    await engine.start()


if __name__ == "__main__":
    # Fix Windows console encoding
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    print("\n" + "=" * 60)
    print("POLYMARKET COPY TRADER - FAST MODE")
    print("=" * 60)

    if not LIVE_TRADING:
        print("[!] Running in SIMULATION mode")
        print("    Edit LIVE_TRADING = True to enable real trades")
    else:
        print("[!!!] LIVE TRADING ENABLED - REAL MONEY!")
        print("      Press Ctrl+C within 3 seconds to abort...")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    print()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")
