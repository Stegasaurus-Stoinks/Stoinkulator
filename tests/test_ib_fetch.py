"""`MainStoinker/ib/fetch.py`: the fetch driver, over a real `IbConnection` and `IbMarketData`.

The client behind the connection is `FakeIbClient`; nothing opens a socket. Time is injected:
`SimClock` moves a little on every read and by the slept time on every sleep, so pacing waits
and back-offs cost no real time. Timeouts are shrunk to a fraction of a simulated second.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from ibapi.contract import Contract, ContractDetails

from MainStoinker.engine import feed as feed_module
from MainStoinker.engine.cli import main as cli_main
from MainStoinker.engine.feed import CsvBarFeed, month_file_path, read_symbol_bars
from MainStoinker.ib import fetch as fetch_module
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import DATA_REQUESTS, IbConnection
from MainStoinker.ib.fetch import (
    FetchResult,
    FetchSetupError,
    ProgressEvent,
    classify_162,
    estimate_duration,
    fetch,
    merge_day,
)
from MainStoinker.ib.history import Manifest, Pacer, load_manifest, manifest_path, plan_range
from MainStoinker.ib.market_data import IbMarketData
from tests.fake_ib import FARM_MESSAGES, FakeIbClient, make_bar

NY = ZoneInfo("America/New_York")
CON_IDS = {"AAPL": 265598, "MSFT": 272093, "TSLA": 76792991}
SERVER_NOW = datetime(2024, 8, 20, 12, 0, tzinfo=UTC)
WEEK = (date(2024, 8, 12), date(2024, 8, 16))  # Monday to Friday
PACING_TEXT = "Historical Market Data Service error message:Historical data request pacing violation"
NO_DATA_TEXT = "HMDS query returned no data: AAPL@SMART Trades"
Played = tuple[Any, ...]


class SimClock:
    """A clock that moves by `tick` on every read and by the slept time on every sleep."""

    SLOW = 0.0005  # while bars stream in: 390 events must not use up a timeout
    FAST = 0.5  # while nothing is coming: a timeout is reached in a few polls

    def __init__(self, tick: float = SLOW) -> None:
        self.now = 0.0
        self.tick = tick
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        value = self.now
        self.now += self.tick
        return value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# -- scripted days ---------------------------------------------------------------------------


def day_of(end: str) -> date:
    """The New York date of an `endDateTime` like 20240816-20:00:00 (UTC)."""
    return datetime.strptime(end, "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC).astimezone(NY).date()


Row = tuple[int, float, float, float, float, int]


def day_rows(day: date, minutes: int = 390, symbol: str = "AAPL") -> list[Row]:
    """Deterministic bars of a regular session: (epoch, open, high, low, close, volume)."""
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    base = 100.0 + day.day + (CON_IDS[symbol] % 7)
    rows = []
    for i in range(minutes):
        o = round(base + i * 0.01, 2)
        rows.append((int(start.timestamp()) + 60 * i, o, round(o + 1, 2), round(o - 1, 2),
                     round(o + 0.5, 2), 1000 + 3 * i))
    return rows


Behaviour = Callable[[int, date, str], list[Played]]


def bars_played(req_id: int, rows: list[Row]) -> list[Played]:
    return [("historicalData", req_id, make_bar(*r)) for r in rows]


def end_played(req_id: int) -> Played:
    return ("historicalDataEnd", req_id, "start", "end")


def error_played(req_id: int, code: int, text: str) -> Played:
    return ("error", req_id, 1_700_000_000_000, code, text, "")


def full_day(req_id: int, day: date, symbol: str) -> list[Played]:
    return [*bars_played(req_id, day_rows(day, symbol=symbol)), end_played(req_id)]


def error_behaviour(code: int, text: str) -> Behaviour:
    return lambda req_id, day, symbol: [error_played(req_id, code, text)]


def holiday_behaviour(previous: date) -> Behaviour:
    return lambda req_id, day, symbol: [*bars_played(req_id, day_rows(previous, symbol=symbol)),
                                        end_played(req_id)]


def empty_end(req_id: int, day: date, symbol: str) -> list[Played]:
    return [end_played(req_id)]


def silent(req_id: int, day: date, symbol: str) -> list[Played]:
    return []


def details(symbol: str) -> ContractDetails:
    d = ContractDetails()
    d.contract = Contract()
    d.contract.conId = CON_IDS[symbol]
    d.contract.symbol = symbol
    d.contract.secType = "STK"
    d.contract.exchange = "SMART"
    d.contract.primaryExchange = "NASDAQ"
    d.contract.currency = "USD"
    d.minTick = 0.01
    d.timeZoneId = "US/Eastern"
    return d


@dataclass
class Rig:
    conn: IbConnection
    md: IbMarketData
    fake: FakeIbClient
    clock: SimClock
    plans: dict[tuple[int | None, date], list[Behaviour]] = field(default_factory=dict)
    requests: list[tuple[int, date, float]] = field(default_factory=list)  # conId, day, clock.now
    events: list[ProgressEvent] = field(default_factory=list)

    def script(self, day: date, *behaviours: Behaviour, con_id: int | None = None) -> None:
        """What each request for `day` gets: the behaviours in turn, the last one repeating."""
        self.plans[(con_id, day)] = list(behaviours)

    def handler(self, req_id: int, contract: Contract, end: str, *rest: Any) -> list[Played]:
        day = day_of(end)
        self.requests.append((contract.conId, day, self.clock.now))
        plan = self.plans.get((contract.conId, day)) or self.plans.get((None, day))
        symbol = next(s for s, c in CON_IDS.items() if c == contract.conId)
        if plan is None:
            return self._timed(full_day(req_id, day, symbol))
        behaviour = plan.pop(0) if len(plan) > 1 else plan[0]
        return self._timed(behaviour(req_id, day, symbol))

    def _timed(self, played: list[Played]) -> list[Played]:
        """A request nothing will answer is waited out on a fast clock, the others on a slow one."""
        answered = any(p[0] == "historicalDataEnd" or (p[0] == "error" and not 2100 <= p[3] < 2200)
                       for p in played)
        self.clock.tick = SimClock.SLOW if answered else SimClock.FAST
        return played

    def days_requested(self, con_id: int | None = None) -> list[date]:
        return [d for c, d, _ in self.requests if con_id is None or c == con_id]

    def run(self, symbols: list[str], start: date, end: date, out: Path, **kw: Any) -> FetchResult:
        options: dict[str, Any] = {
            "connection": self.conn, "clock": self.clock, "sleep": self.clock.sleep,
            "on_progress": self.events.append, "request_timeout": 5.0, "resolve_timeout": 3.0,
            "server_time_timeout": 3.0, "farm_timeout": 0.5,
        }
        options.update(kw)
        return fetch(self.md, symbols, start, end, out, **options)


@contextmanager
def make_rig(*, farm: bool = True, server_now: datetime | None = SERVER_NOW) -> Iterator[Rig]:
    clock = SimClock()
    fakes: list[FakeIbClient] = []

    def factory(*, env: Any, on_event: Any, on_raw: Any, logger: Any) -> FakeIbClient:
        fake = FakeIbClient(DataOnlyClient(env, on_event, on_raw, logger),
                            farm_messages=FARM_MESSAGES if farm else ())
        fakes.append(fake)
        return fake

    conn = IbConnection("127.0.0.1", 7497, 21, env={}, client_factory=factory, clock=clock,
                        sleep=clock.sleep, poll_interval=0.002)
    conn.open()
    try:
        rig = Rig(conn, IbMarketData(conn, clock=clock, poll_interval=0.01), fakes[-1], clock)
        rig.fake.respond("reqContractDetails", lambda req_id, contract: [
            ("contractDetails", req_id, details(contract.symbol)), ("contractDetailsEnd", req_id)])
        rig.fake.respond("reqHistoricalData", rig.handler)
        if server_now is not None:
            rig.fake.respond("reqCurrentTime", [("currentTime", int(server_now.timestamp()))])
        yield rig
    finally:
        conn.close()
        for fake in fakes:
            fake.close()


def month_bytes(out: Path, symbol: str = "AAPL") -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((out / symbol).glob(f"{symbol}_*.csv"))}


def manifest_days(out: Path, symbol: str = "AAPL") -> dict[str, str]:
    raw = json.loads((out / symbol / "manifest.json").read_text(encoding="utf-8"))
    return {k: v["status"] for k, v in raw["days"].items()}


def weekdays(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "bars"


# -- acceptance criteria ---------------------------------------------------------------------


def test_fetch_writes_month_files_readable_by_csv_bar_feed(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    assert result.complete and result.stop_reason is None and not result.connection_lost
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed, sym.already_done, sym.not_closed) == (3, 0, 0, 0, 0)
    assert sym.con_id == CON_IDS["AAPL"] and sym.state == "done"
    assert [p.name for p in sorted((out / "AAPL").glob("AAPL_*.csv"))] == ["AAPL_2024-08.csv"]
    frame = read_symbol_bars(out, "AAPL")
    assert len(frame) == 3 * 390
    assert frame["date"].iloc[0] == pd.Timestamp("2024-08-12 09:30:00")
    assert frame["date"].iloc[-1] == pd.Timestamp("2024-08-14 15:59:00")
    assert frame["volume"].iloc[0] == 1000 and str(frame["volume"].dtype) == "int64"
    csv_feed = CsvBarFeed(out, ["AAPL"], warmup_days=1)
    assert len(csv_feed.warmup("AAPL")) == 390
    assert csv_feed.replay_counts() == {"AAPL": 780}
    assert manifest_days(out) == {"2024-08-12": "ok", "2024-08-13": "ok", "2024-08-14": "ok"}


@pytest.mark.parametrize("how", ["closed", "lost"])
def test_fetch_resumes_after_lost_connection(out: Path, tmp_path: Path, how: str) -> None:
    start, end = date(2024, 8, 12), date(2024, 8, 15)
    days = weekdays(start, end)

    def lose(req_id: int, day: date, symbol: str) -> list[Played]:
        if how == "closed":
            rig.fake.drop("connectionClosed")
        else:
            rig.fake.drop(1100)
        return []

    with make_rig() as rig:
        rig.script(days[2], lose)
        first = rig.run(["AAPL"], start, end, out)
    assert first.connection_lost and first.stop_reason is not None
    assert not first.complete and first.pacing_stopped is False
    assert first.symbols["AAPL"].ok == 2 and first.symbols["AAPL"].failed == 1
    assert first.symbols["AAPL"].remaining == 1
    assert rig.days_requested() == days[:3]
    assert manifest_days(out) == {days[0].isoformat(): "ok", days[1].isoformat(): "ok",
                                  days[2].isoformat(): "failed"}
    assert len(read_symbol_bars(out, "AAPL")) == 2 * 390  # what was finished is on disk
    assert (out / "AAPL" / "gaps.csv").is_file()  # also after a stopped run

    with make_rig() as rig2:
        second = rig2.run(["AAPL"], start, end, out)
    assert second.complete and second.symbols["AAPL"].ok == 2
    assert second.symbols["AAPL"].already_done == 2
    assert rig2.days_requested() == days[2:]  # only the remaining days

    reference = tmp_path / "uninterrupted"
    with make_rig() as rig3:
        assert rig3.run(["AAPL"], start, end, reference).complete
    assert month_bytes(out) == month_bytes(reference)
    assert manifest_days(out) == manifest_days(reference)


def test_fetch_holiday_is_no_data_not_failure(out: Path) -> None:
    labor_day = date(2024, 9, 2)
    with make_rig(server_now=datetime(2024, 9, 10, 12, 0, tzinfo=UTC)) as rig:
        rig.script(labor_day, holiday_behaviour(date(2024, 8, 30)))
        result = rig.run(["AAPL"], date(2024, 8, 30), date(2024, 9, 3), out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (2, 1, 0)
    assert result.complete
    assert manifest_days(out)["2024-09-02"] == "no_data"
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"]["2024-09-02"]["bars"] == 0
    frame = read_symbol_bars(out, "AAPL")
    assert not (frame["date"].dt.date == date(2024, 9, 2)).any()
    assert (frame["date"].dt.date == date(2024, 8, 30)).sum() == 390  # the previous session once only
    # a rerun does not ask for it again
    with make_rig(server_now=datetime(2024, 9, 10, 12, 0, tzinfo=UTC)) as rig2:
        again = rig2.run(["AAPL"], date(2024, 8, 30), date(2024, 9, 3), out)
    assert rig2.requests == [] and again.symbols["AAPL"].already_done == 3


def test_fetch_failed_day_recorded_and_retried_next_run(out: Path) -> None:
    start, end = date(2024, 8, 12), date(2024, 8, 14)
    bad = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(bad, error_behaviour(354, "Requested market data is not subscribed."))
        first = rig.run(["AAPL"], start, end, out)
    sym = first.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (2, 0, 1) and not first.complete
    failure = sym.failures[0]
    assert (failure.day, failure.code, failure.text) == (bad, 354, "Requested market data is not subscribed.")
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"]["2024-08-13"]["status"] == "failed"
    assert raw["days"]["2024-08-13"]["error_code"] == 354
    assert raw["days"]["2024-08-13"]["error_text"] == "Requested market data is not subscribed."
    with make_rig() as rig2:
        second = rig2.run(["AAPL"], start, end, out)
    assert rig2.days_requested() == [bad]
    assert second.complete and second.symbols["AAPL"].ok == 1
    assert manifest_days(out)["2024-08-13"] == "ok"
    assert len(read_symbol_bars(out, "AAPL")) == 3 * 390


def test_fetch_respects_pacing(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 16), out)
    assert result.complete
    times = [t for _, _, t in rig.requests]
    assert len(times) == 10
    assert all(b - a >= 10.0 for a, b in zip(times, times[1:], strict=False)), times
    # 60 per 10 minutes: no window of 600 s holds more than 60 (here 10, but the estimate agrees)
    assert result.estimate is not None and result.estimate.requests == 10
    assert result.estimate.min_seconds >= 9 * 10.0


def test_fetch_pacing_violation_retried_once(out: Path) -> None:
    day = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(day, error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    assert result.complete and not result.pacing_stopped
    assert rig.days_requested().count(day) == 2
    assert result.symbols["AAPL"].ok == 3
    waits = [s for s in rig.clock.sleeps if s >= 5.0]
    assert sum(waits) >= 600.0  # the back-off went through the injected sleep
    assert max(rig.clock.sleeps) <= 5.0  # in slices
    # the retry went through the pacer too: the gap to the failed request is at least 600 s
    first, second = [t for _, d, t in rig.requests if d == day]
    assert second - first >= 600.0


def test_fetch_second_pacing_violation_stops_the_run(out: Path) -> None:
    days = weekdays(date(2024, 8, 12), date(2024, 8, 14))
    with make_rig() as rig:
        rig.script(days[1], error_behaviour(162, PACING_TEXT))  # every request for it
        result = rig.run(["AAPL", "MSFT"], days[0], days[-1], out)
    assert result.pacing_stopped and result.stop_reason == "pacing violation"
    assert not result.complete
    assert rig.days_requested() == [days[0], days[1], days[1]]  # two tries, then nothing more
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.failed) == (1, 1) and sym.remaining == 1
    assert result.symbols["MSFT"].state == "not_started"  # the run stopped for all symbols
    assert manifest_days(out)["2024-08-13"] == "failed"
    raw = json.loads((out / "AAPL" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["days"]["2024-08-13"]["error_code"] == 162
    assert raw["days"]["2024-08-13"]["error_text"] == PACING_TEXT
    assert not (out / "MSFT").exists()
    assert (out / "AAPL" / "gaps.csv").is_file()


def test_fetch_second_pacing_violation_counts_across_days(out: Path) -> None:
    days = weekdays(date(2024, 8, 12), date(2024, 8, 14))
    with make_rig() as rig:
        rig.script(days[0], error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))
        rig.script(days[2], error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))
        result = rig.run(["AAPL"], days[0], days[-1], out)
    assert result.pacing_stopped
    assert rig.days_requested() == [days[0], days[0], days[1], days[2]]  # no retry for the 2nd
    assert result.symbols["AAPL"].failed == 1 and result.symbols["AAPL"].ok == 2


def test_fetch_reports_gaps(out: Path) -> None:
    days = weekdays(date(2024, 8, 12), date(2024, 8, 14))

    def short_day(req_id: int, day: date, symbol: str) -> list[Played]:
        rows = day_rows(day, symbol=symbol)
        return [*bars_played(req_id, rows[:100] + rows[110:]), end_played(req_id)]

    with make_rig() as rig:
        rig.script(days[1], short_day)
        rig.script(days[2], error_behaviour(162, NO_DATA_TEXT))
        result = rig.run(["AAPL", "MSFT"], days[0], days[-1], out)
    assert result.symbols["MSFT"].gaps and result.symbols["AAPL"].gaps
    rows = {r.day: r for r in result.symbols["AAPL"].gaps}
    assert [rows[d].status for d in days] == ["complete", "partial", "no_data"]
    csv_text = (out / "AAPL" / "gaps.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "symbol,day,status,expected,present,missing,ranges"
    assert "AAPL,2024-08-13,partial,390,380,10,11:10-11:19" in csv_text
    assert "AAPL,2024-08-14,no_data,390,0,390," in csv_text
    summary = (out / "AAPL" / "gaps_summary.txt").read_text(encoding="utf-8")
    assert "complete: 1" in summary and "partial: 1" in summary and "no_data: 1" in summary
    assert "MSFT" in (out / "MSFT" / "gaps.csv").read_text(encoding="utf-8")  # one pair per folder
    assert result.symbols["AAPL"].gap_files == (out / "AAPL" / "gaps.csv", out / "AAPL" / "gaps_summary.txt")


def test_fetch_month_boundary_splits_files(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL"], date(2024, 7, 30), date(2024, 8, 2), out)
    assert result.complete
    names = sorted(p.name for p in (out / "AAPL").glob("AAPL_*.csv"))
    assert names == ["AAPL_2024-07.csv", "AAPL_2024-08.csv"]
    july = pd.read_csv(month_file_path(out, "AAPL", 2024, 7))
    august = pd.read_csv(month_file_path(out, "AAPL", 2024, 8))
    assert len(july) == 2 * 390 and len(august) == 2 * 390
    assert july["date"].iloc[-1] == "2024-07-31 15:59:00" and august["date"].iloc[0] == "2024-08-01 09:30:00"
    assert len(read_symbol_bars(out, "AAPL")) == 4 * 390


def test_fetch_uses_only_data_requests(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(354, "not subscribed"))
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    sent = {c.name for c in rig.fake.calls} - {"connect", "disconnect", "run"}
    assert sent <= DATA_REQUESTS, sent - DATA_REQUESTS
    assert {"reqCurrentTime", "reqContractDetails", "reqHistoricalData"} <= sent
    assert not any(name.startswith("MainStoinker.ib.orders") for name in sys.modules)
    assert "MainStoinker.ib.orders" not in sys.modules


# -- classification of one day -----------------------------------------------------------------


def test_classify_162_texts() -> None:
    assert classify_162(PACING_TEXT) == fetch_module.KIND_PACING
    assert classify_162("Pacing Violation!") == fetch_module.KIND_PACING
    assert classify_162(NO_DATA_TEXT) == fetch_module.KIND_NO_DATA
    assert classify_162("HMDS: something unknown") == fetch_module.KIND_OTHER
    assert classify_162("") == fetch_module.KIND_OTHER
    assert classify_162("pacing violation, query returned no data") == fetch_module.KIND_PACING


def test_error_162_no_data_text_records_no_data(out: Path) -> None:
    day = date(2024, 8, 13)
    with make_rig() as rig:
        rig.script(day, error_behaviour(162, NO_DATA_TEXT))
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    assert (result.symbols["AAPL"].ok, result.symbols["AAPL"].no_data) == (2, 1)
    assert result.complete
    assert manifest_days(out)[day.isoformat()] == "no_data"
    assert [e.text for e in result.ib_errors if e.code == 162] == [NO_DATA_TEXT]


def test_error_162_with_other_text_is_failed(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(162, "HMDS error message:Weird"))
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.no_data == 0
    assert sym.failures[0].code == 162 and "Weird" in sym.failures[0].text
    assert result.pacing_stopped is False


@pytest.mark.parametrize("code", [354, 10089, 10167, 200, 321, 504, 366, 10197])
def test_other_ib_errors_are_failed_with_code_and_text(out: Path, code: int) -> None:
    text = f"IB says something about {code}"
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(code, text))
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (0, 0, 1)
    assert (sym.failures[0].code, sym.failures[0].text) == (code, text)
    assert manifest_days(out) == {"2024-08-13": "failed"}
    assert not month_file_path(out, "AAPL", 2024, 8).exists()


def test_notice_only_answer_times_out_as_failed(out: Path) -> None:
    """Code 2105 is a notice: it does not answer the request; the day times out, never no_data."""
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(2105, "HMDS data farm connection is broken:ushmds"))
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.no_data == 0
    assert sym.failures[0].reason == "timeout" and sym.failures[0].code is None
    assert any(e.code == 2105 and e.notice for e in result.ib_errors)
    assert "cancelHistoricalData" in rig.fake.call_names  # a timed-out request is cancelled at IB


def test_timeout_is_failed_not_no_data(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), silent)
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    sym = result.symbols["AAPL"]
    assert (sym.ok, sym.no_data, sym.failed) == (2, 0, 1)
    assert sym.failures[0].reason == "timeout"
    assert manifest_days(out)["2024-08-13"] == "failed" and not result.connection_lost


def test_bad_bar_fails_the_whole_day_and_writes_nothing(out: Path) -> None:
    def one_bad(req_id: int, day: date, symbol: str) -> list[Played]:
        rows = day_rows(day, symbol=symbol)
        bars = bars_played(req_id, rows)
        bars[50] = ("historicalData", req_id, make_bar(rows[50][0], -1, -1, -1, -1, -1))
        return [*bars, end_played(req_id)]

    with make_rig() as rig:
        rig.script(date(2024, 8, 13), one_bad)
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.failures[0].reason == "bad_bar"
    assert not month_file_path(out, "AAPL", 2024, 8).exists()  # nothing of the day is written
    assert manifest_days(out) == {"2024-08-13": "failed"}


def test_filter_day_error_fails_the_day(out: Path) -> None:
    def stray(req_id: int, day: date, symbol: str) -> list[Played]:
        rows = day_rows(day, symbol=symbol)
        early = int(datetime(day.year, day.month, day.day, 8, 0, tzinfo=NY).timestamp())
        return [*bars_played(req_id, [(early, 10, 11, 9, 10, 5), *rows]), end_played(req_id)]

    with make_rig() as rig:
        rig.script(date(2024, 8, 13), stray)
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.no_data == 0 and sym.failures[0].reason == "invalid_data"
    assert "outside 09:30 to 15:59" in sym.failures[0].text
    assert not month_file_path(out, "AAPL", 2024, 8).exists()


def test_complete_answer_without_bars_is_no_data(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), empty_end)
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    assert result.symbols["AAPL"].no_data == 1 and result.complete


def test_data_lost_is_requested_again_once(out: Path) -> None:
    day = date(2024, 8, 13)

    def lost_midway(req_id: int, d: date, symbol: str) -> list[Played]:
        rig.fake.drop(1101)
        return bars_played(req_id, day_rows(d, 3, symbol))  # some bars, then the restore: discarded

    with make_rig() as rig:
        rig.script(day, lost_midway, lambda r, d, s: full_day(r, d, s))
        result = rig.run(["AAPL"], day, day, out)
    assert result.complete and rig.days_requested() == [day, day]
    assert len(read_symbol_bars(out, "AAPL")) == 390  # the discarded bars are not in the file
    assert rig.requests[1][2] - rig.requests[0][2] >= 10.0  # the second ask went through the pacer


def test_data_lost_twice_is_failed(out: Path) -> None:
    day = date(2024, 8, 13)

    def lost(req_id: int, d: date, symbol: str) -> list[Played]:
        rig.fake.drop(1101)
        return []

    with make_rig() as rig:
        rig.script(day, lost)
        result = rig.run(["AAPL"], day, day, out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.failures[0].reason == "data_lost" and sym.failures[0].code == 1101
    assert rig.days_requested() == [day, day]
    assert not result.connection_lost


def test_every_request_id_is_cancelled(out: Path) -> None:
    cancelled: list[int] = []
    original = None

    def spy(req_id: int) -> bool:
        cancelled.append(req_id)
        assert original is not None
        return original(req_id)

    days = weekdays(date(2024, 8, 12), date(2024, 8, 16))
    with make_rig() as rig:
        original = rig.md.cancel_history
        rig.md.cancel_history = spy  # type: ignore[method-assign]
        rig.script(days[1], error_behaviour(354, "x"))
        rig.script(days[2], silent)
        rig.script(days[3], error_behaviour(162, NO_DATA_TEXT))
        rig.run(["AAPL"], days[0], days[-1], out)
        ids = [c.args[0] for c in rig.fake.calls_named("reqHistoricalData")]
    assert cancelled == ids and len(ids) == 5


# -- order of writes and merging ---------------------------------------------------------------


def test_bars_are_on_disk_before_the_manifest_says_ok(out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bool]] = []
    real = fetch_module.save_manifest

    def spy(path: Path, manifest: Any, **kw: Any) -> None:
        for key, rec in manifest.days.items():
            if rec.status == "ok":
                day = date.fromisoformat(key)
                month = pd.read_csv(month_file_path(out, "AAPL", day.year, day.month))
                seen.append((key, (pd.to_datetime(month["date"]).dt.date == day).sum() == 390))
        real(path, manifest, **kw)

    monkeypatch.setattr(fetch_module, "save_manifest", spy)
    with make_rig() as rig:
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    assert seen and all(ok for _, ok in seen)


def test_crash_between_month_file_and_manifest_is_repaired_idempotently(
    out: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    days = weekdays(date(2024, 8, 12), date(2024, 8, 14))
    real = fetch_module.save_manifest
    calls = {"n": 0}

    def crash(path: Path, manifest: Any, **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power failure")
        real(path, manifest, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(fetch_module, "save_manifest", crash)
        with make_rig() as rig, pytest.raises(RuntimeError, match="power failure"):
            rig.run(["AAPL"], days[0], days[-1], out)
    assert manifest_days(out) == {days[0].isoformat(): "ok"}  # day 2 is on disk but not recorded
    assert len(read_symbol_bars(out, "AAPL")) == 2 * 390
    before = month_bytes(out)

    writes: list[Path] = []
    real_write = fetch_module.write_month_file

    def counting(path: Path, frame: pd.DataFrame, **kw: Any) -> None:
        writes.append(path)
        real_write(path, frame, **kw)

    monkeypatch.setattr(fetch_module, "write_month_file", counting)
    with make_rig() as rig2:
        result = rig2.run(["AAPL"], days[0], days[-1], out)
    assert rig2.days_requested() == days[1:]  # the unrecorded day is fetched again
    assert result.complete and len(writes) == 1  # day 2 changed nothing: only day 3 was written
    reference = tmp_path / "reference"
    with make_rig() as rig3:
        rig3.run(["AAPL"], days[0], days[-1], reference)
    assert month_bytes(out) == month_bytes(reference)
    assert before != month_bytes(out)


def test_refetching_a_recorded_day_does_not_rewrite_the_file(
    out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_rig() as rig:
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 13), out)
    path = out / "AAPL" / "manifest.json"
    manifest, reason = load_manifest(path, Manifest("AAPL", CON_IDS["AAPL"]))
    assert reason is None
    manifest.mark_pending([date(2024, 8, 12), date(2024, 8, 13)])
    fetch_module.save_manifest(path, manifest)
    before = month_bytes(out)
    mtime = month_file_path(out, "AAPL", 2024, 8).stat().st_mtime_ns

    def refuse(*a: Any, **k: Any) -> None:
        raise AssertionError("an unchanged month file must not be rewritten")

    monkeypatch.setattr(fetch_module, "write_month_file", refuse)
    with make_rig() as rig2:
        result = rig2.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 13), out)
    assert result.complete and result.symbols["AAPL"].ok == 2
    assert month_bytes(out) == before
    assert month_file_path(out, "AAPL", 2024, 8).stat().st_mtime_ns == mtime


def test_merge_day_replaces_the_day_and_keeps_others(tmp_path: Path) -> None:
    path = tmp_path / "AAPL" / "AAPL_2024-08.csv"
    cols = list(feed_module.COLUMNS)

    def frame(day: date, price: float, minutes: int = 3) -> pd.DataFrame:
        open_ = datetime(day.year, day.month, day.day, 9, 30)
        stamps = [open_ + timedelta(minutes=i) for i in range(minutes)]
        return pd.DataFrame({"date": pd.to_datetime(stamps), "time": [int(s.timestamp()) for s in stamps],
                             "open": price, "high": price + 1, "low": price - 1, "close": price,
                             "volume": 7}).astype({"time": "int64", "volume": "int64"})[cols]

    first, changed = merge_day(path, date(2024, 8, 14), frame(date(2024, 8, 14), 10.0))
    assert changed and len(first) == 3
    feed_module.write_month_file(path, first)
    second, changed = merge_day(path, date(2024, 8, 12), frame(date(2024, 8, 12), 20.0))
    assert changed and list(second["date"].dt.day) == [12, 12, 12, 14, 14, 14]  # inserted before
    feed_module.write_month_file(path, second)
    same, changed = merge_day(path, date(2024, 8, 14), frame(date(2024, 8, 14), 10.0))
    assert not changed and len(same) == 6
    again, changed = merge_day(path, date(2024, 8, 14), frame(date(2024, 8, 14), 11.0, minutes=2))
    assert changed and len(again) == 5 and again["open"].tolist()[-2:] == [11.0, 11.0]
    assert again["date"].is_monotonic_increasing and not again["date"].duplicated().any()


def test_damaged_month_file_fails_the_day_and_is_left_alone(out: Path) -> None:
    path = month_file_path(out, "AAPL", 2024, 8)
    path.parent.mkdir(parents=True)
    path.write_text("date,time,open,high,low,close,volume\n"
                    "2024-08-13 09:31:00,1,1,2,0.5,1,1\n"
                    "2024-08-13 09:30:00,1,1,2,0.5,1,1\n", encoding="utf-8")
    before = path.read_bytes()
    with make_rig() as rig:
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 13), out)
    sym = result.symbols["AAPL"]
    assert sym.failed == 1 and sym.failures[0].reason == "damaged_file"
    assert path.read_bytes() == before


# -- layout, manifest, setup --------------------------------------------------------------------


def test_legacy_single_file_is_a_setup_error_before_anything_is_sent(out: Path) -> None:
    out.mkdir()
    (out / "_AAPL_offlinedata_").write_text("x", encoding="utf-8")
    with make_rig() as rig, pytest.raises(FetchSetupError, match="single file"):
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out)
    assert rig.fake.calls_named("reqCurrentTime") == []
    assert rig.fake.calls_named("reqHistoricalData") == []


def test_both_layouts_is_a_setup_error_before_anything_is_sent(out: Path) -> None:
    month = month_file_path(out, "MSFT", 2024, 8)
    month.parent.mkdir(parents=True)
    month.write_text("x", encoding="utf-8")
    (out / "_MSFT_offlinedata_").write_text("x", encoding="utf-8")
    with make_rig() as rig, pytest.raises(FetchSetupError, match="both"):
        rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 12), out)
    assert rig.fake.calls_named("reqCurrentTime") == []
    assert not (out / "AAPL").exists()


@pytest.mark.parametrize("symbols", [[], ["AAPL", "AAPL"], ["../x"], ["NUL"], "AAPL"])
def test_bad_symbols_are_a_setup_error(out: Path, symbols: Any) -> None:
    with make_rig() as rig, pytest.raises(FetchSetupError):
        rig.run(symbols, date(2024, 8, 12), date(2024, 8, 12), out)
    assert rig.fake.calls_named("reqCurrentTime") == []


def test_start_after_end_and_bad_timeouts_are_setup_errors(out: Path) -> None:
    with make_rig() as rig:
        with pytest.raises(FetchSetupError):
            rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 12), out)
        with pytest.raises(FetchSetupError):
            rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out, request_timeout=float("nan"))
        with pytest.raises(FetchSetupError):
            rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out, pacing_backoff=0)
    assert rig.fake.calls_named("reqCurrentTime") == []


@pytest.mark.parametrize("damage", ["conid", "garbage"])
def test_unusable_manifest_stops_that_symbol_only(out: Path, damage: str) -> None:
    folder = out / "AAPL"
    folder.mkdir(parents=True)
    mfile = folder / "manifest.json"
    if damage == "garbage":
        mfile.write_text("{not json", encoding="utf-8")
    else:
        mfile.write_text(json.dumps({
            "version": 1, "symbol": "AAPL", "conId": 111, "data_zone": "America/New_York",
            "use_rth": 1, "what_to_show": "TRADES", "bar_size": "1 min", "days": {}}), encoding="utf-8")
    original = mfile.read_bytes()
    with make_rig() as rig:
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 13), out)
    aapl, msft = result.symbols["AAPL"], result.symbols["MSFT"]
    assert aapl.state == "manifest" and "unusable" in aapl.message and "never deleted" in aapl.message
    assert rig.days_requested(CON_IDS["AAPL"]) == []  # nothing fetched for it
    assert msft.state == "done" and msft.ok == 2  # the run went on
    assert (folder / "manifest.json.unusable").read_bytes() == original  # set aside, not deleted
    assert not result.complete


def test_not_closed_days_are_reported_not_fetched(out: Path) -> None:
    now = datetime(2024, 8, 14, 21, 0, tzinfo=UTC)  # Aug 14 closed at 20:00 UTC, Aug 15 and 16 not
    with make_rig(server_now=now) as rig:
        result = rig.run(["AAPL"], date(2024, 8, 13), date(2024, 8, 16), out)
    assert result.not_closed == [date(2024, 8, 15), date(2024, 8, 16)]
    assert rig.days_requested() == [date(2024, 8, 13), date(2024, 8, 14)]
    assert result.symbols["AAPL"].not_closed == 2 and result.complete
    kinds = [e.kind for e in rig.events]
    assert "not_closed" in kinds
    assert result.estimate is not None and result.estimate.not_closed == 2


def test_no_server_time_fails_the_run_before_any_history_request(out: Path) -> None:
    with make_rig(server_now=None) as rig:
        rig.clock.tick = SimClock.FAST
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 13), out, server_time_timeout=0.2)
    assert result.fatal is not None and "server time" in result.fatal
    assert not result.complete and rig.fake.calls_named("reqHistoricalData") == []
    assert rig.fake.calls_named("reqContractDetails") == []
    assert not (out / "AAPL").exists()


def test_missing_historical_farm_fails_the_run_before_any_history_request(out: Path) -> None:
    with make_rig(farm=False) as rig:
        rig.clock.tick = SimClock.FAST
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 13), out, farm_timeout=0.2)
    assert result.fatal is not None and "farm" in result.fatal
    assert rig.fake.calls_named("reqHistoricalData") == []
    assert rig.fake.calls_named("reqContractDetails") == []


def test_resolve_failure_fails_the_symbol_and_the_run_continues(out: Path) -> None:
    with make_rig() as rig:
        def handler(req_id: int, contract: Contract) -> list[Played]:
            if contract.symbol == "AAPL":
                return [error_played(req_id, 200, "No security definition has been found"),
                        ("contractDetailsEnd", req_id)]
            return [("contractDetails", req_id, details(contract.symbol)), ("contractDetailsEnd", req_id)]

        rig.fake.respond("reqContractDetails", handler)
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 13), out)
    assert result.symbols["AAPL"].state == "resolve_failed" and "200" in result.symbols["AAPL"].message
    assert result.symbols["MSFT"].ok == 2 and not result.complete
    assert not (out / "AAPL").exists()


def test_closed_connection_while_resolving_stops_the_run(out: Path) -> None:
    with make_rig() as rig:
        def handler(req_id: int, contract: Contract) -> list[Played]:
            rig.fake.drop("connectionClosed")
            return []

        rig.fake.respond("reqContractDetails", handler)
        rig.clock.tick = SimClock.FAST
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 13), out, resolve_timeout=20.0)
    assert result.connection_lost and result.symbols["MSFT"].state == "not_started"
    assert rig.days_requested() == []


def test_a_lost_connection_during_a_pause_is_noticed(out: Path) -> None:
    day = date(2024, 8, 13)
    count = {"n": 0}

    with make_rig() as rig:
        rig.script(day, error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))

        def sleep(seconds: float) -> None:
            rig.clock.sleep(seconds)
            if seconds == 5.0:
                count["n"] += 1
                if count["n"] == 3:
                    rig.fake.drop("connectionClosed")
                    for _ in range(2000):
                        if rig.conn.state.value == "closed":
                            break
                        time.sleep(0.001)

        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out, sleep=sleep)
    assert result.connection_lost and result.stop_reason == "connection closed"
    assert count["n"] == 3  # not 120 slices
    assert rig.days_requested() == [date(2024, 8, 12), day]


def test_interrupt_during_a_wait_keeps_what_is_finished_and_writes_gaps(out: Path) -> None:
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        rig.clock.sleep(seconds)

    with make_rig() as rig, pytest.raises(KeyboardInterrupt):
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out, sleep=sleep)
    assert manifest_days(out) == {"2024-08-12": "ok"}
    assert (out / "AAPL" / "gaps.csv").is_file()
    assert len(rig.fake.calls_named("reqHistoricalData")) == 1


# -- the pacer ----------------------------------------------------------------------------------


class SpyPacer(Pacer):
    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__(clock)
        self.log: list[tuple[str, Any]] = []

    def reserve(self, contract: int, request: str) -> Any:
        self.log.append(("reserve", (contract, request)))
        return super().reserve(contract, request)

    def sent(self, reservation: Any) -> float:
        self.log.append(("sent", None))
        return super().sent(reservation)

    def cancel(self, reservation: Any) -> None:
        self.log.append(("cancel", None))
        super().cancel(reservation)


def test_one_pacer_for_the_whole_run_one_reservation_at_a_time(out: Path) -> None:
    with make_rig() as rig:
        pacer = SpyPacer(rig.clock)
        rig.script(date(2024, 8, 13), error_behaviour(162, PACING_TEXT), lambda r, d, s: full_day(r, d, s))
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 14), out, pacer=pacer)
    assert result.complete
    names = [n for n, _ in pacer.log]
    assert names == ["reserve", "sent"] * len(rig.requests)  # strictly alternating, retries included
    for _, (con_id, key) in (item for item in pacer.log if item[0] == "reserve"):
        assert type(con_id) is int and key.startswith(f"{con_id}|")


def test_a_send_that_did_not_happen_cancels_the_reservation(out: Path) -> None:
    with make_rig() as rig:
        pacer = SpyPacer(rig.clock)

        def refuse(*a: Any, **k: Any) -> int:
            raise RuntimeError("cannot send")

        rig.md.request_history = refuse  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="cannot send"):
            rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out, pacer=pacer)
    assert [n for n, _ in pacer.log] == ["reserve", "cancel"]


def test_a_request_sent_before_its_time_stops_the_run(out: Path) -> None:
    with make_rig() as rig:
        clock_value = [100.0]
        rig.clock.now = 0.0
        # a sleep that does nothing and a clock that stands still: the second request is early
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out,
                         clock=lambda: clock_value[0], sleep=lambda s: None)
    assert result.fatal is not None and "before its allowed time" in result.fatal
    assert len(rig.days_requested()) == 2
    assert len(rig.fake.calls_named("cancelHistoricalData")) <= 1


def test_the_end_time_is_checked_against_the_pacer_key(out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_module, "format_end_date_time", lambda moment: "20240101-00:00:00")
    with make_rig() as rig:
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out)
    assert result.fatal is not None and "end time mismatch" in result.fatal
    assert rig.fake.calls_named("reqHistoricalData") == []


# -- what the owner will look at ----------------------------------------------------------------


def test_first_day_and_ib_error_texts_are_observable(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 13), error_behaviour(354, "Requested market data is not subscribed."))
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 14), out)
    first = result.symbols["AAPL"].first_day
    assert first is not None and first.day == date(2024, 8, 12) and first.status == "ok"
    assert (first.bars, first.raw_count) == (390, 390)
    assert first.first_bar == datetime(2024, 8, 12, 9, 30) and first.last_bar == datetime(2024, 8, 12, 15, 59)
    assert first.raw_first == first.first_bar and first.first_volume == 1000
    errors = [e for e in result.ib_errors if not e.notice]
    assert [(e.symbol, e.day, e.code, e.text) for e in errors] == [
        ("AAPL", date(2024, 8, 13), 354, "Requested market data is not subscribed.")]
    kinds = [e.kind for e in rig.events]
    assert kinds.count("first_day") == 1 and "ib_error" in kinds
    event = next(e for e in rig.events if e.kind == "first_day")
    assert event.detail["first_volume"] == 1000 and event.detail["bars"] == 390
    assert event.detail["first_bar"] == "2024-08-12T09:30:00"
    err_event = next(e for e in rig.events if e.kind == "ib_error")
    assert err_event.detail["text"] == "Requested market data is not subscribed."


def test_first_day_of_a_failed_first_day_is_still_reported(out: Path) -> None:
    with make_rig() as rig:
        rig.script(date(2024, 8, 12), error_behaviour(354, "nope"))
        result = rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 13), out)
    first = result.symbols["AAPL"].first_day
    assert first is not None and first.status == "failed" and first.bars == 0 and first.first_bar is None


def test_estimate_before_the_first_request_and_progress_order(out: Path) -> None:
    with make_rig() as rig:
        result = rig.run(["AAPL", "MSFT"], date(2024, 8, 12), date(2024, 8, 16), out)
    estimate = result.estimate
    assert estimate is not None and (estimate.symbols, estimate.days, estimate.requests) == (2, 5, 10)
    assert estimate.min_seconds == estimate_duration(10)
    kinds = [e.kind for e in rig.events]
    assert kinds.index("estimate") < kinds.index("farm_ready") < kinds.index("day")
    assert kinds[-1] == "done" and "server_time" in kinds
    assert "requests" in estimate.describe()


def test_estimate_of_a_year_for_three_symbols() -> None:
    seconds = estimate_duration(756)
    assert 2 * 3600 + 5 * 60 <= seconds <= 2 * 3600 + 8 * 60  # about 2 h 6 min
    assert estimate_duration(0) == 0.0 and estimate_duration(1) == 0.0
    assert estimate_duration(2) == 10.0
    with pytest.raises(ValueError):
        estimate_duration(-1)


def test_plan_matches_the_driver(out: Path) -> None:
    plan = plan_range(date(2024, 8, 12), date(2024, 8, 16), SERVER_NOW)
    with make_rig() as rig:
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 16), out)
    assert rig.days_requested() == plan.days


def test_progress_is_the_only_output(out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with make_rig() as rig:
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_fetch_module_has_no_environment_socket_or_ibapi_import() -> None:
    source = Path(fetch_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            imported.add(node.module if top == "MainStoinker" else top)
    assert not imported & {"os", "socket", "ssl", "asyncio", "http", "ibapi", "subprocess", "threading"}
    assert "environ" not in source and "getenv" not in source
    assert "print(" not in source
    assert not any(m.startswith("MainStoinker.ib.orders") for m in imported)
    assert all(m.startswith("MainStoinker.engine.feed") or m.startswith("MainStoinker.ib")
               or not m.startswith("MainStoinker") for m in imported)


def test_manifest_identity_is_what_the_driver_fetched(out: Path) -> None:
    with make_rig() as rig:
        rig.run(["AAPL"], date(2024, 8, 12), date(2024, 8, 12), out)
    raw = json.loads(manifest_path(out, "AAPL").read_text(encoding="utf-8"))
    assert (raw["symbol"], raw["conId"], raw["data_zone"], raw["use_rth"], raw["what_to_show"],
            raw["bar_size"]) == ("AAPL", CON_IDS["AAPL"], "America/New_York", 1, "TRADES", "1 min")
    assert raw["days"]["2024-08-12"]["bars"] == 390
    assert raw["days"]["2024-08-12"]["fetched_utc"].startswith("2024-08-20T12:00:")  # IB's clock + elapsed


# -- end to end ---------------------------------------------------------------------------------


def committed_rows(symbol: str) -> pd.DataFrame:
    source = Path(feed_module.__file__).resolve().parents[2] / "OfflineData" / f"_{symbol}_offlinedata_"
    return pd.read_csv(source, index_col=0)


@pytest.mark.slow
@pytest.mark.parametrize("fills", ["legacy", "realistic"])
def test_fetched_month_files_backtest_like_the_committed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fills: str
) -> None:
    committed = committed_rows("AAPL")
    by_day: dict[date, pd.DataFrame] = {
        d: g for d, g in committed.groupby(pd.to_datetime(committed["date"]).dt.date)}
    start, end = min(by_day), max(by_day)

    def from_committed(req_id: int, day: date, symbol: str) -> list[Played]:
        rows = by_day[day]
        bars = [("historicalData", req_id, make_bar(int(r.time), r.open, r.high, r.low, r.close,
                                                    Decimal(int(r.volume)))) for r in rows.itertuples()]
        return [*bars, end_played(req_id)]

    fetched = tmp_path / "fetched"
    with make_rig(server_now=datetime(2024, 8, 28, 12, 0, tzinfo=UTC)) as rig:
        for day in by_day:
            rig.script(day, from_committed)
        result = rig.run(["AAPL"], start, end, fetched)
    assert result.complete and result.symbols["AAPL"].ok == len(by_day) == 7

    committed_dir = tmp_path / "committed"
    committed_dir.mkdir()
    shutil.copy(Path(feed_module.__file__).resolve().parents[2] / "OfflineData" / "_AAPL_offlinedata_",
                committed_dir / "_AAPL_offlinedata_")
    algos = tmp_path / "algos.json"
    algos.write_text(json.dumps([{"ID": "EMACrossing", "data": [{"idname": "AAPL-10/30EMA", "ticker": "AAPL",
                                  "short": "10", "long": "30"}]}]), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    outs = {}
    for name, data in (("fetched", fetched), ("committed", committed_dir)):
        target = tmp_path / f"out_{name}"
        extra = ["--fills", "realistic"] if fills == "realistic" else []
        code = cli_main(["backtest", "--algos", str(algos), "--data", str(data), "--out", str(target),
                         "--quiet", *extra])
        assert code == 0
        outs[name] = target
    assert (outs["fetched"] / "trades.json").read_bytes() == (outs["committed"] / "trades.json").read_bytes()
    trades = json.loads((outs["fetched"] / "trades.json").read_text(encoding="utf-8"))
    assert trades  # the comparison is not between two empty runs
    if fills == "realistic":
        fetched_ledger = (outs["fetched"] / "ledger.json").read_bytes()
        assert fetched_ledger == (outs["committed"] / "ledger.json").read_bytes()
