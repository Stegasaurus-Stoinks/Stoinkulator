"""Engine package: runs backtests with every dependency passed in.

Deliberately empty of imports and re-exports. Modules here must not import
the legacy globals, broker wrapper or algos (see tests/test_engine_import_boundary.py).
"""
