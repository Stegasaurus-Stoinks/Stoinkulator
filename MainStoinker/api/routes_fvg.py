"""`GET /api/runs/{run_id}/fvg`: the fair value gap zones of one run's symbol, for the price chart.

Read-only and computed on request from the bars the dashboard already serves (the bars route's own
loading function, `routes_bars.load_window`); nothing is stored. The run, symbol and window rules, the
errors (404 before 422) and the time format are the bars route's. When the bars route would serve no bars
(`available: false`) the answer is the same with an empty zone list and the reason.

The detector (`MainStoinker.indicators.fvg`, defaults of `FvgConfig` except what the query changes) runs
over the bars from midnight of the window's first day to the window's END, never beyond it: the ATR
window is warm, zones formed before the window that are still live inside it appear, and nothing is
known that the chart's last bar did not know. `bar_minutes` 5 and 15 resample the 1-minute bars first
(`resample_session`). The zones kept are those that overlap the window: usable (`known_at`) no later
than its end and not ended before its start. A zone is drawn from `known_at`, never from `formed_time`;
`end_time` is the time it stopped being live (the known time of its closing change), null while it is
still live at the end of the data. At most `MAX_ZONES` zones are returned (the newest kept,
`truncated: true`). A bar the detector refuses (a price that is not a cent price) gives an empty list
and the reason "non_cent_price", not an error.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from MainStoinker.api.db import with_store
from MainStoinker.api.routes_bars import check_width, load_window
from MainStoinker.api.windows import format_time
from MainStoinker.engine.store import Store
from MainStoinker.indicators.fvg import FvgConfig, detect_fvgs, resample_session

router = APIRouter()

MAX_ZONES = 2_000
BAR_MINUTES = (1, 5, 15)
MAX_MIN_SIZE_ATR = 1_000.0
_LIVE = ("open", "touched")


def _bar_minutes(text: str | None) -> int:
    if text is None:
        return 1
    if text in {str(m) for m in BAR_MINUTES}:
        return int(text)
    raise HTTPException(422, "bar_minutes must be 1, 5 or 15.")


def _same_direction(text: str | None) -> bool:
    if text is None or text == "0":
        return False
    if text == "1":
        return True
    raise HTTPException(422, "same_direction must be 0 or 1.")


def _min_size_atr(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not 0 <= value <= MAX_MIN_SIZE_ATR:
        raise HTTPException(422, f"min_size_atr must be a number from 0 to {MAX_MIN_SIZE_ATR:g}.")
    return value


def _config(bar_minutes: int, min_size_atr: float | None, same_direction: bool) -> FvgConfig:
    base = FvgConfig()
    return FvgConfig(
        bar_minutes=bar_minutes,
        min_size_atr=base.min_size_atr if min_size_atr is None else min_size_atr,
        require_same_direction=same_direction)


def _answer(symbol: str, available: bool, reason: str | None, config: FvgConfig,
            zones: list[dict[str, Any]] | None = None, truncated: bool = False) -> dict[str, Any]:
    return {"symbol": symbol, "available": available, "reason": reason, "bar_minutes": config.bar_minutes,
            "config": asdict(config), "truncated": truncated, "zones": zones or []}


def _time(value: Any) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _zone(row: Any) -> dict[str, Any]:
    ended = row.state not in _LIVE
    return {
        "id": int(row.id), "direction": row.direction,
        "top": float(row.top), "bottom": float(row.bottom), "midpoint": float(row.midpoint),
        "size": float(row.size), "size_atr": _number(row.size_atr),
        "formed_time": format_time(_time(row.formed_time)), "known_at": format_time(_time(row.known_at)),
        "state": row.state, "touches": int(row.touches), "deepest_fill": float(row.deepest_fill),
        "last_change_time": format_time(_time(row.last_change_time)),
        "last_change_known_at": format_time(_time(row.last_change_known_at)),
        "end_time": format_time(_time(row.last_change_known_at)) if ended else None,
    }


def _fvg(store: Store, run_id: int, symbol: str, start: str | None, end: str | None,
         bar_minutes: str | None, min_size_atr: str | None, same_direction: str | None) -> dict[str, Any]:
    loaded = load_window(store, run_id, symbol, start, end, widen=True)  # 404, then the window's 422
    minutes = _bar_minutes(bar_minutes)
    config = _config(minutes, _min_size_atr(min_size_atr), _same_direction(same_direction))
    if loaded.frame is None:
        return _answer(symbol, False, loaded.reason or "missing", config)
    check_width(loaded.frame)
    if loaded.frame.empty or loaded.first is None or loaded.last is None:
        return _answer(symbol, True, loaded.reason, config)
    frame = loaded.frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if minutes > 1:
        frame = resample_session(frame, minutes)
    try:
        found = detect_fvgs(frame, config)
    except ValueError:  # a price that is not a cent price (or not finite)
        return _answer(symbol, True, "non_cent_price", config)
    first, last = loaded.first, loaded.last
    kept = []
    for row in found.itertuples(index=False):
        known = _time(row.known_at)
        ends = None if row.state in _LIVE else _time(row.last_change_known_at)
        if known <= last and (ends is None or ends >= first):
            kept.append((known, int(row.id), row))
    kept.sort(key=lambda item: (item[0], item[1]))
    truncated = len(kept) > MAX_ZONES
    zones = [_zone(row) for _known, _id, row in kept[-MAX_ZONES:]]
    return _answer(symbol, True, loaded.reason, config, zones, truncated)


@router.get("/api/runs/{run_id}/fvg")
async def run_fvg(request: Request, run_id: int, symbol: str,
                  start: str | None = None, end: str | None = None,
                  bar_minutes: str | None = None, min_size_atr: str | None = None,
                  same_direction: str | None = None) -> dict[str, Any]:
    return await with_store(request, lambda store: _fvg(
        store, run_id, symbol, start, end, bar_minutes, min_size_atr, same_direction))
