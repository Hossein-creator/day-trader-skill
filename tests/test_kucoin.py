"""trader-kucoin tests. Fixtures in tests/fixtures/trader-kucoin/, real KuCoin answers
fetched 2026-10-29 14:49 UTC (em dashes in announcement titles turned into hyphens):

contracts.json            /api/v1/contracts/active, cut to 12 contracts (crypto, stocks, gold)
status.json               /api/v1/status
announcements.json        /api/v3/announcements, latest-announcements, 50 items
kline-SOLUSDTM-1.json     the 30 minutes of 1-minute candles to 14:49 (the last one still forming)
kline-SOLUSDTM-5.json     about 28 hours of 5-minute candles to 14:49, fetched in pieces of 200

The signal tests build SYNTHETIC bars (not market data) so each rule can be seen alone.
"""

import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

ku = load("trader-kucoin", "kucoin")

KDIR = os.path.join(FIX, "trader-kucoin")
FETCHED = datetime.datetime(2026, 10, 29, 14, 49)
DT = datetime.datetime


def contracts():
    with open(os.path.join(KDIR, "contracts.json")) as fh:
        return {c["symbol"]: c for c in json.load(fh)["data"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ku.main(argv)
    return code, out.getvalue(), err.getvalue()


def synthetic(now, breakout=100.8, volume=3000.0, bars_after=0, short=False):
    """24 hours of 5-minute bars climbing from 90 to 99.5 (the EMA sits under the price),
    then a 15-minute range 100.0-100.5 on 1-minute bars and one bar that closes outside it.
    With short=True everything is mirrored: falling from 110, range 99.5-100.0, close 99.2."""
    sign = -1 if short else 1
    start = now - datetime.timedelta(hours=26)
    b5, t, n = [], start, 26 * 12
    for i in range(n - 6):
        p = 100 - sign * 10 * (1 - i / float(n))
        b5.append({"t": t, "o": p, "h": p + 0.05, "l": p - 0.05, "c": p, "v": 1000.0})
        t += datetime.timedelta(minutes=5)
    b1 = []
    t = now - datetime.timedelta(minutes=16 + bars_after)
    lo, hi = (100.0, 100.5) if not short else (99.5, 100.0)
    for i in range(15):
        c = lo + 0.25 + (0.2 if i % 2 else -0.2)
        b1.append({"t": t, "o": c, "h": hi if i == 7 else c + 0.02, "l": lo if i == 3 else c - 0.02, "c": c, "v": 1000.0})
        t += datetime.timedelta(minutes=1)
    close = breakout if not short else 100 - (breakout - 100.5) - 0.5
    b1.append({"t": t, "o": hi if not short else lo, "h": max(close, hi), "l": min(close, lo), "c": close, "v": volume})
    t += datetime.timedelta(minutes=1)
    for _ in range(bars_after):
        b1.append({"t": t, "o": close, "h": close + 0.01, "l": close - 0.01, "c": close, "v": 1000.0})
        t += datetime.timedelta(minutes=1)
    return b1, b5


SOL = None


def sol(**kw):
    c = dict(contracts()["SOLUSDTM"])
    c["nextFundingRateDateTime"] = None
    c.update(kw)
    return c


class Basics(unittest.TestCase):
    def test_symbols(self):
        for text, want in (("BTC", "XBTUSDTM"), ("btc-usdt", "XBTUSDTM"), ("XBTUSDTM", "XBTUSDTM"),
                           ("sol", "SOLUSDTM"), ("TSLA", "TSLAUSDTM"), ("ETH/USDT", "ETHUSDTM")):
            self.assertEqual(ku.symbol_of(text), want, text)
        self.assertEqual(ku.base_of("XBTUSDTM"), "BTC")
        with self.assertRaises(ku.KuError):
            ku.symbol_of("")

    def test_berlin_and_new_york(self):
        self.assertEqual(ku.berlin(DT(2026, 10, 29, 14, 49)), DT(2026, 10, 29, 15, 49))   # CET since Oct 25
        self.assertEqual(ku.berlin(DT(2026, 7, 1, 12, 0)), DT(2026, 7, 1, 14, 0))         # CEST
        self.assertTrue(ku.us_market_open(DT(2026, 10, 29, 14, 49)))     # 10:49 New York (EDT to Nov 1)
        self.assertFalse(ku.us_market_open(DT(2026, 10, 29, 21, 0)))
        self.assertFalse(ku.us_market_open(DT(2026, 10, 31, 15, 0)))     # Saturday
        self.assertTrue(ku.us_market_open(DT(2026, 11, 2, 14, 45)))      # EST: 9:45 New York

    def test_leverage_is_fixed_at_one(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "kucoin.json")
            with open(p, "w") as fh:
                json.dump({"leverage": 5}, fh)
            with self.assertRaises(ku.KuError) as cm:
                ku.read_config(p)
            self.assertIn("fixed at 1x", str(cm.exception))
            self.assertEqual(ku.read_config(os.path.join(tmp, "none.json"))["leverage"], 1)
        finally:
            shutil.rmtree(tmp)


class RealData(unittest.TestCase):
    def test_klines_keep_closed_bars_only(self):
        f = ku.Fetcher(KDIR)
        b1 = ku.klines(f, "SOLUSDTM", 1, FETCHED - datetime.timedelta(minutes=30), FETCHED)
        self.assertEqual(len(b1), 30)
        self.assertEqual(b1[-1]["t"], DT(2026, 10, 29, 14, 48))   # the 14:49 bar is still forming
        self.assertTrue(all(b["l"] <= min(b["o"], b["c"]) and b["h"] >= max(b["o"], b["c"]) for b in b1))
        b5 = ku.klines(f, "SOLUSDTM", 5, FETCHED - datetime.timedelta(hours=24), FETCHED)
        self.assertEqual(len(b5), 287)             # 14:50 yesterday to the 14:40 bar; 14:45 closes at 14:50
        self.assertEqual([b["t"] for b in b5], sorted(b["t"] for b in b5))

    def test_brief_ranks_crypto_then_the_rest(self):
        b = ku.build_brief(ku.Fetcher(KDIR), FETCHED, dict(ku.DEFAULTS))
        self.assertEqual(b["status"], "open")
        self.assertEqual(b["date"], "2026-10-29")
        classes = [x["class"] for x in b["ranked"]]
        self.assertEqual(classes[0], "CRYPTO")
        self.assertEqual(classes, sorted(classes, key=lambda c: c != "CRYPTO"))
        syms = [x["symbol"] for x in b["ranked"]]
        self.assertIn("MUUSDTM", syms)                 # a stock over 3M USDT
        self.assertNotIn("AAPLUSDTM", syms)            # under it
        evaa = [x for x in b["ranked"] if x["symbol"] == "EVAAUSDTM"][0]
        self.assertTrue(any(f.startswith("wild") for f in evaa["flags"]))
        self.assertEqual(evaa["change_pct"], -14.85)          # just under the 15 % "moved" flag
        self.assertFalse(any("moved" in f for f in evaa["flags"]))
        self.assertTrue(any(a["watch"] for a in b["announcements"]))
        self.assertIsNone(b["news_checked_at"])
        text = "\n".join(ku.describe_brief(b))
        self.assertIn("nobody knows what will pay", text)
        self.assertIn("15:49 Berlin", text)

    def test_an_announcement_puts_a_coin_on_the_avoid_list(self):
        tmp = tempfile.mkdtemp()
        try:
            for f in os.listdir(KDIR):
                shutil.copy(os.path.join(KDIR, f), tmp)
            with open(os.path.join(tmp, "announcements.json")) as fh:
                d = json.load(fh)
            d["data"]["items"].insert(0, {"annTitle": "KuCoin Futures Will Delist the SOLUSDT Perpetual Contract",
                                          "annType": ["delistings"], "cTime": 1793280000000, "annUrl": "x"})
            with open(os.path.join(tmp, "announcements.json"), "w") as fh:
                json.dump(d, fh)
            with open(os.path.join(tmp, "status.json"), "w") as fh:
                json.dump({"code": "200000", "data": {"status": "open", "msg": ""}}, fh)
            b = ku.build_brief(ku.Fetcher(tmp), FETCHED, dict(ku.DEFAULTS))
            self.assertIn("SOL", b["avoid"])
            self.assertNotIn("ETH", b["avoid"])
        finally:
            shutil.rmtree(tmp)

    def test_real_check_runs(self):
        c = contracts()["SOLUSDTM"]
        b1, b5 = ku.load_bars(ku.Fetcher(KDIR), "SOLUSDTM", FETCHED, dict(ku.DEFAULTS))
        res = ku.evaluate(c, b1, b5, FETCHED, dict(ku.DEFAULTS))
        self.assertIn(res["status"], ("waiting", "triggered", "late", "blocked"))
        self.assertEqual(res["range"]["from"], "14:33")
        self.assertTrue(res["zones"])


class Setup(unittest.TestCase):
    now = DT(2026, 10, 29, 14, 0)

    def cfg(self, **kw):
        c = dict(ku.DEFAULTS)
        c.update(kw)
        return c

    def test_long_triggers_on_the_close_above_the_range(self):
        b1, b5 = synthetic(self.now)
        res = ku.evaluate(sol(), b1, b5, self.now, self.cfg())
        self.assertEqual(res["status"], "triggered", res["reasons"])
        self.assertEqual(res["side"], "long")
        self.assertEqual((res["range"]["high"], res["range"]["low"]), (100.5, 100.0))
        self.assertEqual(res["trigger"]["price"], 100.8)
        self.assertEqual(res["trigger"]["volume_x"], 3.0)

    def test_short_triggers_on_the_close_below(self):
        b1, b5 = synthetic(self.now, short=True)
        res = ku.evaluate(sol(), b1, b5, self.now, self.cfg())
        self.assertEqual((res["status"], res["side"]), ("triggered", "short"), res["reasons"])
        self.assertLess(res["trigger"]["price"], res["range"]["low"])
        self.assertEqual(ku.evaluate(sol(), b1, b5, self.now, self.cfg(), side="long")["status"], "waiting")

    def test_late_blocked_and_waiting(self):
        b1, b5 = synthetic(self.now, bars_after=2)
        self.assertEqual(ku.evaluate(sol(), b1, b5, self.now, self.cfg())["status"], "late")
        b1, b5 = synthetic(self.now, volume=1200)
        res = ku.evaluate(sol(), b1, b5, self.now, self.cfg())
        self.assertEqual(res["status"], "blocked")
        self.assertIn("volume 1.20x", res["reasons"][0])
        b1, b5 = synthetic(self.now, breakout=100.45)
        res = ku.evaluate(sol(), b1, b5, self.now, self.cfg())
        self.assertEqual(res["status"], "waiting")
        self.assertIn("no close outside the range", res["reasons"][0])

    def test_trend_and_funding_gates(self):
        b1, b5 = synthetic(self.now)
        down = [dict(b, c=200 - b["c"], o=200 - b["o"], h=200 - b["l"], l=200 - b["h"]) for b in b5]
        res = ku.evaluate(sol(), b1, down, self.now, self.cfg())
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(any("against the trend" in x for x in res["reasons"]))
        self.assertEqual(ku.evaluate(sol(), b1, down, self.now, self.cfg(trend_filter=False))["status"], "triggered")
        c = sol(nextFundingRateDateTime=ku.ms_of(self.now + datetime.timedelta(minutes=5)))
        res = ku.evaluate(c, b1, b5, self.now, self.cfg())
        self.assertTrue(any("funding at" in x for x in res["reasons"]))

    def test_no_room_to_resistance_blocks(self):
        b1, b5 = synthetic(self.now)
        wall = []
        for i, b in enumerate(b5[-40:]):
            wall.append(dict(b, h=100.95 if i in (10, 25) else b["h"]))
        res = ku.evaluate(sol(), b1, b5[:-40] + wall, self.now, self.cfg())
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(any("to the next resistance" in x for x in res["reasons"]), res["reasons"])

    def test_zones_merge_and_need_two_touches(self):
        t = DT(2026, 10, 29, 0, 0)
        bars = []
        for i in range(40):
            h = 101.0 if i in (8, 24) else 100.0 + (0.001 if i == 30 else 0)
            bars.append({"t": t + datetime.timedelta(minutes=5 * i), "o": 99.8, "h": h, "l": 99.5, "c": 99.8, "v": 1})
        z = ku.zones_from(bars)
        top = [x for x in z if x["high"] == 101.0][0]
        self.assertEqual(top["touches"], 3)                    # two swings and the 24h high
        self.assertIn("24h high", top["kinds"])
        self.assertTrue(any("UTC day open" in x["kinds"] for x in z))


class Plan(unittest.TestCase):
    now = DT(2026, 10, 29, 14, 0)

    def entry(self, side="long"):
        return {"symbol": "SOLUSDTM", "side": side, "price": 100.8, "status": "triggered",
                "time": "2026-10-29T14:00:00Z", "range": {"high": 100.5, "low": 100.0, "mid": 100.25}, "zones": []}

    def test_long_plan(self):
        p = ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now, equity=1000)
        self.assertEqual((p["stop"], p["target"]), (100.25, 101.625))
        self.assertEqual(p["r_multiple"], 1.5)
        self.assertEqual(p["leverage"], 1)
        self.assertEqual(p["close_by"], "2026-10-29T14:30:00Z")
        self.assertLessEqual(p["max_loss"], 4.0)
        self.assertLessEqual(p["notional_usdt"], 500.0)            # 50 % of 1,000
        self.assertEqual(p["orders"]["stop_loss"]["stop"], "down")
        self.assertEqual(p["orders"]["take_profit"]["stop"], "up")
        self.assertTrue(p["orders"]["stop_loss"]["closeOrder"])
        self.assertEqual(p["orders"]["entry"]["side"], "buy")
        self.assertLess(p["liquidation_est"], p["stop"])

    def test_short_plan_and_rounding(self):
        e = self.entry("short")
        e["price"] = 99.2
        e["range"] = {"high": 100.0, "low": 99.5, "mid": 99.7503}
        p = ku.build_plan(sol(), e, dict(ku.DEFAULTS), self.now)
        self.assertEqual(p["stop"], 99.751)              # rounded up, away from a short's entry
        self.assertGreater(p["liquidation_est"], p["stop"])
        self.assertEqual(p["orders"]["stop_loss"]["stop"], "up")
        self.assertEqual(p["orders"]["entry"]["side"], "sell")
        self.assertEqual(p["decided_by"], "max loss per trade")

    def test_refusals(self):
        cfg = dict(ku.DEFAULTS, max_loss_per_trade_usdt=0.01)
        with self.assertRaises(ku.KuError) as cm:
            ku.build_plan(sol(), self.entry(), cfg, self.now)
        self.assertIn("skip this trade", str(cm.exception))
        with self.assertRaises(ku.KuError):
            ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now, stop="price:101")
        log = {"trades": [{"date": "2026-10-29", "max_loss": 9.0}] * 4}
        with self.assertRaises(ku.KuError) as cm:
            ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now, log=log)
        self.assertIn("done for the day", str(cm.exception))
        log = {"trades": [{"date": "2026-10-29", "max_loss": 3.9}] * 2 + [{"date": "2026-10-29", "max_loss": 2.19}]}
        with self.assertRaises(ku.KuError):       # 0.01 USDT of the 10 USDT daily limit left
            ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now, log=log)
        log = {"trades": [{"date": "2026-10-29", "max_loss": 4.0}] * 2 + [{"date": "2026-10-29", "max_loss": 2.0}]}
        with self.assertRaises(ku.KuError) as cm:
            ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now, log=log)
        self.assertIn("daily limit", str(cm.exception))

    def test_ready_to_send(self):
        p = ku.build_plan(sol(), self.entry(), dict(ku.DEFAULTS), self.now)
        brief = {"date": "2026-10-29", "status": "open", "news_checked_at": None, "avoid": []}
        self.assertIn("news check", ku.ready_to_send(p, brief, self.now, ku.DEFAULTS)[0])
        brief["news_checked_at"] = "2026-10-29T13:00:00Z"
        self.assertEqual(ku.ready_to_send(p, brief, self.now, ku.DEFAULTS), [])
        self.assertIn("avoid list", ku.ready_to_send(p, dict(brief, avoid=["SOL"]), self.now, ku.DEFAULTS)[0])
        self.assertIn("no brief from today", ku.ready_to_send(p, dict(brief, date="2026-10-28"), self.now, ku.DEFAULTS)[0])
        late = self.now + datetime.timedelta(minutes=4)
        self.assertIn("plan again", ku.ready_to_send(p, brief, late, ku.DEFAULTS)[0])


class Private(unittest.TestCase):
    def test_signature(self):
        h = ku.signed_headers("POST", "/api/v1/orders", '{"a":1}', "key", "secret", "pass", ts=1700000000000)
        import base64
        import hashlib
        import hmac
        want = base64.b64encode(hmac.new(b"secret", b'1700000000000POST/api/v1/orders{"a":1}', hashlib.sha256).digest()).decode()
        self.assertEqual(h["KC-API-SIGN"], want)
        self.assertEqual(h["KC-API-KEY-VERSION"], "2")
        self.assertNotEqual(h["KC-API-PASSPHRASE"], "pass")
        self.assertNotIn("secret", json.dumps(h))

    def test_cli_order_flow_offline(self):
        tmp = tempfile.mkdtemp()
        saved = {k: os.environ.pop(k, None) for k in ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE")}
        try:
            j = lambda n: os.path.join(tmp, n)
            cfg = ["--config", j("kucoin.json")]
            now = ["--now", "2026-10-29 14:49"]
            code, text, _ = run(["brief", "--offline", KDIR, "--out", j("brief.json")] + now + cfg)
            self.assertEqual(code, 0)
            with open(j("entry.json"), "w") as fh:
                json.dump({"symbol": "SOLUSDTM", "side": "long", "price": 102.3, "status": "triggered",
                           "time": "2026-10-29T14:49:00Z", "range": {"high": 102.197, "low": 100.939, "mid": 101.568}}, fh)
            code, text, err = run(["plan", "--offline", KDIR, "--from", j("entry.json"), "--equity", "500",
                                   "--write", j("plan.json"), "--log", j("log.json")] + now + cfg)
            self.assertEqual(code, 0, err)
            self.assertIn("isolated 1x", text)
            order = ["order", "--plan", j("plan.json"), "--brief", j("brief.json"), "--log", j("log.json")] + now + cfg
            code, _, err = run(order + ["--yes", "ja", "--dry-run"])
            self.assertEqual(code, 1)
            self.assertIn("news check is not recorded", err)
            code, _, _ = run(["news", "--brief", j("brief.json"), "--summary", "no events", "--avoid", "HYPE"] + now)
            self.assertEqual(code, 0)
            code, _, err = run(order + ["--yes", "maybe later", "--dry-run"])
            self.assertIn("nothing was sent", err)
            code, text, err = run(order + ["--yes", "ja", "--dry-run"])
            self.assertEqual(code, 0, err)
            self.assertIn('"closeOrder":true', text)
            self.assertIn("Dry run: nothing was sent.", text)
            code, _, err = run(order + ["--yes", "ja"])
            self.assertEqual(code, 1)
            self.assertIn("never in the chat", err)
            self.assertFalse(os.path.exists(j("log.json")))
            code, _, err = run(["close", "SOL", "--log", j("log.json")])
            self.assertIn("needs --plan", err)
            with open(j("brief.json")) as fh:
                self.assertEqual(json.load(fh)["avoid"], ["HYPE"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            shutil.rmtree(tmp)


def history(items):
    return {"code": "200000", "data": {"currentPage": 1, "pageSize": 200, "totalNum": len(items), "totalPage": 1,
                                       "items": items}}


def pos(sym, kind, opened, minutes, op, cp, pnl, fee=0.1, funding=0.0):
    o = ku.ms_of(opened)
    return {"symbol": sym, "type": kind, "openTime": o, "closeTime": o + minutes * 60000, "openPrice": op,
            "closePrice": cp, "pnl": pnl, "tradeFee": fee, "fundingFee": funding, "leverage": 1}


class Report(unittest.TestCase):
    """SYNTHETIC closed positions in KuCoin's history-positions shape."""
    now = DT(2026, 11, 5, 20, 0)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        d = DT(2026, 11, 2, 14, 0)
        items = [pos("SOLUSDTM", "CLOSE_LONG", d, 6, 100.8, 100.25, -3.9),
                 pos("SOLUSDTM", "CLOSE_LONG", d + datetime.timedelta(days=1), 12, 100.8, 101.625, 5.6),
                 pos("XBTUSDTM", "CLOSE_SHORT", d + datetime.timedelta(days=2), 30, 81200, 81100, 1.1, funding=-0.02),
                 pos("ETHUSDTM", "CLOSE_SHORT", d - datetime.timedelta(days=9), 30, 3000, 2990, 2.0)]   # outside the week
        with open(os.path.join(self.tmp, "history-positions.json"), "w") as fh:
            json.dump(history(items), fh)
        journal = {"trades": [
            {"id": "t1", "symbol": "SOLUSDTM", "sent_at": "2026-11-02T14:00:20Z", "entry": 100.8, "stop": 100.25,
             "target": 101.625, "max_loss": 3.95},
            {"id": "t2", "symbol": "SOLUSDTM", "sent_at": "2026-11-03T14:00:10Z", "entry": 100.8, "stop": 100.25,
             "target": 101.625, "max_loss": 3.95},
            {"id": "t3", "symbol": "XBTUSDTM", "sent_at": "2026-11-04T14:01:00Z", "entry": 81200, "stop": 81524.8,
             "target": 80712.8, "max_loss": 1.27}]}
        self.log = os.path.join(self.tmp, "log.json")
        with open(self.log, "w") as fh:
            json.dump(journal, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_week(self):
        items = ku.closed_positions(ku.Fetcher(self.tmp), self.now - datetime.timedelta(days=7), self.now)
        self.assertEqual(len(items), 3)
        with open(self.log) as fh:
            rows = ku.trade_rows(items, json.load(fh)["trades"])
        self.assertEqual([t["exit"] for t in rows], ["stop-loss", "target", "time stop"])
        self.assertEqual([t["side"] for t in rows], ["long", "long", "short"])
        self.assertEqual(rows[1]["r"], 1.42)
        s = ku.stats(rows)
        self.assertEqual((s["trades"], s["wins"], s["losses"], s["net"]), (3, 2, 1, 2.8))
        self.assertEqual(s["profit_factor"], 1.72)
        self.assertEqual(s["max_drawdown"], -3.9)
        self.assertIn("too few to judge", ku.verdict(s, ku.DEFAULTS))
        self.assertIn("made money", ku.verdict(s, ku.DEFAULTS))

    def test_unmatched_and_losing(self):
        rows = ku.trade_rows([pos("SOLUSDTM", "LIQUID_LONG", DT(2026, 11, 3), 3, 100, 60, -9.0)], [])
        self.assertEqual((rows[0]["exit"], rows[0]["r"]), ("liquidated", None))
        s = ku.stats(rows)
        self.assertIn("lost money", ku.verdict(s, ku.DEFAULTS))
        self.assertIn("filling past their price", ku.verdict(s, ku.DEFAULTS))
        self.assertIn("nothing to judge", ku.verdict(ku.stats([]), ku.DEFAULTS))

    def test_cli(self):
        out = os.path.join(self.tmp, "report.json")
        code, text, err = run(["report", "--offline", self.tmp, "--log", self.log, "--now", "2026-11-05 20:00",
                               "--config", os.path.join(self.tmp, "none.json"), "--write", out])
        self.assertEqual(code, 0, err)
        self.assertIn("Trades 3: 2 won, 1 lost", text)
        self.assertIn("By exit:", text)
        self.assertIn("Not financial advice", text)
        with open(out) as fh:
            self.assertEqual(json.load(fh)["stats"]["net"], 2.8)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-kucoin", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-kucoin\n"))
        low = " ".join(text.lower().split())
        for must in ("kucoin.py brief", "kucoin.py news", "1x", "never in the chat", "not financial advice",
                     "yes", "30 minutes", "short"):
            self.assertIn(must, low)
        for never in ("\u2014", "real " + "time", "guaranteed"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
