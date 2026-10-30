#!/usr/bin/env python3
"""KuCoin Futures day trader for trader-kucoin: the daily brief, a 15-minute
range breakout on 1-minute bars for trades of about 30 minutes, long or short,
the plan, and the order. Standard library only. Leverage is fixed at 1x.

    python3 kucoin.py brief                       # status, announcements, what is tradeable today
    python3 kucoin.py news --summary "CPI 14:30 CET, no crypto headlines" --avoid HYPE
    python3 kucoin.py levels SOL                  # support and resistance, last 24 hours
    python3 kucoin.py check SOL --write           # triggered / late / blocked / waiting
    python3 kucoin.py watch SOL --minutes 60      # once a minute until it triggers
    python3 kucoin.py plan --write                # stop, target, size, close-by time
    python3 kucoin.py order --yes "ja"            # sends the plan: entry, stop-loss, take-profit
    python3 kucoin.py close SOL --plan            # the 30-minute time stop (covered by that yes)
    python3 kucoin.py report --days 7             # the week: P/L, win rate, by exit, side and contract
    python3 kucoin.py account                     # equity and open positions

Data: KuCoin's public futures API (https://api-futures.kucoin.com): the
contract list, 1- and 5-minute candles (200 a request), the service status;
and KuCoin's announcements (https://api.kucoin.com/api/v3/announcements).
Orders, the account and positions need KUCOIN_API_KEY, KUCOIN_API_SECRET and
KUCOIN_API_PASSPHRASE in the environment (never in the chat).

The setup (one rule, long or short):
  range    the 15 closed 1-minute bars before the signal bar (--range-minutes).
  long     the signal bar CLOSES above the range high, or above the top of a
           resistance zone the range high sits in.
  short    it closes below the range low, or below the bottom of a support
           zone the range low sits in.
  gates    volume at least 1.5x the range bars' average; a range between
           0.15 % and 1.5 % of the price; with the trend (long over the 5-minute
           EMA 50, short under it); room to the next zone of at least 1R; not
           in the 10 minutes before a funding time.
A signal on the newest closed bar is triggered; one or two bars old is late
(do not chase it); a breakout that fails a gate is blocked, with the reason.

Zones come from the 24 hours of 5-minute bars before the range: swing highs
and lows (beyond every bar 30 minutes on each side), the 24-hour high and low
and the UTC day's open. Prices within 0.2 % merge; a zone needs 2 touches or
a named level. They are where price turned before, not a forecast.

Exit codes: 0 on success (check: triggered), 1 otherwise or on an error.
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

FUTURES = "https://api-futures.kucoin.com"
SPOT = "https://api.kucoin.com"
UA = "day-trader-skill/2.0 trader-kucoin (+https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.15      # seconds between public requests
MAX_BARS = 200    # KuCoin answers at most 200 candles a request, from `from` on
EPOCH = datetime.datetime(1970, 1, 1)

DEFAULTS = {
    "leverage": 1,                    # fixed: this pack does not trade with leverage
    "max_loss_per_trade_usdt": 4.0,   # the loss at the stop, fees included
    "max_loss_pct": 1.0,              # and at most this % of the account equity
    "daily_loss_limit_usdt": 10.0,    # the summed max loss of today's planned trades
    "max_trades_per_day": 4,
    "max_position_pct": 50.0,         # the position's value as % of equity (1x: its margin)
    "hold_minutes": 30,               # the time stop
    "range_minutes": 15,
    "min_volume_x": 1.5,
    "min_room_r": 1.0,
    "target_r": 1.5,
    "min_range_pct": 0.15,
    "max_range_pct": 1.5,
    "trend_filter": True,
    "funding_block_minutes": 10,
    "min_turnover_usdt": 20000000.0,  # 24-hour turnover a crypto contract needs to make the brief
    "min_turnover_other_usdt": 3000000.0,  # the same for stocks, metals and commodities
    "plan_max_age_minutes": 3,        # an order from an older plan is refused: re-plan
    "equity_usdt": None,              # the futures account's balance, for the 1 % rule when KuCoin cannot be asked
}
SWING_BARS = 6      # 5-minute bars on each side of a swing (30 minutes)
ZONE_PCT = 0.2
SR_SHOW = 3
EMA_BARS = 50
WATCH_WORDS = ("delist", "maintenance", "suspend", "halt", "upgrade", "migration", "hack", "exploit",
               "discontinu", "pause", "risk warning", "settlement")


class KuError(Exception):
    pass


# ---------------------------------------------------------------- time

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of_ms(ms):
    return EPOCH + datetime.timedelta(milliseconds=int(ms))


def ms_of(utc):
    return int((utc - EPOCH).total_seconds() * 1000)


def _last_sunday(year, month):
    d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else datetime.date(year, 12, 31)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def berlin(utc):
    """UTC -> Berlin wall time: CEST (+2) from 01:00 UTC on the last Sunday of March
    to 01:00 UTC on the last Sunday of October, CET (+1) otherwise (EU rule)."""
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return utc + datetime.timedelta(hours=2 if start <= utc < end else 1)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def us_market_open(utc):
    """Is the NYSE in its regular session (weekday, 9:30-16:00 New York)? Holidays are not checked."""
    start = datetime.datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.time(6, 0))
    ny = utc + datetime.timedelta(hours=-4 if start <= utc < end else -5)
    return ny.weekday() < 5 and datetime.time(9, 30) <= ny.time() < datetime.time(16, 0)


def stamp(utc):
    return "%s UTC (%s Berlin)" % (utc.strftime("%H:%M"), berlin(utc).strftime("%H:%M"))


def parse_now(text):
    """--now: "2026-10-29 14:45" is UTC; an offset makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise KuError('--now wants a UTC time like "2026-10-29 14:45"')
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- data

def symbol_of(text):
    """BTC, btc-usdt, XBTUSDTM -> XBTUSDTM; TSLA -> TSLAUSDTM. USDT-margined perpetuals only."""
    s = re.sub(r"[-/_ ]", "", str(text or "").upper())
    if not s:
        raise KuError("give a symbol, like BTC, SOL or TSLA")
    if s.endswith("USDTM"):
        base = s[:-5]
    elif s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    if base == "BTC":
        base = "XBT"
    return base + "USDTM"


def base_of(sym):
    b = sym[:-5] if sym.endswith("USDTM") else sym
    return "BTC" if b == "XBT" else b


class Fetcher(object):
    """GETs the public API, or reads saved answers from --offline DIR:
    status.json, contracts.json, announcements.json, kline-<SYMBOL>-<1|5>.json."""

    def __init__(self, offline=None):
        self.offline = offline
        self.first = True

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.exists(path):
                raise KuError("no offline file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if not self.first:
            time.sleep(PAUSE)
        self.first = False
        return http_json("GET", url)


def http_json(method, url, body=None, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = body.encode("utf-8")
        h["Content-Type"] = "application/json"
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            e.close()
            if e.code == 429 and attempt == 0:
                time.sleep(2)
                continue
            try:
                doc = json.loads(text)
                raise KuError("KuCoin answered HTTP %d: %s (code %s)" % (e.code, doc.get("msg"), doc.get("code")))
            except ValueError:
                raise KuError("KuCoin answered HTTP %d for %s" % (e.code, url.split("?")[0]))
        except urllib.error.URLError as e:
            raise KuError("could not reach %s: %s (is the host allowed in the network settings?)"
                          % (url.split("/api")[0], e.reason))
        except ValueError:
            raise KuError("KuCoin sent something that is not JSON for %s" % url.split("?")[0])
    raise KuError("KuCoin rate-limited us twice: wait a minute")


def payload(doc, what):
    if not isinstance(doc, dict) or str(doc.get("code")) != "200000":
        raise KuError("KuCoin refused the %s request: %s" % (what, (doc or {}).get("msg") if isinstance(doc, dict) else doc))
    return doc.get("data")


def contracts(fetcher):
    return payload(fetcher.get(FUTURES + "/api/v1/contracts/active", "contracts.json"), "contract list") or []


def contract(fetcher, sym):
    for c in contracts(fetcher):
        if c.get("symbol") == sym:
            return c
    raise KuError("KuCoin Futures has no open contract %s" % sym)


def klines(fetcher, sym, minutes, start, end):
    """Closed candles [start, end) as dicts, oldest first. KuCoin sends at most
    200 a request from `from` on, so longer windows go in pieces."""
    step = datetime.timedelta(minutes=minutes)
    out = {}
    if fetcher.offline:
        rows = payload(fetcher.get(None, "kline-%s-%d.json" % (sym, minutes)), "candle") or []
    else:
        rows, a = [], start
        while a < end:
            b = min(end, a + step * MAX_BARS)
            url = "%s/api/v1/kline/query?symbol=%s&granularity=%d&from=%d&to=%d" % (
                FUTURES, sym, minutes, ms_of(a), ms_of(b))
            rows += payload(fetcher.get(url, None), "candle") or []
            a = b
    for r in rows:
        t = utc_of_ms(r[0])
        if t < start or t + step > end:
            continue                      # outside the window, or still forming
        out[t] = {"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                  "v": float(r[5])}
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- config and files

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise KuError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def read_config(path):
    cfg = dict(DEFAULTS)
    doc = read_json(path, {}) or {}
    for k in DEFAULTS:
        if k in doc and doc[k] is not None:
            cfg[k] = doc[k]
    if float(cfg["leverage"]) != 1:
        raise KuError("leverage is fixed at 1x in this pack (%s has %s): set \"leverage\": 1" % (path, cfg["leverage"]))
    return cfg


# ---------------------------------------------------------------- brief

def r(x, n=2):
    return None if x is None else round(float(x), n) + 0.0


def mentions(title, base):
    """BASE as a word, or as KuCoin writes a pair: SOLUSDT, SOLUSDTM, SOL-USDT."""
    pat = r"(?<![A-Z0-9])%s(?:-?USDTM?|-?USDC)?(?![A-Z0-9])" % re.escape(base)
    return re.search(pat, title.upper()) is not None


def build_brief(fetcher, now, cfg, top=10):
    status = payload(fetcher.get(FUTURES + "/api/v1/status", "status.json"), "status") or {}
    problems = []
    if str(status.get("status")) != "open":
        problems.append("KuCoin Futures is %s%s: no trading" % (
            status.get("status"), (": " + status["msg"]) if status.get("msg") else ""))
    anns = []
    try:
        url = SPOT + "/api/v3/announcements?annType=latest-announcements&lang=en_US&pageSize=50"
        items = (payload(fetcher.get(url, "announcements.json"), "announcement") or {}).get("items") or []
        for a in items:
            t = utc_of_ms(a.get("cTime") or 0)
            if now - t > datetime.timedelta(hours=72) or t > now + datetime.timedelta(minutes=5):
                continue
            title = str(a.get("annTitle") or "")
            anns.append({"time": t.strftime("%Y-%m-%d %H:%M"), "title": title, "url": a.get("annUrl"),
                         "types": a.get("annType") or [],
                         "watch": any(w in title.lower() for w in WATCH_WORDS)})
    except KuError as e:
        problems.append("could not read KuCoin's announcements (%s): check kucoin.com/announcement yourself" % e)
    rows = []
    for c in contracts(fetcher):
        if c.get("quoteCurrency") != "USDT" or c.get("isInverse") or c.get("status") != "Open":
            continue
        if c.get("marketStage", "NORMAL") != "NORMAL":
            continue
        turnover = float(c.get("turnoverOf24h") or 0)
        cls = c.get("assetClass") or "CRYPTO"
        if turnover < float(cfg["min_turnover_usdt" if cls == "CRYPTO" else "min_turnover_other_usdt"]):
            continue
        hi, lo = float(c.get("highPrice") or 0), float(c.get("lowPrice") or 0)
        rng = (hi / lo - 1) * 100 if lo > 0 else 0.0
        chg = float(c.get("priceChgPct") or 0) * 100
        fund = float(c.get("fundingFeeRate") or 0) * 100
        sym, base = c["symbol"], base_of(c["symbol"])
        flags = []
        if rng < 1.5:
            flags.append("quiet: %.1f %% range in 24 h, little room for a 30-minute trade" % rng)
        if rng > 25:
            flags.append("wild: %.1f %% range in 24 h, stops get run" % rng)
        if abs(chg) > 15:
            flags.append("moved %+.1f %% in 24 h: news-driven, check why" % chg)
        if abs(fund) >= 0.05:
            flags.append("funding %+.3f %% per 8 h" % fund)
        if cls == "STOCK" and not us_market_open(now):
            flags.append("US market closed now: the stock itself is not trading")
        hits = [a for a in anns if a["watch"] and mentions(a["title"], base)]
        for a in hits:
            flags.append("announcement: " + a["title"])
        fit = 1 - min(abs(math.log(max(rng, 0.1) / 6.0)), 1.5) / 1.5     # 6 % a day fits 30-minute trades best
        score = math.log10(turnover / 1e6) + 2 * fit - 1.0 * len(flags) - (5 if hits else 0)
        rows.append({"symbol": sym, "base": base, "class": cls, "turnover_musdt": r(turnover / 1e6, 1),
                     "range_pct": r(rng), "change_pct": r(chg), "funding_pct": r(fund, 4),
                     "price": c.get("lastTradePrice"), "score": r(score), "flags": flags,
                     "avoid": bool(hits) or str(status.get("status")) != "open"})
    rows.sort(key=lambda x: -x["score"])
    crypto = [x for x in rows if x["class"] == "CRYPTO"][:top]
    other = [x for x in rows if x["class"] != "CRYPTO"][:max(3, top // 2)]
    rows = crypto + other
    return {"created_by": "trader-kucoin", "date": now.strftime("%Y-%m-%d"), "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status.get("status"), "problems": problems,
            "announcements": [a for a in anns if a["watch"]][:15], "ranked": rows,
            "news_checked_at": None, "news_summary": None, "avoid": [x["base"] for x in rows if x["avoid"]]}


def describe_brief(b):
    now = parse_now(b["created_at"])
    out = ["KuCoin Futures brief %s, %s. Futures status: %s." % (b["date"], stamp(now), b["status"])]
    for p in b["problems"]:
        out.append("PROBLEM  " + p)
    if b["announcements"]:
        out.append("Announcements to read (72 h):")
        for a in b["announcements"]:
            out.append("  %s UTC  %s" % (a["time"], a["title"]))
    out.append("Most tradeable today for a 30-minute trade (liquidity and range; nobody knows what will pay):")
    head = "  %-14s %-9s %10s %8s %8s %10s  %s" % ("CONTRACT", "CLASS", "24H TURN", "RANGE", "CHANGE", "FUNDING", "FLAGS")
    out.append(head)
    shown = None
    for x in b["ranked"]:
        if shown == "CRYPTO" and x["class"] != "CRYPTO":
            out.append("Stocks, metals and commodities (thinner: smaller size, wider spreads):")
            out.append(head)
        shown = x["class"]
        out.append("  %-14s %-9s %8.1fM %7.1f%% %+7.1f%% %+9.4f%%  %s" % (
            x["symbol"], x["class"], x["turnover_musdt"], x["range_pct"], x["change_pct"], x["funding_pct"],
            ("AVOID: " if x["avoid"] else "") + "; ".join(x["flags"]) if x["flags"] else "-"))
    out.append("Next: check the news (macro calendar: Fed, CPI, jobs; headlines on the top names), then "
               "record it: kucoin.py news --summary \"...\" [--avoid SYM,...]. No order goes out before that.")
    return out


# ---------------------------------------------------------------- levels

def ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / float(n)
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def zones_from(bars5):
    """Support/resistance zones from 5-minute bars (the 24 hours before the range)."""
    if not bars5:
        return []
    cands = [(max(b["h"] for b in bars5), "24h high"), (min(b["l"] for b in bars5), "24h low")]
    day = bars5[-1]["t"].date()
    opens = [b for b in bars5 if b["t"].date() == day and b["t"].time() == datetime.time(0, 0)]
    if opens:
        cands.append((opens[0]["o"], "UTC day open"))
    k = SWING_BARS
    for i in range(k, len(bars5) - k):
        near = bars5[i - k:i] + bars5[i + 1:i + 1 + k]
        if bars5[i]["h"] >= max(x["h"] for x in near):
            cands.append((bars5[i]["h"], "swing high"))
        if bars5[i]["l"] <= min(x["l"] for x in near):
            cands.append((bars5[i]["l"], "swing low"))
    groups, cur = [], []
    for p, kind in sorted(cands):
        if cur and p > cur[0][0] * (1 + ZONE_PCT / 100.0):
            groups.append(cur)
            cur = []
        cur.append((p, kind))
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        named = [k for _, k in g if not k.startswith("swing")]
        if len(g) < 2 and not named:
            continue
        swings = len(g) - len(named)
        kinds = named + (["%d swing%s" % (swings, "s" if swings > 1 else "")] if swings else [])
        out.append({"low": min(p for p, _ in g), "high": max(p for p, _ in g), "touches": len(g), "kinds": kinds})
    return out


def split(zones, price):
    above = [z for z in zones if z["low"] > price]
    below = [z for z in reversed(zones) if z["high"] < price]
    inside = next((z for z in zones if z["low"] <= price <= z["high"]), None)
    return above, below, inside


def fmt(p, tick):
    d = max(0, int(round(-math.log10(tick)))) if tick and tick < 1 else 0
    return "%.*f" % (d, p)


def zone_text(z, tick):
    where = fmt(z["low"], tick) if z["low"] == z["high"] else "%s-%s" % (fmt(z["low"], tick), fmt(z["high"], tick))
    return "%s (%s)" % (where, ", ".join(z["kinds"]))


# ---------------------------------------------------------------- the setup

def funding_times(c, now):
    """The next funding time: KuCoin's own, else every 8 hours from 00:00 UTC."""
    nxt = c.get("nextFundingRateDateTime")
    if nxt:
        return utc_of_ms(nxt)
    gran = int(c.get("currentFundingRateGranularity") or 28800000) // 60000
    mins = now.hour * 60 + now.minute
    return datetime.datetime.combine(now.date(), datetime.time(0, 0)) + datetime.timedelta(
        minutes=(mins // gran + 1) * gran)


def evaluate(c, bars1, bars5, now, cfg, side="both"):
    """Pure function: the setup's status at `now` from closed 1- and 5-minute bars."""
    n = int(cfg["range_minutes"])
    tick = float(c.get("tickSize") or 0.01)
    res = {"symbol": c["symbol"], "class": c.get("assetClass") or "CRYPTO", "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "status": "waiting", "side": None, "reasons": [], "range": None, "trigger": None, "tick": tick,
           "multiplier": c.get("multiplier"), "zones": None, "last": None}
    if len(bars1) < n + 1:
        res.update(status="no-data", reasons=["KuCoin sent %d closed 1-minute bars; the setup needs %d" % (len(bars1), n + 1)])
        return res
    last = bars1[-1]
    res["last"] = {"time": last["t"].strftime("%H:%M"), "close": last["c"]}
    fund_at = funding_times(c, now)
    res["next_funding"] = fund_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = None
    for back in (0, 1, 2):
        i = len(bars1) - 1 - back
        if i < n:
            break
        rbars = bars1[i - n:i]
        b = bars1[i]
        rh, rl = max(x["h"] for x in rbars), min(x["l"] for x in rbars)
        zones = zones_from([x for x in bars5 if x["t"] + datetime.timedelta(minutes=5) <= rbars[0]["t"]])
        _, _, in_hi = split(zones, rh)
        _, _, in_lo = split(zones, rl)
        lvl_long = max(rh, in_hi["high"]) if in_hi else rh
        lvl_short = min(rl, in_lo["low"]) if in_lo else rl
        want = []
        if side in ("both", "long") and b["c"] > lvl_long:
            want.append("long")
        if side in ("both", "short") and b["c"] < lvl_short:
            want.append("short")
        if back == 0:
            res["range"] = {"high": rh, "low": rl, "mid": (rh + rl) / 2, "from": rbars[0]["t"].strftime("%H:%M"),
                            "to": (rbars[-1]["t"] + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                            "long_above": lvl_long, "short_below": lvl_short,
                            "width_pct": r(100 * (rh / rl - 1), 3)}
            res["zones"] = zones
        if want:
            sig = (back, want[0], b, rbars, rh, rl, lvl_long if want[0] == "long" else lvl_short, zones)
            break
    if not sig:
        rg = res["range"]
        res["reasons"].append("no close outside the range yet: last %s, long above %s (%+.2f %%), short below %s (%+.2f %%)" % (
            fmt(last["c"], tick), fmt(rg["long_above"], tick), 100 * (rg["long_above"] / last["c"] - 1),
            fmt(rg["short_below"], tick), 100 * (rg["short_below"] / last["c"] - 1)))
        return res
    back, d, b, rbars, rh, rl, lvl, zones = sig
    mid = (rh + rl) / 2
    risk = abs(b["c"] - mid)
    avg_v = sum(x["v"] for x in rbars) / float(len(rbars))
    vx = b["v"] / avg_v if avg_v else None
    width = 100 * (rh / rl - 1)
    closes5 = [x["c"] for x in bars5 if x["t"] + datetime.timedelta(minutes=5) <= b["t"] + datetime.timedelta(minutes=1)]
    trend = ema(closes5, EMA_BARS)
    above, below, _ = split(zones, b["c"])
    nxt = (above[0] if d == "long" else below[0]) if (above if d == "long" else below) else None
    room = (nxt["low"] - b["c"] if d == "long" else b["c"] - nxt["high"]) if nxt else None
    gates = []
    if cfg["min_volume_x"] and (vx is None or vx < float(cfg["min_volume_x"])):
        gates.append("volume %.2fx the range's average, under %.2fx" % (vx or 0, float(cfg["min_volume_x"])))
    if width < float(cfg["min_range_pct"]):
        gates.append("the range is %.2f %% wide, under %.2f %%: noise" % (width, float(cfg["min_range_pct"])))
    if width > float(cfg["max_range_pct"]):
        gates.append("the range is %.2f %% wide, over %.2f %%: the stop is too far for a 30-minute trade" % (
            width, float(cfg["max_range_pct"])))
    if cfg["trend_filter"]:
        if trend is None:
            gates.append("fewer than %d closed 5-minute bars for the trend" % EMA_BARS)
        elif d == "long" and b["c"] <= trend:
            gates.append("against the trend: the close is under the 5-minute EMA %d (%s)" % (EMA_BARS, fmt(trend, tick)))
        elif d == "short" and b["c"] >= trend:
            gates.append("against the trend: the close is over the 5-minute EMA %d (%s)" % (EMA_BARS, fmt(trend, tick)))
    if room is not None and risk > 0 and room < float(cfg["min_room_r"]) * risk:
        gates.append("only %s (%.2fR) to the next %s %s" % (
            fmt(room, tick), room / risk, "resistance" if d == "long" else "support", zone_text(nxt, tick)))
    end = b["t"] + datetime.timedelta(minutes=1)
    if datetime.timedelta(0) <= fund_at - end <= datetime.timedelta(minutes=int(cfg["funding_block_minutes"])):
        gates.append("funding at %s: no new entries in the %d minutes before" % (
            stamp(fund_at), int(cfg["funding_block_minutes"])))
    res.update(side=d, trigger={"time": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "bar": b["t"].strftime("%H:%M"),
                                "price": b["c"], "level": lvl, "volume_x": r(vx), "bars_ago": back,
                                "ema": trend, "room": room, "risk_to_mid": risk},
               range={"high": rh, "low": rl, "mid": mid, "from": rbars[0]["t"].strftime("%H:%M"),
                      "to": b["t"].strftime("%H:%M"), "long_above": res["range"]["long_above"] if back == 0 else None,
                      "short_below": res["range"]["short_below"] if back == 0 else None, "width_pct": r(width, 3)},
               zones=zones)
    if gates:
        res.update(status="blocked", reasons=gates)
    elif back:
        res.update(status="late", reasons=["the signal bar closed %d minute%s ago: do not chase it, wait for the next one"
                                           % (back, "s" if back > 1 else "")])
    else:
        res.update(status="triggered")
    return res


def describe(res):
    tick = res["tick"]
    out = ["%s  %s  15-minute range breakout on 1-minute bars (KuCoin Futures, 1x)" % (
        res["symbol"], stamp(parse_now(res["now"])))]
    rg = res.get("range")
    if rg:
        out.append("Range %s-%s UTC: high %s, low %s (%.2f %% wide)." % (
            rg["from"], rg["to"], fmt(rg["high"], tick), fmt(rg["low"], tick), rg["width_pct"]))
    t = res.get("trigger")
    if t:
        word = "LONG above" if res["side"] == "long" else "SHORT below"
        out.append("%s  %s %s: the %s bar closed at %s (volume %.2fx)." % (
            res["status"].upper(), word, fmt(t["level"], tick), t["bar"], fmt(t["price"], tick), t["volume_x"] or 0))
    else:
        out.append(res["status"].upper())
    for x in res["reasons"]:
        out.append("  " + x)
    if res.get("zones"):
        ref = t["price"] if t else (res["last"] or {}).get("close")
        if ref:
            above, below, inside = split(res["zones"], ref)
            if inside:
                out.append("  Inside the zone %s." % zone_text(inside, tick))
            out.append("  Resistance: %s." % ("; ".join(zone_text(z, tick) for z in above[:SR_SHOW]) or "none in 24 h"))
            out.append("  Support:    %s." % ("; ".join(zone_text(z, tick) for z in below[:SR_SHOW]) or "none in 24 h"))
    if res["status"] == "triggered":
        out.append("Next: kucoin.py plan --write (stop, target, size). Nothing was sent.")
    else:
        out.append("Nothing was sent.")
    return out


def entry_doc(res):
    return {"created_by": "trader-kucoin", "symbol": res["symbol"], "status": res["status"], "side": res["side"],
            "time": (res.get("trigger") or {}).get("time"), "price": (res.get("trigger") or {}).get("price"),
            "range": res.get("range"), "zones": res.get("zones"), "reasons": res["reasons"], "checked_at": res["now"]}


def load_bars(fetcher, sym, now, cfg):
    end = now.replace(second=0)
    b1 = klines(fetcher, sym, 1, end - datetime.timedelta(minutes=int(cfg["range_minutes"]) + 10), end)
    b5 = klines(fetcher, sym, 5, end - datetime.timedelta(hours=24) - datetime.timedelta(minutes=5 * EMA_BARS), end)
    return b1, b5


# ---------------------------------------------------------------- plan

def round_away(p, tick, down):
    q = p / tick
    q = math.floor(q + 1e-9) if down else math.ceil(q - 1e-9)
    return round(q * tick, 10)


def todays_log(log, day):
    return [x for x in (log or {}).get("trades", []) if str(x.get("date")) == day]


def build_plan(c, entry, cfg, now, equity=None, log=None, stop="mid", target=None):
    side = entry["side"]
    if side not in ("long", "short"):
        raise KuError("the entry has no side: run kucoin.py check first")
    tick = float(c.get("tickSize") or 0.01)
    mult = float(c["multiplier"])
    px = float(entry["price"])
    rg = entry.get("range") or {}
    long_ = side == "long"
    if stop == "mid":
        sp = rg.get("mid")
    elif stop == "edge":
        sp = rg.get("low") if long_ else rg.get("high")
    elif stop.startswith("price:"):
        sp = float(stop[6:])
    elif stop.startswith("pct:"):
        sp = px * (1 - float(stop[4:]) / 100) if long_ else px * (1 + float(stop[4:]) / 100)
    else:
        raise KuError("--stop is mid, edge, price:X or pct:X")
    if sp is None:
        raise KuError("the entry has no range for a %s stop: give --stop price:X" % stop)
    sp = round_away(sp, tick, down=long_)
    risk = px - sp if long_ else sp - px
    if risk <= 0:
        raise KuError("the stop %s is on the wrong side of the entry %s" % (fmt(sp, tick), fmt(px, tick)))
    if risk < 3 * tick:
        raise KuError("the stop is %s from the entry, under 3 ticks: too tight to survive the spread" % fmt(risk, tick))
    tr = float(cfg["target_r"])
    if target and target.startswith("price:"):
        tp = float(target[6:])
    else:
        if target:
            tr = float(target.rstrip("Rr"))
        tp = px + tr * risk if long_ else px - tr * risk
    tp = round_away(tp, tick, down=long_)        # a target rounds toward the entry
    reward = tp - px if long_ else px - tp
    if reward <= 0:
        raise KuError("the target is on the wrong side of the entry")
    fee = float(c.get("takerFeeRate") or 0.0006)
    per_lot = risk * mult + (px + sp) * mult * fee
    cap_loss = float(cfg["max_loss_per_trade_usdt"])
    limits = {"max loss per trade": cap_loss}
    if equity:
        limits["%.1f %% of equity" % float(cfg["max_loss_pct"])] = equity * float(cfg["max_loss_pct"]) / 100
    day = now.strftime("%Y-%m-%d")
    today = todays_log(log, day)
    used = sum(float(x.get("max_loss") or 0) for x in today)
    if len(today) >= int(cfg["max_trades_per_day"]):
        raise KuError("%d trades planned today, your limit is %d: done for the day" % (len(today), int(cfg["max_trades_per_day"])))
    left = float(cfg["daily_loss_limit_usdt"]) - used
    if left <= 0:
        raise KuError("today's planned losses (%.2f USDT) reached your daily limit %.2f: done for the day" % (
            used, float(cfg["daily_loss_limit_usdt"])))
    limits["daily limit left"] = left
    budget = min(limits.values())
    decided = min(limits, key=limits.get)
    lots = int(math.floor(budget / per_lot + 1e-9))
    if equity:
        cap = int(math.floor(equity * float(cfg["max_position_pct"]) / 100 / (px * mult) + 1e-9))
        if cap < lots:
            lots, decided = cap, "position cap %.0f %% of equity" % float(cfg["max_position_pct"])
    if lots < 1:
        raise KuError("one contract (%s %s) would lose %.2f USDT at the stop, over %.2f (%s): skip this trade "
                      "or take a closer stop" % (mult, base_of(c["symbol"]), per_lot, budget, decided))
    mmr = float(c.get("maintainMargin") or 0.004)
    liq = px * mmr if long_ else px * (2 - mmr)       # isolated 1x: leverage 1 leaves only the maintenance margin
    start = parse_now(entry.get("time")) if entry.get("time") else now
    close_by = start + datetime.timedelta(minutes=int(cfg["hold_minutes"]))
    oid = "dts-" + uuid.uuid4().hex[:16]
    open_side, close_side = ("buy", "sell") if long_ else ("sell", "buy")
    plan = {"created_by": "trader-kucoin", "id": oid, "symbol": c["symbol"], "base": base_of(c["symbol"]),
            "side": side, "date": day, "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entry": px, "stop": sp, "target": tp, "risk_per_unit": r(risk, 10), "r_multiple": r(reward / risk),
            "lots": lots, "multiplier": mult, "size_units": r(lots * mult, 8), "notional_usdt": r(lots * mult * px),
            "margin_usdt": r(lots * mult * px), "leverage": 1, "margin_mode": "ISOLATED",
            "max_loss": r(lots * per_lot), "fees_est": r(lots * (px + sp) * mult * fee),
            "reward_at_target": r(lots * (reward * mult - (px + tp) * mult * fee)),
            "liquidation_est": r(liq, 8), "close_by": close_by.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "decided_by": decided, "tick": tick,
            "orders": {
                "entry": {"clientOid": oid + "-e", "symbol": c["symbol"], "side": open_side, "type": "market",
                          "size": lots, "leverage": 1, "marginMode": "ISOLATED"},
                "stop_loss": {"clientOid": oid + "-s", "symbol": c["symbol"], "side": close_side, "type": "market",
                              "stop": "down" if long_ else "up", "stopPriceType": "TP", "stopPrice": fmt(sp, tick),
                              "closeOrder": True, "marginMode": "ISOLATED"},
                "take_profit": {"clientOid": oid + "-t", "symbol": c["symbol"], "side": close_side, "type": "market",
                                "stop": "up" if long_ else "down", "stopPriceType": "TP", "stopPrice": fmt(tp, tick),
                                "closeOrder": True, "marginMode": "ISOLATED"}}}
    zones = entry.get("zones") or []
    above, below, _ = split(zones, px)
    nxt = (above if long_ else below)
    if nxt and ((long_ and tp > nxt[0]["low"]) or (not long_ and tp < nxt[0]["high"])):
        plan["note"] = "the target is past the next %s %s: it has to get through it" % (
            "resistance" if long_ else "support", zone_text(nxt[0], tick))
    return plan


def ticket(p):
    t = p["tick"]
    word = "LONG (buy)" if p["side"] == "long" else "SHORT (sell)"
    out = ["TICKET %s  %s %s, %d contract%s = %s %s, market, isolated 1x" % (
        p["id"], word, p["symbol"], p["lots"], "s" if p["lots"] > 1 else "", p["size_units"], p["base"]),
        "  entry about %s (value %.2f USDT, margin %.2f USDT)" % (fmt(p["entry"], t), p["notional_usdt"], p["margin_usdt"]),
        "  stop-loss %s, take-profit %s (%.2fR), both on the last price, both close the position" % (
            fmt(p["stop"], t), fmt(p["target"], t), p["r_multiple"]),
        "  max loss at the stop %.2f USDT incl. fees (%s decided the size); at the target +%.2f USDT" % (
            p["max_loss"], p["decided_by"], p["reward_at_target"]),
        "  time stop: close at market by %s" % stamp(parse_now(p["close_by"])),
        "  liquidation about %s (1x: far beyond the stop)" % fmt(p["liquidation_est"], t)]
    if p.get("note"):
        out.append("  note: " + p["note"])
    out.append("A yes covers this ticket only: the entry, its stop-loss, its take-profit and the time-stop close. "
               "A stop is a plan, not a guarantee: in a fast market it can fill past its price.")
    return out


# ---------------------------------------------------------------- private API

def keys():
    k, s, p = (os.environ.get(n) for n in ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE"))
    if not (k and s and p):
        raise KuError("KUCOIN_API_KEY, KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE are not all set: add them in the "
                      "environment's settings (never in the chat) and start a new session")
    return k, s, p


def signed_headers(method, endpoint, body, k, s, p, ts=None):
    """KuCoin API key version 2: HMAC-SHA256 over timestamp + method + endpoint (with its
    query) + body, base64; the passphrase is signed with the secret too."""
    ts = str(ts if ts is not None else int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(s.encode(), (ts + method + endpoint + (body or "")).encode(), hashlib.sha256).digest())
    pas = base64.b64encode(hmac.new(s.encode(), p.encode(), hashlib.sha256).digest())
    return {"KC-API-KEY": k, "KC-API-SIGN": sig.decode(), "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": pas.decode(), "KC-API-KEY-VERSION": "2"}


def private(method, endpoint, doc=None):
    k, s, p = keys()
    body = json.dumps(doc, separators=(",", ":")) if doc is not None else ""
    out = http_json(method, FUTURES + endpoint, body if doc is not None else None,
                    signed_headers(method, endpoint, body, k, s, p))
    return payload(out, "%s %s" % (method, endpoint.split("?")[0]))


# ---------------------------------------------------------------- commands

def _fetcher(a):
    return Fetcher(a.offline)


def cmd_brief(a):
    cfg = read_config(a.config)
    b = build_brief(_fetcher(a), parse_now(a.now), cfg, a.top)
    write_json(a.out, b)
    if a.json:
        print(json.dumps(b, indent=2))
    else:
        print("\n".join(describe_brief(b)))
        print("Wrote %s." % a.out)
    return 0


def cmd_news(a):
    b = read_json(a.brief)
    if not b:
        raise KuError("no brief at %s: run kucoin.py brief first" % a.brief)
    now = parse_now(a.now)
    if b.get("date") != now.strftime("%Y-%m-%d"):
        raise KuError("the brief is from %s, not today: run kucoin.py brief again" % b.get("date"))
    if not a.summary.strip():
        raise KuError("--summary wants what the news check found, in a line or two")
    avoid = set(b.get("avoid") or [])
    for x in (a.avoid or "").split(","):
        if x.strip():
            avoid.add(base_of(symbol_of(x)))
    b.update(news_checked_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), news_summary=a.summary.strip(), avoid=sorted(avoid))
    write_json(a.brief, b)
    print("News check recorded at %s. Avoid today: %s." % (stamp(now), ", ".join(b["avoid"]) or "nothing"))
    return 0


def cmd_levels(a):
    cfg = read_config(a.config)
    f = _fetcher(a)
    sym = symbol_of(a.symbol)
    c = contract(f, sym)
    now = parse_now(a.now)
    _, b5 = load_bars(f, sym, now, cfg)
    tick = float(c.get("tickSize") or 0.01)
    zones = zones_from([x for x in b5 if x["t"] >= now - datetime.timedelta(hours=24)])
    px = float(b5[-1]["c"]) if b5 else float(c.get("lastTradePrice"))
    above, below, inside = split(zones, px)
    print("%s  %s  support/resistance from the last 24 h of 5-minute bars, around %s" % (sym, stamp(now), fmt(px, tick)))
    if inside:
        print("  Inside the zone %s." % zone_text(inside, tick))
    print("  Resistance: %s." % ("; ".join(zone_text(z, tick) for z in above[:SR_SHOW]) or "none"))
    print("  Support:    %s." % ("; ".join(zone_text(z, tick) for z in below[:SR_SHOW]) or "none"))
    print("  Levels are where price turned before, not a forecast.")
    return 0


def cmd_check(a):
    cfg = read_config(a.config)
    f = _fetcher(a)
    sym = symbol_of(a.symbol)
    c = contract(f, sym)
    now = parse_now(a.now)
    b1, b5 = load_bars(f, sym, now, cfg)
    res = evaluate(c, b1, b5, now, cfg, a.side)
    if a.write:
        write_json(a.write, entry_doc(res))
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print("\n".join(describe(res)))
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    f = _fetcher(a)
    sym = symbol_of(a.symbol)
    c = contract(f, sym)
    sim = parse_now(a.now) if (a.now or a.offline) else None
    stop_at = (sim or utc_now()) + datetime.timedelta(minutes=a.minutes)
    seen = None
    while True:
        now = sim or utc_now()
        if now >= stop_at:
            print("Stopped after %d minutes without a trigger. Nothing was sent." % a.minutes)
            return 1
        b1, b5 = load_bars(f, sym, now, cfg)
        res = evaluate(c, b1, b5, now, cfg, a.side)
        key = (res["status"], (res.get("trigger") or {}).get("time"), (res.get("last") or {}).get("time"))
        if key != seen:
            print("\n".join(describe(res)) if res["status"] in ("triggered", "blocked") else
                  "%s  %s  %s" % (stamp(now), res["status"], "; ".join(res["reasons"])), flush=True)
            seen = key
        if res["status"] == "triggered":
            write_json(a.out, entry_doc(res))
            print("Wrote %s." % a.out)
            return 0
        if sim:
            sim += datetime.timedelta(minutes=1)
        else:
            wait = 60 - utc_now().second + 3            # just after the next 1-minute bar closes
            time.sleep(wait)


def equity_of(a, cfg):
    """The futures equity for the 1 % rule: --equity, else the config, else KuCoin.
    KuCoin refuses account calls from some regions (a US cloud session among them),
    so a plan without any of them is refused rather than sized without the rule."""
    if a.equity is not None:
        return float(a.equity)
    if cfg.get("equity_usdt"):
        return float(cfg["equity_usdt"])
    if not a.offline and os.environ.get("KUCOIN_API_KEY"):
        try:
            acct = private("GET", "/api/v1/account-overview?currency=USDT") or {}
            if float(acct.get("accountEquity") or 0) > 0:
                return float(acct["accountEquity"])
        except KuError as e:
            print("KuCoin did not give the balance (%s)." % str(e)[:160], file=sys.stderr)
    raise KuError("no account balance for the 1 %% rule: give --equity USDT (the futures balance in the app) "
                  "or set \"equity_usdt\" in %s" % a.config)


def app_steps(p):
    """The ticket as taps in the KuCoin app, for a trade the user places by hand."""
    t = p["tick"]
    pair = p["symbol"][:-1] if p["symbol"].endswith("USDTM") else p["symbol"]
    pair = ("BTC" + pair[3:]) if pair.startswith("XBT") else pair
    return ["In the KuCoin app: Futures > %s Perpetual." % pair,
            "  1. Margin mode Isolated, leverage 1x.",
            "  2. Order type Market. Size %d contract%s (= %s %s; the app may show either)." % (
                p["lots"], "s" if p["lots"] > 1 else "", p["size_units"], p["base"]),
            "  3. Tick TP/SL: take-profit %s, stop-loss %s, trigger on the last price." % (
                fmt(p["target"], t), fmt(p["stop"], t)),
            "  4. %s, check the summary, confirm." % ("Buy/Long" if p["side"] == "long" else "Sell/Short"),
            "  5. Set an alarm for %s: close whatever is left at market (the time stop)." % stamp(parse_now(p["close_by"])),
            "Then say it is in, with the fill price: kucoin.py placed --fill PRICE."]


def cmd_plan(a):
    cfg = read_config(a.config)
    f = _fetcher(a)
    now = parse_now(a.now)
    entry = read_json(a.from_path)
    if a.entry is not None:
        if not a.side or not a.symbol:
            raise KuError("--entry needs --symbol and --side long|short")
        entry = {"symbol": symbol_of(a.symbol), "side": a.side, "price": a.entry, "status": "triggered",
                 "range": (entry or {}).get("range") if entry and entry.get("symbol") == symbol_of(a.symbol) else None}
    if not entry:
        raise KuError("no entry at %s: run kucoin.py check SYMBOL --write first" % a.from_path)
    if entry.get("status") != "triggered" and a.entry is None:
        raise KuError("%s is %s, not triggered: plan once it triggers, or give --entry" % (entry.get("symbol"), entry.get("status")))
    c = contract(f, entry["symbol"])
    p = build_plan(c, entry, cfg, now, equity_of(a, cfg), read_json(a.log, {}), a.stop, a.target)
    if a.write:
        write_json(a.write, p)
    if a.json:
        print(json.dumps(p, indent=2))
    else:
        print("\n".join(ticket(p)))
        print("\n".join(app_steps(p)))
        print("Wrote %s." % a.write if a.write else "Not saved: add --write to keep it.")
    return 0


def cmd_placed(a):
    """The user placed the ticket by hand: put it in the log so the daily limits count it."""
    p = read_json(a.plan)
    if not p:
        raise KuError("no plan at %s: run kucoin.py plan --write" % a.plan)
    now = parse_now(a.now)
    log = read_json(a.log, {"trades": []}) or {"trades": []}
    if any(x.get("id") == p["id"] for x in log["trades"]):
        raise KuError("ticket %s is already in the log" % p["id"])
    fill = float(a.fill) if a.fill is not None else p["entry"]
    log["trades"].append({"id": p["id"], "date": now.strftime("%Y-%m-%d"), "symbol": p["symbol"], "side": p["side"],
                          "lots": p["lots"], "multiplier": p["multiplier"], "max_loss": p["max_loss"],
                          "close_by": (now + datetime.timedelta(minutes=int(read_config(a.config)["hold_minutes"])))
                          .strftime("%Y-%m-%dT%H:%M:%SZ"), "sent_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "entry": fill, "planned_entry": p["entry"], "stop": p["stop"], "target": p["target"],
                          "r_multiple": p["r_multiple"], "manual": True})
    write_json(a.log, log)
    rec = log["trades"][-1]
    print("Logged %s %s %s at %s. Time stop at %s." % (p["id"], p["side"], p["symbol"], fmt(fill, p["tick"]),
                                                       stamp(parse_now(rec["close_by"]))))
    return 0


def cmd_result(a):
    """How a hand-placed trade ended: the exit price, and the P/L if the app shows it."""
    now = parse_now(a.now)
    log = read_json(a.log, {"trades": []}) or {"trades": []}
    sym = symbol_of(a.ticket) if not a.ticket.startswith("dts-") else None
    open_ = [x for x in log["trades"] if not x.get("result") and (x.get("id") == a.ticket or x.get("symbol") == sym)]
    if not open_:
        raise KuError("no open ticket %s in %s" % (a.ticket, a.log))
    rec = open_[-1]
    sign = 1 if rec["side"] == "long" else -1
    units = rec["lots"] * float(rec.get("multiplier") or 0)
    fee = float(a.fee_rate) * units * (rec["entry"] + a.exit)
    pnl = float(a.pnl) if a.pnl is not None else sign * (a.exit - rec["entry"]) * units - fee
    tol = abs(rec["entry"] - rec["stop"]) * 0.25
    why = a.why or ("stop-loss" if abs(a.exit - rec["stop"]) <= tol else
                    "target" if abs(a.exit - rec["target"]) <= tol else "time stop")
    rec["result"] = {"exit": a.exit, "pnl": r(pnl, 4), "fees": r(fee, 4) if a.pnl is None else None, "why": why,
                     "closed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    write_json(a.log, log)
    print("%s %s closed at %s (%s): %+.2f USDT%s." % (rec["symbol"], rec["side"], a.exit, why, pnl,
                                                      ", %+.2fR" % (pnl / rec["max_loss"]) if rec.get("max_loss") else ""))
    return 0


def ready_to_send(p, brief, now, cfg):
    """Every reason an order may not go out, or [] when it may."""
    why = []
    day = now.strftime("%Y-%m-%d")
    if not brief or brief.get("date") != day:
        why.append("no brief from today: run kucoin.py brief")
    elif not brief.get("news_checked_at"):
        why.append("the news check is not recorded: kucoin.py news --summary \"...\"")
    elif p["base"] in (brief.get("avoid") or []):
        why.append("%s is on today's avoid list (%s)" % (p["base"], brief.get("news_summary") or "brief"))
    elif brief.get("status") != "open":
        why.append("KuCoin Futures was %s at the brief" % brief.get("status"))
    age = now - parse_now(p["created_at"])
    if age > datetime.timedelta(minutes=int(cfg["plan_max_age_minutes"])):
        why.append("the plan is %d minutes old, over %d: the price has moved, plan again" % (
            age.total_seconds() // 60, int(cfg["plan_max_age_minutes"])))
    if p.get("leverage") != 1:
        why.append("the plan is not 1x")
    return why


def cmd_order(a):
    cfg = read_config(a.config)
    p = read_json(a.plan)
    if not p:
        raise KuError("no plan at %s: run kucoin.py plan --write" % a.plan)
    now = parse_now(a.now)
    if not a.yes or not re.search(r"\b(yes|ja|j|y|ok|go)\b", a.yes.strip().lower()):
        raise KuError("--yes wants the user's own words for this ticket (like \"ja\"); nothing was sent")
    log = read_json(a.log, {"trades": []}) or {"trades": []}
    if any(x.get("id") == p["id"] for x in log["trades"]):
        raise KuError("ticket %s was already sent" % p["id"])
    why = ready_to_send(p, read_json(a.brief), now, cfg)
    if why:
        raise KuError("not sent: " + "; ".join(why))
    print("\n".join(ticket(p)))
    if a.dry_run:
        for name in ("entry", "stop_loss", "take_profit"):
            print("DRY RUN  POST /api/v1/orders %s" % json.dumps(p["orders"][name], separators=(",", ":")))
        print("Dry run: nothing was sent.")
        return 0
    rec = {"id": p["id"], "date": p["date"], "symbol": p["symbol"], "side": p["side"], "lots": p["lots"],
           "max_loss": p["max_loss"], "close_by": p["close_by"], "yes": a.yes, "sent_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "entry": p["entry"], "stop": p["stop"], "target": p["target"], "r_multiple": p["r_multiple"]}
    rec["entry_order"] = (private("POST", "/api/v1/orders", p["orders"]["entry"]) or {}).get("orderId")
    print("Entry sent: order %s." % rec["entry_order"])
    log["trades"].append(rec)
    write_json(a.log, log)
    try:
        rec["stop_order"] = (private("POST", "/api/v1/orders", p["orders"]["stop_loss"]) or {}).get("orderId")
        print("Stop-loss %s placed: order %s." % (p["orders"]["stop_loss"]["stopPrice"], rec["stop_order"]))
    except KuError as e:
        print("The stop-loss was REFUSED (%s): closing the position now." % e)
        rec["closed"] = close_position(p["symbol"])
        write_json(a.log, log)
        return 1
    try:
        rec["target_order"] = (private("POST", "/api/v1/orders", p["orders"]["take_profit"]) or {}).get("orderId")
        print("Take-profit %s placed: order %s." % (p["orders"]["take_profit"]["stopPrice"], rec["target_order"]))
    except KuError as e:
        print("The take-profit was refused (%s): the stop-loss stands; close by hand or at the time stop." % e)
    write_json(a.log, log)
    print("Time stop: kucoin.py close %s --plan at %s." % (p["base"], stamp(parse_now(p["close_by"]))))
    return 0


def close_position(sym):
    """Close at market if a position is open, and always cancel the symbol's leftover
    stop orders: a take-profit left behind after the stop-loss filled would close the
    next position on that contract."""
    pos = private("GET", "/api/v1/position?symbol=%s" % sym) or {}
    oid = None
    if int(pos.get("currentQty") or 0) != 0:
        doc = {"clientOid": "dts-" + uuid.uuid4().hex[:16], "symbol": sym, "type": "market", "closeOrder": True,
               "marginMode": "ISOLATED"}
        oid = (private("POST", "/api/v1/orders", doc) or {}).get("orderId")
    try:
        private("DELETE", "/api/v1/stopOrders?symbol=%s" % sym)
    except KuError as e:
        print("Could not cancel the stop orders (%s): cancel them in the app." % e)
    return oid


def cmd_close(a):
    sym = symbol_of(a.symbol)
    if a.plan:
        log = read_json(a.log, {"trades": []}) or {"trades": []}
        mine = [x for x in log["trades"] if x.get("symbol") == sym and not x.get("closed")]
        if not mine:
            raise KuError("no approved, open ticket for %s in %s: closing needs the user's yes (--yes)" % (sym, a.log))
        rec = mine[-1]
        if a.wait:
            due = parse_now(rec["close_by"])
            print("Time stop armed: closing %s at %s." % (sym, stamp(due)), flush=True)
            while utc_now() < due:
                time.sleep(min(30, max(1, (due - utc_now()).total_seconds())))
    elif not a.yes:
        raise KuError("closing needs --plan (the time stop of an approved ticket) or the user's --yes")
    else:
        rec = None
    oid = close_position(sym)
    if oid:
        print("Closed %s at market: order %s. Leftover stop orders cancelled." % (sym, oid))
    else:
        print("%s had no open position (the stop-loss or take-profit already closed it). "
              "Leftover stop orders cancelled." % sym)
        oid = "flat"
    if rec is not None:
        rec["closed"] = oid
        write_json(a.log, log)
    return 0


# ---------------------------------------------------------------- the weekly report

def closed_positions(fetcher, start, end):
    """KuCoin's closed positions in [start, end), newest first, all pages."""
    out, page = [], 1
    while True:
        if fetcher.offline:
            doc = fetcher.get(None, "history-positions.json")
        else:
            doc = {"code": "200000", "data": private("GET", "/api/v1/history-positions?from=%d&to=%d&limit=200&pageId=%d"
                                                     % (ms_of(start), ms_of(end), page))}
        data = payload(doc, "closed positions") or {}
        for x in data.get("items") or []:
            t = utc_of_ms(x.get("closeTime") or 0)
            if start <= t < end:
                out.append(x)
        if fetcher.offline or page >= int(data.get("totalPage") or 1):
            return out
        page += 1


def match(x, journal):
    """The journal ticket for a closed position: same symbol, opened within 3 minutes of the send."""
    opened = utc_of_ms(x.get("openTime") or 0)
    for rec in journal:
        if rec.get("symbol") == x.get("symbol") and rec.get("sent_at") and \
                abs((parse_now(rec["sent_at"]) - opened).total_seconds()) <= 180:
            return rec
    return None


def trade_rows(items, journal, hold_minutes=30):
    rows = []
    for x in items:
        kind = str(x.get("type") or "").upper()
        side = "short" if "SHORT" in kind else "long"
        opened, closed = utc_of_ms(x.get("openTime") or 0), utc_of_ms(x.get("closeTime") or 0)
        pnl = float(x.get("pnl") or 0)
        rec = match(x, journal)
        out = float(x.get("closePrice") or 0)
        why = "liquidated" if "LIQUID" in kind or "ADL" in kind else "closed"
        if rec:
            tol = abs(rec["entry"] - rec["stop"]) * 0.25
            if abs(out - rec["stop"]) <= tol:
                why = "stop-loss"
            elif abs(out - rec["target"]) <= tol:
                why = "target"
            elif (closed - opened) >= datetime.timedelta(minutes=hold_minutes - 1):
                why = "time stop"
        rows.append({"symbol": x.get("symbol"), "side": side, "opened": opened.strftime("%Y-%m-%d %H:%M"),
                     "closed": closed.strftime("%Y-%m-%d %H:%M"), "minutes": int((closed - opened).total_seconds() // 60),
                     "open_price": float(x.get("openPrice") or 0), "close_price": out, "pnl": pnl,
                     "fees": float(x.get("tradeFee") or 0), "funding": float(x.get("fundingFee") or 0),
                     "max_loss": rec.get("max_loss") if rec else None,
                     "r": r(pnl / rec["max_loss"]) if rec and rec.get("max_loss") else None,
                     "exit": why, "ticket": rec.get("id") if rec else None})
    rows.sort(key=lambda t: t["closed"])
    return rows


def journal_rows(journal, start, end):
    """Trades with a recorded result, in the shape trade_rows gives."""
    rows = []
    for rec in journal:
        res = rec.get("result")
        if not res:
            continue
        closed, opened = parse_now(res["closed_at"]), parse_now(rec["sent_at"])
        if not (start <= closed < end):
            continue
        rows.append({"symbol": rec["symbol"], "side": rec["side"], "opened": opened.strftime("%Y-%m-%d %H:%M"),
                     "closed": closed.strftime("%Y-%m-%d %H:%M"), "minutes": int((closed - opened).total_seconds() // 60),
                     "open_price": rec["entry"], "close_price": res["exit"], "pnl": float(res["pnl"]),
                     "fees": float(res.get("fees") or 0), "funding": 0.0, "max_loss": rec.get("max_loss"),
                     "r": r(float(res["pnl"]) / rec["max_loss"]) if rec.get("max_loss") else None,
                     "exit": res.get("why") or "closed", "ticket": rec.get("id")})
    rows.sort(key=lambda t: t["closed"])
    return rows


def stats(rows):
    n = len(rows)
    wins = [t["pnl"] for t in rows if t["pnl"] > 0]
    losses = [t["pnl"] for t in rows if t["pnl"] <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in rows:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    rs = [t["r"] for t in rows if t["r"] is not None]
    return {"trades": n, "wins": len(wins), "losses": len(losses), "win_rate": r(100.0 * len(wins) / n) if n else None,
            "net": r(sum(t["pnl"] for t in rows)), "fees": r(sum(t["fees"] for t in rows)),
            "funding": r(sum(t["funding"] for t in rows)),
            "avg_win": r(sum(wins) / len(wins)) if wins else None, "avg_loss": r(sum(losses) / len(losses)) if losses else None,
            "profit_factor": r(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "expectancy": r(sum(t["pnl"] for t in rows) / n) if n else None,
            "avg_r": r(sum(rs) / len(rs)) if rs else None, "max_drawdown": r(dd),
            "best": max(rows, key=lambda t: t["pnl"]) if rows else None,
            "worst": min(rows, key=lambda t: t["pnl"]) if rows else None}


def groups(rows, key):
    out = {}
    for t in rows:
        out.setdefault(t[key], []).append(t)
    return {k: {"trades": len(v), "net": r(sum(t["pnl"] for t in v)),
                "win_rate": r(100.0 * sum(1 for t in v if t["pnl"] > 0) / len(v))} for k, v in sorted(out.items())}


def verdict(s, cfg):
    if not s["trades"]:
        return "No closed trades in this window: nothing to judge yet."
    head = "%d trades is too few to judge a strategy: luck dominates under about 30. " % s["trades"] if s["trades"] < 30 else ""
    if s["net"] > 0 and (s["profit_factor"] or 0) >= 1.2:
        body = "So far it made money after fees (profit factor %.2f). Keep the rules as they are and keep counting." % s["profit_factor"]
    elif s["net"] > 0:
        body = "Slightly in profit, but thin (profit factor %s): fees and one bad trade can flip it." % (s["profit_factor"] or "-")
    else:
        body = "It lost money in this window. Check the exits below before changing anything: one rule at a time."
    if s["avg_loss"] is not None and s["avg_loss"] < -1.25 * float(cfg["max_loss_per_trade_usdt"]):
        body += " The average loss is over the planned max loss: stops are filling past their price."
    return head + body


def cmd_report(a):
    cfg = read_config(a.config)
    end = parse_now(a.now)
    start = end - datetime.timedelta(days=a.days)
    journal = (read_json(a.log, {"trades": []}) or {}).get("trades", [])
    source = a.source
    if source == "auto":
        source = "kucoin" if (a.offline or os.environ.get("KUCOIN_API_KEY")) else "journal"
    if source == "kucoin":
        try:
            rows = trade_rows(closed_positions(_fetcher(a), start, end), journal, int(cfg["hold_minutes"]))
        except KuError as e:
            if a.source == "kucoin":
                raise
            print("KuCoin did not give the closed positions (%s): using the journal." % str(e)[:120], file=sys.stderr)
            source = "journal"
    if source == "journal":
        rows = journal_rows(journal, start, end)
    s = stats(rows)
    doc = {"created_by": "trader-kucoin", "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "stats": s, "by_symbol": groups(rows, "symbol"), "by_side": groups(rows, "side"),
           "by_exit": groups(rows, "exit"), "trades": rows, "verdict": verdict(s, cfg)}
    if a.write:
        write_json(a.write, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("KuCoin Futures report %s to %s UTC (%d days), from %s." % (
        start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"), a.days,
        "KuCoin's closed positions" if source == "kucoin" else "the journal (results as recorded)"))
    if not rows:
        print(doc["verdict"])
        return 0
    print("Trades %d: %d won, %d lost (%.0f %%). Net %+.2f USDT (fees %.2f, funding %+.2f)." % (
        s["trades"], s["wins"], s["losses"], s["win_rate"], s["net"], s["fees"], s["funding"]))
    print("Average win %s, average loss %s, profit factor %s, per trade %+.2f USDT%s. Deepest drawdown %.2f USDT." % (
        "%+.2f" % s["avg_win"] if s["avg_win"] is not None else "-", "%+.2f" % s["avg_loss"] if s["avg_loss"] is not None else "-",
        s["profit_factor"] if s["profit_factor"] is not None else "-", s["expectancy"],
        (", %+.2fR" % s["avg_r"]) if s["avg_r"] is not None else "", s["max_drawdown"]))
    for title, g in (("By exit", doc["by_exit"]), ("By side", doc["by_side"]), ("By contract", doc["by_symbol"])):
        print("%s: %s." % (title, "; ".join("%s %d trades %+.2f (%.0f %% won)" % (k, v["trades"], v["net"], v["win_rate"])
                                            for k, v in g.items())))
    print("  %-16s %-12s %-5s %6s %10s %10s %8s  %s" % ("CLOSED UTC", "CONTRACT", "SIDE", "MIN", "OPEN", "CLOSE", "P/L", "EXIT"))
    for t in rows:
        print("  %-16s %-12s %-5s %6d %10s %10s %+8.2f  %s%s" % (
            t["closed"], t["symbol"], t["side"], t["minutes"], t["open_price"], t["close_price"], t["pnl"], t["exit"],
            (" (%+.2fR)" % t["r"]) if t["r"] is not None else ""))
    print(doc["verdict"])
    print("Past results do not predict the next week. Not financial advice.")
    return 0


def cmd_account(a):
    acct = private("GET", "/api/v1/account-overview?currency=USDT") or {}
    print("Equity %.2f USDT, available %.2f USDT, unrealised P/L %.2f USDT." % (
        float(acct.get("accountEquity") or 0), float(acct.get("availableBalance") or 0),
        float(acct.get("unrealisedPNL") or 0)))
    pos = [x for x in (private("GET", "/api/v1/positions") or []) if x.get("isOpen")]
    for x in pos:
        print("  %s  %+d contracts  entry %s  mark %s  P/L %s USDT  liquidation %s" % (
            x.get("symbol"), int(x.get("currentQty") or 0), x.get("avgEntryPrice"), x.get("markPrice"),
            x.get("unrealisedPnl"), x.get("liquidationPrice")))
    if not pos:
        print("  No open positions.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="KuCoin Futures day trader (trader-kucoin), 1x only.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--now", help='pretend it is this UTC time, e.g. "2026-10-29 14:45"')
        p.add_argument("--offline", metavar="DIR", help="saved KuCoin answers instead of the API")
        p.add_argument("--config", default="trader/kucoin.json")

    p = sub.add_parser("brief", help="status, announcements, the most tradeable contracts today")
    common(p)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--out", default="trader/kucoin-brief.json")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("news", help="record today's news check in the brief")
    common(p)
    p.add_argument("--summary", required=True)
    p.add_argument("--avoid", help="symbols to leave alone today, comma separated")
    p.add_argument("--brief", default="trader/kucoin-brief.json")
    for name in ("levels", "check", "watch"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
        common(p)
        if name != "levels":
            p.add_argument("--side", choices=["both", "long", "short"], default="both")
        if name == "check":
            p.add_argument("--write", nargs="?", const="trader/kucoin-entry.json", metavar="PATH")
            p.add_argument("--json", action="store_true")
        if name == "watch":
            p.add_argument("--minutes", type=int, default=60)
            p.add_argument("--out", default="trader/kucoin-entry.json")
    p = sub.add_parser("plan", help="stop, target, size and the close-by time")
    common(p)
    p.add_argument("--from", dest="from_path", default="trader/kucoin-entry.json")
    p.add_argument("--symbol")
    p.add_argument("--side", choices=["long", "short"])
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", default="mid", help="mid (default), edge, price:X, pct:X")
    p.add_argument("--target", help="1.5R (default from config), 2R, price:X")
    p.add_argument("--equity", type=float, help="account equity in USDT (else read from KuCoin)")
    p.add_argument("--log", default="trader/kucoin-log.json")
    p.add_argument("--write", nargs="?", const="trader/kucoin-plan.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("order", help="send the plan: entry, stop-loss, take-profit")
    common(p)
    p.add_argument("--plan", default="trader/kucoin-plan.json")
    p.add_argument("--brief", default="trader/kucoin-brief.json")
    p.add_argument("--log", default="trader/kucoin-log.json")
    p.add_argument("--yes", help="the user's own words approving this ticket")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("close", help="close a position at market, cancel its stop orders")
    p.add_argument("symbol")
    p.add_argument("--plan", action="store_true", help="the time stop of an approved ticket")
    p.add_argument("--wait", action="store_true", help="with --plan: wait until its close-by time first")
    p.add_argument("--yes")
    p.add_argument("--log", default="trader/kucoin-log.json")
    p = sub.add_parser("placed", help="log a ticket the user placed by hand in the app")
    common(p)
    p.add_argument("--plan", default="trader/kucoin-plan.json")
    p.add_argument("--log", default="trader/kucoin-log.json")
    p.add_argument("--fill", type=float, help="the fill price the app shows (default: the plan's entry)")
    p = sub.add_parser("result", help="record how a hand-placed trade ended")
    p.add_argument("ticket", help="the ticket id (dts-...) or the symbol")
    p.add_argument("--exit", type=float, required=True, help="the exit price")
    p.add_argument("--pnl", type=float, help="the realised P/L in USDT the app shows (else computed)")
    p.add_argument("--why", choices=["stop-loss", "target", "time stop", "by hand"])
    p.add_argument("--fee-rate", type=float, default=0.0006, help="taker fee for the computed P/L")
    p.add_argument("--log", default="trader/kucoin-log.json")
    p.add_argument("--now")
    p = sub.add_parser("report", help="the last N days of closed trades: P/L, win rate, what worked")
    common(p)
    p.add_argument("--source", choices=["auto", "kucoin", "journal"], default="auto")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--log", default="trader/kucoin-log.json")
    p.add_argument("--write", nargs="?", const="trader/kucoin-report.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("account", help="equity and open positions")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"brief": cmd_brief, "news": cmd_news, "levels": cmd_levels, "check": cmd_check, "watch": cmd_watch,
                "plan": cmd_plan, "order": cmd_order, "close": cmd_close, "report": cmd_report,
                "placed": cmd_placed, "result": cmd_result,
                "account": cmd_account}[a.cmd](a)
    except KuError as e:
        print("kucoin.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("kucoin.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
