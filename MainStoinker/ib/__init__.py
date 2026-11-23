"""The only new-code package allowed to talk to Interactive Brokers.

The rules below are enforced by `tests/test_ib_import_boundary.py` (and, from
the other side, by `tests/test_engine_import_boundary.py` and
`tests/test_api_boundary.py`). Changing a rule is a reviewed edit of those tests.

1. `ibapi` may be imported only here, plus a frozen allowlist of the three
   legacy files that import it today. The allowlist can only shrink.
2. From the rest of `MainStoinker`, this package may import only
   `MainStoinker.ib.*`, `MainStoinker.engine.*` and
   `MainStoinker.Util.broker_guard` (pure, stdlib only). Never the legacy
   runtime (`MainStuff`, `DataCollection`, `TradeTools`, `Algos`, other `Util`).
3. No `socket`, `ssl`, `asyncio` or `http` modules (nor their close relatives):
   every connection goes through `ibapi.connection.Connection`, which the
   pytest block patches, so no test can reach a broker.
4. The engine and the API import neither `ibapi` nor `MainStoinker.ib`, so
   importing them never loads this package.
"""
