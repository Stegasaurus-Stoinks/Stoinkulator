"""The history a live session needs before the open (issue 287): `MainStoinker/ib/live_history.py`
and its use in `live_run`, on the fakes only (`FakeIbClient`); nothing opens a socket.

Three layers: the coverage check (offline), the fetch step on the fake of `tests/test_ib_fetch.py`
(a real `IbConnection`, `IbMarketData` and `fetch`, injected clock) and whole sessions on the rig of
`tests/test_ib_live_run.py`. Nothing here writes to the real `%USERPROFILE%\\Stoinkulator`: the bars
live under `tmp_path` and the rig points the environment at `tmp_path` (`real_home_untouched`).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from ibapi.contract import Contract

from MainStoinker.engine.feed import COLUMNS, read_symbol_bars, write_month_file
from MainStoinker.engine.store import SqliteStore
from MainStoinker.ib import live_run, watch_bars
from MainStoinker.ib.history import DAY_FINAL_AT, Manifest, manifest_path, save_manifest
from MainStoinker.ib.live_history import (
    DEFAULT_WARMUP_DAYS,
    HistoryStep,
    check_coverage,
    fetch_command,
    needed_days,
    open_epoch,
    warmup_sessions_for,
)
from tests.test_ib_fetch import (
    CON_IDS,
    bars_played,
    day_of,
    day_rows,
    end_played,
    error_behaviour,
    make_rig,
    silent,
)
from tests.test_ib_live_run import (
    SYMBOLS,
    Rig,
    ema_entries,
    live_args,
    script_minutes,
    session,
    set_backfills,
    stores,  # noqa: F401  (a fixture)
    write_warmup_files,
)
from tests.test_ib_watch_bars import (  # noqa: F401  (`real_home_untouched` is an autouse fixture)
    DATA_CALLS,
    ep,
    real_home_untouched,
)

NY = ZoneInfo("America/New_York")
TODAY = date(2024, 8, 16)  # a Friday; the sessions before it are 14 and 15 (Wednesday, Thursday)
AUG_14, AUG_15 = date(2024, 8, 14), date(2024, 8, 15)
NINE = datetime(2024, 8, 16, 9, 0, tzinfo=NY)  # an hour and a half before the open


def at(day: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NY)


# -- files and manifests on disk ---------------------------------------------------------------------


def _frame(symbol: str, days: list[tuple[date, int]]) -> pd.DataFrame:
    rows = []
    for day, minutes in days:
        for epoch, o, h, lo, c, v in day_rows(day, minutes, symbol):
            stamp = datetime.fromtimestamp(epoch, tz=NY).replace(tzinfo=None)
            rows.append((pd.Timestamp(stamp), epoch, o, h, lo, c, v))
    return pd.DataFrame(rows, columns=list(COLUMNS))


def write_days(bars: Path, days: list[tuple[date, int]], symbols: tuple[str, ...] = SYMBOLS) -> None:
    """Month files holding `days` (day, number of bars) for `symbols`, month by month."""
    for symbol in symbols:
        months: dict[tuple[int, int], list[tuple[date, int]]] = {}
        for day, minutes in days:
            months.setdefault((day.year, day.month), []).append((day, minutes))
        for (year, month), part in months.items():
            write_month_file(bars / symbol / f"{symbol}_{year}-{month:02d}.csv", _frame(symbol, part))


def write_manifest(bars: Path, records: dict[date, datetime], symbols: tuple[str, ...] = SYMBOLS,
                   status: str = "ok") -> None:
    """A manifest with an `ok` record per day, fetched at the given aware time."""
    for symbol in symbols:
        manifest = Manifest(symbol, CON_IDS[symbol])
        for day, fetched in records.items():
            manifest.record(day, status, bars=390, fetched_utc=fetched.astimezone(UTC))
        save_manifest(manifest_path(bars, symbol), manifest)


def old(day: date) -> datetime:
    """A fetch time long after `day` is final."""
    return at(day + timedelta(days=3), 12)


# -- the algos' need and the warm-up length ----------------------------------------------------------


def fvg_entries(**settings: Any) -> list[dict[str, Any]]:
    return [{"ID": "FVGZone", "data": [{"idname": "x", "ticker": "AAPL", "mode": "paper", **settings}]}]


def test_the_need_is_computed_from_the_algos_and_is_at_least_the_default() -> None:
    plain = live_run.algo_needs(fvg_entries())
    assert plain == [("x", 1)]
    assert warmup_sessions_for(plain, None) == (DEFAULT_WARMUP_DAYS, "the default; the algos declare 1")
    heavy = live_run.algo_needs(fvg_entries(vol_slot_mult=1.5, trend_day_adr=0.3))
    assert heavy == [("x", 22)]
    assert warmup_sessions_for(heavy, None) == (22, "the most any algo declares: x")
    # the largest of several algos, and an algo that declares nothing needs nothing
    both = live_run.algo_needs(fvg_entries(vol_slot_mult=1.5) + ema_entries(("MSFT",)))
    assert [n for _, n in both] == [22, 1]
    assert warmup_sessions_for(both, None)[0] == 22
    # an explicit --warmup-days is the answer, below or above the need
    assert warmup_sessions_for(heavy, 5)[0] == 5 and warmup_sessions_for(plain, 9)[0] == 9


def test_the_warmup_days_are_counted_in_trading_sessions_by_the_calendar() -> None:
    # Monday 2024-12-02: Saturday, Sunday and Thursday's Thanksgiving are not sessions
    assert needed_days(date(2024, 12, 2), 3) == [date(2024, 11, 26), date(2024, 11, 27),
                                                 date(2024, 11, 29)]
    assert needed_days(TODAY, 2) == [AUG_14, AUG_15]
    with pytest.raises(ValueError):
        needed_days(TODAY, 0)


# -- the coverage check ------------------------------------------------------------------------------


def test_a_holiday_and_a_half_day_inside_the_window(tmp_path: Path) -> None:
    """Thanksgiving is no session (nothing is missing for it) and the half day after it is complete
    with its 210 bars; the same window with the half day cut short is not."""
    today = date(2024, 12, 2)
    days = needed_days(today, 3)
    bars = tmp_path / "bars"
    write_days(bars, [(date(2024, 11, 26), 390), (date(2024, 11, 27), 390), (date(2024, 11, 29), 210)],
               ("AAPL",))
    coverage = check_coverage(bars, ["AAPL"], days, at(today, 9))
    assert coverage.clean and coverage.requests == 0
    write_days(bars, [(date(2024, 11, 26), 390), (date(2024, 11, 27), 390), (date(2024, 11, 29), 100)],
               ("AAPL",))
    short = check_coverage(bars, ["AAPL"], days, at(today, 9))
    assert short.todo() == {"AAPL": [date(2024, 11, 29)]}


def test_missing_days_files_and_symbols_are_found(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_15, 390)], ("AAPL",))  # AAPL lacks the 14th, MSFT has no folder at all
    coverage = check_coverage(bars, ["AAPL", "MSFT"], [AUG_14, AUG_15], NINE)
    assert coverage.todo() == {"AAPL": [AUG_14], "MSFT": [AUG_14, AUG_15]}
    assert coverage.requests == 3 and not coverage.blocked()
    assert "AAPL missing 2024-08-14" in coverage.describe_gaps()


def test_a_day_with_holes_counts_when_the_manifest_records_it_and_not_otherwise(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_14, 390), (AUG_15, 380)], ("AAPL",))  # a day with a gap at the end
    assert check_coverage(bars, ["AAPL"], [AUG_14, AUG_15], NINE).todo() == {"AAPL": [AUG_15]}
    write_manifest(bars, {AUG_14: old(AUG_14), AUG_15: old(AUG_15)}, ("AAPL",))
    kept = check_coverage(bars, ["AAPL"], [AUG_14, AUG_15], NINE)
    assert kept.clean and kept.symbols[0].partial == ((AUG_15, 380),)  # IB's own final answer


def test_a_provisional_day_is_fetched_again_once_final_and_blocked_before(tmp_path: Path) -> None:
    """Yesterday's bars fetched the evening before are not taken as final before 06:00 New York of
    the session day (`history.DAY_FINAL_AT`)."""
    assert DAY_FINAL_AT.hour == 6
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_14, 390), (AUG_15, 390)], ("AAPL",))
    write_manifest(bars, {AUG_14: old(AUG_14), AUG_15: at(AUG_15, 22)}, ("AAPL",))
    after = check_coverage(bars, ["AAPL"], [AUG_14, AUG_15], at(TODAY, 6, 0, 1))
    assert after.todo() == {"AAPL": [AUG_15]} and not after.blocked()
    before = check_coverage(bars, ["AAPL"], [AUG_14, AUG_15], at(TODAY, 5, 59))
    assert before.todo() == {} and before.blocked() == {"AAPL": [AUG_15]} and not before.clean
    assert "provisional until 06:00" in before.describe_gaps()


def test_the_fetch_bars_command_names_the_symbols_the_days_and_the_folder(tmp_path: Path) -> None:
    text = fetch_command(tmp_path / "bars", {"AAPL": [AUG_14, AUG_15], "MSFT": [AUG_15]})
    assert text.startswith("python -m MainStoinker fetch-bars --symbols AAPL,MSFT --start 2024-08-14 "
                           "--end 2024-08-15 --out ")
    assert text.endswith(' --paper --no-prompt') and str(tmp_path / "bars") in text


# -- the plan: refusals before the connection --------------------------------------------------------


def plan_args(tmp: Path, **over: Any) -> Any:
    values: dict[str, Any] = {"warmup_days": None}
    values.update(over)
    return live_args(tmp, **values)


def test_the_plan_takes_the_default_warmup_and_needs_no_fetch_when_nothing_is_missing(tmp_path: Path) -> None:
    write_warmup_files(tmp_path / "bars")
    plan = live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001
    assert (plan.warmup_days, plan.will_fetch, plan.cold) == (2, False, ())
    assert plan.days == (AUG_14, AUG_15) and plan.preflight is not None
    assert plan.notes[0].startswith("Warm-up: 2 trading session(s) of history (the default")
    assert sum("nothing missing" in n and "nothing is requested" in n for n in plan.notes) == 1


def test_missing_days_before_the_open_are_to_be_fetched_not_refused(tmp_path: Path) -> None:
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    plan = live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001
    assert plan.will_fetch and plan.preflight is None
    assert plan.coverage is not None and plan.coverage.requests == 3
    assert any("fetched after the connection, before the open" in n for n in plan.notes)


def test_no_fetch_with_a_gap_is_a_refusal_that_names_the_command(tmp_path: Path) -> None:
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    with pytest.raises(live_run.SessionRefused) as info:
        live_run._plan(plan_args(tmp_path, no_fetch=True), TODAY, NINE)  # noqa: SLF001
    text = str(info.value)
    assert "--no-fetch" in text and "python -m MainStoinker fetch-bars --symbols AAPL,MSFT,TSLA" in text
    assert "--start 2024-08-14 --end 2024-08-14" in text and "\n" not in text


def test_no_fetch_with_nothing_missing_is_fine(tmp_path: Path) -> None:
    write_warmup_files(tmp_path / "bars")
    plan = live_run._plan(plan_args(tmp_path, no_fetch=True), TODAY, NINE)  # noqa: SLF001
    assert not plan.will_fetch


def test_a_gap_after_the_open_is_refused_unless_cold_is_allowed(tmp_path: Path) -> None:
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    late = at(TODAY, 9, 31)
    with pytest.raises(live_run.SessionRefused, match="the open .* has passed"):
        live_run._plan(plan_args(tmp_path), TODAY, late)  # noqa: SLF001
    plan = live_run._plan(plan_args(tmp_path, allow_cold=True), TODAY, late)  # noqa: SLF001
    assert not plan.will_fetch and plan.preflight is not None
    assert plan.cold and "has passed" in plan.cold[0]
    assert any("COLD START" in n for n in plan.notes)


def test_a_fetch_that_cannot_finish_before_the_open_is_refused_before_connecting(tmp_path: Path) -> None:
    """22 sessions needed, one on disk: 63 requests take far more than the ten minutes left."""
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    entries = [{"ID": "EMACrossing", "data": [
        {"idname": f"{s}-e", "ticker": s, "short": "10", "long": str(390 * 21 + 1), "mode": "paper"}
        for s in SYMBOLS]}]
    args = plan_args(tmp_path, entries=entries)
    with pytest.raises(live_run.SessionRefused, match="cannot finish before the open"):
        live_run._plan(args, TODAY, at(TODAY, 9, 20))  # noqa: SLF001
    # started early enough, the same history is simply fetched
    plan = live_run._plan(args, TODAY, at(TODAY, 5, 0))  # noqa: SLF001
    assert plan.will_fetch and plan.warmup_days == 22 and plan.coverage is not None
    assert plan.coverage.requests == 63


def test_a_provisional_yesterday_before_six_is_refused_and_after_six_refetched(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_warmup_files(bars)
    write_manifest(bars, {AUG_14: old(AUG_14), AUG_15: at(AUG_15, 22)})
    with pytest.raises(live_run.SessionRefused, match="provisional until 06:00"):
        live_run._plan(plan_args(tmp_path), TODAY, at(TODAY, 5, 30))  # noqa: SLF001
    plan = live_run._plan(plan_args(tmp_path), TODAY, at(TODAY, 9, 0))  # noqa: SLF001
    assert plan.will_fetch and plan.coverage is not None
    assert plan.coverage.todo() == dict.fromkeys(SYMBOLS, [AUG_15])


def test_warmup_days_below_the_need_is_refused_unless_cold_is_allowed(tmp_path: Path) -> None:
    write_warmup_files(tmp_path / "bars")
    entries = [{"ID": "EMACrossing", "data": [
        {"idname": f"{s}-e", "ticker": s, "short": "10", "long": "391", "mode": "paper"}
        for s in SYMBOLS]}]  # 391 bars: two sessions
    with pytest.raises(live_run.SessionRefused, match=r"--warmup-days 1 is below .* \(2: "):
        live_run._plan(plan_args(tmp_path, entries=entries, warmup_days=1), TODAY, NINE)  # noqa: SLF001
    plan = live_run._plan(plan_args(tmp_path, entries=entries, warmup_days=1, allow_cold=True),  # noqa: SLF001
                          TODAY, NINE)
    assert plan.warmup_days == 1 and plan.cold == ("--warmup-days 1 is below the declared need of 2",)
    ok = live_run._plan(plan_args(tmp_path, entries=entries, warmup_days=2), TODAY, NINE)  # noqa: SLF001
    assert ok.warmup_days == 2 and ok.cold == ()


def test_a_config_that_cannot_be_built_is_a_refusal_with_no_connection(
        tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    """An algo whose constructor raises used to end a connected session (exit 4); the algos are
    built before connecting now, so it is a refusal and no client, no connection, no run exists."""
    write_warmup_files(tmp_path / "bars")
    broken = [{"ID": "EMACrossing", "data": [
        {"idname": "a", "ticker": "AAPL", "short": "abc", "long": "30", "mode": "paper"}]}]
    rig = Rig(tmp_path, SYMBOLS)
    try:
        result = session(rig, tmp_path, stores, entries=broken, warmup_days=None)
        assert result.code == 2 and "the algo config cannot be used" in rig.text, rig.text
        assert not rig.connections and not rig.fakes and not rig.mds and not rig.recorders
        assert result.store.list_runs() == ()
    finally:
        for fake in rig.fakes:
            fake.close()


# -- the step on the fake of the fetch tests ---------------------------------------------------------


class StepRig:
    """A `HistoryStep` over a real connection to the fetch tests' fake; time is `now` plus the rig's
    clock, so the wall clock moves with every wait."""

    def __init__(self, rig: Any, bars: Path, today: date, *, now: datetime, symbols: tuple[str, ...],
                 sessions: int = 2, allow_cold: bool = False) -> None:
        self.rig = rig
        self.rebuilt: list[int] = []
        self.lines: list[str] = []
        days = tuple(needed_days(today, sessions))
        self.step = HistoryStep(
            bars_dir=bars, symbols=symbols, days=days, today=today,
            coverage=check_coverage(bars, symbols, days, now), allow_cold=allow_cold,
            clock=rig.clock, sleep=rig.clock.sleep, wall=lambda: now + timedelta(seconds=rig.clock.now),
            exit_stop=7, exit_lost=4, rebuild=lambda: self.rebuilt.append(1))

    def run(self) -> int | None:
        return self.step(self.rig.conn, self.rig.md, self.lines.append)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


NINE_UTC = NINE.astimezone(UTC)


def test_missing_days_are_fetched_only_for_the_symbols_and_days_that_lack_them(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_14, 390), (AUG_15, 390)], ("AAPL",))  # complete
    write_days(bars, [(AUG_15, 390)], ("MSFT",))  # lacks the 14th
    with make_rig(server_now=NINE_UTC) as rig:
        step = StepRig(rig, bars, TODAY, now=NINE, symbols=("AAPL", "MSFT"))
        assert step.run() is None, step.text
        asked = [(next(s for s, c in CON_IDS.items() if c == con), day) for con, day, _ in rig.requests]
    assert asked == [("MSFT", AUG_14)]  # nothing for AAPL, nothing for MSFT's 15th
    assert step.rebuilt == [1] and step.step.outcome.stop is None and step.step.outcome.cold is None
    assert len(read_symbol_bars(bars, "MSFT")) == 2 * 390
    assert "History: estimate: up to 1 request for 1 symbol" in step.text
    assert "History: complete: the month files hold the 2 session(s) 2024-08-14 to 2024-08-15" in step.text
    check = check_coverage(bars, ["AAPL", "MSFT"], [AUG_14, AUG_15], NINE)
    assert check.clean  # the manifest records the fetched day, final by now


def test_a_provisional_yesterday_is_fetched_again_and_replaced(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_14, 390), (AUG_15, 390)], ("AAPL",))
    write_manifest(bars, {AUG_14: old(AUG_14), AUG_15: at(AUG_15, 22)}, ("AAPL",))
    before = read_symbol_bars(bars, "AAPL")
    with make_rig(server_now=NINE_UTC) as rig:
        def changed(req_id: int, day: date, symbol: str) -> list[Any]:
            rows = [(t, o, h, lo, c, v + 7) for t, o, h, lo, c, v in day_rows(day, 390, symbol)]
            return [*bars_played(req_id, rows), end_played(req_id)]

        rig.script(AUG_15, changed)
        step = StepRig(rig, bars, TODAY, now=NINE, symbols=("AAPL",))
        assert step.run() is None, step.text
        assert rig.days_requested() == [AUG_15]  # the final day 14 is left alone
    after = read_symbol_bars(bars, "AAPL")
    assert len(after) == len(before) and after["volume"].sum() == before["volume"].sum() + 7 * 390
    assert step.rebuilt == [1]
    assert check_coverage(bars, ["AAPL"], [AUG_14, AUG_15], NINE).clean


def test_nothing_missing_means_no_step_to_run(tmp_path: Path) -> None:
    """With nothing to fetch the step is never installed (`plan.will_fetch`); called anyway it sends
    nothing and builds nothing."""
    write_warmup_files(tmp_path / "bars")
    with make_rig(server_now=NINE_UTC) as rig:
        step = StepRig(rig, tmp_path / "bars", TODAY, now=NINE, symbols=SYMBOLS)
        assert step.run() is None
        assert not rig.requests and not step.rebuilt and step.lines == []


def test_a_fetch_that_cannot_finish_by_the_open_stops_before_any_request(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_15, 390)], ("AAPL",))
    with make_rig(server_now=NINE_UTC) as rig:
        # the plan was made at nine; by the time the step runs the open is 5 s away
        step = StepRig(rig, bars, TODAY, now=at(TODAY, 9, 29, 55), symbols=("AAPL",), sessions=7)
        assert step.run() == 7
        assert rig.requests == []
    assert "cannot finish before the open" in step.text and "STOPPED" in step.text
    assert "no run was stored" in step.text and step.rebuilt == []
    assert step.step.outcome.stop is not None and step.step.outcome.stop[0] == 7


def test_a_fetch_not_done_at_the_open_stops_with_what_it_has_on_disk(tmp_path: Path) -> None:
    """Three days to fetch, ten seconds apart; the second times out (60 s on the clock), so the open
    passes inside the fetch: the first day is stored, the session does not start trading."""
    bars = tmp_path / "bars"
    with make_rig(server_now=NINE_UTC) as rig:
        step = StepRig(rig, bars, TODAY, now=at(TODAY, 9, 29, 0), symbols=("AAPL",), sessions=3)
        first, second = needed_days(TODAY, 3)[:2]
        rig.script(second, silent)
        assert step.run() == 7, step.text
        assert rig.days_requested()[:2] == [first, second]
        assert len(rig.days_requested()) == 2  # nothing is asked for after the open
    assert "had not finished by the open, 09:30 New York" in step.text
    assert len(read_symbol_bars(bars, "AAPL")) == 390  # the first day is kept
    assert step.rebuilt == []


def test_a_fetch_cut_at_the_open_goes_on_cold_when_that_is_allowed(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    with make_rig(server_now=NINE_UTC) as rig:
        step = StepRig(rig, bars, TODAY, now=at(TODAY, 9, 29, 0), symbols=("AAPL",), sessions=3,
                       allow_cold=True)
        rig.script(needed_days(TODAY, 3)[1], silent)
        assert step.run() is None, step.text
    assert "--allow-cold: the session goes on with the history it has" in step.text
    assert step.step.outcome.cold == "the fetch was cut at the open" and step.rebuilt == [1]


def test_a_day_that_fails_leaves_the_history_incomplete_and_the_session_stops(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_14, 390)], ("AAPL",))
    with make_rig(server_now=NINE_UTC) as rig:
        rig.script(AUG_15, error_behaviour(321, "Error validating request"))
        step = StepRig(rig, bars, TODAY, now=NINE, symbols=("AAPL",))
        assert step.run() == 7
    assert "still incomplete after the fetch: AAPL missing 2024-08-15" in step.text
    assert "2024-08-15: failed" in step.text and step.rebuilt == []
    # the same with --allow-cold: said, and the session goes on with what it has
    with make_rig(server_now=NINE_UTC) as rig:
        rig.script(AUG_15, error_behaviour(321, "Error validating request"))
        cold = StepRig(rig, bars, TODAY, now=NINE, symbols=("AAPL",), allow_cold=True)
        assert cold.run() is None
    assert cold.rebuilt == [1] and cold.step.outcome.cold is not None


def test_a_lost_connection_stops_with_exit_4_and_no_run(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    with make_rig(server_now=NINE_UTC) as rig:
        step = StepRig(rig, bars, TODAY, now=NINE, symbols=("AAPL",))
        rig.conn.close()
        assert step.run() == 4
    assert "no run was stored" in step.text and step.rebuilt == []


def test_a_holiday_and_a_half_day_are_fetched_by_the_calendar(tmp_path: Path) -> None:
    """Window 2024-11-26 to 11-29 before Monday 12-02: no request for Thanksgiving, the half day
    comes back with its 210 bars and that is complete."""
    today = date(2024, 12, 2)
    bars = tmp_path / "bars"
    now = at(today, 9)
    half = date(2024, 11, 29)
    with make_rig(server_now=now.astimezone(UTC)) as rig:
        rig.script(half, lambda req_id, day, symbol: [*bars_played(req_id, day_rows(day, 210, symbol)),
                                                     end_played(req_id)])
        step = StepRig(rig, bars, today, now=now, symbols=("AAPL",), sessions=3)
        assert step.run() is None, step.text
        assert rig.days_requested() == [date(2024, 11, 26), date(2024, 11, 27), half]
    assert step.rebuilt == [1]
    assert check_coverage(bars, ["AAPL"], needed_days(today, 3), now).clean


# -- whole sessions on the watcher rig ---------------------------------------------------------------


class HistRig(Rig):
    """The live rig, which also answers the day requests of the fetch (`keepUpToDate` false) with
    full sessions, and whose clock answer follows the script time."""

    def __init__(self, tmp_path: Path, symbols: tuple[str, ...] = SYMBOLS) -> None:
        super().__init__(tmp_path, symbols)
        self.daily: list[tuple[str, date]] = []
        self.day_minutes: dict[date, int] = {}

    def client_factory(self, **kw: Any) -> Any:
        fake = super().client_factory(**kw)
        fake.respond("reqCurrentTime", lambda: [("currentTime", int(self.server_epoch + self.t))])
        return fake

    def _history(self, req_id: int, contract: Contract, end: str, duration: str, size: str, what: str,
                 rth: int, fmt: int, keep: bool, opts: Any) -> list[tuple[Any, ...]]:
        if keep:
            return super()._history(req_id, contract, end, duration, size, what, rth, fmt, keep, opts)
        day = day_of(end)
        self.daily.append((contract.symbol, day))
        rows = day_rows(day, self.day_minutes.get(day, 390), contract.symbol)
        return [*bars_played(req_id, rows), end_played(req_id)]

    def daily_requests(self) -> list[tuple[str, date]]:
        return list(self.daily)


@pytest.fixture
def hrig(tmp_path: Path) -> Iterator[HistRig]:
    r = HistRig(tmp_path, SYMBOLS)
    r.server_epoch = ep(9, 28)
    r.backfills = {}
    set_backfills(r)
    yield r
    for conn in r.connections:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


def finish_script(rig: HistRig, last: int, minutes: int) -> None:
    script_minutes(rig, SYMBOLS, 0, last + 1)
    rig.at(rig.tt(30) + (last + 2) * 60 + minutes * 60, lambda: None)  # a last step past the end


def test_nothing_missing_sends_no_history_request_and_says_so_in_one_line(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_warmup_files(tmp_path / "bars")
    finish_script(hrig, 4, 8)
    result = session(hrig, tmp_path, stores, minutes=8, warmup_days=None)
    assert result.code == 0, hrig.text
    assert hrig.daily_requests() == []  # only the keep-up-to-date requests of the watcher
    assert sum("History:" in line for line in hrig.text.splitlines()) == 1
    assert "History: nothing missing" in hrig.text
    assert result.run().config["warmup"]["trading_days"] == 2


def test_missing_days_are_fetched_before_the_open_and_the_algo_is_warm_at_the_first_bar(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_15, 390)])  # the day before yesterday is missing for every symbol
    finish_script(hrig, 6, 12)
    result = session(hrig, tmp_path, stores, minutes=12, warmup_days=None)
    text = hrig.text
    assert result.code == 0, text
    assert sorted(hrig.daily_requests()) == sorted((s, AUG_14) for s in SYMBOLS)  # only what was missing
    # the order: the paper check, the fetch, then the live subscriptions and the run
    assert text.index("History: estimate") < text.index("History: complete")
    assert text.index("History: complete") < text.index("backfill:")
    assert text.index("History: complete") < text.index("live: run 1 begun")
    assert "History: incomplete: AAPL missing 2024-08-14" in text
    run = result.run()
    for symbol in SYMBOLS:
        assert run.config["warmup"]["symbols"][symbol]["days"] == ["2024-08-14", "2024-08-15"]
    assert result.times("AAPL")[0] == "09:30"  # the first live bar, with a full warm-up behind it
    assert [d for d in hrig.daily if d[0] == "AAPL"] == [("AAPL", AUG_14)]
    for symbol in SYMBOLS:
        assert len(read_symbol_bars(bars, symbol)) == 2 * 390
    # nothing but data requests, and the same ones as `fetch-bars` sends
    names = {c.name for f in hrig.fakes for c in f.calls}
    assert names <= DATA_CALLS, names
    assert not any("order" in n.lower() for n in names)


def test_the_fetch_comes_off_the_minutes_window(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    """The session never runs longer than `--minutes` counted from the start of the command."""
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    finish_script(hrig, 6, 12)
    result = session(hrig, tmp_path, stores, minutes=12, warmup_days=None)
    assert result.code == 0, hrig.text
    watcher_minutes = [line for line in hrig.text.splitlines() if "Watch time over" in line]
    assert watcher_minutes and "(11 minutes)" in watcher_minutes[0], watcher_minutes


def test_a_session_started_too_late_to_fetch_stops_without_a_run_or_a_connection(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    hrig.server_epoch = ep(9, 31)
    result = session(hrig, tmp_path, stores, minutes=5, warmup_days=None)
    assert result.code == 2 and "the open (09:30 New York) has passed" in hrig.text
    assert not hrig.connections and result.store.list_runs() == ()


def test_no_fetch_with_a_gap_is_refused_and_nothing_connects(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    result = session(hrig, tmp_path, stores, minutes=5, warmup_days=None, no_fetch=True)
    assert result.code == 2
    assert "--no-fetch" in hrig.text and "fetch-bars --symbols AAPL,MSFT,TSLA" in hrig.text
    assert not hrig.connections and not hrig.fakes and result.store.list_runs() == ()


def test_a_fetch_that_fails_ends_the_session_with_exit_7_and_no_run(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    hrig.history_error = {}
    original = hrig._history  # noqa: SLF001

    def refuse(req_id: int, contract: Contract, end: str, *rest: Any) -> list[tuple[Any, ...]]:
        if not rest[6]:  # a day request: IB has no data for it
            hrig.daily.append((contract.symbol, day_of(end)))
            return [("error", req_id, 1_700_000_000_000, 321, "Error validating request", "")]
        return original(req_id, contract, end, *rest)

    hrig._history = refuse  # type: ignore[method-assign]  # noqa: SLF001
    finish_script(hrig, 3, 6)
    result = session(hrig, tmp_path, stores, minutes=6, warmup_days=None)
    assert result.code == 7, hrig.text
    assert "History: STOPPED" in hrig.text and "still incomplete after the fetch" in hrig.text
    assert result.store.list_runs() == ()  # no run was begun: nothing is "failed" in the store
    assert "no run was stored" in hrig.text
    assert not any(keep for *_, keep in hrig.requests)  # no live subscription was ever made
    assert hrig.connections and hrig.connections[0].state.name == "CLOSED"


def test_allow_cold_starts_with_what_is_stored_and_says_so_in_the_run(
        hrig: HistRig, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    hrig.server_epoch = ep(9, 31)  # after the open: no fetch is possible
    set_backfills(hrig, last_closed=0)
    script_minutes(hrig, SYMBOLS, 1, 6)
    hrig.at(hrig.tt(30) + 600, lambda: None)
    result = session(hrig, tmp_path, stores, minutes=5, warmup_days=None, allow_cold=True)
    assert result.code == 0, hrig.text
    assert hrig.daily_requests() == []
    assert "History: COLD START (--allow-cold)" in hrig.text
    warnings = result.run().summary.data.warnings
    assert any(w.startswith("cold start (--allow-cold):") for w in warnings), warnings


def test_the_open_epoch_is_half_past_nine_new_york() -> None:
    assert open_epoch(TODAY) == ep(9, 30)


# -- fix round (PR 291 review) -----------------------------------------------------------------------


def write_raw_manifest(bars: Path, records: dict[str, dict[str, Any]],
                       symbols: tuple[str, ...] = SYMBOLS) -> None:
    for symbol in symbols:
        manifest = {"version": 1, "symbol": symbol, "conId": CON_IDS[symbol],
                    "data_zone": "America/New_York", "use_rth": 1, "what_to_show": "TRADES",
                    "bar_size": "1 min", "days": records}
        path = manifest_path(bars, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("status", ["no_data", "ok"])
def test_a_missing_day_the_fetch_would_not_request_is_refused_before_connecting(
        tmp_path: Path, status: str) -> None:
    bars = tmp_path / "bars"
    write_days(bars, [(AUG_15, 390)])
    stamp = "2024-08-18T12:00:00Z"
    write_raw_manifest(bars, {
        AUG_14.isoformat(): {"status": status, "bars": 0 if status == "no_data" else 390,
                             "fetched_utc": stamp},
        AUG_15.isoformat(): {"status": "ok", "bars": 390, "fetched_utc": stamp}})
    coverage = check_coverage(bars, SYMBOLS, [AUG_14, AUG_15], NINE)
    assert coverage.todo() == {} and set(coverage.stuck()) == set(SYMBOLS) and not coverage.clean
    with pytest.raises(live_run.SessionRefused) as info:
        live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001
    text = str(info.value)
    assert f"AAPL 2024-08-14 has no bars but its manifest says '{status}'" in text
    assert "--allow-cold" in text and "manifest.json" in text
    plan = live_run._plan(plan_args(tmp_path, allow_cold=True), TODAY, NINE)  # noqa: SLF001
    assert not plan.will_fetch and plan.cold


def test_an_unusable_manifest_is_a_refusal_and_no_manifest_is_said(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    write_warmup_files(bars)
    plan = live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001
    line = next(n for n in plan.notes if "nothing missing" in n)
    assert "finality could not be checked for AAPL, MSFT, TSLA (no manifest.json)" in line
    (bars / "AAPL" / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(live_run.SessionRefused, match=r"AAPL.*manifest\.json is not a usable manifest"):
        live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001
    write_raw_manifest(bars, {AUG_15.isoformat(): {"status": "bogus"}}, ("AAPL",))  # partly invalid
    with pytest.raises(live_run.SessionRefused, match="not a usable manifest"):
        live_run._plan(plan_args(tmp_path), TODAY, NINE)  # noqa: SLF001


def test_a_huge_warmup_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    write_warmup_files(tmp_path / "bars")
    with pytest.raises(live_run.SessionRefused, match="cannot be counted"):
        live_run._plan(plan_args(tmp_path, warmup_days=10**6), TODAY, NINE)  # noqa: SLF001


def test_a_missing_yesterday_before_six_is_refused_before_connecting(tmp_path: Path) -> None:
    write_days(tmp_path / "bars", [(AUG_14, 390)])  # the 15th is missing altogether
    with pytest.raises(live_run.SessionRefused, match="missing and not final until 06:00"):
        live_run._plan(plan_args(tmp_path), TODAY, at(TODAY, 5, 30))  # noqa: SLF001
    assert live_run._plan(plan_args(tmp_path), TODAY, NINE).will_fetch  # noqa: SLF001


def test_the_console_warns_when_the_fetch_cuts_the_window_short(
        hrig: Any, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    finish_script(hrig, 6, 12)
    result = session(hrig, tmp_path, stores, minutes=12, warmup_days=None)
    assert result.code == 0, hrig.text
    assert "live: WARNING: the fetch took" in hrig.text
    assert "the run will stop at about 09:" in hrig.text
    assert "before the end of the session (16:20)" in hrig.text


def test_history_to_fetch_on_a_not_paper_account_sends_no_history_request(
        hrig: Any, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])
    hrig.accounts = "U1234567"
    result = session(hrig, tmp_path, stores, minutes=5, warmup_days=None)
    assert result.code == 5, hrig.text
    assert hrig.daily_requests() == [] and hrig.requests == []
    assert result.store.list_runs() == ()


def test_ctrl_c_during_the_fetch_exits_130_closes_the_connection_and_stores_no_run(
        hrig: Any, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    write_days(tmp_path / "bars", [(AUG_15, 390)])

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    hrig.sleep = interrupt  # the fetch's first pacing wait
    result = session(hrig, tmp_path, stores, minutes=5, warmup_days=None)
    assert result.code == 130, hrig.text
    assert hrig.connections[0].state.name == "CLOSED"
    assert result.store.list_runs() == ()
    assert not any(keep for *_, keep in hrig.requests)


def watch_args(tmp: Path, paper: bool) -> argparse.Namespace:
    return argparse.Namespace(
        paper=paper, host="127.0.0.1", port=7497, client_id=22, connect_timeout=30.0,
        symbols=["AAPL"], minutes=5, backfill_minutes=10, quiet=True, out=tmp / "live", record=None,
        private=())


def run_watch_with(hrig: Any, tmp: Path, paper: bool, hook: Any) -> int:
    return watch_bars.run_watch(
        watch_args(tmp, paper), connection_factory=hrig.connection_factory,
        market_data_factory=hrig.market_data_factory, recorder_factory=hrig.recorder_factory,
        out=hrig.out, clock=hrig, sleep=hrig.sleep, wall=hrig.wall,
        seams=watch_bars.WatchSeams(before_watch=hook))


def test_a_nonzero_before_watch_ends_the_watch_with_that_code_and_builds_no_watcher(
        hrig: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoWatcher:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("a Watcher was built")

    monkeypatch.setattr(watch_bars, "Watcher", NoWatcher)
    calls: list[int] = []

    def hook(conn: Any, md: Any, say: Any) -> int:
        calls.append(1)
        return 7

    assert run_watch_with(hrig, tmp_path, True, hook) == 7
    assert calls == [1] and hrig.requests == [] and hrig.connections[0].state.name == "CLOSED"
    assert "(exit 7)" in hrig.text


def test_before_watch_is_not_called_outside_a_verified_paper_session(
        hrig: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stub:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def run(self) -> int:
            return 0

    monkeypatch.setattr(watch_bars, "Watcher", Stub)
    calls: list[int] = []

    def hook(conn: Any, md: Any, say: Any) -> int:
        calls.append(1)
        return 7

    assert run_watch_with(hrig, tmp_path, False, hook) == 0
    assert calls == []


def test_a_server_date_other_than_the_machines_is_checked_for_history_and_stops_the_session(
        hrig: Any, tmp_path: Path, stores: list[SqliteStore]) -> None:  # noqa: F811
    """This computer says 2024-08-15 (its sessions 13 and 14 are on disk); IB says the 16th, whose
    sessions 14 and 15 are not: exit 7, no run."""
    write_days(tmp_path / "bars", [(date(2024, 8, 13), 390), (AUG_14, 390)])
    hrig.server_epoch = ep(9, 30, 10)
    hrig.wall_offset = -86400.0 - 0.4
    finish_script(hrig, 2, 4)
    result = session(hrig, tmp_path, stores, minutes=4, warmup_days=None)
    assert result.code == 7, hrig.text
    assert "is not this computer's 2024-08-15" in hrig.text
    assert "history for IB's date is incomplete" in hrig.text
    assert result.store.list_runs() == ()
