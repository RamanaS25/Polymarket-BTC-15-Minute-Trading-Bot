#!/usr/bin/env python3
"""
Database Setup Script

Run this script to initialize the PostgreSQL database and create all tables.

Usage:
    python -m persistence.setup_db

Environment Variables:
    POSTGRES_HOST: PostgreSQL host (default: localhost)
    POSTGRES_PORT: PostgreSQL port (default: 5432)
    POSTGRES_DB: Database name (default: trading_bot)
    POSTGRES_USER: Database user (default: trading)
    POSTGRES_PASSWORD: Database password

To create the database and user (run as postgres superuser):
    CREATE USER trading WITH PASSWORD 'your_password';
    CREATE DATABASE trading_bot OWNER trading;
    GRANT ALL PRIVILEGES ON DATABASE trading_bot TO trading;
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()


async def setup_database():
    """Initialize database and create all tables."""
    from persistence import init_database, close_database

    print("=" * 60)
    print("TRADING BOT DATABASE SETUP")
    print("=" * 60)

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "trading_bot")
    user = os.getenv("POSTGRES_USER", "trading")

    print(f"\nConnection details:")
    print(f"  Host: {host}")
    print(f"  Port: {port}")
    print(f"  Database: {database}")
    print(f"  User: {user}")
    print()

    try:
        print("Connecting to database...")
        db = await init_database(create_tables=True)
        print("[OK] Database connection established")
        print("[OK] All tables created successfully")

        # List tables
        from persistence.models import Base
        tables = list(Base.metadata.tables.keys())
        print(f"\nCreated {len(tables)} tables:")
        for table in sorted(tables):
            print(f"  - {table}")

        await close_database()
        print("\n[OK] Database setup complete!")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Create the database and user:")
        print("   CREATE USER trading WITH PASSWORD 'your_password';")
        print("   CREATE DATABASE trading_bot OWNER trading;")
        print("3. Set environment variables in .env file:")
        print("   POSTGRES_HOST=localhost")
        print("   POSTGRES_PORT=5432")
        print("   POSTGRES_DB=trading_bot")
        print("   POSTGRES_USER=trading")
        print("   POSTGRES_PASSWORD=your_password")
        sys.exit(1)


async def verify_database():
    """Verify database connection and show table counts."""
    from persistence import init_database, close_database, get_repository

    print("\n" + "=" * 60)
    print("DATABASE VERIFICATION")
    print("=" * 60)

    try:
        db = await init_database(create_tables=False)
        repo = get_repository()

        # Get some stats
        stats = await repo.get_trade_stats()
        paper_trades = await repo.get_paper_trades(limit=5)

        print(f"\nDatabase Statistics:")
        print(f"  Total trades: {stats['total_trades']}")
        print(f"  Winning trades: {stats['winning_trades']}")
        print(f"  Losing trades: {stats['losing_trades']}")
        print(f"  Win rate: {stats['win_rate']:.1f}%")
        print(f"  Total P&L: ${float(stats['total_pnl']):.2f}")

        if paper_trades:
            print(f"\nRecent paper trades:")
            for pt in paper_trades[:5]:
                outcome = pt.outcome or "PENDING"
                pnl = f"${float(pt.pnl_usd):.2f}" if pt.pnl_usd else "N/A"
                print(f"  [{pt.timestamp.strftime('%Y-%m-%d %H:%M')}] {pt.direction} - {outcome} ({pnl})")

        await close_database()
        print("\n[OK] Database verification complete!")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bot Database Setup")
    parser.add_argument("--verify", action="store_true", help="Verify database and show stats")
    args = parser.parse_args()

    if args.verify:
        asyncio.run(verify_database())
    else:
        asyncio.run(setup_database())


if __name__ == "__main__":
    main()
