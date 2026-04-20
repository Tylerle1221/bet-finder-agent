# Bet Finder Agent

This worker reads open bets from `reports.ibetcoin.win`, searches external books, and sends Telegram alerts when matching odds are available.

## Supported books

- Smash66
- DiamondSB
- Sports411
- Leftcoast797

## Safety mode (default)

This project now runs in **report-only mode** by default:

- `REPORT_ONLY_MODE=true`
- `AUTO_SUBMIT_ENABLED=false`

In this mode, the agent only reports available matches to Telegram and never submits bets.

## Low-memory mode (default)

To reduce hosting RAM/GB usage:

- `LOW_MEMORY_MODE=true`
- `IDLE_SHUTDOWN_CYCLES=3`

Behavior:

- Platform browsers are not kept open all the time.
- Browsers start only when new ibet open bets appear.
- If cycles stay idle, browser sessions are closed after the configured idle cycle count.

## Match rules

Totals use slippage-aware matching:

- Over: accept if `actual_line <= detected_line + line_slippage`
- Under: accept if `actual_line >= detected_line - line_slippage`
- Juice must also stay inside `juice_slippage` threshold

Default thresholds:

- `line_slippage = 1.0`
- `juice_slippage = 20` (american odds points)

## Configure credentials

Set these environment variables (Render or local `.env`):

- `IBETCOIN_USERNAME`, `IBETCOIN_PASSWORD`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `SMASH66_USERNAME`, `SMASH66_PASSWORD`
- `DIAMONDSB_USERNAME`, `DIAMONDSB_PASSWORD`
- `SPORTS411_USERNAME`, `SPORTS411_PASSWORD`
- `LEFTCOAST797_USERNAME`, `LEFTCOAST797_PASSWORD`

Optional tuning:

- `CHECK_INTERVAL` (seconds)
- `SIMILARITY_THRESHOLD`
- `REPORT_ONLY_MODE`
- `AUTO_SUBMIT_*` (only used when report-only is disabled)
