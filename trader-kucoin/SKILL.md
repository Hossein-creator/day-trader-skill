---
name: trader-kucoin
description: >-
  KuCoin Futures day trader. Trades of about 30 minutes, long or short, at 1x
  leverage only. Starts every trading day with a brief: KuCoin's service
  status, its announcements (delistings, maintenance, migrations), and the
  contracts most tradeable today for a 30-minute trade across crypto, stocks,
  metals and commodities (24-hour turnover, range, change, funding, flags),
  then a news check (the macro calendar and headlines) recorded before any
  order. Then a 15-minute range breakout on 1-minute bars with volume, trend,
  support/resistance and funding gates, a plan (stop, target, size by max
  loss, a 30-minute time stop) and the order through KuCoin's API: entry,
  stop-loss and take-profit, only after the user's yes for that ticket. Use
  when the user says "KuCoin", "futures", "crypto day trade", "what should I
  trade today", "check the news first", "BTC", "SOL", "short it", "place it
  on KuCoin", "close my KuCoin position", or names a KuCoin contract.
argument-hint: "[brief | check SYMBOL | plan | order | close SYMBOL]"
---

# trader-kucoin

Day trades on KuCoin Futures that last about 30 minutes, long or short, at 1x:
the position is worth what you put up, no more. Every day starts with the
brief and the news. Then one setup, one plan, and one yes per order. Most
people who day trade lose money, crypto and futures included. Nothing here
changes that, and nothing here knows in advance which trade will pay.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" brief                  # status, announcements, the most tradeable today
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" news --summary "..." --avoid HYPE,EVAA
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" levels SOL             # support and resistance, last 24 hours
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" check SOL --write      # triggered / late / blocked / waiting
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" watch SOL --minutes 60 # once a minute until it triggers
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" plan --write           # stop, target, size, close-by time
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" order --yes "ja" --dry-run
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" order --yes "ja"       # entry, stop-loss, take-profit
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" close SOL --plan --wait  # the time stop
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" report --days 7        # the week: P/L, win rate, what worked
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" account                # equity and open positions
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" placed --fill 102.31   # by hand: the user placed the ticket in the app
python3 "${CLAUDE_SKILL_DIR}/kucoin.py" result SOL --exit 103.40 --pnl 0.31   # by hand: how it ended
```

Symbols: `BTC`, `SOL`, `ETH-USDT` or `XBTUSDTM` all work (BTC is `XBT` on
KuCoin Futures). Stocks, metals and commodities too: `TSLA`, `MU`, `XAUT`
(gold), `XAG` (silver), `CL` (oil). Other flags: `--side long|short|both`,
`--now "2026-10-29 14:45"` (UTC) and `--offline DIR` to replay saved answers,
`--json`. Times print in UTC and Berlin.

## Setup, once

- **Keys, never in the chat.** KuCoin, API Management (best in a sub-account
  with a small balance): permissions General and Futures, never Withdrawal or
  Transfer; IP restriction Unrestricted, since a cloud session has no fixed
  address. Put them in the environment's settings as `KUCOIN_API_KEY`,
  `KUCOIN_API_SECRET` and `KUCOIN_API_PASSPHRASE`, then start a new session.
- **Network.** Allow `api-futures.kucoin.com` and `api.kucoin.com`.
- **Rules.** Copy `kucoin.example.json` to `trader/kucoin.json` and set the
  max loss per trade, the daily loss limit and the trades per day. `leverage`
  stays 1: any other value is refused.

## Every day, before the first trade

1. **The brief.** `kucoin.py brief`. It writes `trader/kucoin-brief.json`.
   Read the user every PROBLEM line, every announcement, and the top of the
   list with its flags. Say plainly that the list ranks liquidity and range
   for a 30-minute trade, not what will pay.
2. **The news.** Search the web for today's macro calendar with times (Fed
   and FOMC, CPI, PPI, jobs, jobless claims, ECB) and for headlines on the
   top names and on crypto as a whole (exchange outages, hacks, ETF flows,
   regulation). For stock contracts: earnings today or tonight. Tell the user
   what you found and what to avoid, with times in Berlin.
3. **Record it.** `kucoin.py news --summary "<one or two lines>" --avoid
   SYM,...`. No order goes out before this, and none in a name on the avoid
   list. Leave 15 minutes either side of a big release (CPI, FOMC, jobs) alone.

## The setup: 15-minute range breakout, about 30 minutes in the trade

- **Range.** The 15 closed 1-minute bars before the signal bar.
- **Long:** the signal bar CLOSES above the range high (above the top of a
  resistance zone if the range high sits in one). **Short:** it closes below
  the range low (below the bottom of a support zone the low sits in). A wick
  through is not a signal.
- **Gates**, all of them: volume at least 1.5x the range's average; a range
  0.15 % to 1.5 % wide; with the trend (long over the 5-minute EMA 50, short
  under it); room to the next zone of at least 1R; not in the 10 minutes
  before a funding time (KuCoin's own, usually every 8 hours); and after
  taker fees in and out (about 0.12 % of the price) the 2R target must still
  pay 1.2 times the loss. That last gate needs a stop about 0.33 % away, a
  range about 0.7 % wide: a tighter one is mostly fees.
- **Zones** come from the 24 hours of 5-minute bars before the range: swing
  highs and lows (30 minutes on each side), the 24-hour high and low, the UTC
  day open; within 0.2 % they merge. Where price turned before, not a forecast.
- **Statuses.** Triggered: the newest closed bar signalled and passed every
  gate. Late: it signalled one or two bars ago, do not chase it. Blocked: a
  breakout that failed a gate, with the reason. Waiting: no close outside the
  range yet, with how far each side is.

## The plan

`kucoin.py plan --write` reads `trader/kucoin-entry.json` and writes
`trader/kucoin-plan.json`:

- **Stop** at the range midpoint by default (`--stop edge` for the far side
  of the range, `price:X`, `pct:X`), rounded to the tick away from the entry.
- **Target** 2R by default (`--target 3R`, `price:X`). The ticket shows the
  R after fees too, and a plan under `min_net_r` (1.2) after fees is refused.
  It says when the target sits past the next zone.
- **Size** in whole contracts so the loss at the stop, taker fees in and out
  included, fits the smallest of: the max loss per trade, the % of equity,
  what is left of the daily loss limit. At 1x the position's value is its
  margin, and it is capped at 50 % of equity. Too far a stop is a refusal,
  not a smaller trade.
- **Time stop** 30 minutes after the entry: close at market, win or lose.
- **Daily limits**: the trades per day and the daily loss limit count every
  ticket sent today (`trader/kucoin-log.json`).

## The order

1. Show the ticket word for word (`plan` prints it): side, contract, size,
   stop-loss, take-profit, max loss, the close-by time.
2. Ask for a yes for THAT ticket. A yes covers its entry, its stop-loss, its
   take-profit and its time-stop close. Nothing else, and not the next one.
3. Only after the user's own yes: `kucoin.py order --yes "<their words>"`. It
   refuses without today's brief and news check, for a name on the avoid
   list, for a plan over 3 minutes old (plan again), and twice for one ticket.
   It sends the market entry (isolated, 1x), then the stop-loss and the
   take-profit as close-position stop orders on the last price. If KuCoin
   refuses the stop-loss, it closes the position at once.
4. Arm the time stop right away: run `kucoin.py close SYMBOL --plan --wait` in
   the background. At the close-by time it closes what is left and cancels
   the leftover stop order, even when the stop-loss or the take-profit already
   ended the trade (a leftover order would close the next position).
5. Report the order ids, the fill from `kucoin.py account`, and the result.

`--dry-run` prints the exact requests and sends nothing. Use it the first
time, and whenever something looks off.

## By hand: when KuCoin refuses the account API

KuCoin refuses every account call (balance, positions, orders) from regions
it does not serve, and a Claude cloud session runs in the US, so from there
the user places the order in the app. Never route around the block (a VPN or
a proxy breaks KuCoin's terms and risks the account). The public data still
works, so the brief, the news, the setup and the plan stay the same:

1. The balance for the 1 % rule: `"equity_usdt"` in `trader/kucoin.json`, or
   `plan --equity USDT`. Ask the user for the futures balance shown in the
   app, and update it when it changes. A plan without one is refused.
2. `plan --write` prints the ticket and the taps in the app: Futures, the
   pair, Isolated 1x, Market, the size, TP/SL on the last price, Buy/Long or
   Sell/Short, and an alarm for the time stop.
3. When the user says it is in: `placed --fill PRICE` logs it, so the daily
   limits count it, and prints the time stop. Remind the user at that time
   (a scheduled check-in, when the session has one) to close what is left.
4. When it is closed: `result SYMBOL --exit PRICE --pnl USDT` with the
   realised P/L from the app (without `--pnl` it is computed from the prices
   with the taker fee). The exit reason comes from the price, or `--why`.
5. `report` then reads the journal (`--source journal`; `auto` falls back to
   it when KuCoin refuses). Keep the log somewhere lasting: the user's private
   journal, never a public repository.

## The weekly report

`kucoin.py report --days 7` reads the closed positions straight from KuCoin
(`/api/v1/history-positions`), so the results survive any session. Each one
is matched to its ticket in `trader/kucoin-log.json` (same contract, opened
within 3 minutes of the send) for its planned max loss, its R and how it
ended: stop-loss, target, time stop, or liquidated. It prints the trades, the
win rate, the net after fees and funding, average win and loss, profit
factor, per-trade result in USDT and R, the deepest drawdown, and the split
by exit, side and contract, then one verdict. Under about 30 trades it says
the sample is too small to judge, and it means it: a week of results is
mostly luck. Change one rule at a time, never after one bad day.

The log lives in this session's `trader/` folder, which a cloud session does
not keep. Copy it somewhere lasting after each trade (the user's private
journal, never a public repository); without it the report still has every
KuCoin number, only not the planned R or the exit reason.

## Rules

- Never send an order without the user's explicit yes for that ticket, and
  never from a background watch: a watch only tells the user it triggered.
- Never raise leverage, never trade margin or options, never move a stop
  further away once the trade is open.
- Never read the keys out, print them, or ask for them in the chat.
- Closed bars only. KuCoin's candles are free and good, but they are bars,
  not every trade, and the API can lag or rate-limit. Say so when a minute
  matters.
- A stop is a plan, not a promise: in a fast market a stop order fills past
  its price, and the loss with it.
- No promises of winning trades. The brief ranks what is tradeable, not
  what will pay.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker and trader-kucoin. trader-broker only when
  all three hold: trader-risk passed that exact order today, the user typed an explicit yes for that one
  order in the chat, and the user clicked Allow once on Claude's own card because the order tool is set
  to "Needs approval" (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- trader-kucoin only when today's brief and news check are recorded, its daily limits hold, the plan is
  under 3 minutes old, and the user typed an explicit yes for that one ticket in the chat. That yes covers
  the ticket's entry, its stop-loss and target orders and its time-stop close, nothing else. It has no paper mode: it
  trades the KuCoin account its keys belong to, so use a sub-account with a small balance.
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
  trader-kucoin trades KuCoin futures at 1x only, long or short, never with more leverage.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
