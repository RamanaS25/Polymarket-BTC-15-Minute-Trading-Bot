#!/usr/bin/env python3
"""
View Trades from PostgreSQL Database

Provides a command-line interface to query and analyze trading data.

Usage:
    python -m persistence.view_trades [options]

Examples:
    python -m persistence.view_trades                    # Show recent trades
    python -m persistence.view_trades --stats            # Show overall statistics
    python -m persistence.view_trades --signals          # Show signal effectiveness
    python -m persistence.view_trades --date 2024-03-17  # Show trades for specific date
    python -m persistence.view_trades --export trades.csv # Export to CSV
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()


def format_decimal(val, prefix="$", decimals=2):
    """Format decimal value for display."""
    if val is None:
        return "N/A"
    return f"{prefix}{float(val):,.{decimals}f}"


def format_pct(val, decimals=1):
    """Format percentage value for display."""
    if val is None:
        return "N/A"
    return f"{float(val):.{decimals}f}%"


async def show_recent_trades(limit: int = 20, simulation_only: bool = None):
    """Show recent trades."""
    from persistence import init_database, close_database, get_repository

    await init_database(create_tables=False)
    repo = get_repository()

    trades = await repo.get_recent_trades(limit=limit, simulation_only=simulation_only)

    print(f"\n{'='*80}")
    print(f"RECENT TRADES (last {len(trades)})")
    print(f"{'='*80}")

    if not trades:
        print("No trades found.")
        await close_database()
        return

    print(f"\n{'Time':<20} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Outcome':<8} {'Market':<25}")
    print("-" * 100)

    for trade in trades:
        time_str = trade.entry_time.strftime("%Y-%m-%d %H:%M") if trade.entry_time else "N/A"
        direction = trade.direction.upper()[:5] if trade.direction else "N/A"
        entry = format_decimal(trade.entry_price, "$", 4)
        exit_p = format_decimal(trade.exit_price, "$", 4) if trade.exit_price else "OPEN"
        pnl = format_decimal(trade.pnl_usd, "$", 4) if trade.pnl_usd else "N/A"
        outcome = trade.outcome or "PENDING"
        market = (trade.market_slug or "unknown")[:25]

        # Color coding (using ANSI codes)
        if outcome == "WIN":
            outcome_str = f"\033[92m{outcome}\033[0m"  # Green
        elif outcome == "LOSS":
            outcome_str = f"\033[91m{outcome}\033[0m"  # Red
        else:
            outcome_str = outcome

        print(f"{time_str:<20} {direction:<6} {entry:>10} {exit_p:>10} {pnl:>10} {outcome_str:<8} {market:<25}")

    await close_database()


async def show_stats(since_days: int = 30):
    """Show overall trading statistics."""
    from persistence import init_database, close_database, get_repository

    await init_database(create_tables=False)
    repo = get_repository()

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    stats = await repo.get_trade_stats(since=since)

    print(f"\n{'='*60}")
    print(f"TRADING STATISTICS (last {since_days} days)")
    print(f"{'='*60}")

    print(f"\n  Total Trades:    {stats['total_trades']}")
    print(f"  Winning Trades:  {stats['winning_trades']}")
    print(f"  Losing Trades:   {stats['losing_trades']}")
    print(f"  Win Rate:        {format_pct(stats['win_rate'])}")
    print(f"\n  Total P&L:       {format_decimal(stats['total_pnl'])}")
    print(f"  Average P&L:     {format_decimal(stats['avg_pnl'])}")
    print(f"  Best Trade:      {format_decimal(stats['best_trade'])}")
    print(f"  Worst Trade:     {format_decimal(stats['worst_trade'])}")

    await close_database()


async def show_signal_effectiveness(since_days: int = 30):
    """Show signal processor effectiveness."""
    from persistence import init_database, close_database, get_repository

    await init_database(create_tables=False)
    repo = get_repository()

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    signals = await repo.get_signal_effectiveness(since=since)

    print(f"\n{'='*60}")
    print(f"SIGNAL EFFECTIVENESS (last {since_days} days)")
    print(f"{'='*60}")

    if not signals:
        print("\nNo signal data found.")
        await close_database()
        return

    print(f"\n{'Source':<25} {'Signals':>10} {'Avg Score':>12} {'Avg Conf':>12}")
    print("-" * 60)

    for sig in signals:
        source = sig['source'][:25]
        count = sig['signal_count']
        avg_score = format_pct(sig['avg_score'], 1) if sig['avg_score'] else "N/A"
        avg_conf = format_pct(sig['avg_confidence'] * 100, 1) if sig['avg_confidence'] else "N/A"

        print(f"{source:<25} {count:>10} {avg_score:>12} {avg_conf:>12}")

    await close_database()


async def show_paper_trades(limit: int = 20):
    """Show recent paper trades."""
    from persistence import init_database, close_database, get_repository

    await init_database(create_tables=False)
    repo = get_repository()

    trades = await repo.get_paper_trades(limit=limit)

    print(f"\n{'='*80}")
    print(f"PAPER TRADES (last {len(trades)})")
    print(f"{'='*80}")

    if not trades:
        print("No paper trades found.")
        await close_database()
        return

    print(f"\n{'Time':<20} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Outcome':<8} {'Signal':>8}")
    print("-" * 90)

    for trade in trades:
        time_str = trade.timestamp.strftime("%Y-%m-%d %H:%M") if trade.timestamp else "N/A"
        direction = trade.direction[:5] if trade.direction else "N/A"
        entry = format_decimal(trade.entry_price, "$", 4)
        exit_p = format_decimal(trade.exit_price, "$", 4) if trade.exit_price else "N/A"
        pnl = format_decimal(trade.pnl_usd, "$", 4) if trade.pnl_usd else "N/A"
        outcome = trade.outcome or "PENDING"
        signal = f"{trade.signal_score:.0f}" if trade.signal_score else "N/A"

        # Color coding
        if outcome == "WIN":
            outcome_str = f"\033[92m{outcome}\033[0m"
        elif outcome == "LOSS":
            outcome_str = f"\033[91m{outcome}\033[0m"
        else:
            outcome_str = outcome

        print(f"{time_str:<20} {direction:<6} {entry:>10} {exit_p:>10} {pnl:>10} {outcome_str:<8} {signal:>8}")

    await close_database()


async def export_trades_csv(output_file: str, since_days: int = 30):
    """Export trades to CSV file."""
    import csv
    from persistence import init_database, close_database, get_repository

    await init_database(create_tables=False)
    repo = get_repository()

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    trades = await repo.get_recent_trades(limit=10000)  # Get all recent trades

    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'entry_time', 'exit_time', 'direction', 'entry_price', 'exit_price',
            'size_usd', 'pnl_usd', 'pnl_pct', 'outcome', 'signal_score',
            'signal_confidence', 'market_slug', 'is_simulation'
        ])

        for trade in trades:
            writer.writerow([
                trade.entry_time.isoformat() if trade.entry_time else '',
                trade.exit_time.isoformat() if trade.exit_time else '',
                trade.direction,
                float(trade.entry_price) if trade.entry_price else '',
                float(trade.exit_price) if trade.exit_price else '',
                float(trade.size_usd) if trade.size_usd else '',
                float(trade.pnl_usd) if trade.pnl_usd else '',
                trade.pnl_pct if trade.pnl_pct else '',
                trade.outcome,
                trade.signal_score,
                trade.signal_confidence,
                trade.market_slug,
                trade.is_simulation,
            ])

    print(f"\n✓ Exported {len(trades)} trades to {output_file}")
    await close_database()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="View Trading Data from PostgreSQL")
    parser.add_argument("--trades", action="store_true", help="Show recent trades (default)")
    parser.add_argument("--paper", action="store_true", help="Show paper trades")
    parser.add_argument("--stats", action="store_true", help="Show overall statistics")
    parser.add_argument("--signals", action="store_true", help="Show signal effectiveness")
    parser.add_argument("--limit", type=int, default=20, help="Number of records to show")
    parser.add_argument("--days", type=int, default=30, help="Days to look back for stats")
    parser.add_argument("--export", type=str, help="Export trades to CSV file")
    parser.add_argument("--simulation", action="store_true", help="Show only simulation trades")
    parser.add_argument("--live", action="store_true", help="Show only live trades")
    args = parser.parse_args()

    # Determine which view to show
    if args.export:
        asyncio.run(export_trades_csv(args.export, args.days))
    elif args.stats:
        asyncio.run(show_stats(args.days))
    elif args.signals:
        asyncio.run(show_signal_effectiveness(args.days))
    elif args.paper:
        asyncio.run(show_paper_trades(args.limit))
    else:
        simulation_only = None
        if args.simulation:
            simulation_only = True
        elif args.live:
            simulation_only = False
        asyncio.run(show_recent_trades(args.limit, simulation_only))


if __name__ == "__main__":
    main()
