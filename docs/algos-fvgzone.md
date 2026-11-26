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
| `last_entry` | "15:30" | no entry on a bar labelled after this |
| `flat_time` | "15:55" | an open position is closed on the first bar labelled at or after this |
| `min_size_atr` | 0.25 | the detector's `min_size_atr`; `null` switches it off |

Times are bar labels (the bar START, naive New York time, as in the data). Required:
`09:30 <= first_entry <= last_entry < flat_time`. The detector runs with
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
(segment). The zone edges are taken from the detector's live zones with the same
nearest rule as `nearest_zone` (not through `nearest_zone` itself, so that zones not yet
known or left from an earlier session are not drawn). `AlgoData` is a property built
lazily from per-column lists; during a run no frame is built (`update_frontend` is
overridden to read the last row), so there is no per-bar cost that grows with history.

## Warm-up and history

On the first `update` the algo processes every earlier row of the price frame (the
engine's warm-up bars) to warm the EMA, the ATR and the detector, with trading
disabled; the same happens if rows were skipped. One row of `AlgoData` exists per bar,
as the series store expects. A zone retested only during that history is not marked
used (the algo cannot know whether another process traded it).

## Limits

- **Half days and data that ends early.** The algo cannot know a session closes early
  (it does not know the close; `flat_time` is its only scheduled exit). What happens to a
  position still open when its session's data stops (a half day closing at 13:00, a feed
  that stops) depends on where the algo runs:
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
    run (end of session, `--minutes` elapsing before `flat_time`, a half day, a stop file)
    `engine.end_live()` is called, which does NOT close open positions; they are left open
    at the end of the run and listed in the output ("position(s) left OPEN at the end of
    the run (listed, never closed by the session)"). Nothing closes them afterwards.
    Set `flat_time` / `last_entry` for a half day or a short `--minutes` yourself.
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
`tests/fvgzone_support.py` (builders, mirror helper). Goldens: cases `fvgzone_*` in
`tests/test_golden_backtests.py` (`tests/golden/`) and `tests/test_paper_goldens.py`
(`tests/golden_paper/`, with the per-bar agreement check of algo and broker).
