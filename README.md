# MEXC Futures Signal Bot — 5m / next 5 candles

Signal-only Telegram bot for BTCUSDT and ETHUSDT MEXC perpetual futures.
No order placement, account API keys, or trading endpoints are used.

## Rules
- MEXC public futures K-lines: `https://api.mexc.com/api/v1/contract/kline/{symbol}`
- MEXC contract symbols: `BTC_USDT`, `ETH_USDT`; fixed interval: `Min5` only.
- Each pattern is consecutive and exact. The final symbol in a pattern is the signal candle.
- Warning: when the currently open 5m candle changes to the required final color.
- Confirmation: after that exact candle closes in the required color.
- Outcome: after five subsequent candles close, report WIN or LOSS.
- LONG WIN: at least one of those five closes above the signal candle close.
- SHORT WIN: at least one of those five closes below the signal candle close.
- Otherwise LOSS after all five close. This is a close-price direction test, not a fee/slippage/stop-loss simulation.
- Doji candles are neither green nor red.
- Percentages/sample counts are reference figures supplied with the patterns, not verified guarantees.

## Deploy
1. Upload this folder to a GitHub repository.
2. In Railway, deploy from that repository.
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Optional: `SYMBOLS=BTCUSDT,ETHUSDT`, `POLL_SECONDS=3`.
5. For persistent SQLite storage, attach a Railway Volume and set `DB_PATH=/data/signals.sqlite3`.
Never commit `.env` or your Telegram token.

All project files are UTF-8. Python version 3.11+ recommended.
