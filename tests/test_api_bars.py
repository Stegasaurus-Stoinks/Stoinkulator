"""`GET /api/runs/{id}/bars`: window, defaults, limits, symbol check and the availability reasons.

Every run is stored in a database under `tmp_path`, over a small CSV written
next to it in the offline-data format. Nothing reads `os.environ`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import MainStoinker.api.routes_bars as routes_bars
from MainStoinker.engine.feed import file_fingerprint, offline_data_path, read_offline_bars
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from tests.api_support import NO_ENV, make_client

DAYS = [datetime(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10)]  # seven trading days
PER_DAY = 6


def write_bars(directory: Path, symbol: str = "AAPL", days: list[datetime] = DAYS) -> Path:
    path = offline_data_path(directory, symbol)
    lines = [",date,time,open,high,low,close,volume"]
    for day in days:
        for minute in range(PER_DAY):
            stamp = day + timedelta(hours=9, minutes=30 + minute)
            n = len(lines)
            lines.append(f"{n - 1},{stamp:%Y-%m-%d %H:%M:%S},{n},{100 + n / 8},{101 + n / 8},"
                         f"{99 + n / 8},{100.5 + n / 8},{1000 + n}")
    path.write_text("\n".join(lines) + "\n")
    return path


def store_run(db: Path, directory: Path, *, files: bool = True,
              symbols: tuple[str, ...] = ("AAPL",), last_bar: datetime | None = None) -> int:
    """Store one finished run over `directory`; with `files`, record the AAPL fingerprint."""
    recorded = ()
    if files:
        sha, size = file_fingerprint(offline_data_path(directory, "AAPL"))
        recorded = (("AAPL", sha, size),)
    data = DataMeta(source="offline", directory=str(directory), symbols=symbols,
                    first_bar=DAYS[0], last_bar=last_bar or DAYS[-1] + timedelta(hours=9, minutes=35),
                    files=recorded)
    with SqliteStore(db, env=NO_ENV) as store:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data))
        store.end_run(run_id, "completed")
    return run_id


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bars-data"
    directory.mkdir()
    write_bars(directory)
    return directory


@pytest.fixture
def client(tmp_path: Path, data_dir: Path) -> TestClient:
    store_run(tmp_path / "runs.db", data_dir)
    return make_client(tmp_path / "runs.db")


def test_bars_window_matches_csv(client: TestClient, data_dir: Path) -> None:
    frame = read_offline_bars(offline_data_path(data_dir, "AAPL"))
    start, end = datetime(2024, 1, 3, 9, 32), datetime(2024, 1, 4, 9, 31)
    response = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": start.isoformat(), "end": end.isoformat()})
    assert response.status_code == 200
    body = response.json()
    rows = frame[(frame["date"] >= start) & (frame["date"] <= end)]
    assert body["symbol"] == "AAPL" and body["available"] is True and body["reason"] is None
    assert len(body["bars"]) == len(rows) > 0
    for bar, (_, row) in zip(body["bars"], rows.iterrows(), strict=True):
        assert bar == {"time": str(row["date"]), "open": row["open"], "high": row["high"],
                       "low": row["low"], "close": row["close"], "volume": row["volume"]}
        assert isinstance(bar["open"], float) and isinstance(bar["volume"], int)
        assert "T" not in bar["time"] and "+" not in bar["time"] and "Z" not in bar["time"]


def test_default_window_is_last_day(client: TestClient, data_dir: Path) -> None:
    body = client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
    assert len(body["bars"]) == PER_DAY
    assert {bar["time"][:10] for bar in body["bars"]} == {"2024-01-10"}
    assert body["bars"][0]["time"] == "2024-01-10 09:30:00"


def test_window_too_wide_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    wide = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-02T00:00:00", "end": "2024-01-10T23:59:59"})
    assert wide.status_code == 422 and "calendar days" in wide.json()["detail"]
    five = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-02T00:00:00", "end": "2024-01-08T23:59:59"})
    assert five.status_code == 200 and len(five.json()["bars"]) == 5 * PER_DAY
    edge = client.get("/api/runs/1/bars", params={  # 7 calendar days apart fails, whatever the data
        "symbol": "AAPL", "start": "2024-01-02 00:00", "end": "2024-01-09 00:00"})
    assert edge.status_code == 422 and "2024" not in edge.json()["detail"]
    monkeypatch.setattr(routes_bars, "MAX_BARS", 10)
    many = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-02T00:00:00", "end": "2024-01-04T23:59:59"})
    assert many.status_code == 422 and "bars" in many.json()["detail"]


def test_space_separator_and_window_error_detail(client: TestClient) -> None:
    ok = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-10 09:30", "end": "2024-01-10 09:32"})
    assert ok.status_code == 200 and len(ok.json()["bars"]) == 3
    bad = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-02", "end": "2024-01-03T09:30:00"})
    assert bad.status_code == 422 and "start" in bad.json()["detail"]
    assert "2024-01-02" not in bad.json()["detail"]


def test_one_bound_422(client: TestClient) -> None:
    for params in ({"start": "2024-01-02T09:30:00"}, {"end": "2024-01-02T09:30:00"}):
        assert client.get("/api/runs/1/bars", params={"symbol": "AAPL", **params}).status_code == 422


@pytest.mark.parametrize("bound", [
    "2024-01-02T09:30:00Z", "2024-01-02T09:30:00+01:00", "2024-01-02T09:30:00-05:00",
    "2024-01-02", "20240102", "not a time", "2024-13-45T09:30:00", ""])
def test_timezone_or_malformed_bound_422(client: TestClient, bound: str) -> None:
    for params in ({"start": bound, "end": "2024-01-03T09:30:00"},
                   {"start": "2024-01-02T09:30:00", "end": bound}):
        response = client.get("/api/runs/1/bars", params={"symbol": "AAPL", **params})
        assert response.status_code == 422, bound
        assert "\n" not in response.text.strip()


def test_end_before_start_422(client: TestClient) -> None:
    response = client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": "2024-01-03T09:30:00", "end": "2024-01-02T09:30:00"})
    assert response.status_code == 422


def test_unknown_symbol_404(client: TestClient) -> None:
    response = client.get("/api/runs/1/bars", params={"symbol": "NOPE"})
    assert response.status_code == 404
    assert client.get("/api/runs/99/bars", params={"symbol": "AAPL"}).status_code == 404


@pytest.mark.parametrize("symbol", ["../x", "..", "..\\x", "AAPL/../MSFT", "MSFT", "aapl", ""])
def test_symbol_not_in_run_404(client: TestClient, tmp_path: Path, symbol: str) -> None:
    (tmp_path / "x").write_text("a,b\n")
    write_bars(tmp_path, "MSFT")  # a bar file exists next to the data directory, not in the run
    response = client.get("/api/runs/1/bars", params={"symbol": symbol})
    assert response.status_code == 404
    assert str(tmp_path) not in response.text


def test_missing_directory_reports_missing(tmp_path: Path) -> None:
    gone = tmp_path / "soon-gone"
    gone.mkdir()
    write_bars(gone)
    store_run(tmp_path / "runs.db", gone)
    for child in gone.iterdir():
        child.unlink()
    gone.rmdir()
    response = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={"symbol": "AAPL"})
    assert response.status_code == 200
    assert response.json() == {"symbol": "AAPL", "available": False, "reason": "missing",
                               "bars": []}
    assert "soon-gone" not in response.text


def test_missing_file_without_fingerprint_reports_missing(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir, files=False)
    offline_data_path(data_dir, "AAPL").unlink()
    body = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
    assert (body["available"], body["reason"], body["bars"]) == (False, "missing", [])


def test_changed_file_reports_changed(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    path = offline_data_path(data_dir, "AAPL")
    path.write_text(path.read_text().replace("100.", "200.", 1))
    response = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={"symbol": "AAPL"})
    assert response.status_code == 200
    assert response.json() == {"symbol": "AAPL", "available": False, "reason": "changed",
                               "bars": []}


def test_run_without_fingerprint_is_unverified(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir, files=False)
    body = make_client(tmp_path / "runs.db").get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
    assert body["available"] is True and body["reason"] == "unverified"
    assert len(body["bars"]) == PER_DAY


def test_response_contains_no_path(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    client = make_client(tmp_path / "runs.db")
    path = offline_data_path(data_dir, "AAPL")
    texts = [client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).text,
             client.get("/api/runs/1/bars", params={"symbol": "../x"}).text,
             client.get("/api/runs/1/bars", params={"symbol": "AAPL", "start": "2024-01-02"}).text,
             client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).text]
    path.write_text("garbage")
    texts.append(client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).text)  # changed
    path.unlink()
    texts.append(client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).text)  # missing
    for text in texts:
        assert data_dir.name not in text and str(tmp_path) not in text
        assert "offlinedata" not in text and "\\\\" not in text
