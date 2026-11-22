"""Further probes of `GET /api/runs/{id}/bars`: real offline data, odd data states, blocking, cost."""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import MainStoinker.api.routes_bars as routes_bars
from MainStoinker.engine.feed import file_fingerprint, offline_data_path, read_offline_bars
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from tests.api_support import NO_ENV, make_client
from tests.test_api_bars import store_run, write_bars

REPO_DATA = Path(__file__).resolve().parent.parent / "OfflineData"


def store_real_run(db: Path, directory: Path, symbols: tuple[str, ...] = ("AAPL", "MSFT", "TSLA"),
                   last_bar: datetime | None = None, fingerprints: bool = True) -> int:
    files = tuple((s, *file_fingerprint(offline_data_path(directory, s))) for s in symbols) \
        if fingerprints else ()
    data = DataMeta(source="offline", directory=str(directory), symbols=symbols,
                    first_bar=None, last_bar=last_bar, files=files)
    with SqliteStore(db, env=NO_ENV) as store:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data))
        store.end_run(run_id, "completed")
    return run_id


@pytest.fixture
def real_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "real"
    shutil.copytree(REPO_DATA, directory)
    return directory


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "TSLA"])
def test_real_file_windows_equal_csv_rows(tmp_path: Path, real_dir: Path, symbol: str) -> None:
    frame = read_offline_bars(offline_data_path(real_dir, symbol))
    first, last = frame["date"].iloc[0].to_pydatetime(), frame["date"].iloc[-1].to_pydatetime()
    store_real_run(tmp_path / "runs.db", real_dir, last_bar=last)
    client = make_client(tmp_path / "runs.db")
    mid = frame["date"].iloc[len(frame) // 2].to_pydatetime()
    windows = [(first, first), (last, last), (first, first + timedelta(minutes=30)),
               (mid - timedelta(minutes=5), mid + timedelta(minutes=5)),
               (last - timedelta(hours=1), last), (first - timedelta(days=1), first)]
    for lo, hi in windows:
        body = client.get("/api/runs/1/bars", params={
            "symbol": symbol, "start": lo.isoformat(), "end": hi.isoformat()}).json()
        rows = frame[(frame["date"] >= lo) & (frame["date"] <= hi)]
        assert body["available"] is True and body["reason"] is None
        assert [b["time"] for b in body["bars"]] == [str(d) for d in rows["date"]]
        for bar, (_, row) in zip(body["bars"], rows.iterrows(), strict=True):
            assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (
                row["open"], row["high"], row["low"], row["close"], row["volume"])
        assert len(body["bars"]) >= 1
    # default window: the calendar day of last_bar, all of that day's bars
    body = client.get("/api/runs/1/bars", params={"symbol": symbol}).json()
    day = frame[frame["date"].dt.date == last.date()]
    assert [b["time"] for b in body["bars"]] == [str(d) for d in day["date"]]


def test_window_without_bars_is_available_and_empty(tmp_path: Path, real_dir: Path) -> None:
    store_real_run(tmp_path / "runs.db", real_dir, last_bar=datetime(2024, 8, 16, 12))
    body = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "1999-01-01T00:00", "end": "1999-01-01T01:00"}).json()
    assert body == {"symbol": "AAPL", "available": True, "reason": None, "bars": []}


def test_run_with_no_last_bar_needs_a_window(tmp_path: Path, real_dir: Path) -> None:
    store_real_run(tmp_path / "runs.db", real_dir, last_bar=None)
    client = make_client(tmp_path / "runs.db")
    response = client.get("/api/runs/1/bars", params={"symbol": "AAPL"})
    assert response.status_code == 422 and str(tmp_path) not in response.text
    ok = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-08-16T06:30", "end": "2024-08-16T06:32"})
    assert ok.status_code == 200 and len(ok.json()["bars"]) == 3


@pytest.mark.parametrize("symbol", [
    "%2e%2e", "..%2fx", "a%2fb", "a%5cb", "%41%41%50%4c", "AAPL%00", "AAPL ", " AAPL", "A" * 5000,
    "AAPL%0A", "%D0%90APL", "C:\\Windows", "//server/share", "aapl"])
def test_odd_symbols_never_leave_the_run(tmp_path: Path, real_dir: Path, symbol: str) -> None:
    store_real_run(tmp_path / "runs.db", real_dir, symbols=("AAPL",),
                   last_bar=datetime(2024, 8, 16, 12))
    client = make_client(tmp_path / "runs.db")
    for url in (f"/api/runs/1/bars?symbol={symbol}", f"/api/runs/1/bars?symbol=AAPL&symbol={symbol}"):
        response = client.get(url)
        if symbol == "%41%41%50%4c":  # decodes to AAPL: that is a legitimate request
            assert response.status_code == 200
            continue
        assert response.status_code in (404, 422), (symbol, response.status_code)
        assert str(tmp_path) not in response.text
        assert "real" not in response.text.lower().replace("unreal", "")


def test_symbol_missing_param_422(tmp_path: Path, real_dir: Path) -> None:
    store_real_run(tmp_path / "runs.db", real_dir)
    assert make_client(tmp_path / "runs.db").get("/api/runs/1/bars").status_code == 422


def test_file_replaced_by_junk_or_empty_or_directory(tmp_path: Path, data_dir_factory) -> None:
    for kind in ("junk", "empty", "dir", "header_only"):
        directory = data_dir_factory()
        db = tmp_path / f"{kind}.db"
        store_run(db, directory, files=False)  # unverified path reads the file
        path = offline_data_path(directory, "AAPL")
        path.unlink()
        if kind == "junk":
            path.write_bytes(b"\x00\xff\xfe garbage\n" * 50)
        elif kind == "empty":
            path.write_bytes(b"")
        elif kind == "header_only":
            path.write_text(",date,time,open,high,low,close,volume\n")
        else:
            path.mkdir()
        response = make_client(db).get("/api/runs/1/bars", params={"symbol": "AAPL"})
        assert response.status_code == 200, kind
        body = response.json()
        assert (body["available"], body["reason"], body["bars"]) == (False, "missing", []), kind
        assert str(tmp_path) not in response.text


def test_directory_replaced_by_file(tmp_path: Path, data_dir_factory) -> None:
    directory = data_dir_factory()
    db = tmp_path / "runs.db"
    store_run(db, directory)
    shutil.rmtree(directory)
    directory.write_text("i am a file")
    body = make_client(db).get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
    assert (body["available"], body["reason"]) == (False, "missing")


def test_fingerprint_is_of_the_symbols_own_file(tmp_path: Path, data_dir_factory) -> None:
    directory = data_dir_factory()
    write_bars(directory, "MSFT")
    sha, size = file_fingerprint(offline_data_path(directory, "AAPL"))
    data = DataMeta(source="offline", directory=str(directory), symbols=("AAPL", "MSFT"),
                    first_bar=None, last_bar=datetime(2024, 1, 10, 9, 35),
                    files=(("AAPL", sha, size), ("MSFT", "0" * 64, size)))
    with SqliteStore(tmp_path / "runs.db", env=NO_ENV) as store:
        store.end_run(store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data)), "completed")
    client = make_client(tmp_path / "runs.db")
    assert client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()["reason"] is None
    assert client.get("/api/runs/1/bars", params={"symbol": "MSFT"}).json()["reason"] == "changed"


def test_repeated_requests_leak_no_state(tmp_path: Path, data_dir_factory) -> None:
    directory = data_dir_factory()
    store_run(tmp_path / "runs.db", directory)
    client = make_client(tmp_path / "runs.db")
    first = client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
    for _ in range(3):
        assert client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json() == first
    other = make_client(tmp_path / "runs.db")
    assert other.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json() == first


def test_bar_cap_with_dense_file(tmp_path: Path) -> None:
    directory = tmp_path / "dense"
    directory.mkdir()
    lines = [",date,time,open,high,low,close,volume"]
    stamp = datetime(2024, 1, 2)
    for n in range(routes_bars.MAX_BARS + 1):  # 10,001 one-minute bars inside one day
        t = stamp + timedelta(seconds=n * 8)
        lines.append(f"{n},{t:%Y-%m-%d %H:%M:%S},{n},1.0,2.0,0.5,1.5,{n}")
    offline_data_path(directory, "AAPL").write_text("\n".join(lines) + "\n")
    store_run(tmp_path / "runs.db", directory, last_bar=datetime(2024, 1, 2, 12))
    client = make_client(tmp_path / "runs.db")
    over = client.get("/api/runs/1/bars", params={"symbol": "AAPL"})
    assert over.status_code == 422 and str(tmp_path) not in over.text
    exact = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-02T00:00:00", "end": "2024-01-02T22:13:12"})
    assert exact.status_code == 200 and len(exact.json()["bars"]) == routes_bars.MAX_BARS


def test_slow_read_does_not_block_other_requests(tmp_path: Path, data_dir_factory,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    directory = data_dir_factory()
    store_run(tmp_path / "runs.db", directory)
    client = make_client(tmp_path / "runs.db")
    real = routes_bars.read_offline_bars

    def slow(path: Path):  # type: ignore[no-untyped-def]
        time.sleep(1.5)
        return real(path)

    monkeypatch.setattr(routes_bars, "read_offline_bars", slow)
    result: dict[str, float] = {}

    def call() -> None:
        started = time.perf_counter()
        client.get("/api/runs/1/bars", params={"symbol": "AAPL"})
        result["bars"] = time.perf_counter() - started

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.3)
    started = time.perf_counter()
    other = client.get("/api/health") if False else client.get("/")
    quick = time.perf_counter() - started
    thread.join()
    assert other.status_code in (200, 404)
    assert result["bars"] >= 1.5 and quick < 1.0, (quick, result)


def test_locked_database_gives_fixed_503_quickly(tmp_path: Path, data_dir_factory) -> None:
    import sqlite3
    directory = data_dir_factory()
    db = tmp_path / "runs.db"
    store_run(db, directory)
    client = make_client(db)
    lock = sqlite3.connect(db, isolation_level=None)
    try:
        lock.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock.execute("BEGIN EXCLUSIVE")
        started = time.perf_counter()
        response = client.get("/api/runs/1/bars", params={"symbol": "AAPL"})
        elapsed = time.perf_counter() - started
    finally:
        lock.close()
    assert response.status_code == 503 and elapsed < 5
    assert response.json() == {"detail": "the run database is unavailable"}


@pytest.mark.xfail(strict=True, raises=ValueError,
                   reason="D9 defect: offline_data_path raises ValueError (500) for a stored symbol "
                          "that is not a plain ticker; the route should answer 404 or missing")
@pytest.mark.parametrize("stored", ["a/b", "..", "", "a\\b"])
def test_run_with_an_invalid_stored_symbol_is_not_a_500(tmp_path: Path, data_dir_factory,
                                                         stored: str) -> None:
    directory = data_dir_factory()
    data = DataMeta(source="offline", directory=str(directory), symbols=(stored,), first_bar=None,
                    last_bar=datetime(2024, 1, 10, 9, 35), files=())
    with SqliteStore(tmp_path / "runs.db", env=NO_ENV) as store:
        store.end_run(store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data)), "completed")
    response = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={"symbol": stored})
    assert response.status_code in (200, 404, 422)


def test_no_path_or_user_in_headers_or_bodies(tmp_path: Path, real_dir: Path) -> None:
    store_real_run(tmp_path / "runs.db", real_dir, last_bar=datetime(2024, 8, 16, 12))
    client = make_client(tmp_path / "runs.db")
    user = os.environ.get("USERNAME", "")
    for params in ({"symbol": "AAPL"}, {"symbol": "NOPE"}, {"symbol": "AAPL", "start": "x", "end": "y"},
                   {"symbol": "AAPL", "start": "2024-08-16T06:30"}):
        response = client.get("/api/runs/1/bars", params=params)
        blob = response.text + str(dict(response.headers))
        assert str(tmp_path) not in blob and "real" not in blob.lower().replace("unreal", "")
        if user:
            assert user not in blob


def test_cost_numbers_for_current_files(tmp_path: Path, real_dir: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    store_real_run(tmp_path / "runs.db", real_dir, last_bar=datetime(2024, 8, 16, 12))
    client = make_client(tmp_path / "runs.db")
    for label, params in (("default day", {"symbol": "AAPL"}),
                          ("max window", {"symbol": "AAPL", "start": "2024-08-10T00:00",
                                          "end": "2024-08-16T23:59:59"})):
        started = time.perf_counter()
        response = client.get("/api/runs/1/bars", params=params)
        with capsys.disabled():
            print(f"COST {label}: {time.perf_counter() - started:.3f}s, {len(response.content)} bytes, "
                  f"{len(response.json()['bars'])} bars")
    assert response.status_code == 200


@pytest.fixture
def data_dir_factory(tmp_path: Path):  # type: ignore[no-untyped-def]
    counter = iter(range(1000))

    def make() -> Path:
        directory = tmp_path / f"bars-data-{next(counter)}"
        directory.mkdir()
        write_bars(directory)
        return directory

    return make


@pytest.mark.skipif(os.name != "nt", reason="byte-range locks block readers on Windows only")
def test_file_locked_by_another_handle_reports_missing_not_500(tmp_path: Path,
                                                               data_dir_factory) -> None:
    import msvcrt
    directory = data_dir_factory()
    db = tmp_path / "runs.db"
    store_run(db, directory)
    path = offline_data_path(directory, "AAPL")
    with path.open("r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 16)
        try:
            response = make_client(db).get("/api/runs/1/bars", params={"symbol": "AAPL"})
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 16)
    assert response.status_code == 200
    body = response.json()
    assert (body["available"], body["bars"]) == (False, []) and str(tmp_path) not in response.text


@pytest.mark.parametrize("bound", ["٢٠٢٤-01-02T09:30", "2024-01-02T09:30\n",
                                   "2024-W01-1T09:30", "2024-001T09:30", "2024-01-02t09:30"])
def test_unusual_iso_forms_are_rejected_by_the_route(tmp_path: Path, data_dir_factory,
                                                     bound: str) -> None:
    store_run(tmp_path / "runs.db", data_dir_factory())
    response = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": bound, "end": "2024-01-03T09:30"})
    assert response.status_code == 422 and bound.strip() not in response.text
