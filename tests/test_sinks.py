"""EventSink implementations."""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from MainStoinker.engine.feed import Bar
from MainStoinker.engine.sink import EventSink, NullSink, RecordingSink


def make_bar(minute: int = 0) -> Bar:
    return Bar(pd.Timestamp("2024-01-01 09:30:00") + pd.Timedelta(minutes=minute), 1, 1.0, 2.0, 0.5, 1.5, 10)


def test_null_sink_wants_no_algo_data_and_ignores_events() -> None:
    sink = NullSink()
    assert sink.wants_algo_data is False
    assert sink.on_bar("AAPL", make_bar(), None) is None
    assert sink.on_bar("AAPL", make_bar(), [{"a": 1}]) is None
    assert sink.on_intrabar("AAPL", make_bar()) is None


def test_recording_sink_records_in_order() -> None:
    sink = RecordingSink(wants_algo_data=True)
    b0, b1, b2 = make_bar(0), make_bar(1), make_bar(2)
    sink.on_bar("AAPL", b0, [{"x": 1}])
    sink.on_intrabar("MSFT", b1)
    sink.on_bar("MSFT", b2, [])
    events = sink.events
    assert [(e.kind, e.symbol, e.bar) for e in events] == [
        ("bar", "AAPL", b0),
        ("intrabar", "MSFT", b1),
        ("bar", "MSFT", b2),
    ]
    assert events[0].algo_data == [{"x": 1}]
    assert events[1].algo_data is None
    assert events[2].algo_data == []


def test_recording_sink_wants_algo_data_flag() -> None:
    assert RecordingSink().wants_algo_data is False
    assert RecordingSink(wants_algo_data=True).wants_algo_data is True
    assert RecordingSink(True).wants_algo_data is True


def test_sinks_satisfy_protocol() -> None:
    assert isinstance(NullSink(), EventSink)
    assert isinstance(RecordingSink(), EventSink)


def test_object_without_methods_fails_protocol() -> None:
    class Partial:
        wants_algo_data = False

        def on_bar(self, symbol: str, bar: Bar, algo_data: list | None) -> None:
            return None

    assert not isinstance(Partial(), EventSink)
    assert not isinstance(object(), EventSink)


def test_recording_view_cannot_corrupt_the_log() -> None:
    sink = RecordingSink()
    sink.on_bar("AAPL", make_bar(), None)
    view = sink.events
    assert isinstance(view, tuple)
    with pytest.raises(AttributeError):
        view.append("junk")  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        view[0].symbol = "X"  # type: ignore[misc]
    sink.on_bar("MSFT", make_bar(1), None)
    assert len(view) == 1
    assert [e.symbol for e in sink.events] == ["AAPL", "MSFT"]
