# FVGZone

`MainStoinker/Algos/FVGZone/FVGZone_Algo.py` (`class Algo(ParentAlgo)`), issue 277.
It trades retests of fair value gap zones found by the reusable detector
`MainStoinker/indicators/fvg.py` (`docs/indicators.md`), with a trend filter from a
second indicator. It is the reference for how an algo consumes the detector.

Honest context: the offline research of 2026-11-25 found no edge in gap retests on
AAPL, MSFT and TSLA. This is a clean, configurable, correctly simulated implementation
to build on, not a claim of profitability. The existing `FVG` algo is untouched.

No registration is needed: both loaders (`engine/algos.py` and the legacy
`main_utils.algo_starter`) import `MainStoinker.Algos.<ID>.<ID>_Algo` by name.

## Config

```json
{"ID": "FVGZone", "data": [{"idname": "AAPL-fvgzone", "ticker": "AAPL"}]}
```

`"mode": "paper"` is accepted like for every algo. Every other key is a parameter
below; an unknown key, a wrong type or an out-of-range value raises `ConfigError`
(a `ValueError`) at construction naming the algo and the parameter. Numbers may be
given as JSON numbers or numeric strings (like the other algos).

| parameter | default | meaning |
|-----------|---------|---------|
| `zone_minutes` | 5 | minutes per zone bar (1 to 390); 1 feeds the 1-minute bars to the detector directly |
| `trend_filter` | true | `false` switches the trend filter off |
| `trend_ema` | 50 | EMA length over 1-minute closes (also the number of bars before it counts as warm) |
| `RRRatio` | 2 | target distance as a multiple of the risk |
| `stop_buffer` | 0.02 | stop distance beyond the far edge of the zone (whole cents) |
| `min_risk` | 0.10 | skip if the risk per share is below this (whole cents) |
| `max_risk_atr` | 3 | skip if the risk per share is above this multiple of the zone timeframe's ATR |
| `risk_dollars` | 50 | money risked per trade |
| `max_shares` | 500 | cap on the size |
| `first_entry` | "09:45" | no entry on a bar labelled before this |
| `last_entry_before_close` | 30 | minutes before the day's close: no entry on a bar labelled after it (15:30 on a full day, 12:30 on a half day) |
| `flat_before_close` | 5 | minutes before the day's close: an open position is closed on the first bar labelled at or after it (15:55 / 12:55) |
| `last_entry` | unset | optional absolute "HH:MM" instead of `last_entry_before_close` (see "End-of-day times") |
| `flat_time` | unset | optional absolute "HH:MM" instead of `flat_before_close` |
| `min_size_atr` | 0.25 | the detector's `min_size_atr`; `null` switches it off |
| `htf_minutes` | 0 | minutes per higher-timeframe trend bar: 0 switches the filter off, otherwise 2 to 390 |
| `htf_ema` | 20 | EMA length over the completed higher-timeframe closes (1 or more) |
| `vol_slot_mult` | 0 | volume-by-slot filter: 0 is off; above 0 the zone's middle candle needs a volume of at least this multiple of the slot mean (0 or more) |
| `vol_slot_sessions` | 20 | sessions in the slot mean (1 or more) |
| `trend_day_adr` | 0 | trend-day filter: 0 is off; above 0 an entry needs the move from the session open, in the side's direction, to be at least this multiple of the ADR (0 or more) |
| `adr_sessions` | 20 | sessions in the ADR (1 or more) |

Times are bar labels (the bar START, naive New York time, as in the data). Required, for a
full day: `09:30 <= first_entry <= last_entry < flat_time`.

**End-of-day times.** The close of each day comes from the market calendar
(`MainStoinker/engine/market_calendar.py`: 16:00, or 13:00 on an early-close day; holidays and
weekends have no session). The defaults are relative to it, so a full day ends flat at 15:55 with
the last entry at 15:30 exactly as before, and a half day ends flat at 12:55 with the last entry
at 12:30. An absolute `last_entry` / `flat_time` replaces its relative key. It is used exactly as
given on a full day. On a day that closes early it is also held to the close-relative limit
(the minimum of the two), and the last entry to one minute before the flat time, so a position can
never outlive an early close: an absolute 15:55 on a half day behaves as 12:55, an absolute
12:40 stays 12:40. The algo takes each day's times when the day's first bar arrives; bars on a day
the calendar calls closed (data on a holiday) use the full-day times. `last_entry_before_close`
must be greater than `flat_before_close` when both times are relative (1 to 390 each; with an
absolute `last_entry` or `flat_time` the order is checked on the full-day times only). The detector runs with
`FvgConfig(bar_minutes=zone_minutes, min_size_atr=min_size_atr)`: textbook rule,
`min_size` 0.05 exclusive, invalidation on a close beyond the far edge, zones expire
at the end of the session.

## Rules as built

**Zone bars.** The algo builds `zone_minutes` bars incrementally, aligned to 09:30
of each session, bucket label = bucket START. A bucket is fed to the detector when it
is complete (all its minutes present: on its last minute), or, with a missing minute,
when a later bucket of the same session begins. A session's last bucket that never
completes is dropped. This is `resample_session` exactly (a test compares the zones
with `detect_fvgs(resample_session(...))`). Detection runs on every bar, in a trade
or not.

**Usable zone.** A live zone (detector state open or touched) of the current session
whose `known_at` is not after the bar's label. A 5-minute zone formed by the 09:40
candle is known at 09:45 and cannot trigger on any earlier bar, including the 09:44
bar that completes it. A zone the detector reports filled, invalidated or expired is
gone. One trade per zone: a zone is used up when an entry order is sent for it.

**Far edge, watched every minute.** The detector learns of a fill or invalidation only
when the zone-timeframe bar completes, up to `zone_minutes - 1` minutes late. So the algo
itself watches each usable zone on every 1-minute bar: once a bar trades to or through the
FAR edge (up zone: `low <= bottom`; down zone: `high >= top`) the zone is dead for the
algo, for good, whatever the detector says later. The bar that itself reaches the far edge
does NOT enter either, even if it closes back above the near edge: its stop distance
(far edge +- buffer) was already traded through on that very bar. A bar that stops one
cent short of the far edge leaves the zone usable. Dead zones are not drawn in the zone
series. A zone only touched in the warm-up history is handled the same way.

**Trend filter** (when on): session VWAP (typical price (high+low+close)/3, volume
weighted, reset each session, exact integer arithmetic) and an EMA of 1-minute closes
(`alpha = 2/(trend_ema+1)`, seeded with the first close, continuous across sessions,
shown and used only after `trend_ema` bars). Long setups need close > VWAP and close >
EMA, short setups close < VWAP and close < EMA (both strict). While the EMA is not warm
or the session has no volume, the filter blocks every entry.

**Higher-timeframe trend filter** (when `htf_minutes` is not 0; off by default, and then the algo behaves
exactly as before: every golden file is unchanged). `MainStoinker.indicators.trend.TrendIndicator`
(see `docs/indicators.md`) is fed every 1-minute bar and builds its own `htf_minutes` buckets aligned to
09:30, independent of `zone_minutes`. A long entry needs its direction to be `up`, a short entry `down`, as
known at the entry bar's close: only complete buckets count (one that this very bar completes does,
a bucket still in progress does not). `none` (including a trend that is not ready yet) blocks both sides. It is checked IN ADDITION to
`trend_filter`; the two are independent. A retest blocked by the trend is handled like one blocked by
`trend_filter`: nothing is sent, the zone is NOT used up and may be retested later, and nothing is put in
`algo.skipped` (that list is for risk skips). The EMA runs on from session to session.

**Volume-by-slot filter** (`vol_slot_mult` above 0; off by default, and then the algo behaves exactly as
before: every golden file is unchanged). `MainStoinker.indicators.session.SlotVolume` (see
`docs/indicators.md`) builds `zone_minutes` buckets aligned to 09:30 and keeps, for each time slot, the volumes
of the last `vol_slot_sessions` sessions that HAVE that slot (a half day has no afternoon slots, so it is
skipped for them). A zone may be traded only if the volume of its MIDDLE candle is at least `vol_slot_mult`
times the mean of its slot: `volume * sessions >= mult * sum of the slot's history`, in exact arithmetic
(the multiple read as the decimal it is written as). A session joins the history only when the next session
starts, so today is never in its own mean. Not available (fewer than `vol_slot_sessions` earlier sessions have
the slot) blocks.

**Trend-day filter** (`trend_day_adr` above 0; off by default, same guarantee).
`MainStoinker.indicators.session.DailyRange` gives the session open (the open of the 09:30 bar) and the ADR,
the mean of high minus low over the previous `adr_sessions` sessions (a session joins when the next starts).
With D = the entry bar's close minus the session open, an entry needs D, signed for the side (a long needs D,
a short needs -D), to be at least `trend_day_adr` times the ADR, exactly as above. Blocks while fewer than
`adr_sessions` sessions have completed, and on a day whose first bar is later than 09:30 (no session open).
Both filters are independent of each other (when both are on, both must pass), of `trend_filter` and of
`htf_minutes`. A retest blocked by either is handled like one blocked by `trend_filter`: nothing is sent, the
zone is NOT used up and may be retested later, and nothing is put in `algo.skipped`. They reproduce the
research variants V2b and T4b (`vol_slot_mult` 1.5 and `trend_day_adr` 0.3 are the values studied).

**Entry, long** (short is the exact mirror, all in integer cents): a usable up zone is
retested by a 1-minute bar with `low <= zone top` that CLOSES above the zone top. The
newest qualifying zone wins when several qualify on a bar; if its setup is then skipped
for risk (below), older qualifying zones on that bar are NOT tried. One position at a time, and
no new entry on the bar on which a trade ended. Entry is at market; the entry
reference is the signal bar's close. The engine fills by its own rules (`SimBroker`:
at that close; `PaperBroker`: at the next bar's open).

**Stop and target.** Stop = zone bottom - `stop_buffer` (short: zone top + buffer),
fixed. R = reference - stop. The setup is skipped (nothing is sent, the zone is NOT
used up and may be retested later with another R) if `R < min_risk`, or if
`R > max_risk_atr * ATR`, or if fewer than 5 zone bars exist yet for the ATR. The ATR
is the mean true range of the last 20 completed zone bars (previous close taken across
sessions), the same window as the detector's. Skips are listed in `algo.skipped`.
Target = reference + `RRRatio` * R, the distance rounded half up to the cent.
Size = `floor(risk_dollars / R)`, at least 1, at most `max_shares`.

**Exits.** The stop and target are given to the broker (`Trade` with a fixed stop and a
take-profit, as the other algos do); under `SimBroker` the algo's own per-bar check
(`Trade.check_stoploss` on the close, then `check_tp` on the high/low) closes the
position at the stop or target price, under `PaperBroker` the broker fills them
against the bar's range. At `flat_time` the algo closes the position at the bar's close
(`PaperBroker`: fills at the next open).

**Entry refused or cancelled.** The engine's gap rule refuses an entry on a bar that
follows a missing minute, and `PaperBroker` cancels a pending entry whose fill bar
follows a gap. The algo sees it as the broker no longer holding the position on its next
bar and returns to flat. Decision: the zone stays USED UP (it counts attempts, not
fills; no dependence on broker internals). Tested through the real engine for both.

**Indicator series** for the dashboard (`FrontEndDataStruct` / `FrontEndDataType`):
`VWAP`, `EMA` (line), `ZoneAboveTop`, `ZoneAboveBottom`, `ZoneBelowTop`,
`ZoneBelowBottom` (segment: the usable live zone nearest above and nearest below the
bar's close, ties to the newest) and, while in a trade, `StopPrice` and `tp`
(segment). With `htf_minutes` on there is one more line, `HTFEMA` (the trend EMA, drawn from the bar on which
the trend is ready, holding its value between buckets). The module's `SERIES` stays the fixed base list;
`HTFEMA` is added per instance, so an algo without the filter has exactly the columns it always had. The zone edges are taken from the detector's live zones with the same
nearest rule as `nearest_zone` (not through `nearest_zone` itself, so that zones not yet
known or left from an earlier session are not drawn). `AlgoData` is a property built
lazily from per-column lists; during a run no frame is built (`update_frontend` is
overridden to read the last row), so there is no per-bar cost that grows with history.

## Warm-up and history

On the first `update` the algo processes every earlier row of the price frame (the
engine's warm-up bars) to warm the EMA, the ATR and the detector, with trading
disabled; the same happens if rows were skipped. One row of `AlgoData` exists per bar,
as the series store expects. The trend filter's indicator is fed in the same pass, so a warm-up frame makes it ready: it needs
`htf_ema` completed `htf_minutes` buckets, that is `htf_ema * htf_minutes` minutes of regular-session
bars (600 for the defaults 30 and 20, about 1.5 sessions; only complete sessions, and the EMA also carries
over days, so a longer history gives a steadier EMA). Until then it blocks every entry. `run` supplies `--warmup-days`
days of month-file bars (default 2, about 780 bars, enough for the defaults; see `docs/live-data.md`); for a
longer trend ask for more days, or the algo does not trade until the live session has filled the gap.

The two session filters need history too: `vol_slot_sessions` earlier sessions that have the zone's slot and
`adr_sessions` completed earlier sessions (20 each by default), and until then they block every entry. The
history comes from the same frame: the sessions of the warm-up bars count, and the last one joins when the next
session's first bar arrives. `run` loads only 2 warm-up days by default, so with these filters on (defaults)
the algo does not trade in a live session; start it with `--warmup-days 25` (20 sessions plus a margin for a
half day that lacks a slot) and month files (`fetch-bars`) that cover that many trading days before today.
The trend-day filter also needs the 09:30 bar of the session itself. A live run started after 09:30 still has
it, because the warm-up frame holds today's closed bars since the open; the filter blocks for the day only when
that 09:30 bar is missing from the data.

A zone retested only during that history is not marked
used (the algo cannot know whether another process traded it).

### How much history: `warmup_sessions()`

Every algo can say how many whole trading sessions of 1-minute bars it needs before its first
tradable bar (`ParentAlgo.warmup_sessions()`, default 0). FVGZone works it out from its settings
as the largest of the parts that are on (a session is 390 bars, every quotient rounds up):

| part | on when | sessions |
|------|---------|----------|
| ATR | always | `ceil(atr_bars * zone_minutes / 390)` (`atr_bars` is the detector's 20) |
| EMA of the 1-minute closes | `trend_filter` | `ceil(trend_ema / 390)` |
| higher-timeframe EMA | `htf_minutes` > 0 | `ceil(htf_ema / (390 // htf_minutes))` (whole buckets per session) |
| volume by slot | `vol_slot_mult` > 0 | `vol_slot_sessions + 1` (one session of margin: a half day has no afternoon slot) |
| trend-day filter | `trend_day_adr` > 0 | `adr_sessions` |

So the defaults need 1 session, `htf_minutes` 30 needs 2 (20 buckets, 13 per session), and
`vol_slot_mult` 1.5 with `trend_day_adr` 0.3 needs 21 (20 + the margin). "Ready" here is what the
algo itself checks: bar and session counts, not numerical convergence. An EMA never fully forgets
its seed: the 50-bar EMA of 1-minute closes is down to about 1e-7 of its seed after one session, but
a 20-bucket higher-timeframe EMA (alpha 2/21) is far from converged after the 2 declared sessions
(a seed weight of 0.905 per bucket: still a noticeable part of it), so such
a filter's early values still depend a little on where the history began. Sessions are counted at 390
bars; a half day has 210, and two half days inside a 21-session window can leave an afternoon slot
one session short, in which case the filter simply blocks (never trades wrongly) until it fills.

`backtest --start DATE [--end DATE]` uses it: the largest need of the run's algos is counted back
from `DATE` by the market calendar and those sessions are given to the algos as warm-up, through the
same engine seeding a live session's warm-up frames use (no order, trade, equity or statistic comes
from them). For FVGZone with the defaults on data that has a session before `DATE`, the trades of
`--start DATE` equal those of a run over all the data from `DATE` on, trade for trade (the algo is flat
every day at 15:55, and its zones, VWAP and used-up marks are per session). If the bars do not reach back
far enough the command refuses and names the first day needed and the first day available;
`--allow-cold` starts anyway, says so, and records it in the stored run.

## Limits

- **Data that ends early.** A half day (13:00 close) is known from the market calendar and
  ends flat at 12:55 (see "End-of-day times"), so it no longer leaves a position open; only
  an early close the calendar does not know (add it to its override table), a feed that
  stops, or an absolute time set later than the data ends can. What happens to a
  position still open when its session's data stops depends on where the algo runs
  (the cases below named "a half day" are such a day without a calendar entry):
  - *Backtest, `SimBroker` (legacy fills).* The first bar of the next session closes the
    position at that bar's close (an overnight carry in the data), after
    `check_stoploss` has had its say (a close below the stop on that bar exits at the
    stop price).
  - *Backtest, `PaperBroker` (realistic fills).* On the first bar of the next session the
    broker has already processed the bar: a stop beyond the open is filled at the open
    (stop fill, no ignored event, because the algo asks `check_stoploss` first and only
    closes a position the broker still holds). If the position is still held the algo
    asks to close it and it fills at the next bar's open; the data's last bar leaves
    it to the ledger's end-of-data close.
  - *The live `run` command.* Each session builds the algo fresh, so the next-day logic
    never runs there. As found by reading `MainStoinker/ib/live_run.py`: at the end of a
    run (end of session, `--minutes` elapsing before the flat time, a stop file)
    `engine.end_live()` is called, which does NOT close open positions; they are left open
    at the end of the run and listed in the output ("position(s) left OPEN at the end of
    the run (listed, never closed by the session)"). Nothing closes them afterwards.
    The algo itself ends flat at 12:55 on a half day (the calendar is checked in the algo, not in
    the session driver); set `flat_time` / `last_entry` for a short `--minutes` yourself.
- **Retest geometry.** A retest bar that closes above the zone top qualifies when its
  low stays above the far edge (see "Far edge"); the far-edge watch is per 1-minute bar,
  while the detector's own state (invalidation on a close beyond the far edge, fill) is
  still set by zone-timeframe bars.
- **Legacy fills (`SimBroker`).** Stops are checked on the close and fills happen at
  the signal close and at the exact stop or target price: optimistic. The realistic
  numbers are the `golden_paper` ones.
- **Cent prices.** The detector raises for a price that is not a multiple of 0.01, and
  so does the algo; the run then fails loudly instead of trading on rounded data.
- **Bars before 09:30** are ignored (no row values); bars from 16:00 on are processed.
- The VWAP is the session VWAP of the bars the algo has seen: a run started mid-session
  with a short warm-up has a VWAP over fewer bars.
- Legacy path: the algo also runs on the legacy runtime (`Ticker`), which the parity
  test checks against the same golden files.

## Tests

`tests/test_fvgzone_algo.py` (rules on hand-built bars), `tests/test_fvgzone_engine.py`
(engine, gap rule, batch-detector equality, series, per-bar cost),
`tests/fvgzone_support.py` (builders, mirror helper), `tests/test_fvgzone_htf.py` (trend filter),
`tests/test_fvgzone_session_filters.py` (volume-by-slot and trend-day filters). Goldens: cases `fvgzone_*` in
`tests/test_golden_backtests.py` (`tests/golden/`) and `tests/test_paper_goldens.py`
(`tests/golden_paper/`, with the per-bar agreement check of algo and broker).
