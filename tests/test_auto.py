import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

auto = load("trader-auto", "auto")

PRICES = {"prices": {"AAPL": 356.4, "AXP": 320.6, "GOOGL": 295.79, "KO": 99.25, "BAC": 51.01, "SPY": 754.25,
                     "NVDA": 190.0, "MSFT": 500.0}}
TRADES = {"trades": [
    {"politician_name": "A", "symbol": "NVDA", "transaction_type": "BUY", "disclosure_date": "2026-11-05",
     "transaction_date": "2026-10-23", "asset_type": "EQUITY"},
    {"politician_name": "B", "symbol": "NVDA", "transaction_type": "BUY", "disclosure_date": "2026-11-01",
     "transaction_date": "2026-10-01", "asset_type": "EQUITY"},
    {"politician_name": "B", "symbol": "NVDA", "transaction_type": "BUY", "disclosure_date": "2026-11-01",
     "transaction_date": "2026-10-01", "asset_type": "EQUITY"},   # duplicate, counted once
    {"politician_name": "C", "symbol": "MSFT", "transaction_type": "BUY", "disclosure_date": "2026-11-10",
     "transaction_date": "2026-10-20", "asset_type": "EQUITY"},
    {"politician_name": "D", "symbol": "MSFT", "transaction_type": "SELL", "disclosure_date": "2026-11-10",
     "transaction_date": "2026-10-20", "asset_type": "EQUITY"},
    {"politician_name": "E", "symbol": "AAPL", "transaction_type": "BUY", "disclosure_date": "2026-08-01",
     "transaction_date": "2026-07-01", "asset_type": "EQUITY"},   # too old
]}


class Picks(unittest.TestCase):
    def test_net_buys_in_window(self):
        picks, score = auto.congress_picks(TRADES, "2026-11-13", 30, 5)
        self.assertEqual(picks, {"NVDA": 100.0})
        self.assertEqual(score, {"NVDA": 2, "MSFT": 0})


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "prices.json")
        self.c = os.path.join(self.d, "congress.json")
        self.cfg = os.path.join(self.d, "config.json")
        self.st = os.path.join(self.d, "state.json")
        for path, doc in ((self.p, PRICES), (self.c, TRADES), (self.cfg, {})):
            with open(path, "w") as fh:
                json.dump(doc, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = auto.main(["--config", self.cfg, "--state", self.st] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_daily_cap_and_no_double_run(self):
        code, text, _ = self.run_("run", "--prices", self.p, "--congress", self.c, "--today", "2026-11-13")
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent to any broker", text)
        with open(self.st) as fh:
            st = json.load(fh)
        for name, b in st["books"].items():
            spent = sum(float(o["dollar_amount"]) for o in b["orders"])
            self.assertLessEqual(spent, 250.0 + 1e-6, name)
            self.assertTrue(all(o["placed"] is False for o in b["orders"]))
        self.assertEqual(st["books"]["congress"]["targets"], {"NVDA": 100.0})
        code, text, _ = self.run_("run", "--prices", self.p, "--today", "2026-11-13")
        self.assertIn("Already ran", text)

    def test_day_one_spreads_across_targets(self):
        self.run_("run", "--prices", self.p, "--congress", self.c, "--today", "2026-11-13")
        with open(self.st) as fh:
            st = json.load(fh)
        bought = {o["symbol"] for o in st["books"]["copycat"]["orders"]}
        self.assertEqual(bought, {"AAPL", "AXP", "GOOGL", "KO", "BAC"})

    def test_fully_deployed_after_four_days_never_negative_cash(self):
        for d in ("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"):
            self.run_("run", "--prices", self.p, "--congress", self.c, "--today", d)
        with open(self.st) as fh:
            st = json.load(fh)
        for b in st["books"].values():
            self.assertGreaterEqual(b["cash"], 0)
            self.assertLess(b["cash"], 10)

    def test_live_mode_is_refused(self):
        with open(self.cfg, "w") as fh:
            json.dump({"auto": {"mode": "live"}}, fh)
        code, _, err = self.run_("report")
        self.assertEqual(code, 1)
        self.assertIn("only runs dry_run", err)


if __name__ == "__main__":
    unittest.main()
