#!/usr/bin/env python3
"""Dry-run auto trader: strategies compete on paper. Standard library only.

This file NEVER talks to a broker. It reads prices and Congress trades that
Claude fetched (read-only) and writes what each strategy WOULD have done.

    python3 auto.py run --prices P.json [--congress C.json] [--today D]
    python3 auto.py report
    python3 auto.py congress-picks --congress C.json [--today D]

State: trader/auto/state.json (append-only log inside).
"""

import argparse
import datetime
import json
import os
import sys

DEFAULTS = {
    "mode": "dry_run",
    "book_usd": 1000.0,          # each strategy's paper budget (your total cap)
    "daily_cap_usd": 250.0,      # most any one book may trade in a day (buys + sells)
    "min_trade_usd": 5.0,
    "congress_lookback_days": 30,
    "congress_top_n": 5,
    "dry_run_days": 10,
    "strategies": {
        # Berkshire Hathaway 13F-HR, Q2 2026 (filed 2026-08-14), top 5 renormalised
        "copycat": {"AAPL": 30.67, "AXP": 23.85, "GOOGL": 17.56, "KO": 15.11, "BAC": 12.81},
        "congress": {},          # filled from disclosures
        "benchmark": {"SPY": 100.0},
    },
}


class AutoError(Exception):
    pass


def read_json(path, required=True):
    if not os.path.exists(path):
        if required:
            raise AutoError("missing %s" % path)
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def config(path):
    c = json.loads(json.dumps(DEFAULTS))
    user = (read_json(path, required=False) or {}).get("auto", {})
    strategies = user.pop("strategies", None)
    c.update(user)
    if strategies:
        c["strategies"].update(strategies)
    if c["mode"] != "dry_run":
        raise AutoError("auto.py only runs dry_run. Live auto trading is not built in this version.")
    return c


def load_prices(path):
    """Accepts {"prices": {"AAPL": 1.0}} or Robinhood get_equity_quotes output."""
    doc = read_json(path)
    if isinstance(doc, dict) and "prices" in doc:
        return {k.upper(): float(v) for k, v in doc["prices"].items()}
    results = (doc.get("data") or doc).get("results", []) if isinstance(doc, dict) else doc
    out = {}
    for r in results:
        q = r.get("quote", r)
        p = q.get("last_trade_price") or q.get("price")
        if p and q.get("state", "active") == "active":
            out[q["symbol"].upper()] = float(p)
    if not out:
        raise AutoError("no usable prices in %s" % path)
    return out


def congress_picks(trades, today, lookback, top_n):
    """Net disclosed buys minus sells per ticker, by disclosure date in the
    lookback window. Ties broken by ticker. Equal weight on the top N net buys."""
    if isinstance(trades, dict):
        trades = (trades.get("data") or trades).get("trades", [])
    t0 = datetime.date.fromisoformat(today) - datetime.timedelta(days=lookback)
    score, seen = {}, set()
    for t in trades:
        if str(t.get("asset_type", "EQUITY")).upper() != "EQUITY" or not t.get("symbol"):
            continue
        key = (t.get("politician_name"), t.get("symbol"), t.get("transaction_type"),
               t.get("transaction_date"), t.get("disclosure_date"))
        if key in seen:
            continue
        seen.add(key)
        try:
            d = datetime.date.fromisoformat(str(t.get("disclosure_date"))[:10])
        except ValueError:
            continue
        if d < t0 or d > datetime.date.fromisoformat(today):
            continue
        side = str(t.get("transaction_type", "")).upper()
        sym = t["symbol"].upper()
        if side.startswith("BUY") or side == "PURCHASE":
            score[sym] = score.get(sym, 0) + 1
        elif side.startswith("SELL"):
            score[sym] = score.get(sym, 0) - 1
    ranked = sorted((s for s in score.items() if s[1] > 0), key=lambda x: (-x[1], x[0]))[:top_n]
    if not ranked:
        return {}, score
    w = round(100.0 / len(ranked), 4)
    return {s: w for s, _ in ranked}, score


def value(book, prices):
    return book["cash"] + sum(sh * prices.get(s, book["last_px"].get(s, 0)) for s, sh in book["positions"].items())


def step(book, targets, prices, cfg, today):
    """Trade one book toward its targets, within the daily cap. Sells first."""
    for s, p in prices.items():
        if s in book["positions"] or s in targets:
            book["last_px"][s] = p
    total = value(book, prices)
    room = cfg["daily_cap_usd"]
    wsum = sum(targets.values()) or 1.0
    want = {s: total * w / wsum for s, w in targets.items() if s in prices}
    have = {s: sh * prices[s] for s, sh in book["positions"].items() if s in prices}
    orders = []
    # sells: anything over target (incl. dropped names)
    for s in sorted(have):
        excess = have[s] - want.get(s, 0.0)
        if excess >= cfg["min_trade_usd"] and room >= cfg["min_trade_usd"]:
            amt = round(min(excess, room), 2)
            sh = amt / prices[s]
            book["positions"][s] -= sh
            if book["positions"][s] * prices[s] < 0.01:
                del book["positions"][s]
            book["cash"] = round(book["cash"] + amt, 2)
            room -= amt
            orders.append({"symbol": s, "side": "sell", "dollar_amount": "%.2f" % amt, "price": prices[s]})
    # buys: today's room is shared across every shortfall in proportion, never below zero cash
    gaps = {s: want[s] - have.get(s, 0.0) for s in want if want[s] - have.get(s, 0.0) > 0}
    budget = min(room, book["cash"], sum(gaps.values()))
    total_gap = sum(gaps.values()) or 1.0
    for s in sorted(gaps, key=lambda k: -gaps[k]):
        amt = round(min(budget * gaps[s] / total_gap, gaps[s], book["cash"]), 2)
        if amt < cfg["min_trade_usd"]:
            continue
        book["positions"][s] = book["positions"].get(s, 0.0) + amt / prices[s]
        book["cash"] = round(book["cash"] - amt, 2)
        room -= amt
        orders.append({"symbol": s, "side": "buy", "dollar_amount": "%.2f" % amt, "price": prices[s]})
    for o in orders:
        o.update({"type": "market", "time_in_force": "gfd", "date": today, "placed": False})
    return orders


def new_book(cfg):
    return {"cash": cfg["book_usd"], "positions": {}, "last_px": {}, "targets": {}, "orders": [], "marks": []}


def cmd_run(a):
    cfg = config(a.config)
    today = a.today or datetime.date.today().isoformat()
    prices = load_prices(a.prices)
    st = read_json(a.state, required=False) or {"start": today, "books": {}, "runs": []}
    if any(r["date"] == today for r in st["runs"]):
        print("Already ran for %s; nothing done (one run per day)." % today)
        return 0
    if a.congress:
        picks, _ = congress_picks(read_json(a.congress), today, cfg["congress_lookback_days"], cfg["congress_top_n"])
        cfg["strategies"]["congress"] = picks or st.get("books", {}).get("congress", {}).get("targets", {})
    else:
        cfg["strategies"]["congress"] = st.get("books", {}).get("congress", {}).get("targets", {})
    summary = []
    for name, targets in cfg["strategies"].items():
        book = st["books"].setdefault(name, new_book(cfg))
        if targets:
            book["targets"] = targets
        orders = step(book, book["targets"], prices, cfg, today) if book["targets"] else []
        book["orders"].extend(orders)
        v = round(value(book, prices), 2)
        book["marks"].append({"date": today, "value": v})
        summary.append((name, v, orders))
    st["runs"].append({"date": today, "prices": {k: prices[k] for k in sorted(prices)}})
    write_json(a.state, st)
    print("DRY RUN %s (run %d of %d). Nothing was sent to any broker." % (today, len(st["runs"]), cfg["dry_run_days"]))
    for name, v, orders in summary:
        print("\n%-10s value $%.2f (%+.2f%%)" % (name, v, (v / cfg["book_usd"] - 1) * 100))
        for o in orders:
            print("  would %-4s $%s of %s @ %.2f" % (o["side"], o["dollar_amount"], o["symbol"], o["price"]))
        if not orders:
            print("  no trades" + ("" if st["books"][name]["targets"] else " (no targets yet)"))
    return report(st, cfg)


def report(st, cfg):
    print("\nSTANDINGS since %s, %d run(s):" % (st["start"], len(st["runs"])))
    bench = st["books"].get("benchmark", {}).get("marks", [{}])[-1].get("value")
    rows = []
    for name, b in st["books"].items():
        v = b["marks"][-1]["value"] if b["marks"] else cfg["book_usd"]
        rows.append((v, name, b))
    for v, name, b in sorted(rows, reverse=True, key=lambda r: r[0]):
        vs = "" if bench is None or name == "benchmark" else "  vs SPY %+.2f" % (v - bench)
        hold = ", ".join("%s %.0f%%" % (s, w) for s, w in sorted(b["targets"].items(), key=lambda x: -x[1]))
        print("  %-10s $%8.2f  %+6.2f%%%s   [%s]" % (name, v, (v / cfg["book_usd"] - 1) * 100, vs, hold))
    if len(st["runs"]) >= cfg["dry_run_days"]:
        print("\nDry run complete. Decide before anything goes live; this tool cannot place orders.")
    print("Paper results ignore spreads, slippage and taxes. Education and tooling, not financial advice.")
    return 0


def cmd_report(a):
    cfg = config(a.config)
    st = read_json(a.state, required=False)
    if not st:
        print("No dry run yet.")
        return 1
    return report(st, cfg)


def cmd_picks(a):
    cfg = config(a.config)
    today = a.today or datetime.date.today().isoformat()
    picks, score = congress_picks(read_json(a.congress), today, cfg["congress_lookback_days"], cfg["congress_top_n"])
    print(json.dumps({"picks": picks, "net_buys": dict(sorted(score.items(), key=lambda x: -x[1]))}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dry-run auto trader. Never places orders.")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--state", default="trader/auto/state.json")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--prices", required=True)
    p.add_argument("--congress")
    p.add_argument("--today")
    sub.add_parser("report")
    p = sub.add_parser("congress-picks")
    p.add_argument("--congress", required=True)
    p.add_argument("--today")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "report": cmd_report, "congress-picks": cmd_picks}[a.cmd](a)
    except AutoError as e:
        print("auto.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
