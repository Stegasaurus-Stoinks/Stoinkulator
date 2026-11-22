"""Per-bar state golden for the Elliot algo.

The trade goldens (tests/golden/elliot_*.json) only pin the trades. Elliot's
wave detection and its `AlgoData` (which feeds the front end) can change
without moving a single trade, so this file pins, after EVERY replayed bar, a
digest of everything observable about the algo:

- `inTrade`, the algo's trade bookkeeping and, while in a trade, the `Trade`
  state (stop price, stop delta, take-profit, stop-loss type, ...);
- `finishedWaves` / `tradingWaves`: lengths and every wave's point coordinates,
  in list order;
- `AlgoData`: the last row, the non-NaN count per column, and the whole frame
  (dtypes, every cell, NaN-for-NaN);
- the full return value of `update_frontend()` (it only reads algo state and
  logs; checked in ParentAlgo).

One bar is one string `state.waves.algodata.frontend` of four short hashes, so
a mismatch names the first bar and which part differs. Hashes are sha256 over
canonical JSON; floats are rounded to 10 significant digits and NaN is spelled
out. The full-frame hash is over the raw float64 bytes, so it is bit-exact.

Files live in tests/golden_state/ (NOT tests/golden, which only
test_golden_backtests.py may write). Regenerate deliberately with
`UPDATE_ELLIOT_STATE=1 pytest -m slow tests/test_elliot_state_golden.py`; the
`--update-golden` option does not touch them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests import harness

STATE_DIR = Path(__file__).parent / "golden_state"
UPDATE_ENV = "UPDATE_ELLIOT_STATE"
TICKERS = ["MSFT", "AAPL", "TSLA"]


def elliot_cfg(ticker: str) -> dict:
    return {"idname": f"elliot-{ticker.lower()}", "ticker": ticker, "order": 10, "trailingStopDelta": 0.10}


def _sig(x: object) -> object:
    """A JSON-able, platform-stable form of a scalar."""
    if x is None:
        return None
    if isinstance(x, bool | np.bool_):
        return bool(x)
    if isinstance(x, int | np.integer):
        return int(x)
    if isinstance(x, float | np.floating):
        f = float(x)
        if math.isnan(f):
            return "nan"
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return float(f"{f:.10g}")
    return str(x)


def _canon(obj: object) -> object:
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_canon(v) for v in obj]
    return _sig(obj)


def _hash(obj: object) -> str:
    text = json.dumps(_canon(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _waves(waves: list) -> list:
    out = []
    for w in waves:
        out.append([_sig(getattr(w, f"{kind}_{i}")) for i in range(1, 7) for kind in ("time", "price")])
    return out


def _frame_hash(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(repr((list(df.columns), len(df))).encode())
    for col in df.columns:
        s = df[col]
        h.update(f"|{col}:{s.dtype}|".encode())
        if s.dtype.kind == "f":
            a = s.to_numpy(dtype="float64")
            nan = np.isnan(a)
            h.update(nan.tobytes())
            h.update(np.where(nan, 0.0, a).tobytes())
        elif s.dtype.kind in "iub":
            h.update(s.to_numpy().astype("int64").tobytes())
        else:
            mask = s.notna().to_numpy()
            h.update(mask.tobytes())
            h.update(json.dumps([_sig(v) for v in s[mask].tolist()]).encode())
    return h.hexdigest()[:8]


def _state_part(algo: object) -> str:
    trade = None
    if algo.inTrade:
        t = algo.trade
        trade = {
            name: _sig(getattr(t, name, None))
            for name in ("stopPrice", "stopDelta", "tp", "stopLossType", "direction", "openPrice",
                         "openTime", "tradeID", "volume", "status")
        }
    return _hash({
        "inTrade": algo.inTrade,
        "trade": trade,
        "nTrades": len(algo.trades),
        "waveNum": algo.waveNum,
        "secondExitPrice": algo.secondExitPrice,
        "entryPrice": algo.entryPrice,
        "hasCurrentWave": algo.currentWave is not None,
    })


def _waves_part(algo: object) -> str:
    return _hash({
        "nFinished": len(algo.finishedWaves),
        "nTrading": len(algo.tradingWaves),
        "finished": _waves(algo.finishedWaves),
        "trading": _waves(algo.tradingWaves),
    })


def _algodata_part(algo: object) -> str:
    df = algo.AlgoData
    last = {c: _sig(df[c].iloc[-1]) for c in df.columns}
    counts = {c: int(df[c].notna().sum()) for c in df.columns}
    return _hash({"last": last, "counts": counts, "frame": _frame_hash(df)})


def _frontend_part(algo: object) -> str:
    return _hash(algo.update_frontend())


def bar_digest(algo: object) -> str:
    """One string of four short hashes describing the algo right after an update."""
    return ".".join((_state_part(algo), _waves_part(algo), _algodata_part(algo), _frontend_part(algo)))


def collect_state(ticker: str, max_rows: int | None = None) -> dict:
    """Run Elliot over `ticker`'s offline bars through the engine; digest every bar.

    The current directory must be a scratch directory (the autouse fixture
    provides one under pytest).
    """
    harness.stage_offline_data(Path.cwd(), [ticker], max_rows=max_rows)
    cfg = elliot_cfg(ticker)
    bars: list[str] = []
    seen = {"inTrade": 0, "maxFinished": 0, "maxTrading": 0}

    def wrap(algo: object) -> None:
        original = algo.update

        def update(frame: pd.DataFrame) -> None:
            original(frame)
            bars.append(bar_digest(algo))
            seen["inTrade"] += bool(algo.inTrade)
            seen["maxFinished"] = max(seen["maxFinished"], len(algo.finishedWaves))
            seen["maxTrading"] = max(seen["maxTrading"], len(algo.tradingWaves))

        algo.update = update

    (algo,) = harness.run_mixed_offline_backtest([("Elliot", cfg)], wrap=wrap)
    return {
        "ticker": ticker,
        "config": cfg,
        "summary": {
            "bars": len(bars),
            "trades": len(algo.trades),
            "barsInTrade": seen["inTrade"],
            "maxFinishedWaves": seen["maxFinished"],
            "maxTradingWaves": seen["maxTrading"],
        },
        "bars": bars,
    }


def state_path(ticker: str) -> Path:
    return STATE_DIR / f"elliot_{ticker.lower()}.json"


def first_difference(actual: list[str], expected: list[str]) -> str | None:
    parts = ("state", "waves", "algodata", "frontend")
    for i, (a, e) in enumerate(zip(actual, expected, strict=False)):
        if a != e:
            bad = [p for p, x, y in zip(parts, a.split("."), e.split("."), strict=True) if x != y]
            return f"first differing bar index {i}: {', '.join(bad)}"
    if len(actual) != len(expected):
        return f"bar count {len(actual)} != {len(expected)}"
    return None


@pytest.mark.slow
@pytest.mark.parametrize("ticker", TICKERS)
def test_elliot_state_matches_golden(ticker: str) -> None:
    state = collect_state(ticker)
    path = state_path(ticker)
    if os.environ.get(UPDATE_ENV) == "1":
        STATE_DIR.mkdir(exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(state, indent=1) + "\n")
        return
    assert path.exists(), f"No state file {path.name}; regenerate with {UPDATE_ENV}=1"
    expected = json.loads(path.read_text(encoding="utf-8"))
    diff = first_difference(state["bars"], expected["bars"])
    assert diff is None, diff
    assert state == expected
