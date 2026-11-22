"""Per-symbol bar history for the engine.

This module must not import anything from `MainStoinker.*` outside
`MainStoinker.engine`, nor `ibapi`.
"""

from __future__ import annotations

import pandas as pd

from .feed import Bar


class BarStore:
    """Growing bar history of one symbol.

    Seeded from the feed's warm-up frame; `append` adds one closed bar and
    `view` returns the full history for algos. For now the history is kept as
    a DataFrame and extended exactly as the legacy `Ticker.append` does in
    non-live mode, so `view()` is frame-identical to the legacy `ticker.data`
    (values, dtypes, column order, RangeIndex). Callers must treat that as an
    implementation detail: a later task swaps in linear-time storage behind
    this same interface.

    Aliasing: the store never shares memory writably with anyone. The
    constructor copies the warm-up frame, and `view()` returns a new frame
    object on every call (a shallow copy, `copy(deep=False)`). Under pandas 3
    copy-on-write the copy shares the underlying buffers only until either
    side is written to, at which point the writer gets its own data. So an
    algo can add a column to or overwrite cells of the view without changing
    the store, and a view an algo already holds is not changed by later
    appends. The cost of `view()` is O(1) in the number of rows.
    """

    def __init__(self, warmup: pd.DataFrame) -> None:
        self._data: pd.DataFrame = warmup.copy()

    def append(self, bar: Bar) -> None:
        """Add one closed bar to the end of the history."""
        record = pd.DataFrame.from_records([bar.as_record()])
        self._data = pd.concat([self._data, record], ignore_index=True)

    def view(self) -> pd.DataFrame:
        """The full history, oldest first. Writing to it does not change the store."""
        return self._data.copy(deep=False)

    def __len__(self) -> int:
        return len(self._data)
