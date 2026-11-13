#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does three
things around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON Claude will send to the broker: Alpaca's place_stock_order,
            or Robinhood's review_equity_order then place_equity_order.
            An order is READY only if trader-risk passed THIS exact order
            today (matched by fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with the broker's order id), denied, or failed.
  fill      adds a filled order to trader/portfolio.json as a tax lot.
  rh-gate   records Robinhood's trade-approval setting as Claude just read it.
            Robinhood orders can only be approved while that reading is under
            15 minutes old AND approvals are ON (every order waits for your tap).
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 61e6...
    python3 ticket.py fill --symbol AAPL --side buy --qty 3.6628 --price 341.27
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import uuid

ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "opg": "at the open",
             "cls": "at the close", "ioc": "immediate or cancel", "fok": "fill or kill"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
# Robinhood (its official MCP connector: review_equity_order, place_equity_order).
# Orders stay in Alpaca shape inside this pack (that is what trader-risk
# fingerprints); this file translates them at the last step.
RH_TYPES = {"market": "market", "limit": "limit", "stop": "stop_market", "stop_limit": "stop_limit"}
RH_TIF = {"day": "gfd", "gfd": "gfd", "gtc": "gtc"}
RH_UNSUPPORTED = ("trail_price", "trail_percent", "order_class", "take_profit_limit_price",
                  "stop_loss_stop_price", "stop_loss_limit_price", "advanced_instructions")
GATE_MINUTES = 15
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the --today it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def tool_input(order):
    """The exact arguments for place_stock_order: Alpaca's parameter names,
    numbers as strings (its signature types qty and notional as str)."""
    out = {}
    for k in ALPACA_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


# ---------------------------------------------------------------- robinhood

def broker_of(cfg):
    return str(cfg.get("broker", "alpaca")).lower()


def is_rh(cfg):
    return broker_of(cfg) == "robinhood"


def _is_fraction(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f != int(f)


def rh_ref_id(order, today, attempt=0):
    """Stable idempotency key: same order, same day, same attempt -> same UUID,
    so a retried call after a network error can never buy twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "trader-skill:%s:%s:%d" % (fingerprint(order), today, attempt)))


def rh_input(order, cfg, today=None, attempt=0):
    """(arguments for Robinhood's review_equity_order / place_equity_order, problems).
    Any problem means the order cannot go to Robinhood as written."""
    problems = []
    acct = str(cfg.get("robinhood_account") or "").strip()
    if not acct:
        problems.append('no "robinhood_account" in the config: put your agent account number there yourself '
                        "(Claude never picks the account for you)")
    out = {"account_number": acct, "symbol": str(order.get("symbol", "")).upper()}
    side = str(order.get("side", "")).lower()
    if side not in ("buy", "sell"):
        problems.append("side must be buy or sell")
    out["side"] = side
    typ = str(order.get("type", "market")).lower()
    rtyp = RH_TYPES.get(typ)
    if not rtyp:
        problems.append("Robinhood takes no %s orders (only market, limit, stop, stop_limit)" % typ)
    out["type"] = rtyp or typ
    tif = str(order.get("time_in_force", "day")).lower()
    rtif = RH_TIF.get(tif)
    if not rtif:
        problems.append("Robinhood takes no %s time in force (only day or gtc)" % tif)
    out["time_in_force"] = rtif or tif
    for k in RH_UNSUPPORTED:
        if order.get(k) is not None:
            problems.append("Robinhood's agent tools take no %s (trailing and bracket orders are out)" % k)
    notional, qty = order.get("notional"), order.get("qty")
    if (notional is None) == (qty is None):
        problems.append("give exactly one of a dollar amount (notional) or a share count (qty)")
    frac = False
    if notional is not None:
        if rtyp != "market" or rtif != "gfd":
            problems.append("a dollar amount only works as a market order good for the day")
        out["dollar_amount"] = "%.2f" % float(notional)
    if qty is not None:
        frac = _is_fraction(qty)
        if frac and (rtyp != "market" or rtif != "gfd"):
            problems.append("fractional shares only work on a market order good for the day; "
                            "limit and stop orders need whole shares")
        if frac and side == "sell" and order.get("short"):
            problems.append("no fractional short sales")
        out["quantity"] = _str_num(qty)
    if order.get("limit_price") is not None:
        out["limit_price"] = _str_num(order["limit_price"])
    elif rtyp in ("limit", "stop_limit"):
        problems.append("a %s order needs a limit_price" % typ)
    if order.get("stop_price") is not None:
        out["stop_price"] = _str_num(order["stop_price"])
    elif rtyp in ("stop_market", "stop_limit"):
        problems.append("a %s order needs a stop_price" % typ)
    if order.get("extended_hours"):
        if rtyp != "limit":
            problems.append("outside regular hours Robinhood only fills limit orders")
        if notional is not None or frac:
            problems.append("dollar and fractional orders only run in regular hours")
        out["market_hours"] = "extended_hours"
    else:
        out["market_hours"] = "regular_hours"
    if order.get("tax_lots"):
        if side != "sell":
            problems.append("tax_lots only apply to a sell")
        out["tax_lots"] = order["tax_lots"]
    if cfg.get("paper", True):
        problems.append('Robinhood has no paper account. Every order is REAL MONEY. To accept that, set '
                        '"paper": false in trader/config.json yourself')
    review = dict(out)
    out["ref_id"] = rh_ref_id(order, today or datetime.date.today().isoformat(), attempt)
    return {"review": review, "place": out}, problems


def rh_gate(state, cfg, now=None):
    """(ok, why): did Claude read Robinhood's trade-approval setting in the last
    GATE_MINUTES, for this account, and was it ON?"""
    acct = str(cfg.get("robinhood_account") or "").strip()
    gates = [g for g in state.get("gates", []) if g.get("account") == acct]
    if not gates:
        return False, ("nobody has checked Robinhood's trade-approval setting yet: call get_trade_approval_setting, "
                       "then ticket.py rh-gate --approvals on|off")
    g = gates[-1]
    now = now or datetime.datetime.now()
    try:
        age = (now - datetime.datetime.fromisoformat(g["at"])).total_seconds() / 60.0
    except (KeyError, ValueError):
        return False, "the last approval-setting check is unreadable; check again"
    if age > GATE_MINUTES or age < -1:
        return False, "the approval setting was last checked %.0f minutes ago; check it again" % age
    if not g.get("approvals_on"):
        return False, ("trade approvals are OFF in your Robinhood app, so an order would fill with no tap from you. "
                       "Turn them on in the Robinhood app (only you can), then check again")
    return True, "Robinhood will ask you to approve it in the app (checked %s)" % g["at"]


def attempts_today(state, fp, today):
    return sum(1 for r in state.get("orders", []) if r.get("fingerprint") == fp and rec_day(r) == today
               and r.get("status") == "failed")


def _money(v):
    f = float(v)
    s = "{:,.2f}".format(f)
    return "$" + (s[:-3] if s.endswith(".00") else s)


def plain(order):
    """BUY $1,250 of AAPL, market order, good for today"""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    if order.get("notional") is not None:
        what = "%s of %s" % (_money(order["notional"]), sym)
    else:
        q = _str_num(order.get("qty"))
        what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    typ = str(order.get("type", "market"))
    if typ == "limit":
        how = "limit order at %s" % _money(order.get("limit_price"))
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"))
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_money(order.get("stop_price")), _money(order.get("limit_price")))
    else:
        how = "%s order" % typ.replace("_", " ")
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    return "%s %s, %s, %s" % (side, what, how, tif)


def amount_numbers(order):
    nums = []
    for k in ("notional", "qty", "limit_price"):
        if order.get(k) is not None:
            try:
                nums.append(float(order[k]))
            except (TypeError, ValueError):
                pass
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z.\-]{0,6}", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today, cfg=None):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    if cfg is not None and is_rh(cfg):
        _, problems = rh_input(order, cfg, today)
        if problems:
            return False, "BLOCKED for Robinhood: " + "; ".join(problems), None
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the order call" % rec.get("at"), rv
        if st == "awaiting_app":
            return False, "SENT to your Robinhood app at %s, waiting for your tap there" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


# ---------------------------------------------------------------- rendering

BROKER_NAMES = {"ibkr": "Interactive Brokers", "robinhood": "Robinhood", "alpaca": "Alpaca"}


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today, cfg)
    broker = broker_of(cfg)
    paper = cfg.get("paper", True) and broker != "robinhood"
    acct = "PAPER account" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, BROKER_NAMES.get(broker, "Alpaca")),
             "  " + plain(order)]
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if broker == "ibkr":
        lines.append("  Claude drafts this with the Interactive Brokers connector. IBKR sends it to your")
        lines.append("  IBKR account, and it only goes to the market after you approve it there.")
    elif broker == "robinhood":
        args, problems = rh_input(order, cfg, today, attempts_today(state, fingerprint(order), today))
        if not problems:
            gate_ok, gate_why = rh_gate(state, cfg)
            lines.append("  " + ("app approval: " if gate_ok else "CANNOT PLACE YET: ") + gate_why)
            lines.append("  Claude will call review_equity_order, then place_equity_order with exactly:")
            for l in json.dumps(args["place"], indent=2).splitlines():
                lines.append("    " + l)
    else:
        lines.append("  Claude will call place_stock_order with exactly:")
        for l in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + l)
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if broker == "robinhood":
            lines.append("  Then Claude's Allow once / Deny card, then the approve button in your Robinhood app.")
        elif broker != "ibkr":
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today, cfg)
    out = {"index": idx, "ready": ready, "status": label, "plain": plain(order),
           "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)) and not is_rh(cfg),
           "broker": cfg.get("broker", "alpaca"), "tool": "place_stock_order",
           "tool_input": tool_input(order), "why": order.get("why", "")}
    if is_rh(cfg):
        args, _ = rh_input(order, cfg, today, attempts_today(state, fingerprint(order), today))
        out.update({"tool": "place_equity_order", "review_tool": "review_equity_order",
                    "review_input": args["review"], "tool_input": args["place"],
                    "app_approval": rh_gate(state, cfg)[1]})
    return out


# ---------------------------------------------------------------- fills -> lots

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json (a lot for a buy, FIFO for a sell)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (qty, symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


# ---------------------------------------------------------------- commands

def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, trader-rebalance or trader-tax first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    today = a.today or datetime.date.today().isoformat()
    return orders, cfg, risk, state, today


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, cfg)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    if is_rh(cfg):
        gate_ok, gate_why = rh_gate(state, cfg)
        if not gate_ok:
            raise TicketError("order %d cannot go to Robinhood: %s" % (a.index, gate_why))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    rec = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "alpaca")}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    if broker_of(cfg) == "ibkr":
        print("Now draft it with the Interactive Brokers connector; approve it in your IBKR account.")
    elif is_rh(cfg):
        args, _ = rh_input(order, cfg, today, attempts_today(state, fingerprint(order), today))
        print("1. Call review_equity_order with exactly:")
        print(json.dumps(args["review"], indent=2))
        print("   Stop if it returns any order_checks alert, or if customer_approval_required is not true.")
        print("2. Then call place_equity_order with exactly:")
        print(json.dumps(args["place"], indent=2))
        print("   Claude's card shows Allow once / Deny; then Robinhood asks you to approve in the app.")
        print("Then: ticket.py result --index %d --status awaiting_app|placed|denied|failed" % a.index)
    else:
        print("Now call place_stock_order with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") not in ("approved", "awaiting_app"):
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    if a.status == "awaiting_app" and rec.get("status") != "approved":
        raise TicketError("order %d is already waiting in your Robinhood app" % a.index)
    new = {"at": now_iso(), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "awaiting_app":
        print("Approve or decline it in your Robinhood app. When it is done, check get_equity_orders or "
              "get_trade_approvals and record: ticket.py result --index %d --status placed|denied" % a.index)
        return 0
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it for trader-tax: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
    return 0


def cmd_rh_gate(a):
    cfg = read_json(a.config, "config", required=False) or {}
    acct = str(cfg.get("robinhood_account") or "").strip()
    if not acct:
        raise TicketError('no "robinhood_account" in %s' % a.config)
    if a.account and a.account != acct:
        raise TicketError("that reading is for account ...%s, but the config trades ...%s"
                          % (a.account[-4:], acct[-4:]))
    state = load_state(a.state)
    rec = {"at": now_iso(), "account": acct, "approvals_on": a.approvals == "on"}
    state.setdefault("gates", []).append(rec)
    write_json(a.state, state)
    ok, why = rh_gate(state, cfg)
    print(("OK: " if ok else "BLOCKED: ") + why)
    return 0 if ok else 1


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    date = a.date or datetime.date.today().isoformat()
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    print("Recorded %s %s %s at %s on %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(),
                                                          _money(a.price), date, _money(pf["cash"])))
    return 0


def cmd_log(a):
    state = load_state(a.state)
    today = a.today or datetime.date.today().isoformat()
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        print("%s  #%s %-9s %s%s%s" % (r.get("at"), r.get("index"), r.get("status"), r.get("plain"), reply, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--today", help="YYYY-MM-DD (default: today)")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["awaiting_app", "placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="add a filled order to trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("rh-gate", help="record Robinhood's trade-approval setting, as just read")
    p.add_argument("--approvals", required=True, choices=["on", "off"],
                   help="human_must_approve_trades from get_trade_approval_setting: true=on")
    p.add_argument("--account", help="the account_number that was checked")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--today")
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "log": cmd_log, "rh-gate": cmd_rh_gate}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
