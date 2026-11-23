# Stoinkulator
calculator for ze stoinks
for project imports:
pip install -e .
for all dependencies:
pip install -r requirements.txt

## Tests
```
pip install -r requirements-dev.txt
pytest            # fast suite, in parallel (pytest-xdist, one worker per core)
pytest -n 0       # the same, in one process (slower; easier to debug)
pytest -m slow    # slow golden backtests (also parallel)
ruff check .
```
Tests run offline against `OfflineData/` and never connect to TWS. See `CLAUDE.md` for conventions.

## Run a backtest
```
python -m MainStoinker backtest --algos my_algos.json --data OfflineData --out results
```
Replays the offline bars through the algos in the config (same shape as `Algo_config.json`) with a simulated broker and writes `results/trades.json`, one list of trades per algo `idname`. It never connects to TWS. Options: `--warmup-days N` (default 2), `--log` (algo logs to `results/backtest.log`), `--quiet` (hide algo prints). While running, the working directory is `--out`, so side files such as `tradey.txt` land there; nothing is written to the directory you started from. Exit code 2 means a usage or input error (one line on stderr).
