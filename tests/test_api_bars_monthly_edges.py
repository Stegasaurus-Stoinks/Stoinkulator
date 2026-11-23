"""More `GET /api/runs/{id}/bars` cases over per-month bar files and mixed layouts."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from MainStoinker.engine.feed import (
    file_fingerprint,
    month_file_path,
    offline_data_path,
    read_symbol_bars,
)
from tests.api_support import make_client
from tests.test_api_bars_monthly import DAYS, PER_DAY, get, month_frame, store_run, write_folder


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bars-data"
    write_folder(directory)
    return directory


def test_window_inside_one_month_and_default_day(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    client = make_client(tmp_path / "runs.db")
    frame = read_symbol_bars(data_dir, "AAPL")
    body = get(client, start="2024-08-29T00:00:00", end="2024-08-29T23:59:59")
    assert body["available"] is True and body["reason"] is None
    assert [b["time"] for b in body["bars"]] == [str(d) for d in frame["date"][:PER_DAY]]
    default = get(client)  # no window: some day of the run, verified
    assert default["available"] is True and default["reason"] is None and default["bars"]
    assert str(data_dir) not in json.dumps(body) + json.dumps(default)


def test_deleted_folder_is_missing_without_a_path(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    for path in sorted(data_dir.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    data_dir.rmdir()
    body = get(make_client(tmp_path / "runs.db"))
    assert body["available"] is False and body["reason"] == "missing" and body["bars"] == []
    assert str(data_dir) not in json.dumps(body)


def test_a_non_month_extra_file_does_not_change_verification(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    (data_dir / "AAPL" / "manifest.json").write_text("{}")
    (data_dir / "AAPL" / "AAPL_2024-09.csv.1.tmp").write_text("x")
    body = get(make_client(tmp_path / "runs.db"))
    assert body["available"] is True and body["reason"] is None


def test_both_layouts_present_is_missing_not_an_error(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir)
    offline_data_path(data_dir, "AAPL").write_text(
        "," + ",".join(month_frame(DAYS[:1]).columns) + "\n"
    )
    body = get(make_client(tmp_path / "runs.db"))
    assert body["available"] is False and body["reason"] == "missing"
    assert str(data_dir) not in json.dumps(body)


def test_both_layouts_present_without_recorded_files_is_missing(tmp_path: Path, data_dir: Path) -> None:
    store_run(tmp_path / "runs.db", data_dir, files=False)
    offline_data_path(data_dir, "AAPL").write_text("x\n")
    body = get(make_client(tmp_path / "runs.db"))
    assert body["available"] is False and body["reason"] == "missing"


def test_a_corrupt_month_with_matching_fingerprint_is_missing(tmp_path: Path, data_dir: Path) -> None:
    """A run stored over a bad month file verifies (same bytes) but cannot be read: missing, no 500."""
    september = month_file_path(data_dir, "AAPL", 2024, 9)
    september.write_text("date,time,open,high,low,close,volume\n2024-08-29 09:30:00,1,2,3,1,2,5\n")
    store_run(tmp_path / "runs.db", data_dir)
    body = get(make_client(tmp_path / "runs.db"))
    assert body["available"] is False and body["reason"] == "missing"


def test_legacy_single_file_run_still_verifies_with_the_old_fingerprint(tmp_path: Path) -> None:
    directory = tmp_path / "legacy"
    directory.mkdir()
    frame = month_frame(DAYS)
    frame.to_csv(offline_data_path(directory, "AAPL"))
    store_run(tmp_path / "runs.db", directory, files=False)
    # Re-store with the fingerprint computed the pre-T7 way (file_fingerprint of the file).
    from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
    from tests.api_support import NO_ENV

    data = DataMeta(source="offline", directory=str(directory), symbols=("AAPL",),
                    first_bar=DAYS[0], last_bar=DAYS[-1] + pd.Timedelta(hours=9, minutes=35),
                    files=(("AAPL", *file_fingerprint(offline_data_path(directory, "AAPL"))),))
    with SqliteStore(tmp_path / "runs2.db", env=NO_ENV) as store:
        run_id = store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data))
        store.end_run(run_id, "completed")
    client = make_client(tmp_path / "runs2.db")
    body = client.get(f"/api/runs/{run_id}/bars", params={"symbol": "AAPL"}).json()
    assert body["available"] is True and body["reason"] is None
