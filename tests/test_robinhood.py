import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-broker", "config.robinhood.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reel.json")
TODAY = "2026-10-13"
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
LIVE = {"broker": "robinhood", "robinhood_account": "ACCT1234", "paper": False}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class Translate(unittest.TestCase):
    def test_dollar_market_order(self):
        args, problems = ticket.rh_input(AAPL, LIVE, TODAY)
        self.assertEqual(problems, [])
        self.assertEqual(args["review"], {"account_number": "ACCT1234", "symbol": "AAPL", "side": "buy",
                                          "type": "market", "time_in_force": "gfd", "dollar_amount": "1250.00",
                                          "market_hours": "regular_hours"})
        self.assertEqual(args["place"]["ref_id"], ticket.rh_ref_id(AAPL, TODAY))
        self.assertNotIn("ref_id", args["review"])

    def test_ref_id_is_stable_and_changes_per_attempt(self):
        self.assertEqual(ticket.rh_ref_id(AAPL, TODAY), ticket.rh_ref_id(dict(AAPL), TODAY))
        self.assertNotEqual(ticket.rh_ref_id(AAPL, TODAY, 0), ticket.rh_ref_id(AAPL, TODAY, 1))
        self.assertNotEqual(ticket.rh_ref_id(AAPL, TODAY), ticket.rh_ref_id(AAPL, "2026-10-14"))

    def test_limit_and_stop(self):
        args, p = ticket.rh_input({"symbol": "ko", "side": "sell", "qty": 3, "type": "stop",
                                   "stop_price": 60, "time_in_force": "gtc"}, LIVE, TODAY)
        self.assertEqual(p, [])
        self.assertEqual((args["place"]["type"], args["place"]["stop_price"], args["place"]["quantity"],
                          args["place"]["symbol"]), ("stop_market", "60", "3", "KO"))

    def test_what_robinhood_cannot_take(self):
        cases = [
            dict(AAPL, type="limit", limit_price="300"),                        # dollars need market
            {"symbol": "KO", "side": "buy", "qty": "1.5", "type": "limit", "limit_price": "60",
             "time_in_force": "day"},                                           # fractional limit
            dict(AAPL, type="trailing_stop"),
            {"symbol": "KO", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day",
             "trail_percent": "5"},
            dict(AAPL, time_in_force="ioc"),
            {"symbol": "KO", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day",
             "extended_hours": True},                                           # market after hours
            {"symbol": "KO", "side": "buy", "type": "market", "time_in_force": "day"},  # no size
        ]
        for o in cases:
            self.assertTrue(ticket.rh_input(o, LIVE, TODAY)[1], o)

    def test_no_account_and_paper_both_block(self):
        p = ticket.rh_input(AAPL, {"broker": "robinhood"}, TODAY)[1]
        self.assertTrue(any("robinhood_account" in x for x in p))
        self.assertTrue(any("no paper account" in x for x in p))


class Gate(unittest.TestCase):
    def state(self, minutes_ago, on, acct="ACCT1234"):
        at = (datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()
        return {"gates": [{"at": at, "account": acct, "approvals_on": on}]}

    def test_gate(self):
        self.assertFalse(ticket.rh_gate({}, LIVE)[0])
        self.assertTrue(ticket.rh_gate(self.state(1, True), LIVE)[0])
        self.assertFalse(ticket.rh_gate(self.state(1, False), LIVE)[0])
        self.assertIn("OFF", ticket.rh_gate(self.state(1, False), LIVE)[1])
        self.assertFalse(ticket.rh_gate(self.state(30, True), LIVE)[0])
        self.assertFalse(ticket.rh_gate(self.state(1, True, acct="OTHER"), LIVE)[0])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        with open(CFG_PATH) as fh:
            c = json.load(fh)
        c.update({"robinhood_account": "ACCT1234", "paper": False})
        self.write_cfg(c)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                        "--prices", os.path.join(self.d, "none"), "--state", self.state,
                        "--today", TODAY, "--out", self.risk])

    def write_cfg(self, c):
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--today", TODAY]

    def gate(self, on):
        return run(["rh-gate", "--approvals", on, "--account", "ACCT1234",
                    "--config", self.cfg, "--state", self.state])

    def test_example_config_blocks_until_the_user_edits_it(self):
        shutil.copy(CFG_PATH, self.cfg)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED for Robinhood", text)
        self.assertIn("no paper account", text)

    def test_approvals_off_means_no_yes_is_recorded(self):
        code, text, _ = self.gate("off")
        self.assertEqual(code, 1)
        self.assertIn("OFF", text)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("CANNOT PLACE YET", text)
        self.assertIn("LIVE ACCOUNT, REAL MONEY", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("OFF", err)

    def test_the_full_four_gate_flow(self):
        self.assertEqual(self.gate("on")[0], 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("Robinhood", text)
        self.assertIn('"dollar_amount": "1250.00"', text)
        self.assertIn("place_equity_order", text)
        self.assertNotIn("place_stock_order", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes to all"))
        self.assertEqual(code, 1)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0, text)
        self.assertIn("review_equity_order", text)
        self.assertIn('"ref_id"', text)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "awaiting_app"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("SENT to your Robinhood app", text)
        # waiting in the app counts against duplicates and the daily limit
        with open(self.state) as fh:
            st = json.load(fh)
        res = check.check([AAPL], {"budget": 5000, "max_position_pct": 100}, state=st, today=TODAY)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "rh-1"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)

    def test_json_ticket(self):
        self.gate("on")
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        t = json.loads(text)
        self.assertEqual((t["tool"], t["review_tool"], t["paper"]), ("place_equity_order", "review_equity_order", False))
        self.assertEqual(t["tool_input"]["account_number"], "ACCT1234")


if __name__ == "__main__":
    unittest.main()
