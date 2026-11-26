"""`GET /api/runs/{id}/fvg` (issue 276): the zones of the FVG detector over a run's bars.

Runs are stored in databases under `tmp_path`; the bars are a seeded random walk in whole cents written
in the offline-data format (two days, so the warm-up and the day cut are exercised). The expected zones are
always `detect_fvgs` run directly on the same bars. Nothing reads `os.environ`.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import MainStoinker.api.routes_fvg as routes_fvg
from MainStoinker.engine.feed import file_fingerprint, offline_data_path, read_offline_bars
from MainStoinker.engine.store import DataMeta, RunMeta, SqliteStore
from MainStoinker.indicators.fvg import FvgConfig, detect_fvgs, resample_session
from tests.api_support import NO_ENV, make_client, stored_run_database

DAYS = (datetime(2024, 1, 2), datetime(2024, 1, 3))
PER_DAY = 150  # 09:30 to 11:59
TIMES = ["time", "known_at", "formed_time", "last_change_time", "last_change_known_at", "end_time"]
ZONE_KEYS = {"id", "direction", "top", "bottom", "midpoint", "size", "size_atr", "formed_time", "known_at",
             "state", "touches", "deepest_fill", "last_change_time", "last_change_known_at", "end_time"}


Row = tuple[str, float, float, float, float, int]


def bar_rows(day: datetime, seed: int, *, non_cent: bool = False) -> list[Row]:
    """One day of 1-minute bars: a random walk in cents with wicks, so gaps of both directions appear."""
    rng = random.Random(seed)
    price = 10_000 + seed
    rows = []
    for minute in range(PER_DAY):
        open_ = price
        close = open_ + rng.randint(-40, 40)
        high = max(open_, close) + rng.randint(0, 8)
        low = min(open_, close) - rng.randint(0, 8)
        price = close
        stamp = day + timedelta(hours=9, minutes=30 + minute)
        rows.append((f"{stamp:%Y-%m-%d %H:%M:%S}", open_ / 100, high / 100, low / 100, close / 100,
                     1000 + minute))
    if non_cent:
        t, o, h, lo, c, v = rows[40]
        rows[40] = (t, o, h + 0.003, lo, c, v)
    return rows


def write_bars(directory: Path, *, non_cent: bool = False) -> Path:
    path = offline_data_path(directory, "AAPL")
    lines = [",date,time,open,high,low,close,volume"]
    for seed, day in enumerate(DAYS, start=1):
        for stamp, o, h, lo, c, v in bar_rows(day, seed * 7, non_cent=non_cent and seed == 2):
            n = len(lines)
            lines.append(f"{n - 1},{stamp},{n},{o},{h},{lo},{c},{v}")
    path.write_text("\n".join(lines) + "\n")
    return path


def store_run(db: Path, directory: Path, *, files: bool = True) -> int:
    recorded = ()
    if files:
        sha, size = file_fingerprint(offline_data_path(directory, "AAPL"))
        recorded = (("AAPL", sha, size),)
    data = DataMeta(source="offline", directory=str(directory), symbols=("AAPL",), first_bar=DAYS[0],
                    last_bar=DAYS[-1] + timedelta(hours=11, minutes=59), files=recorded)
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
def client(tmp_path: Path, data_dir: Path):
    store_run(tmp_path / "runs.db", data_dir)
    return make_client(tmp_path / "runs.db")


def frame_of(directory: Path) -> pd.DataFrame:
    return read_offline_bars(offline_data_path(directory, "AAPL"))


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text)


def expected(frame: pd.DataFrame, first: str, last: str, *, minutes: int = 1,
             config: FvgConfig | None = None) -> list[dict[str, Any]]:
    """What the API must answer: the detector over the previous available day, then the bars from midnight of
    `first`'s day to `last`; zones of the previous day are not returned."""
    lo, hi = ts(first), ts(last)
    day0 = lo.normalize()
    earlier = frame[frame["date"] < day0]
    start = earlier["date"].max().normalize() if len(earlier) else day0
    bars = frame[(frame["date"] >= start) & (frame["date"] <= hi)].reset_index(drop=True)
    if minutes > 1:
        bars = resample_session(bars, minutes)
    cfg = config or FvgConfig(bar_minutes=minutes)
    out = []
    for row in detect_fvgs(bars, cfg).itertuples(index=False):
        if row.formed_time < day0:
            continue
        live = row.state in ("open", "touched")
        end = None if live else row.last_change_known_at
        if row.known_at <= hi and (end is None or end >= lo):
            out.append({"id": int(row.id), "direction": row.direction, "top": float(row.top),
                        "bottom": float(row.bottom), "state": row.state, "touches": int(row.touches),
                        "known_at": str(row.known_at), "formed_time": str(row.formed_time),
                        "end_time": None if end is None else str(end),
                        "deepest_fill": float(row.deepest_fill)})
    return sorted(out, key=lambda z: (z["known_at"], z["id"]))


def project(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("id", "direction", "top", "bottom", "state", "touches", "known_at", "formed_time", "end_time",
            "deepest_fill")
    return [{k: z[k] for k in keep} for z in zones]


def get_fvg(client, **params: Any):
    return client.get("/api/runs/1/fvg", params={"symbol": "AAPL", **params})


DAY1 = ("2024-01-02 00:00:00", "2024-01-02 23:59:59")


def test_the_fixture_makes_zones_of_both_directions(data_dir: Path) -> None:
    zones = expected(frame_of(data_dir), "2024-01-02 09:30:00", "2024-01-02 23:59:59")
    assert len(zones) >= 4
    assert {z["direction"] for z in zones} == {"up", "down"}
    assert {z["state"] for z in zones} >= {"filled"} or {z["state"] for z in zones} >= {"invalidated"}


def test_zones_equal_the_detector_run_directly_on_the_same_bars(client, data_dir: Path) -> None:
    response = get_fvg(client, start=DAY1[0], end=DAY1[1])
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL" and body["available"] is True and body["reason"] is None
    assert body["bar_minutes"] == 1 and body["truncated"] is False
    assert body["config"]["min_size"] == 0.05 and body["config"]["min_size_atr"] == 0.25
    assert body["config"]["require_same_direction"] is False
    assert all(set(z) == ZONE_KEYS for z in body["zones"])
    want = expected(frame_of(data_dir), *DAY1)
    assert want and project(body["zones"]) == want
    # a default window is the day of the run's last bar
    default = get_fvg(client).json()
    assert project(default["zones"]) == expected(
        frame_of(data_dir), "2024-01-03 00:00:00", "2024-01-03 23:59:59")
    # the other fields come straight from the detector's row
    row = detect_fvgs(frame_of(data_dir)[lambda f: f["date"] < "2024-01-03"], FvgConfig()).iloc[0]
    first = next(z for z in body["zones"] if z["id"] == int(row.id))
    assert first["midpoint"] == row["midpoint"] and first["size"] == row["size"]
    assert first["size_atr"] == (None if pd.isna(row["size_atr"]) else row["size_atr"])
    assert first["last_change_time"] == str(row.last_change_time)
    assert first["last_change_known_at"] == str(row.last_change_known_at)


def test_a_window_cuts_zones_and_warms_up_from_before_it(client, data_dir: Path) -> None:
    frame = frame_of(data_dir)
    start, end = "2024-01-02 10:00:00", "2024-01-02 10:30:00"
    body = get_fvg(client, start=start, end=end).json()
    want = expected(frame, start, end)
    assert want and project(body["zones"]) == want
    for zone in body["zones"]:
        assert zone["known_at"] <= end
        assert zone["end_time"] is None or zone["end_time"] >= start
    # zones formed before the window that are still live inside it are there
    assert any(z["formed_time"] < start for z in body["zones"])
    # and the ATR window was warm at 10:00: the zones are those of a run over the whole morning, not of a
    # detector started at the window's first bar
    cold = frame[(frame["date"] >= ts(start)) & (frame["date"] <= ts(end))].reset_index(drop=True)
    cold_ids = detect_fvgs(cold, FvgConfig())["formed_time"].astype(str).tolist()
    assert cold_ids != [z["formed_time"] for z in body["zones"] if z["formed_time"] >= start]
    # a zone that ended before the window is not there
    full = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()["zones"]
    ended_before = [z for z in full if z["end_time"] is not None and z["end_time"] < start]
    assert ended_before
    assert not {z["id"] for z in ended_before} & {z["id"] for z in body["zones"]}


def test_nothing_is_known_before_known_at_or_from_bars_after_the_window(client, data_dir: Path) -> None:
    frame = frame_of(data_dir)
    end = "2024-01-02 10:30:00"
    short = get_fvg(client, start="2024-01-02 09:30:00", end=end).json()["zones"]
    full = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()["zones"]
    assert short and all(z["known_at"] > z["formed_time"] for z in full)
    assert all(z["known_at"] <= end for z in short)
    # the window's own answer is the detector over the bars up to its end only: a zone that a later bar
    # filled is still live at 10:30 and says nothing of that later bar
    assert project(short) == expected(frame, "2024-01-02 09:30:00", end)
    by_id = {z["id"]: z for z in full}
    later = [z for z in short if z["end_time"] is None and by_id[z["id"]]["end_time"] is not None]
    assert later, "the fixture must have a zone that a later bar ended"
    for zone in short:
        for key in ("end_time", "last_change_known_at", "last_change_time"):
            assert zone[key] is None or zone[key] <= "2024-01-02 10:31:00"
        assert zone["touches"] <= by_id[zone["id"]]["touches"]
    # the last bar is 10:30; nothing refers to a later bar
    assert max(z["known_at"] for z in short) <= end


@pytest.mark.parametrize("minutes", [5, 15])
def test_five_and_fifteen_minute_zones(client, data_dir: Path, minutes: int) -> None:
    body = get_fvg(client, start=DAY1[0], end=DAY1[1], bar_minutes=minutes).json()
    assert body["bar_minutes"] == minutes and body["config"]["bar_minutes"] == minutes
    want = expected(frame_of(data_dir), *DAY1, minutes=minutes)
    assert project(body["zones"]) == want
    for zone in body["zones"]:  # on the grid of the larger bars: formed at the label of candle 3
        stamp = ts(zone["known_at"])
        assert (stamp.hour * 60 + stamp.minute - (9 * 60 + 30)) % minutes == 0
    assert body["zones"] != get_fvg(client, start=DAY1[0], end=DAY1[1]).json()["zones"]


def test_a_day_shows_the_same_zones_alone_or_as_the_second_day_of_a_window(client, data_dir: Path) -> None:
    frame = frame_of(data_dir)
    day2 = ("2024-01-03 00:00:00", "2024-01-03 23:59:59")
    for minutes in (1, 5, 15):
        alone = get_fvg(client, start=day2[0], end=day2[1], bar_minutes=minutes).json()
        both = get_fvg(client, start=DAY1[0], end=day2[1], bar_minutes=minutes).json()
        day_of_both = [z for z in both["zones"] if z["formed_time"] >= day2[0]]
        assert alone["zones"] and day_of_both == alone["zones"], minutes
        assert project(alone["zones"]) == expected(frame, *day2, minutes=minutes)
        assert alone["warm"] is True and both["warm"] is False
    # equal to the detector over the previous day plus the day, minus the previous day's own zones
    both_days = frame[frame["date"] >= "2024-01-02"].reset_index(drop=True)
    direct = detect_fvgs(both_days, FvgConfig())
    direct = direct[direct["formed_time"] >= pd.Timestamp("2024-01-03")]
    alone = get_fvg(client, start=day2[0], end=day2[1]).json()["zones"]
    assert [z["id"] for z in alone] == sorted(direct["id"].tolist(), key=lambda i: (
        direct.loc[direct["id"] == i, "known_at"].iloc[0], i))
    # larger bars can form zones from the open: a cold detector needs five of them for its ATR window
    today = frame[frame["date"] >= "2024-01-03"].reset_index(drop=True)
    differs = []
    for minutes in (5, 15):
        cold = detect_fvgs(resample_session(today, minutes), FvgConfig(bar_minutes=minutes))
        warm = get_fvg(client, start=day2[0], end=day2[1], bar_minutes=minutes).json()["zones"]
        cold_view = list(zip(cold["formed_time"].astype(str), cold["size_atr"].round(6), strict=True))
        warm_view = [(z["formed_time"], None if z["size_atr"] is None else round(z["size_atr"], 6))
                     for z in warm]
        differs.append(cold_view != warm_view)
    assert any(differs)


def test_warm_is_false_on_the_first_day_of_the_data_and_the_zones_are_the_cold_ones(
        client, data_dir: Path) -> None:
    body = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()
    assert body["warm"] is False
    cold = detect_fvgs(frame_of(data_dir).query("date < '2024-01-03'").reset_index(drop=True), FvgConfig())
    assert [z["id"] for z in body["zones"]] == sorted(cold["id"].tolist(), key=lambda i: (
        cold.loc[cold["id"] == i, "known_at"].iloc[0], i))
    assert get_fvg(client).json()["warm"] is True  # the default window is the last day


def test_the_width_check_counts_the_rows_of_the_window_asked_for(client, data_dir: Path, monkeypatch) -> None:
    import MainStoinker.api.routes_bars as routes_bars

    monkeypatch.setattr(routes_bars, "MAX_BARS", 215)  # the warm-up rows (211 to 10:30) fit
    mid = {"start": "2024-01-03 10:00:00", "end": "2024-01-03 10:30:00"}  # 31 rows asked for
    assert client.get("/api/runs/1/bars", params={"symbol": "AAPL", **mid}).status_code == 200
    response = get_fvg(client, **mid)
    assert response.status_code == 200 and response.json()["warm"] is True
    assert project(response.json()["zones"]) == expected(frame_of(data_dir), mid["start"], mid["end"])
    wide = {"start": "2024-01-02 09:30:00", "end": "2024-01-03 11:59:59"}  # 300 rows: too many for both
    assert client.get("/api/runs/1/bars", params={"symbol": "AAPL", **wide}).status_code == 422
    assert get_fvg(client, **wide).status_code == 422
    # the rows before the window never make a narrow window "too wide": with a limit that the warm-up rows
    # exceed, the answer is still a 200 (read without the warm-up)
    monkeypatch.setattr(routes_bars, "MAX_BARS", 100)
    assert client.get("/api/runs/1/bars", params={"symbol": "AAPL", **mid}).status_code == 200
    assert get_fvg(client, **mid).status_code == 200


def test_the_options_change_the_config_and_the_zones(client, data_dir: Path) -> None:
    frame = frame_of(data_dir)
    body = get_fvg(client, start=DAY1[0], end=DAY1[1], min_size_atr="0", same_direction="1").json()
    assert body["config"]["min_size_atr"] == 0.0 and body["config"]["require_same_direction"] is True
    config = FvgConfig(min_size_atr=0.0, require_same_direction=True)
    assert project(body["zones"]) == expected(frame, *DAY1, config=config)
    plain = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()
    assert len(body["zones"]) != len(plain["zones"])


def test_errors_match_the_bars_route(client, data_dir: Path) -> None:
    def both(url: str, **params: Any) -> tuple[int, int]:
        return (client.get(f"/api/runs/{url}/bars", params=params).status_code,
                client.get(f"/api/runs/{url}/fvg", params=params).status_code)

    assert both("9", symbol="AAPL") == (404, 404)
    assert both("1", symbol="TSLA") == (404, 404)
    assert both("1") == (422, 422)  # no symbol
    assert both("1", symbol="AAPL", start="2024-01-02T09:30:00") == (422, 422)
    assert both("1", symbol="AAPL", start="2024-01-03T09:30:00", end="2024-01-02T09:30:00") == (422, 422)
    assert both("1", symbol="AAPL", start="2024-01-02T00:00:00", end="2024-01-20T00:00:00") == (422, 422)
    assert both("1", symbol="AAPL", start="2024-01-02T00:00:00Z", end="2024-01-02T01:00:00Z") == (422, 422)
    # the detail of a window error is the bars route's
    bars = client.get("/api/runs/1/bars", params={"symbol": "AAPL", "start": "x", "end": "y"})
    fvg = get_fvg(client, start="x", end="y")
    assert bars.json() == fvg.json()
    # 404 comes before 422: a bad option on an unknown run or symbol is a 404
    assert client.get("/api/runs/9/fvg", params={"symbol": "AAPL", "bar_minutes": "7"}).status_code == 404
    assert client.get("/api/runs/1/fvg", params={"symbol": "TSLA", "bar_minutes": "7"}).status_code == 404
    for bad in ({"bar_minutes": "2"}, {"bar_minutes": "0"}, {"bar_minutes": "x"}, {"bar_minutes": ""},
                {"same_direction": "2"}, {"same_direction": "true"}, {"min_size_atr": "-1"},
                {"min_size_atr": "nan"}, {"min_size_atr": "inf"}, {"min_size_atr": "abc"},
                {"min_size_atr": "1e9"}):
        response = get_fvg(client, **bad)
        assert response.status_code == 422, bad
        assert isinstance(response.json()["detail"], str)
    for ok in ({"bar_minutes": "1"}, {"bar_minutes": "5"}, {"bar_minutes": "15"}, {"same_direction": "0"},
               {"min_size_atr": "0.5"}):
        assert get_fvg(client, **ok).status_code == 200, ok


def test_the_window_limit_is_the_bars_routes(client, monkeypatch) -> None:
    import MainStoinker.api.routes_bars as routes_bars

    monkeypatch.setattr(routes_bars, "MAX_BARS", 100)
    assert get_fvg(client, start=DAY1[0], end=DAY1[1]).status_code == 422
    assert client.get("/api/runs/1/bars", params={
        "symbol": "AAPL", "start": DAY1[0], "end": DAY1[1]}).status_code == 422


def test_the_number_of_zones_is_capped_and_the_newest_are_kept(client, monkeypatch) -> None:
    everything = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()
    assert everything["truncated"] is False and len(everything["zones"]) > 3
    monkeypatch.setattr(routes_fvg, "MAX_ZONES", 3)
    cut = get_fvg(client, start=DAY1[0], end=DAY1[1]).json()
    assert cut["truncated"] is True
    assert cut["zones"] == everything["zones"][-3:]
    assert routes_fvg.MAX_ZONES == 3


def test_unavailable_bars_give_the_bars_routes_reason_and_no_zones(tmp_path: Path, data_dir: Path) -> None:
    db = tmp_path / "runs.db"
    store_run(db, data_dir)
    client = make_client(db)
    assert get_fvg(client).json()["available"] is True
    path = offline_data_path(data_dir, "AAPL")
    path.write_text(path.read_text() + "\n")  # the file no longer matches the run's fingerprint
    for params in ({}, {"bar_minutes": "5"}):
        body = get_fvg(client, **params).json()
        bars = client.get("/api/runs/1/bars", params={"symbol": "AAPL"}).json()
        assert (body["available"], body["reason"], body["zones"]) == (False, "changed", [])
        assert (bars["available"], bars["reason"]) == (False, "changed")
        assert body["bar_minutes"] == int(params.get("bar_minutes", 1))
    path.unlink()
    body = get_fvg(client).json()
    assert (body["available"], body["reason"], body["zones"]) == (False, "missing", [])


def test_a_run_without_a_data_directory_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    data = DataMeta(source="offline", directory="", symbols=("AAPL",), first_bar=DAYS[0], last_bar=DAYS[-1])
    with SqliteStore(db, env=NO_ENV) as store:
        store.end_run(store.begin_run(RunMeta(kind="backtest", fills="legacy", data=data)), "completed")
    body = get_fvg(make_client(db)).json()
    assert (body["available"], body["reason"], body["zones"]) == (False, "missing", [])


def test_a_bar_with_a_non_cent_price_is_a_200_with_a_reason(tmp_path: Path) -> None:
    directory = tmp_path / "bars-data"
    directory.mkdir()
    write_bars(directory, non_cent=True)
    store_run(tmp_path / "runs.db", directory)
    client = make_client(tmp_path / "runs.db")
    response = get_fvg(client, start=DAY1[0], end="2024-01-03 23:59:59")
    assert response.status_code == 200
    body = response.json()
    assert (body["available"], body["reason"], body["zones"]) == (True, "non_cent_price", [])
    assert get_fvg(client, start=DAY1[0], end=DAY1[1]).json()["zones"]  # day 1 is fine: only day 2 has it
    assert get_fvg(client, bar_minutes="5").status_code == 200


def test_a_window_with_no_bars_has_no_zones(client) -> None:
    body = get_fvg(client, start="2024-01-06 00:00:00", end="2024-01-06 23:59:59").json()
    assert (body["available"], body["zones"], body["truncated"]) == (True, [], False)


def test_the_route_is_get_only(client) -> None:
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/api/runs/1/fvg", params={"symbol": "AAPL"}).status_code == 405


def test_zones_of_a_stored_run_of_the_golden_algos_equal_the_detector_on_its_bars(
        tmp_path_factory: pytest.TempPathFactory) -> None:
    """`small_store_run`: the zones over the bars the bars route serves for the run's own data."""
    client = make_client(stored_run_database(tmp_path_factory, "legacy"))
    run = client.get("/api/runs").json()["runs"][0]
    symbol = run["symbols"][0]
    bars = client.get(f"/api/runs/{run['run_id']}/bars", params={"symbol": symbol}).json()
    assert bars["available"] and bars["bars"]
    body = client.get(f"/api/runs/{run['run_id']}/fvg", params={"symbol": symbol}).json()
    assert body["available"] is True
    day = pd.Timestamp(bars["bars"][-1]["time"]).normalize()
    around = client.get(f"/api/runs/{run['run_id']}/bars", params={
        "symbol": symbol, "start": f"{day - pd.Timedelta(days=4):%Y-%m-%d} 00:00:00",
        "end": f"{day:%Y-%m-%d} 23:59:59"}).json()
    frame = pd.DataFrame(around["bars"]).rename(columns={"time": "date"})
    frame["date"] = pd.to_datetime(frame["date"])
    earlier = frame[frame["date"] < day]
    if len(earlier):  # the previous available day warms the ATR window
        frame = frame[frame["date"] >= earlier["date"].max().normalize()].reset_index(drop=True)
    assert body["warm"] is bool(len(earlier))
    try:
        want = detect_fvgs(frame, FvgConfig())
    except ValueError:  # the offline bars are not all cent prices: the route says so
        assert (body["reason"], body["zones"]) == ("non_cent_price", [])
        return
    want = want[want["formed_time"] >= day]
    assert [z["id"] for z in body["zones"]] == sorted(want["id"].tolist(), key=lambda i: (
        want.loc[want["id"] == i, "known_at"].iloc[0], i))


def test_a_live_run_reads_its_stored_bars_with_the_same_rules(tmp_path: Path) -> None:
    from MainStoinker.engine.store import StoredBar
    from tests.test_api_live_runs import bars_run, client_at
    from tests.test_run_documents_live import START

    rows = bar_rows(DAYS[0], 7)
    stored = [StoredBar("AAPL", datetime.fromisoformat(t), o, h, lo, c, float(v))
              for t, o, h, lo, c, v in rows]
    db = tmp_path / "live.db"
    bars_run(db, last_bar=stored[-1].time, bars=stored)
    client = client_at(db, START)
    frame = pd.DataFrame([(b.time, b.open, b.high, b.low, b.close, b.volume) for b in stored],
                         columns=["date", "open", "high", "low", "close", "volume"])
    start, end = "2024-01-02 10:00:00", "2024-01-02 10:30:00"
    body = client.get("/api/runs/1/fvg", params={"symbol": "AAPL", "start": start, "end": end}).json()
    want = expected(frame, start, end)
    assert want and project(body["zones"]) == want
    assert any(z["formed_time"] < start for z in body["zones"])  # warmed up from the stored morning bars
    assert client.get("/api/runs/1/fvg", params={"symbol": "TSLA"}).status_code == 404
    assert project(client.get("/api/runs/1/fvg", params={"symbol": "AAPL"}).json()["zones"]) == expected(
        frame, "2024-01-02 00:00:00", "2024-01-02 23:59:59")
