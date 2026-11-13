---
name: trader-broker
description: >-
  Order Executor. Places APPROVED orders through your brokerage connector, one
  at a time, and never without your explicit yes. Shows each order as a plain
  ticket plus the exact JSON for Alpaca's place_stock_order, records your yes
  word for word, then Claude's own Allow once / Deny card asks again. Covers
  setting up Alpaca's official MCP server in Claude Desktop (paper trading by
  default, keys in env vars, never in chat), the Robinhood connector (real
  money, so a fourth gate: Robinhood's in-app trade approval must be ON, read
  fresh before every yes), the Interactive Brokers connector
  (orders go to your IBKR account for your approval), and setting order tools
  to "Needs approval". Use when the user says "place the orders", "buy it",
  "execute", "send it to my broker", "connect Alpaca", "connect Robinhood", "connect Interactive
  Brokers", "set up paper trading", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

With Robinhood there is a fourth gate, because Robinhood has no paper account:

4. **Robinhood's own trade approval is ON** for the agent account, read with
   `get_trade_approval_setting` no more than 15 minutes before the yes. With it
   on, `place_equity_order` does not fill: it sends the order to the Robinhood
   app and waits for the user's tap. If it is off, nothing is approved here.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol AAPL --side buy --qty 3.6628 --price 341.27
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, and keeps the log in `trader/state/broker.json`.

## Setup A: Alpaca (official MCP server, Claude Desktop)

Alpaca's own server is github.com/alpacahq/alpaca-mcp-server (MIT). It runs on
the user's computer inside Claude Desktop. It is not in Claude's connector
directory. It trades on a **paper** account unless told otherwise.

1. The user makes an Alpaca account and creates **paper trading** API keys in
   the Alpaca dashboard. Paper keys are separate from live keys.
2. The user installs `uv` (it provides `uvx`), from docs.astral.sh/uv.
3. In Claude Desktop: Settings > Developer > Edit Config. That opens
   `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
   Windows: `%APPDATA%\Claude\`). The user adds this block (also in
   `claude_desktop_config.example.json` here) and types their own keys into the file:

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "paste your paper key here, in this file only",
        "ALPACA_SECRET_KEY": "paste your paper secret here, in this file only",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,trading,stock-data"
      }
    }
  }
}
```

   The `env` block hands the keys to the server as environment variables.
   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes a key anyway, tell them
   to delete that message and make new keys in the Alpaca dashboard.
   `ALPACA_PAPER_TRADE` is already true by default; writing it makes it
   visible. `ALPACA_TOOLSETS` is optional and hides the tools they do not need.
4. Restart Claude Desktop.
5. **Set the tool permissions.** Customize > Connectors (Settings > Connectors
   in older versions), open alpaca, and for each tool pick Always allow, Needs
   approval or Blocked:
   - `place_stock_order`: **Needs approval**. Never Always allow.
   - every other tool that places, changes, cancels or closes anything
     (`cancel_order_by_id`, `close_position`, `update_account_config`): Needs approval.
   - `place_option_order`, `place_crypto_order`, `close_all_positions`,
     `cancel_all_orders`, `exercise_options_position`: **Blocked** unless the
     user has a reason and says so.
   - read-only tools (`get_account_info`, `get_all_positions`, `get_stock_snapshot`,
     `get_stock_latest_quote`): Always allow is fine.
6. Check it: ask Claude for the Alpaca account info (read only) and confirm
   in the Alpaca dashboard that it is the paper account.

In Claude Code instead of Claude Desktop, keep the order tool on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__alpaca__place_stock_order"]}`,
and never put an order tool in `allow`.

## Setup B: Interactive Brokers (connector in Claude's directory)

Interactive Brokers publishes a verified connector: claude.ai/directory/interactive-brokers
(connector URL `https://api.ibkr.com/v1/api/mcp`). Add it from Customize >
Connectors > Browse connectors and sign in to IBKR there, not in the chat.
Its listing says Claude can draft trade instructions that are sent to the
user's IBKR account for their review and approval, and that Claude never
submits orders directly to the market. So with IBKR the final yes happens in
IBKR as well. Set `"broker": "ibkr"` in `trader/config.json`, and size with
`--whole-shares` if IBKR wants share counts. Set its tools that draft orders
to Needs approval too.

## Setup C: Robinhood (its official connector)

Robinhood's connector exposes `review_equity_order` (a dry run with buying
power, PDT and halt checks) and `place_equity_order` (real money). There is no
paper account, so this setup is stricter than Alpaca's.

1. The user connects Robinhood in Claude (Customize > Connectors) and signs in
   there, never in the chat. Robinhood lets the agent trade exactly one account
   (`agentic_allowed` true in `get_accounts`); the others are read only.
2. In the Robinhood app, the user turns ON trade approvals for that agent
   account. Only the user can; no tool changes it. Claude checks it with
   `get_trade_approval_setting` (`human_must_approve_trades` must be true).
3. Copy `config.robinhood.example.json` to `trader/config.json`. The user types
   their agent account number into `robinhood_account` and flips `"paper"` to
   `false` themselves. Until both are done every ticket shows BLOCKED for
   Robinhood. Claude never fills the account number in or flips paper.
4. Tool permissions: `place_equity_order`, `cancel_equity_order`,
   `place_advanced_order` and anything else that places, changes or cancels:
   **Needs approval**. Option, crypto order and `exercise_option` tools:
   **Blocked**. Read-only tools (`get_portfolio`, `get_equity_positions`,
   `get_equity_quotes`, `get_equity_tax_lots`, `get_politician_trades`) can be
   Always allow. In Claude Code: `"permissions": {"ask": ["mcp__robinhood__place_equity_order",
   "mcp__robinhood__cancel_equity_order"]}`, never an order tool in `allow`.

What Robinhood cannot take (ticket.py blocks these before you are asked):
trailing stops and bracket orders; a dollar amount on anything but a market
order good for the day; fractional shares on limit or stop orders; market or
stop orders outside regular hours; `ioc`, `fok`, `opg`, `cls`. Size with
`--whole-shares` in trader-budget if you want limit orders.

### Robinhood steps, for each order

1. `get_trade_approval_setting` for the account in the config, then record
   exactly what it said: `ticket.py rh-gate --approvals on|off --account <n>`.
   OFF means stop and tell the user to turn approvals on in the app. Read it
   again if more than 15 minutes pass.
2. `ticket.py show`, one READY ticket, "Place this order? Reply yes or no."
3. `ticket.py approve --index N --reply "<word for word>"`. It prints the
   exact `review_equity_order` and `place_equity_order` arguments.
4. Call `review_equity_order` with exactly those arguments. If it returns any
   `order_checks` alert, or `customer_approval_required` is not true, stop:
   show the user what it said and record `--status failed --note "<why>"`.
5. Call `place_equity_order` with exactly the printed arguments, including the
   same `ref_id` (re-send the same `ref_id` if the call errors in transit,
   never a new one). The Allow once / Deny card appears.
6. Record `ticket.py result --index N --status awaiting_app --broker-id <id>`.
   The user approves or declines in the Robinhood app. Later, from
   `get_trade_approvals` or `get_equity_orders`, record `placed` (with
   `--filled-qty` and `--filled-price` once filled) or `denied`.

### Reading your Robinhood account into the pack

The other skills read `trader/portfolio.json`. Build it from read-only calls:
`get_portfolio` for cash, `get_equity_positions` for shares, and
`get_equity_tax_lots` per symbol for the lots
(`{"ticker", "shares", "lots": [{"id", "date", "shares", "cost"}]}`). Show the
user the file before anything uses it. Never mix in the read-only accounts.

## Steps, for each order

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes", call `place_stock_order` with exactly the
   JSON the ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id from the response>`, or `--status denied` if they pressed
   Deny, or `--status failed` with `--note` for an error. A failed order is
   not retried without a new check and a new yes.
6. When the fill is known (the tool's response, or later from
   `get_all_positions`), `ticket.py fill` adds it to `trader/portfolio.json`
   as a tax lot for trader-tax. Then the next ticket.

## Example: the reel

```
ORDER 1 of 5, PAPER account, Alpaca
  BUY $1,250 of AAPL, market order, good for today
  READY: trader-risk PASS, waiting for your yes
  Claude will call place_stock_order with exactly:
    {"symbol": "AAPL", "side": "buy", "notional": "1250",
     "type": "market", "time_in_force": "day"}
```

The user replies "yes". Claude records it, calls `place_stock_order`, and the
card reads "Claude wants to use Place stock order" with that JSON and two
buttons: Allow once, Deny. Allow once sends it to the paper account.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never switches `ALPACA_PAPER_TRADE` or `"paper"` to live. The user does that themselves.
- Never fills in `robinhood_account`, never trades an account that is not `agentic_allowed`, and never
  places a Robinhood order while its in-app trade approval is off or unchecked in the last 15 minutes.
- Never calls `close_all_positions`, `cancel_all_orders`, option or crypto order tools on its own.

## Safety

- Education and tooling, not financial advice. No order here is a recommendation, and nothing promises returns.
- Paper trading by default (`"paper": true`, `ALPACA_PAPER_TRADE` true). Going live is the user's own decision.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
