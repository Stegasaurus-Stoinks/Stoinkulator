# Stoinkulator
calculator for ze stoinks
for project imports:
pip install -e .
for all dependencies:
pip install -r requirements.txt

## Tests
```
pip install -r requirements-dev.txt
pytest            # fast suite
pytest -m slow    # slow golden backtests
ruff check .
```
Tests run offline against `OfflineData/` and never connect to TWS. See `CLAUDE.md` for conventions.
