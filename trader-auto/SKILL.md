---
name: trader-auto
description: >-
  Dry-run Auto Trader. Runs on a schedule (weekdays after the open) and pits
  strategies against each other on paper with the same budget: Copycat
  (Berkshire's 13F top 5), Congress (the stocks members of Congress net-bought
  most in disclosures from the last 30 days, via Robinhood's
  get_politician_trades) and a SPY benchmark. Each book deploys at most the
  daily cap, logs the exact orders it WOULD have sent, marks itself to market
  with live Robinhood quotes and reports standings. It never places, reviews
  or cancels an order. Use when the user says "auto trade", "run the bot",
  "dry run", "how are the strategies doing", or when the scheduled run fires.
argument-hint: "[run|report]"
---

# trader-auto

The dry run answers one question before any real money moves: does a
strategy beat simply buying SPY, after two weeks of real prices? The tool is
paper only. `auto.py` refuses any mode but `dry_run`, and this skill never
calls an order tool.

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/auto.py" run --prices trader/auto/prices.json --congress trader/auto/congress.json
python3 "${CLAUDE_SKILL_DIR}/auto.py" report
python3 "${CLAUDE_SKILL_DIR}/auto.py" congress-picks --congress trader/auto/congress.json
```

## Settings (the "auto" block in trader/config.json, all optional)

| key | default | meaning |
| --- | --- | --- |
| `book_usd` | 1000 | paper budget for EACH strategy (the user's total cap) |
| `daily_cap_usd` | 250 | the most one book may trade in a day, buys plus sells |
| `congress_lookback_days` | 30 | disclosures counted, by disclosure date |
| `congress_top_n` | 5 | how many net-bought names the Congress book holds, equal weight |
| `dry_run_days` | 10 | runs before the verdict |

## Steps, each scheduled run

1. Only on a weekday the NYSE is open (trader-alerts knows the holidays).
   Run once a day; `auto.py` refuses a second run for the same date.
2. **Congress data, Mondays or the first run only.** Call Robinhood's
   `get_politician_trades` with `equity_symbol` for each name in the
   universe below, one call each, and save every result's `trades` into one
   list in `trader/auto/congress.json` as `{"trades": [...]}`. Other days,
   leave `--congress` off and the book keeps its last picks.
   Universe: AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM V MA UNH XOM LLY
   COST NFLX AMD BAC WMT HD ORCL CRM KO PG DIS.
3. **Prices.** Call `get_equity_quotes` for every symbol in the copycat
   targets, SPY, and the congress picks / current holdings (20 or fewer per
   call), and save the raw response to `trader/auto/prices.json`. Check the
   timestamps are from today's session.
4. `auto.py run ...` and show the user its output as it is: what each book
   would have bought or sold, its value and the standings.
5. Commit `trader/auto/state.json` (it lives on the `dry-run` branch of the
   user's repo) so the next run continues from it.
6. On the last run (`dry_run_days`), give the verdict: each strategy against
   SPY, how much was deployed, the best and worst day. Then ask the user what
   to do next. Do not change anything toward live trading on your own.

## What it never does

- Never calls `place_equity_order`, `review_equity_order`, `cancel_equity_order`,
  `place_advanced_order` or any other order tool. Read-only tools only.
- Never switches `mode` away from `dry_run`. Live auto trading is not in this version.
- Never presents paper gains as a promise. Paper fills ignore spreads, slippage and taxes, and
  Congress disclosures lag the trade by up to 45 days.

## Safety

- Education and tooling, not financial advice. Nothing here recommends a trade.
- A real order still goes only through trader-risk and trader-broker, with an explicit yes for each one.
- Data: Robinhood connector (quotes; politician trades from Tip Ranks, credited as such).
