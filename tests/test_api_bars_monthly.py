"""`GET /api/runs/{id}/bars` over a run whose data directory holds per-month bar files."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from MainStoinker.engine.feed import (
    month_file_path,
    read_symbol_bars,
    symbol_fingerprint,
    write_month_file,
)
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from tests.api_support import NO_ENV, make_client

DAYS = [datetime(2024, 8, 29), datetime(2024, 8, 30), datetime(2024, 9, 3), datetime(2024, 9, 4)]
PER_DAY = 6


def month_frame(days: list[datetime]) -> pd.DataFrame:
    rows = []
    for day in days:
        for minute in range(PER_DAY):
            stamp = day + pd.Timedelta(hours=9, minutes=30 + minute)
            n = len(rows) + 1
            rows.append((stamp, int(stamp.timestamp()), 100 + n / 8, 101 + n / 8, 99 + n / 8,
                         100.5 + n / 8, 1000 + n))
    return pd.DataFrame(rows, columns=["date", "time", "open", "high", "low", "close", "volume"])


def write_folder(directory: Path) -> None:
    frame = month_frame(DAYS)
    for month in (8, 9):
        part = frame[frame["date"].dt.month == month].reset_index(drop=True)
        write_month_file(month_file_path(directory, "AAPL", 2024, month), part)


def store_run(db: Path, directory: Path, *, files: bool = True) -> int:
    recorded = (("AAPL", *symbol_fingerprint(directory, "AAPL")),) if files else ()
    data = DataMeta(source="offline", directory=str(directory), symbols=("AAPL",),
                    first_bar=DAYS[0], last_bar=DAYS[-1] + pd.Timedelta(hours=9, minutes=35),
                    files=recorded)
    with SqliteStore(db, env=NO_ENV) as store:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data))
        store.end_run(run_id, "completed")
    return run_id


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bars-data"
    write_folder(directory)
    return directory


def get(client: TestClient, **params: str) -> dict:
    response = client.get("/api/runs/1/bars", params={"symbol": "AAPL", **params})
    assert response.status_code == 200
    return response.json()


def test_bars_route_serves_a_month_folder_run(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    client = make_client(tmp_path / "runs.db")
    frame = read_symbol_bars(data_dir, "AAPL")
    # A window across the month end: last August day and first September day.
    body = get(client, start="2024-08-30T00:00:00", end="2024-09-03T23:59:59")
    assert body["available"] is True and body["reason"] is None
    rows = frame[(frame["date"] >= "2024-08-30") & (frame["date"] < "2024-09-04")]
    assert len(body["bars"]) == len(rows) == 2 * PER_DAY
    assert [bar["time"] for bar in body["bars"]] == [str(d) for d in rows["date"]]
    assert [bar["close"] for bar in body["bars"]] == rows["close"].tolist()


def test_bars_route_unverified_without_recorded_files(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir, files=False)
    body = get(make_client(tmp_path / "runs.db"), start="2024-09-03T00:00:00",
               end="2024-09-03T23:59:59")
    assert body["available"] is True and body["reason"] == "unverified"
    assert len(body["bars"]) == PER_DAY


def test_bars_route_reports_changed_month_folder(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    client = make_client(tmp_path / "runs.db")
    assert get(client)["available"] is True
    september = month_file_path(data_dir, "AAPL", 2024, 9)
    changed = month_frame(DAYS[2:3])  # one day instead of two
    write_month_file(september, changed)
    body = get(client)
    assert body["available"] is False and body["reason"] == "changed" and body["bars"] == []
    assert str(data_dir) not in str(body)
    # A month added or removed also changes the folder; a missing folder is "missing".
    september.unlink()
    assert get(client)["reason"] == "changed"
    for path in month_file_path(data_dir, "AAPL", 2024, 8).parent.iterdir():
        path.unlink()
    assert get(client)["reason"] == "missing"
