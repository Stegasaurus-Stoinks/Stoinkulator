# Indicators

`MainStoinker/indicators/` holds reusable indicators. They import only the
standard library, numpy and pandas (no legacy module, `engine`, `api` or `ib`;
`tests/test_indicators_import_boundary.py`), keep no globals and take no trade
state: the same bars give the same result whoever asks.

## Fair value gap: `MainStoinker/indicators/fvg.py`

### Definition

Three consecutive bars of the same session, `bar_minutes` apart:

- Up gap: `low3 > high1`. Zone `[high1, low3]`, near edge `low3`, far edge `high1`.
- Down gap: `high3 < low1`. Zone `[high3, low1]`, near edge `high3`, far edge `low1`.

The zone exists at the close of candle 3. `formed_time` is the label of candle 3
and labels are bar STARTS, so the zone is usable from `known_at` (that label plus
`bar_minutes`), not from `formed_time`. The bars do not
need to point the same way (`require_same_direction=True` brings the old
algo's shape rule back). Three candles that span two dates or skip a bar form
no gap, so 15:59 / 09:30 / 09:31 is never an "overnight gap".

Inputs must be cent prices: a price that is not a multiple of 0.01 (within
1e-6 of a cent, so float noise such as `0.30000000000000004` is fine), NaN or inf
raises `ValueError`. Prices are compared as integer ticks of 0.01: each float
input is rounded to the nearest cent. A gap of exactly `min_size` is refused (one cent more is
accepted) whatever the float noise of the subtraction. Integer ticks were
chosen over `Decimal` because they are exact, hashable and fast; the data are
cent prices. The ATR and body means are integer sums over a fixed window
(`deque` plus running sums), so they are exact as well. Fill fractions and ATR
multiples are floats.

### States

All rules are mirrored for up and down zones. "Depth" is measured from the near
edge, so `deepest_fill` runs from 0.0 (untouched) to 1.0 (far edge reached).

| state | meaning |
|-------|---------|
| `OPEN` | formed, no later bar has traded into it. Candle 3 never touches its own zone. |
| `TOUCHED` | a later bar traded into the zone (up: `low < top`; down: `high > bottom`). Each such bar adds one to `touches`. |
| `FILLED` | a bar reached the far edge (up: `low <= bottom`; down: `high >= top`) and the zone was not invalidated by it. Final. |
| `INVALIDATED` | per `invalidate_on`: `close_beyond_far_edge` (default: close past the far edge) or `wick_beyond_far_edge` (any trade past it; a wick exactly at the edge is a fill). Final. |
| `EXPIRED` | `expire_after_bars` bars old, or a new session started with `expire_at_session_end`. Final. |

A bar that enters a fresh zone and also ends it emits `touched` and then the
final event. Events are `formed`, `touched`, `filled`, `invalidated`,
`expired`; each carries a copy of the zone at that moment.

### Config (`FvgConfig`, frozen; defaults shown)

| field | default | meaning |
|-------|---------|---------|
| `min_size` | `0.05` | smallest gap in price units, a whole number of cents (anything else raises); `min_size_inclusive=False` means the gap must be larger |
| `min_size_atr` | `0.25` | smallest gap as a multiple of the mean true range of the `atr_bars` (20) bars ending at candle 1; `None` switches it off. Needs `atr_min_bars` (5) bars, otherwise no gap. The gap may equal the limit. |
| `atr_cross_session` | `True` | the window may reach into the previous session; `False` restarts it each session (the first bar of a session then has no previous close) |
| `require_same_direction` | `False` | `True`: all three candles up (down gap: all down), the old algo's rule |
| `min_displacement_body` | `None` | candle 2's body must be at least this multiple of the mean body of the same window |
| `bar_minutes` | `1` | spacing of the bars you feed (1, 5, 15...) |
| `expire_after_bars` | `None` | expire a live zone after this many bars |
| `expire_at_session_end` | `True` | expire live zones when the first bar of the next date arrives |
| `invalidate_on` | `close_beyond_far_edge` | see the table |
| `max_closed` | `1000` | how many closed zones the detector remembers |

### API

- `FvgDetector(config).update(time, open, high, low, close, volume=None) -> list[FvgEvent]`,
  once per closed bar, oldest first. `time` is a `datetime`/`Timestamp`. A bar
  not after the previous one raises `ValueError`.
- `live_zones(direction=None)`, `closed_zones()`, `zone_containing(price)`
  (newest zone containing it), `nearest_zone(price, direction=None, side=None)`,
  `distance_to_nearest(price, ...)` (0.0 inside a zone; `side` is `"above"` or
  `"below"`; ties go to the newest zone). They return copies.
- `snapshot() -> dict[str, float]`: plain numbers (`live`, `live_up`,
  `live_down`, `in_zone`, `dist_above`, `dist_below`, `newest_direction`,
  `newest_top`, `newest_bottom`, `newest_age_bars`, relative to the last close;
  NaN where absent) for an algo's indicator series.
- `detect_fvgs(frame, config) -> DataFrame`: one row per zone with its final
  state. `frame` has `date` (or a DatetimeIndex), `open`, `high`, `low`,
  `close`; it is not modified. It runs the same detector, so it returns the
  zones of the incremental run, and it keeps every zone whatever `max_closed` is.
- `resample_session(frame, minutes)`: 1-minute bars to N-minute bars aligned to
  09:30 per session, labelled with the bucket START. Bars before 09:30 are
  ignored; bars at or after 16:00 are not filtered. Only a session's
  incomplete LAST bucket (fewer than N source bars) is dropped; a bucket with a
  missing minute in the middle is kept.
- `detect_fvgs` also returns `known_at` and `last_change_known_at`; the
  detector's `config` is read-only.

### Mixing timeframes

Bar labels are bar starts. A zone found on 5-minute bars with `formed_time`
09:40 (candle 3 covers 09:40 to 09:45) is only knowable at 09:45, so its
`known_at` is 09:45. When you join a batch result with finer bars (say 1-minute
bars), use `known_at`, never `formed_time`: at 09:41 to 09:44 that zone does not
exist yet. The same holds for `last_change_known_at` and the state changes.

Work per bar depends on the number of live zones and the window size, not on the
number of bars seen (`last_visits` and `total_visits` count the zones visited).

### Usage inside an algo's `update`

```python
from MainStoinker.indicators.fvg import FvgConfig, FvgDetector

class MyAlgo:
    def __init__(self):
        self.fvg = FvgDetector(FvgConfig(min_size_atr=0.25, expire_after_bars=60))

    def update(self, bar):                      # once per closed bar
        events = self.fvg.update(bar.time, bar.open, bar.high, bar.low, bar.close)
        for e in events:
            if e.kind == "formed":
                print("new", e.zone.direction, e.zone.bottom, e.zone.top)
        zone = self.fvg.nearest_zone(bar.close, direction="up", side="below")
        near = self.fvg.distance_to_nearest(bar.close, side="below")
        return zone is not None and near is not None and near < 0.20
```

The detector has no trade state and never places an order; it is for reading.
`FVG_Algo.py` still uses its own rule; moving it to the detector is a later
task with a golden diff.

### On the dashboard

The algo page of a run (`#/run/<id>/algo/<idname>`, the "Price and trades" panel) can draw the detector's
zones on its candle chart, to see what the detector finds. It is a view only: nothing is stored and no algo
uses it.

- Control: above the chart, "FVG zones" (a checkbox, off by default) and a timeframe, 1, 5 or 15 min. The
  choice is kept in the browser (`localStorage`); the page works without it. While it is off the page makes no
  `/fvg` request.
- Route: `GET /api/runs/{run_id}/fvg?symbol=S[&start=..&end=..][&bar_minutes=1|5|15][&min_size_atr=x][&same_direction=0|1]`.
  Same run, symbol and window rules, errors (404 before 422) and time format as `GET .../bars`, and the same
  bar loading. The detector (`FvgConfig` defaults, except the three options) runs over the bars of the
  previous available trading day (it only fills the ATR window, as in an algo that runs on from day to day;
  no zone of that day is returned), then those from midnight of the window's first day to the window's end.
  So a day shows the same zones whether it is first in the window or not, 5 and 15 minute zones can form
  from the open, and a zone formed earlier in the day that is still live inside the window appears. The
  answer's `warm` is false when the data has no earlier day (the first day of the data): the ATR starts
  cold there and the status line says so. It never reads a bar after the window's end, so the
  picture holds nothing the chart's last bar did not know. 5 and 15 minute zones come from `resample_session`.
  Each zone has the detector's fields plus `end_time`: when it stopped being live (the known time of the
  change that closed it), `null` while it is live at the end of the data. The answer carries the `config` used,
  `bar_minutes`, `warm`, `truncated` (at most 2,000 zones, the newest kept) and, when the bars are not available or a
  price is not a whole cent, an empty list with the `reason` (`missing`, `changed`, `non_cent_price`).
- Drawing: each zone is a band from its bottom to its top, from `known_at` (never `formed_time`) to `end_time`,
  or to the right edge while it is live. Up zones are blue with a solid outline and a "▲" label, down zones
  amber with a dashed outline and a "▼" label; filled, invalidated and expired zones are fainter, dotted and
  name their state. Hovering a band, or clicking it to keep it, shows direction, top, bottom, size, size in
  ATR, state, touches, deepest fill and the formed and known times. The chart library draws the bands as a
  series primitive (`series.attachPrimitive`), so they follow zoom and scroll.
- Zones follow the symbol and day shown, and refresh with the bars of a live run.

This is the first use of `MainStoinker.indicators` by the API; the API import boundary
(`tests/test_api_boundary.py`) allows that package and nothing else new.
