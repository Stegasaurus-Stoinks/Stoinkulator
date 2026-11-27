"""The live session, the watcher and the day planning follow the market calendar (issue 285).

Everything here runs on the fake client (`tests/fake_ib.py`) through the `Rig` of
`tests/test_ib_watch_bars.py`; no socket, no real sleep, nothing under the real home folder. The
scripted day is Friday 2024-11-29, the day after Thanksgiving, which the calendar closes at 13:00
(`HALF`); Thursday 2024-11-28 is the holiday. A half day must end as a normal, completed session:
before this change the silent-symbol check raised `data_fault` at about 13:10 (issue 273, item 1).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from MainStoinker.engine.feed import COLUMNS, write_month_file
from MainStoinker.engine.store import SqliteStore
from MainStoinker.ib import live_run, run_cli, watch_bars
from MainStoinker.ib.watch_bars import SESSION_GRACE
from tests import test_ib_live_run as live
from tests import test_ib_watch_bars as wb
from tests.no_build import forbid_building
from tests.test_ib_run_cli import argv_of, write_algos
from tests.test_ib_watch_bars import (  # noqa: F401  (`real_home_untouched` is an autouse fixture)
    Rig,
    Tty,
    nothing_built,
    real_home_untouched,
    upd_at,
)

NY = ZoneInfo("America/New_York")
HALF = date(2024, 11, 29)  # closes at 13:00 by the calendar
HOLIDAY = date(2024, 11, 28)  # Thanksgiving
SYMBOLS = live.SYMBOLS


def hep(hh: int, mm: int, ss: int = 0, day: date = HALF) -> int:
    """Epoch seconds of a New York wall-clock time on `day`."""
    return int(datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NY).timestamp())


FIRST = hep(9, 30)


# -- a half day, the watcher alone ---------------------------------------------------------------


def _bar(symbol: str, k: int) -> tuple[float, float, float, float, int]:
    return live.minute_ohlcv(symbol, k)


def script_tail(rig: Rig, symbols: tuple[str, ...], first: int, last: int) -> None:
    """Two updates per minute and symbol (4 s and 50 s into it) for minutes `first` to `last` counted
    from 09:30; the update that opens a minute closes the one before."""
    for k in range(first, last + 1):
        for symbol in symbols:
            final = _bar(symbol, k)
            for secs, ohlcv in ((4, live._partial(final)), (50, final)):  # noqa: SLF001
                upd_at(rig, symbol, FIRST + 60 * k, FIRST + 60 * k + secs, ohlcv)


def backfills(rig: Rig, symbols: tuple[str, ...], last_closed: int) -> None:
    for symbol in symbols:
        closed = [(FIRST + 60 * k, *_bar(symbol, k)) for k in range(last_closed - 1, last_closed + 1)]
        forming = (FIRST + 60 * (last_closed + 1), *live._partial(_bar(symbol, last_closed + 1)))  # noqa: SLF001
        rig.backfills[symbol] = [[*closed, forming]]


def quiet_ticks(rig: Rig, first: int, last: int, every: int = 30) -> None:
    for when in range(first, last + 1, every):
        rig.at(when - rig.server_epoch, lambda: None)


def watch_on(rig: Rig, today: date, symbols: str, minutes: int) -> int:
    """`watch_bars.main` as `Rig.main` calls it, but on a chosen day of this computer's clock."""
    rig.run_symbols = symbols.split(",")
    argv = ["--port", "7497", "--symbols", symbols, "--minutes", str(minutes),
            "--out", str(rig.tmp / "live"), "--no-record"]
    return watch_bars.main(
        argv, env=rig.env, stdin=Tty(True), input_fn=rig.input_fn, out=rig.out, clock=rig, sleep=rig.sleep,
        wall=rig.wall, today=today, connection_factory=rig.connection_factory,
        market_data_factory=rig.market_data_factory)


@pytest.fixture
def watch_rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path, ("AAPL", "MSFT"))
    r.server_epoch = hep(12, 57, 0)
    r.backfills = {}
    backfills(r, ("AAPL", "MSFT"), 206)  # 12:55 and 12:56 closed, 12:57 forming
    yield r
    for conn in r.connections:
        conn.close()
    for fake in r.fakes:
        fake.close()


def half_day_script(rig: Rig) -> dict[str, bool]:
    """The stream of a half day: the last bars 12:57 to 12:59, then nothing (the market is closed).
    Returns what a look at 13:10 saw: whether the 12:59 bar was already written."""
    seen: dict[str, bool] = {}
    script_tail(rig, ("AAPL", "MSFT"), 207, 209)
    quiet_ticks(rig, hep(13, 0, 10), hep(13, 25, 0))

    def look() -> None:
        seen["written_at_1310"] = "12:59  AAPL  O " in rig.text

    rig.at(hep(13, 10, 0) - rig.server_epoch + 0.001, look)
    rig.end_at(30, symbols=2)
    return seen


def test_the_watcher_on_a_half_day_says_the_market_closed_at_13_00_and_holds_the_last_bar_to_13_20(
        watch_rig: Rig) -> None:
    rig = watch_rig
    seen = half_day_script(rig)
    assert watch_on(rig, HALF, "AAPL,MSFT", 30) == 0, rig.text
    text = rig.text
    # the session is over at 13:00, not at 16:00
    assert text.count("market closed: no new bars expected") == 2
    assert "no data received" not in text and "delayed" not in text.lower()
    assert "regular session ended" not in text
    # the last two minutes of the session are held until the close plus 20 minutes (13:20)
    assert seen == {"written_at_1310": False}
    for symbol in ("AAPL", "MSFT"):
        path = rig.tmp / "live" / symbol / f"{symbol}_live_2024-11-29.csv"
        rows = path.read_text(encoding="utf-8").splitlines()
        assert [r.split(",")[1] for r in rows[1:]][-3:] == [str(FIRST + 60 * k) for k in (207, 208, 209)]
        assert [watch_bars._hm(int(r.split(",")[1])) for r in rows[1:]][-1] == "12:59"  # noqa: SLF001
    assert "Result: watched to the end, every symbol streaming (exit 0)" in text


def test_the_watcher_note_names_the_half_day_before_connecting(watch_rig: Rig) -> None:
    rig = watch_rig
    half_day_script(rig)
    assert watch_on(rig, HALF, "AAPL,MSFT", 30) == 0
    first_lines = rig.text.splitlines()[:6]
    assert any("2024-11-29 is a half day by the market calendar" in ln and "13:00 New York" in ln
               for ln in first_lines), first_lines


# -- weekends and holidays are refused before anything is built -----------------------------------


@pytest.mark.parametrize("day,words", [
    (HOLIDAY, "a market holiday (Thanksgiving)"),
    (date(2024, 11, 30), "a weekend (Saturday)"),
    (date(2024, 12, 1), "a weekend (Sunday)"),
    (date(2024, 12, 25), "a market holiday (Christmas Day)"),
])
def test_the_watcher_refuses_a_closed_day_before_building_anything(
        tmp_path: Path, day: date, words: str) -> None:
    rig = Rig(tmp_path, ("AAPL",))
    forbid_building(rig)
    assert watch_on(rig, day, "AAPL", 5) == watch_bars.EXIT_REFUSED, rig.text
    nothing_built(rig)
    assert f"Refused: {day.isoformat()} is not a trading day" in rig.text and words in rig.text
    assert "Nothing was started" in rig.text
    assert not (tmp_path / "live").exists() and not rig.prompts  # no folder, no console gate


def test_the_watcher_stops_when_ibs_own_date_is_a_holiday_and_requests_nothing(tmp_path: Path) -> None:
    """This computer's clock says Wednesday, IB's server says Thanksgiving: nothing is requested."""
    rig = Rig(tmp_path, ("AAPL",))
    rig.server_epoch = hep(10, 0, 0, HOLIDAY)
    rig.end_at(1, symbols=1)
    code = watch_on(rig, date(2024, 11, 27), "AAPL", 5)
    assert code == watch_bars.EXIT_REFUSED, rig.text
    assert "by IB's server date, 2024-11-28 is not a trading day" in rig.text and "Thanksgiving" in rig.text
    assert rig.requests == []
    names = {c.name for f in rig.fakes for c in f.calls}
    assert not names & {"reqContractDetails", "reqHistoricalData"}, names
    assert "Result: refused, nothing was watched (exit 2)" in rig.text


# -- the cross-check of the calendar against IB's hours ---------------------------------------------


def with_hours(monkeypatch: pytest.MonkeyPatch, hours: dict[str, str], zone: str = "US/Eastern") -> None:
    real = wb.details

    def patched(symbol: str) -> Any:
        d = real(symbol)
        d.liquidHours = hours.get(symbol, "")
        d.timeZoneId = zone
        return d

    monkeypatch.setattr(wb, "details", patched)


AGREE = "20241128:CLOSED;20241129:0930-20241129:1300"
FULL = "20241129:0930-20241129:1600"


def test_ibs_hours_that_agree_with_the_calendar_are_said_to(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with_hours(monkeypatch, {"AAPL": AGREE, "MSFT": AGREE})
    half_day_script(watch_rig)
    assert watch_on(watch_rig, HALF, "AAPL,MSFT", 30) == 0
    assert ("Trading hours: IB's hours for today agree with the market calendar (close 13:00)."
            in watch_rig.text)
    assert "WARNING" not in watch_rig.text


def test_a_disagreement_is_a_warning_and_ib_is_followed_for_today(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The calendar says 13:00, IB says the session runs to 16:00: IB wins. At 13:00 nothing is
    'market closed', and a symbol is judged by a 16:00 session."""
    with_hours(monkeypatch, {"AAPL": FULL, "MSFT": FULL})
    rig = watch_rig
    script_tail(rig, ("AAPL", "MSFT"), 207, 212)  # bars go on past 13:00
    quiet_ticks(rig, hep(13, 3, 10), hep(13, 8, 0))
    rig.end_at(10, symbols=2)
    assert watch_on(rig, HALF, "AAPL,MSFT", 10) == 0, rig.text
    text = rig.text
    assert ("WARNING: IB reports the regular session closing at 16:00 today, the market calendar says "
            "13:00. Following IB for today") in text
    assert "market closed: no new bars expected" not in text
    rows = (rig.tmp / "live" / "AAPL" / "AAPL_live_2024-11-29.csv").read_text(encoding="utf-8").splitlines()
    assert str(FIRST + 60 * 211) in [r.split(",")[1] for r in rows[1:]]  # the 13:01 bar was written


def test_hours_that_say_closed_end_the_run_before_any_request(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with_hours(monkeypatch, {"AAPL": "20241129:CLOSED", "MSFT": AGREE})
    rig = watch_rig
    rig.end_at(1, symbols=2)
    assert watch_on(rig, HALF, "AAPL,MSFT", 5) == watch_bars.EXIT_REFUSED, rig.text
    assert "WARNING: IB reports no regular session today for AAPL" in rig.text
    assert "Following IB: nothing is requested." in rig.text
    assert rig.requests == []  # no reqHistoricalData
    assert not {c.name for f in rig.fakes for c in f.calls} & {"reqHistoricalData"}


@pytest.mark.parametrize("hours,zone,why", [
    ({}, "US/Eastern", "carry no liquid hours"),
    ({"AAPL": "garbage", "MSFT": "garbage"}, "US/Eastern", "does not mention the day"),
    ({"AAPL": AGREE, "MSFT": AGREE}, "Europe/London", "not New York's"),
    ({"AAPL": "20241129:0400-20241129:2000", "MSFT": "20241129:0400-20241129:2000"}, "US/Eastern",
     "starts at 04:00"),
])
def test_hours_that_cannot_be_read_are_not_compared_and_the_calendar_stands(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch, hours: dict[str, str], zone: str, why: str) -> None:
    with_hours(monkeypatch, hours, zone)
    half_day_script(watch_rig)
    assert watch_on(watch_rig, HALF, "AAPL,MSFT", 30) == 0, watch_rig.text
    text = watch_rig.text
    assert "were not compared with the market calendar" in text and why in text
    assert "following the calendar: close 13:00." in text
    assert text.count("market closed: no new bars expected") == 2  # the calendar's 13:00 was used


def test_symbols_that_disagree_among_themselves_follow_the_earliest_close(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with_hours(monkeypatch, {"AAPL": AGREE, "MSFT": FULL})
    half_day_script(watch_rig)
    assert watch_on(watch_rig, HALF, "AAPL,MSFT", 30) == 0, watch_rig.text
    assert ("WARNING: the symbols' trading hours differ from each other; following the earliest close."
            in watch_rig.text)
    assert "agree with the market calendar (close 13:00)" in watch_rig.text


# -- the pieces, on a half day ----------------------------------------------------------------------


def test_the_session_helpers_follow_the_calendar() -> None:
    assert watch_bars._session_end(HALF) == hep(13, 0)  # noqa: SLF001
    assert watch_bars._session_end(date(2024, 11, 27)) == hep(16, 0, day=date(2024, 11, 27))  # noqa: SLF001
    assert watch_bars._in_session(hep(12, 59, 59))  # noqa: SLF001
    assert not watch_bars._in_session(hep(13, 0, 0))  # noqa: SLF001
    assert not watch_bars._in_session(hep(9, 29, 59))  # noqa: SLF001
    assert not watch_bars._in_session(hep(11, 0, day=HOLIDAY))  # noqa: SLF001
    assert not watch_bars._in_session(hep(11, 0, day=date(2024, 11, 30)))  # noqa: SLF001
    # the session seconds of a quiet stretch stop at the close
    assert watch_bars._session_exposure(hep(12, 0), hep(13, 30)) == 3600.0  # noqa: SLF001
    assert watch_bars._session_exposure(hep(12, 0), hep(13, 30, day=date(2024, 11, 27))) == 0.0  # noqa: SLF001
    assert watch_bars._session_exposure(hep(12, 0, day=date(2024, 11, 27)),  # noqa: SLF001
                                        hep(13, 30, day=date(2024, 11, 27))) == 5400.0
    assert watch_bars._session_exposure(hep(12, 0), hep(13, 30), (), {HALF: 16 * 60}) == 5400.0  # noqa: SLF001
    assert watch_bars._session_exposure(hep(12, 0, day=HOLIDAY), hep(13, 30, day=HOLIDAY)) == 0.0  # noqa: SLF001


class Stub:
    """A watcher as the driver sees it: the table of today's close."""

    def __init__(self, extra: dict[date, int | None]) -> None:
        self.calendar_extra = extra


def test_the_aligner_clock_is_held_to_the_half_days_close(tmp_path: Path) -> None:
    """On a half day the last two minutes are held to 13:00 + the hold, not to 16:00."""
    stores: list[SqliteStore] = []
    live.write_warmup_files(tmp_path / "bars")
    given = live.UnitSession(tmp_path, stores).session
    given.watcher = Stub({})  # type: ignore[assignment]
    given.today = HALF
    end = hep(13, 0)
    held = given._aligner_clock(end + 900.0, end + 900.0)  # noqa: SLF001
    assert held == end + live_run.LAST_BARS_HOLD
    assert given._aligner_clock(end - 3000.0, end - 3000.0) == end - 3000.0  # noqa: SLF001
    after = end + SESSION_GRACE + live_run.LAST_BARS_MARGIN
    assert given._aligner_clock(after, after) == after  # noqa: SLF001
    assert given._session_end(HALF) == end  # noqa: SLF001
    # IB's close for today wins over the calendar's
    given.watcher = Stub({HALF: 16 * 60})  # type: ignore[assignment]
    assert given._session_end(HALF) == hep(16, 0)  # noqa: SLF001
    for store in stores:
        store.close()


def test_a_closed_day_is_refused_by_the_driver_before_it_connects(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    rig = Rig(tmp_path, SYMBOLS)
    rig.server_epoch = hep(9, 30, 10, HOLIDAY)
    live.write_warmup_files(tmp_path / "bars")
    forbid_building(rig)
    result = live.session(rig, tmp_path, stores)
    assert result.code == 2, rig.text
    nothing_built(rig)
    assert "Refused: 2024-11-28 is not a trading day" in rig.text and "Thanksgiving" in rig.text
    assert result.store.list_runs() == ()
    assert not (tmp_path / "live").exists()


# -- `run`: the command ------------------------------------------------------------------------------


@pytest.fixture
def run_rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path, SYMBOLS)
    r.server_epoch = hep(9, 30, 10)
    r.backfills = {}
    yield r
    for conn in r.connections:
        conn.close()
    for fake in r.fakes:
        fake.close()


def run_main(rig: Rig, argv: list[str], today: date) -> int:
    write_algos(rig.tmp)

    def opener(spec: str, env: Any) -> SqliteStore:
        return SqliteStore(Path(spec), clock=rig.wall, env={})

    return run_cli.main(
        argv, env=rig.env, stdin=Tty(True), input_fn=rig.input_fn, out=rig.out,
        market_data_factory=rig.market_data_factory, clock=rig, sleep=rig.sleep, wall=rig.wall, today=today,
        store_opener=opener, connection_factory=rig.connection_factory, recorder_factory=rig.recorder_factory)


@pytest.mark.parametrize("day,words", [
    (HOLIDAY, "a market holiday (Thanksgiving)"),
    (date(2024, 11, 30), "a weekend (Saturday)"),
])
def test_run_refuses_a_closed_day_before_building_anything_or_opening_the_database(
        run_rig: Rig, tmp_path: Path, day: date, words: str) -> None:
    forbid_building(run_rig)
    code = run_main(run_rig, argv_of(tmp_path), day)
    assert code == 2, run_rig.text
    nothing_built(run_rig)
    assert f"Refused: {day.isoformat()} is not a trading day" in run_rig.text and words in run_rig.text
    assert "Nothing was started" in run_rig.text
    assert not (tmp_path / "runs.db").exists() and not run_rig.prompts


def test_run_says_what_a_half_day_means_for_minutes(run_rig: Rig, tmp_path: Path) -> None:
    forbid_building(run_rig)  # refused for another reason (no bars folder) after the note
    argv = argv_of(tmp_path, bars=str(tmp_path / "nowhere"))
    assert run_main(run_rig, argv, HALF) == 2
    assert ("Note: 2024-11-29 is a half day by the market calendar: the session closes at 13:00 New York "
            "and the run ends at 13:20 (from 09:25 that is 235 minutes).") in run_rig.text


# -- a half day, the whole session -------------------------------------------------------------------


def write_warmup(bars_dir: Path) -> None:
    """Two full sessions, 2024-11-26 and 27 (390 bars each), that fall, as the other live tests do."""
    for symbol in SYMBOLS:
        rows = []
        n = 0
        for day in (26, 27):
            for minute in range(390):
                stamp = pd.Timestamp(2024, 11, day, 9, 30) + pd.Timedelta(minutes=minute)
                close = round(live._base(symbol) + 5.0 - 5.0 * n / 780.0, 4)  # noqa: SLF001
                prev = round(live._base(symbol) + 5.0 - 5.0 * max(n - 1, 0) / 780.0, 4)  # noqa: SLF001
                epoch = int(stamp.tz_localize("America/New_York").timestamp())
                rows.append((stamp, epoch, prev, max(prev, close) + 0.05, min(prev, close) - 0.05,
                             close, 1000 + minute))
                n += 1
        write_month_file(tmp_name(bars_dir, symbol), pd.DataFrame(rows, columns=list(COLUMNS)))


def tmp_name(bars_dir: Path, symbol: str) -> Path:
    return bars_dir / symbol / f"{symbol}_2024-11.csv"


@pytest.fixture
def session_rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path, SYMBOLS)
    r.server_epoch = hep(12, 57, 0)
    r.backfills = {}
    backfills(r, SYMBOLS, 206)  # 12:56 closed, 12:57 forming
    write_warmup(tmp_path / "bars")
    yield r
    for conn in r.connections:
        conn.close()
    for recorder in r.recorders:
        recorder.close()
    for fake in r.fakes:
        fake.close()


@pytest.fixture
def stores() -> Iterator[list[SqliteStore]]:
    opened: list[SqliteStore] = []
    yield opened
    for store in opened:
        store.close()


def test_a_half_day_session_ends_completed_at_13_20_with_the_last_bar_12_59(
        session_rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """The scenario of issue 273 item 1: the stream ends at 13:00. Before the calendar the silent-symbol
    check raised `data_fault` (exit 7) at about 13:10; now the run is `completed`, `session_end`, exit 0."""
    rig = session_rig
    script_tail(rig, SYMBOLS, 207, 209)
    quiet_ticks(rig, hep(13, 0, 10), hep(13, 25, 0))
    rig.end_at(30, symbols=3)
    result = live.session(rig, tmp_path, stores, minutes=30)
    text = rig.text
    assert result.code == 0, text
    summary = result.store.list_runs()[0]
    assert (summary.status, summary.end_reason) == ("completed", "session_end")
    for symbol in SYMBOLS:
        assert result.times(symbol) == ["12:57", "12:58", "12:59"], text  # the last bars of the session
        assert result.counts()[(symbol, "late", None)] == 0
    assert "FAULT" not in text and "data_fault" not in text
    assert "ending the session (session_end)" in text
    assert "Live session: completed, end reason session_end (exit 0)" in text
    assert "verify: VERIFIED" in text
    # positions at the end: the existing rule (left open and listed, never closed by the session)
    open_lines = text.count("OPEN POSITION (left open)")
    assert "no position left open" in text and open_lines == 0, text
    # the run ended at the half day's 13:20, not before (the last bars wait for it) and not much after
    ended = rig.server_epoch + rig.t
    assert hep(13, 20) <= ended < hep(13, 23), ended - hep(13, 20)


def test_the_same_session_on_a_full_day_still_ends_at_16_20(
        tmp_path: Path, stores: list[SqliteStore]) -> None:
    """Nothing changed for a full day: the regression guard for the arithmetic above (2024-08-16)."""
    rig = Rig(tmp_path, SYMBOLS)
    rig.server_epoch = live.ep(15, 57, 0)
    rig.backfills = {}
    live.write_warmup_files(tmp_path / "bars")
    live.set_backfills(rig, last_closed=386)
    live.script_minutes(rig, SYMBOLS, 387, 389)
    live.ticks(rig, live.ep(16, 0, 10), live.ep(16, 25, 0), 30)
    rig.end_at(30, symbols=3)
    try:
        result = live.session(rig, tmp_path, stores, minutes=30)
        assert result.code == 0, rig.text
        assert (result.store.list_runs()[0].end_reason) == "session_end"
        assert live.ep(16, 20) <= rig.server_epoch + rig.t < live.ep(16, 23)
    finally:
        for conn in rig.connections:
            conn.close()
        for fake in rig.fakes:
            fake.close()


def test_a_half_day_silent_symbol_still_fails_the_run_during_the_session(
        session_rig: Rig, tmp_path: Path, stores: list[SqliteStore]) -> None:
    """The silent-symbol check is not switched off on a half day: a symbol that stops at 09:33
    (10 session minutes of silence) is a data fault before 13:00."""
    rig = session_rig
    rig.server_epoch = hep(9, 30, 10)
    rig.backfills = {}
    for symbol in SYMBOLS:  # the snapshot of 09:30:10: the 09:30 bar forming
        rig.backfills[symbol] = [[(FIRST, *live._partial(_bar(symbol, 0)))]]  # noqa: SLF001
    for k in range(0, 26):
        for symbol in SYMBOLS:
            if symbol == "MSFT" and k >= 3:
                continue
            final = _bar(symbol, k)
            for secs, ohlcv in ((4, live._partial(final)), (50, final)):  # noqa: SLF001
                upd_at(rig, symbol, FIRST + 60 * k, FIRST + 60 * k + secs, ohlcv)
    quiet_ticks(rig, FIRST + 200, FIRST + 1500, 20)
    rig.end_at(25, symbols=3)
    result = live.session(rig, tmp_path, stores, minutes=25)
    assert result.code == 7, rig.text
    assert result.store.list_runs()[0].end_reason == "data_fault"
    assert "MSFT has shown no bar and no decision" in rig.text


def test_ibs_close_for_today_moves_the_whole_session(
        session_rig: Rig, tmp_path: Path, stores: list[SqliteStore], monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-check end to end: the calendar says 13:00 and IB says 16:00 for today, so the run
    follows IB: the stream's silence at 13:10 is a silent symbol in a session that is still open."""
    with_hours(monkeypatch, {s: FULL for s in SYMBOLS})
    rig = session_rig
    script_tail(rig, SYMBOLS, 207, 209)
    quiet_ticks(rig, hep(13, 0, 10), hep(13, 25, 0))
    rig.end_at(30, symbols=3)
    result = live.session(rig, tmp_path, stores, minutes=30)
    text = rig.text
    assert "WARNING: IB reports the regular session closing at 16:00 today" in text
    assert result.code == 7 and result.store.list_runs()[0].end_reason == "data_fault", text


# -- follow IB only for a close the exchange uses (13:00 or 16:00) --------------------------------


def test_ib_13_00_on_a_calendar_full_day_is_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, ("AAPL", "MSFT"))  # the default scripted day, Friday 2024-08-16: a full day
    hours = "20240816:0930-20240816:1300"
    with_hours(monkeypatch, {"AAPL": hours, "MSFT": hours})
    rig.standard()
    try:
        assert rig.watch(symbols="AAPL,MSFT", minutes=5) == 0, rig.text
        assert ("WARNING: IB reports the regular session closing at 13:00 today, the market calendar says "
                "16:00. Following IB for today") in rig.text
    finally:
        for conn in rig.connections:
            conn.close()
        for fake in rig.fakes:
            fake.close()


@pytest.mark.parametrize("hours,reported", [
    ("20241129:0930-20241129:1200", "12:00"),
    ("20241129:0930-20241129:1500", "15:00"),
])
def test_an_ib_close_the_exchange_does_not_use_is_not_followed(
        watch_rig: Rig, monkeypatch: pytest.MonkeyPatch, hours: str, reported: str) -> None:
    with_hours(monkeypatch, {"AAPL": hours, "MSFT": hours})
    half_day_script(watch_rig)
    assert watch_on(watch_rig, HALF, "AAPL,MSFT", 30) == 0, watch_rig.text
    text = watch_rig.text
    assert (f"WARNING: IB reports the regular session closing at {reported} today, the market calendar "
            f"says 13:00. {reported} is not a close the exchange uses (13:00 or 16:00): IB's hours are NOT "
            "followed, the calendar is: close 13:00.") in text
    assert "Following IB" not in text
    assert text.count("market closed: no new bars expected") == 2  # the calendar's 13:00 stood


def test_the_exchange_closes_are_13_00_and_16_00() -> None:
    assert watch_bars.EXCHANGE_CLOSES == frozenset({13 * 60, 16 * 60})


