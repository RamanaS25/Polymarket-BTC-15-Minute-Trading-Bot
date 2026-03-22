# Polymarket BTC Trading Bot - Mission & Status

## PRIMARY GOAL
Make this trading bot consistently profitable on Polymarket BTC 15-minute UP/DOWN binary markets.

## CURRENT STATUS
- **Date**: 2026-03-22
- **Mode**: Simulation (paper trading)
- **Strategy**: Late-window trend following

## STRATEGY OVERVIEW

### Core Concept
Trade in the **late window** (minutes 13-14 of 15-minute markets) when the price reflects the market's nearly-final verdict. The price IS the prediction - if YES token is at $0.75, the market is 75% confident BTC went UP.

### Trend Filter Thresholds (UPDATED)
```
Price > 0.68 → BUY YES (betting UP) - 68%+ confidence
Price < 0.32 → BUY NO (betting DOWN) - 68%+ confidence
Price 0.32-0.68 → SKIP (too close to coin flip)
```

**Rationale**: The tighter thresholds (was 0.60/0.40) reduce trade frequency but increase quality. At 0.68, expected win rate is ~68% before accounting for momentum continuation edge.

### Signal Processors (6 total)
| Processor | Weight | Purpose |
|-----------|--------|---------|
| OrderBookImbalance | 0.30 | CLOB depth imbalance |
| TickVelocity | 0.25 | Price momentum (60s/30s) |
| PriceDivergence | 0.18 | Polymarket vs BTC spot |
| SpikeDetection | 0.12 | Mean reversion detector |
| DeribitPCR | 0.10 | Options put/call ratio |
| SentimentAnalysis | 0.05 | Fear & Greed Index |

### Position Sizing
- Fixed $1.00 per trade (conservative)
- No variable sizing based on confidence (yet)

## CURRENT PERFORMANCE

### Paper Trades Analysis (27 trades)
- Total Trades: 27
- Wins: 10, Losses: 17
- Win Rate: 37% (POOR - but simulation is flawed)

### Key Issues Identified
1. **Simulation Bug**: Paper trade simulation uses random movement based on signal direction, NOT actual market resolution. This doesn't reflect real trading outcomes.
2. **Too many trades at 0.40-0.60 range** - coin flip territory (with old thresholds)
3. **Need real market validation** to assess true edge

### Simulation vs Reality
The simulation win/loss is determined by:
- Random movement ±2-8% applied to price
- Movement bias based on SIGNAL direction (from fusion engine)
- But our TRADE direction is based on PRICE (trend filter)

This mismatch means simulation results don't predict real performance. Need to:
1. Either fix simulation to match market resolution
2. Or run small live trades to gather real data

## CHANGES MADE

### 2026-03-22
1. **Tightened trend thresholds** from 0.60/0.40 to 0.68/0.32
   - File: `bot.py` line ~976-977
   - Expected impact: Fewer trades but higher win rate

## NEXT STEPS

### Immediate (This Session)
- [ ] Run bot in test mode and collect more data with new thresholds
- [ ] Monitor paper trades to verify improvement
- [ ] Consider moving to live mode if win rate improves to 60%+

### Short Term
- [ ] Analyze which signal processors are most predictive
- [ ] Consider implementing Kelly Criterion for position sizing
- [ ] Add real outcome validation (check market resolution)
- [ ] Evaluate copy trading as alternative strategy

### Long Term
- [ ] Build backtesting framework using historical data
- [ ] Implement adaptive threshold adjustment based on performance
- [ ] Consider multiple timeframe strategies (5-second markets)

## COPY TRADING OPTION

Target wallet: `0xde17f7144fbd0eddb2679132c10ff5e74b120988`
- This is configured in `copy_trader.py`
- Could be an alternative if direct trading underperforms
- Need to research this wallet's historical performance

## FILES TO KNOW

| File | Purpose |
|------|---------|
| `bot.py` | Main 15-minute trading bot |
| `copy_trader.py` | Wallet copy trading |
| `paper_trades.json` | Simulation trade records |
| `persistence/` | PostgreSQL database layer |
| `15m_bot_runner.py` | Auto-restart wrapper |

## COMMANDS

```bash
# Run in simulation test mode (fast trades)
python bot.py --test-mode --no-grafana

# Run in normal simulation mode
python bot.py --no-grafana

# Run LIVE (real money)
python bot.py --live --no-grafana

# View paper trades
python view_paper_trades.py
```

## NOTES

- Bot auto-restarts every 90 minutes to refresh filters
- Redis connection optional (controls sim/live mode toggle)
- PostgreSQL connection optional (for persistence)
- Current thresholds are conservative - may need loosening if too few trades
- Markets near resolution (price at 0.99) have no liquidity - can't trade

## SESSION LOG

### 2026-03-22 15:00 UTC
- Explored codebase comprehensively
- Identified simulation flaw (random movement doesn't match market resolution)
- Tightened trend thresholds: 0.60/0.40 → 0.68/0.32
- Started bot in simulation mode
- Observed market near resolution (bid=0.99, no ask) - normal behavior
- Created this persistence file

### IMMEDIATE TODO
1. Wait for next market cycle (15 min) to test new thresholds
2. If few trades, consider loosening to 0.65/0.35
3. Consider small live trades ($1) to get real outcome data
