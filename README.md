# Crypto Futures Trading Bot

Advanced automated trading bot for cryptocurrency futures with ML-powered signal generation, smart entry filtering, and real-time web dashboard.

## Features

- **Multi-Exchange Support**: Binance, Bybit, OKX
- **Smart Signal Generation**: 13+ technical indicators with weighted scoring
- **ML-Based Filtering**: Machine learning model for trade quality prediction
- **Smart Entry System**: 11-step validation with confidence scoring (0-100%)
- **Signal Categories**: PREMIUM (75%+), STANDARD (65-74%), RISKY (55-64%), WEAK (<55%)
- **Real-Time Dashboard**: WebSocket-powered UI with live positions and PnL
- **Telegram Integration**: Instant notifications and bot control
- **Risk Management**: Position limits, cooldowns, session-based filtering
- **Auto-Training**: Continuous ML model improvement from live trades

## Tech Stack

- **Backend**: Python 3.11, FastAPI, asyncio
- **Database**: SQLite with optimized queries
- **ML**: scikit-learn (Random Forest, XGBoost)
- **WebSocket**: Real-time data streaming
- **APIs**: CCXT, python-telegram-bot
- **Frontend**: Vanilla JS, Chart.js

## Architecture

```
futures_bot/
├── main.py              # Main bot entry point
├── server.py            # FastAPI web server
├── config.py            # Configuration
├── bot/                 # Telegram bot
├── analysis/            # Signal generation & smart entry
├── tracker/             # Position tracking & TP/SL
├── data/                # Exchange clients & market data
├── database/            # SQLite operations
├── ml/                  # ML models & training
├── core/                # WebSocket candle cache
└── docs/                # Web dashboard
```

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/futures_bot.git
cd futures_bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment:
```bash
cp .env.example .env
# Edit .env with your API keys
```

4. Run the bot:
```bash
python main.py
```

5. Start web dashboard (optional):
```bash
python server.py
# Open http://localhost:8000
```

## Configuration

Key settings in `.env`:

```env
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Trading
AUTO_TAKE_SIGNALS=true
MIN_SIGNAL_STRENGTH=60
MAX_OPEN_POSITIONS=20

# ML Filter
ML_FILTER_ENABLED=false
ML_FILTER_MIN_PROB=0.52
```

## Smart Entry System

The bot uses an 11-step validation process:

1. Signal strength ≥65%
2. Category not WEAK
3. Heavy indicators ≥1
4. No counter-trend to strong BTC movement
5. Not Asian session (low liquidity)
6. Market not overheated (>5% in 45min)
7. Max 15 open positions
8. Not low liquidity hours (02:00-06:00 UTC)
9. Confidence calculation (0-100%)
10. Risk assessment (low/medium/high)
11. Final decision

**Confidence Score Breakdown:**
- Category: 25%
- Strength: 20%
- BTC alignment: 20%
- Heavy indicators: 15%
- Session: 10%
- Fear & Greed: 10%

## Signal Categories

- **PREMIUM** (💎): 75%+ strength, strong BTC alignment
- **STANDARD** (⭐): 65-74% strength, good setup
- **RISKY** (⚠️): 55-64% strength, acceptable risk
- **WEAK** (❌): <55% strength, filtered out

## Web Dashboard

Real-time monitoring at `http://localhost:8000`:

- Active positions with live PnL
- Entry/TP/SL levels
- Signal history
- Performance statistics
- Price charts

## ML Model

The bot includes an auto-training system:

- Collects features from every trade
- Trains on 200+ labeled samples
- Cross-validation (5-fold)
- Auto-retrains every 50 new trades
- Tracks model performance (AUC, accuracy)

## Risk Management

- Position size limits
- Stop-loss on all trades
- Cooldown after SL hit
- Session-based filtering
- Overheated market detection
- Symbol cooldown system

## Performance

Expected results with PREMIUM/STANDARD signals:
- Win rate: 55%+
- Risk/Reward: 1:2 average
- Max drawdown: Controlled by position limits

## Telegram Commands

- `/status` - Bot status and open positions
- `/stats` - Performance statistics
- `/signals` - Recent signals
- `/help` - Command list

## Development

Run tests:
```bash
python -m pytest tests/
```

Train ML model:
```bash
python ml_train.py
```

Export data:
```bash
python export_data.py
```

## Security

- All API keys stored in `.env` (gitignored)
- No hardcoded credentials
- Database excluded from git
- Secure WebSocket connections

## License

MIT License - see LICENSE file

## Disclaimer

This bot is for educational purposes. Cryptocurrency trading carries significant risk. Use at your own risk. Past performance does not guarantee future results.

## Contributing

Pull requests welcome! Please ensure:
- Code follows PEP 8
- Tests pass
- Documentation updated

## Support

For issues and questions, open a GitHub issue.

---

**Note**: This is a portfolio project demonstrating automated trading systems, ML integration, and real-time web applications. Not financial advice.
