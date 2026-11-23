"""`STOINK_BROKER_BLOCKED`: a pytest session marks its environment, and `IbConnection.open()`
refuses a real client while the mark is there, so a child process of a test cannot reach a broker.

Nothing here opens a socket. Every client used below is a recording stub whose `__init__` and
`connect` only record (they never call `EClient`), and the one child process uses such a stub too.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ibapi.client import EClient

from MainStoinker.ib import connection as connection_module
from MainStoinker.ib.client import DataOnlyClient
from MainStoinker.ib.connection import BrokerConnectionRefusedHere, ConnectFailed, ConnState, IbConnection
from tests import conftest as tests_conftest
from tests.conftest import BrokerConnectionBlockedUnderPytest

REPO = Path(__file__).resolve().parent.parent
VARIABLE = "STOINK_BROKER_BLOCKED"
CONNECTION_SOURCE = REPO / "MainStoinker" / "ib" / "connection.py"


# -- the recording stubs (no socket can be opened by any of them) ----------------------------


class RecordingClient(DataOnlyClient):
    """A real-client class (a `DataOnlyClient`) that only records; `EClient` is never initialised."""

    built: list[dict[str, Any]] = []
    connects: list[tuple[Any, ...]] = []

    def __init__(self, **kwargs: Any) -> None:  # no super().__init__: nothing of ibapi runs
        RecordingClient.built.append(kwargs)

    def connect(self, *args: Any) -> None:  # type: ignore[override]
        RecordingClient.connects.append(args)

    def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    RecordingClient.built = []
    RecordingClient.connects = []


def open_connection(factory: Any = DataOnlyClient, *, port: int = 7497, env: dict[str, str] | None = None,
                    **options: Any) -> IbConnection:
    options.setdefault("poll_interval", 0.002)
    return IbConnection("127.0.0.1", port, 21, env={} if env is None else env, client_factory=factory,
                        sleep=lambda seconds: None, **options)


def refused(conn: IbConnection, attempts_made: int = 0) -> BrokerConnectionRefusedHere:
    """`attempts_made` is 1 when the factory had to run to show the client was a real one."""
    with pytest.raises(BrokerConnectionRefusedHere) as info:
        conn.open()
    assert conn.state is ConnState.CLOSED
    assert conn.attempts_made == attempts_made
    conn.close()  # a no-op
    assert conn.state is ConnState.CLOSED
    return info.value


# -- the session variable --------------------------------------------------------------------


def test_sentinel_set_for_the_session() -> None:
    assert os.environ.get(VARIABLE) == "1"


def test_sentinel_set_in_every_xdist_worker(tmp_path: Path) -> None:
    probe = REPO / "tests" / "ib_subprocess_block_probe.py"
    env = {k: v for k, v in os.environ.items() if k != VARIABLE}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-n", "2", "--dist", "each",
         "-m", "slow or not slow", str(probe)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "4 passed" in done.stdout  # two probe tests in each of the two workers


def make_config() -> Any:
    return SimpleNamespace(stash=pytest.Stash())


@pytest.mark.parametrize("before", ["old", "", "1"])
def test_unconfigure_restores_a_value_that_was_there(monkeypatch: pytest.MonkeyPatch, before: str) -> None:
    monkeypatch.setenv(VARIABLE, before)
    config = make_config()
    tests_conftest._set_broker_blocked_variable(config)
    assert os.environ[VARIABLE] == "1"
    tests_conftest._restore_broker_blocked_variable(config)
    assert os.environ[VARIABLE] == before


def test_unconfigure_restores_the_unset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    config = make_config()
    tests_conftest._set_broker_blocked_variable(config)
    assert os.environ[VARIABLE] == "1"
    tests_conftest._restore_broker_blocked_variable(config)
    assert VARIABLE not in os.environ


def test_unconfigure_without_configure_changes_nothing() -> None:
    tests_conftest._restore_broker_blocked_variable(make_config())
    assert os.environ[VARIABLE] == "1"


def test_both_hooks_are_registered_next_to_the_existing_ones(pytestconfig: pytest.Config) -> None:
    hooks = pytestconfig.pluginmanager.hook
    configure = {impl.function.__name__ for impl in hooks.pytest_configure.get_hookimpls()}
    unconfigure = {impl.function.__name__ for impl in hooks.pytest_unconfigure.get_hookimpls()}
    assert {"pytest_configure", "pytest_configure_broker_blocked_variable"} <= configure
    assert {"pytest_unconfigure", "pytest_unconfigure_broker_blocked_variable"} <= unconfigure


# -- the guard in a child process -------------------------------------------------------------

CHILD = """
import sys
from MainStoinker.ib import connection
from MainStoinker.ib.client import DataOnlyClient

record = sys.argv[1]


class Recording(DataOnlyClient):
    def __init__(self, **kwargs):
        open(record, "a").write("factory called\\n")

    def connect(self, *args):
        open(record, "a").write("connect called\\n")


conn = connection.IbConnection("127.0.0.1", 7497, 21, env={}, client_factory=Recording)
print("module:", connection.__file__)
try:
    conn.open()
except BaseException as exc:
    print("raised:", type(exc).__name__, "state:", conn.state.value)
    sys.exit(3)
print("opened")
"""


def test_subprocess_ib_connection_refuses(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    record = tmp_path / "record.txt"
    env = {**os.environ, "PYTHONPATH": str(REPO)}  # inherits the session variable
    assert env[VARIABLE] == "1"
    done = subprocess.run(
        [sys.executable, "-P", str(script), str(record)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "raised: BrokerConnectionRefusedHere state: closed" in done.stdout
    assert str(REPO) in done.stdout  # the child imported this checkout
    assert not record.exists()  # the factory was never called, so connect was not either


# -- in-process: which factories are blocked, and when ------------------------------------------


def test_default_factory_is_refused() -> None:
    error = refused(open_connection())
    text = str(error).lower()
    assert "blocked in this process" in text and "pytest" in text
    assert "unset" not in text and VARIABLE.lower() not in text


def test_error_is_neither_a_connect_failure_nor_an_oserror() -> None:
    assert issubclass(BrokerConnectionRefusedHere, RuntimeError)
    assert not issubclass(BrokerConnectionRefusedHere, ConnectFailed)
    assert not issubclass(BrokerConnectionRefusedHere, OSError)


def test_a_client_class_is_refused_without_calling_it() -> None:
    refused(open_connection(RecordingClient))
    assert RecordingClient.built == [] and RecordingClient.connects == []


def test_refusal_comes_before_the_port_guard() -> None:
    refused(open_connection(port=7496))  # live port, no opt-in: the older guard would say BrokerPortRefused
    with pytest.raises(BrokerConnectionRefusedHere):
        open_connection(port="7497").open()  # type: ignore[arg-type]


def test_a_function_returning_a_real_client_is_refused_before_connect() -> None:
    def factory(**kwargs: Any) -> Any:
        return RecordingClient(**kwargs)

    refused(open_connection(factory), attempts_made=1)
    assert len(RecordingClient.built) == 1  # the product exists...
    assert RecordingClient.connects == []  # ...and was never connected


def test_a_plain_eclient_instance_is_refused_before_connect() -> None:
    class Plain(EClient):
        connects: list[Any] = []

        def __init__(self, **kwargs: Any) -> None:
            pass

        def connect(self, *args: Any) -> None:
            Plain.connects.append(args)

    refused(open_connection(lambda **kwargs: Plain(**kwargs)), attempts_made=1)
    refused(open_connection(Plain))
    assert Plain.connects == []


def test_a_client_that_is_not_an_eclient_is_not_refused() -> None:
    class Scripted:
        connects = 0

        def connect(self, *args: Any) -> None:
            Scripted.connects += 1
            raise ValueError("reached connect")

        def disconnect(self) -> None:
            pass

    conn = open_connection(lambda **kwargs: Scripted(), env={VARIABLE: "1"})
    with pytest.raises(ValueError, match="reached connect"):
        conn.open()
    assert Scripted.connects == 1


def test_the_env_mapping_alone_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE)
    refused(open_connection(env={VARIABLE: "1"}))
    refused(open_connection(RecordingClient, env={VARIABLE: "1"}))


def test_the_process_environment_alone_blocks_whatever_the_mapping_says() -> None:
    assert os.environ[VARIABLE] == "1"
    refused(open_connection(env={}))
    refused(open_connection(env={"STOINK_ALLOW_LIVE_PORT": "1", VARIABLE: ""}))


@pytest.mark.parametrize("where", ["process", "mapping"])
def test_an_empty_value_still_blocks(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    if where == "process":
        monkeypatch.setenv(VARIABLE, "")
        refused(open_connection())
    else:
        monkeypatch.delenv(VARIABLE)
        refused(open_connection(env={VARIABLE: ""}))


@pytest.mark.parametrize("value", ["0", "false", "no", " "])
def test_presence_not_truthiness(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(VARIABLE, value)
    refused(open_connection())


def test_nothing_lifts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOINK_ALLOW_LIVE_PORT", "1")
    refused(open_connection(port=7496, env={"STOINK_ALLOW_LIVE_PORT": "1"}))
    refused(open_connection(attempts=1, connect_timeout=1, ready_timeout=1, retry_delay=1, backoff=1,
                            max_retry_delay=1, observers=(), max_requests_per_second=1,
                            poll_interval=0.01, join_timeout=1, logger=None, clock=lambda: 0.0))


def test_the_check_is_reached_only_through_the_one_function(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE)
    assert connection_module._broker_blocked_here({}) is False
    assert connection_module._broker_blocked_here({VARIABLE: ""}) is True
    monkeypatch.setenv(VARIABLE, "")
    assert connection_module._broker_blocked_here({}) is True


# -- the older in-process block still works without the new one -------------------------------


def test_sentinel_does_not_replace_the_in_process_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE)
    conn = open_connection(env={})
    with pytest.raises(BrokerConnectionBlockedUnderPytest):
        conn.open()
    assert conn.attempts_made == 1
    conn.close()


# -- the source of connection.py: exactly one read of os.environ -------------------------------

THE_FUNCTION = "_broker_blocked_here"
FORBIDDEN_NAMES = {"socket", "subprocess", "__import__", "eval", "exec", "open", "EClient", "Connection"}


def environment_problems(source: str) -> list[str]:
    """Everything in `source` that reads the environment, except the one allowed read.

    Allowed: `"STOINK_BROKER_BLOCKED" in os.environ`, once, inside `_broker_blocked_here`; and
    the import of `os` that it needs. Every other use of `os`, `environ`, `getenv`, `putenv`,
    `environb` is a problem, as are raw sockets, `ibapi` and dynamic code.
    """
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    problems: list[str] = []
    allowed_reads = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in {"ibapi", "socket", "subprocess"} or (root == "os" and alias.name != "os"):
                    problems.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in {"os", "ibapi", "socket", "subprocess"}:
                problems.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "putenv", "environb"}:
            parent = parents.get(node)
            scope = node
            while scope in parents and not isinstance(scope, ast.FunctionDef):
                scope = parents[scope]
            exact = (
                node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and isinstance(parent, ast.Compare)
                and parent.comparators[0] is node
                and len(parent.ops) == 1
                and isinstance(parent.ops[0], ast.In)
                and isinstance(parent.left, ast.Constant)
                and parent.left.value == VARIABLE
                and isinstance(scope, ast.FunctionDef)
                and scope.name == THE_FUNCTION
            )
            if exact:
                allowed_reads += 1
            else:
                problems.append(f"line {node.lineno}: {node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                problems.append(f"line {node.lineno}: name {node.id}")
            if node.id == "os" and not (
                isinstance(parents.get(node), ast.Attribute) and parents[node].attr == "environ"
            ):
                problems.append(f"line {node.lineno}: os used other than as os.environ")
        elif isinstance(node, ast.Attribute) and node.attr in {"_inner", "socket", "_conn", "conn"}:
            problems.append(f"line {node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.Constant) and node.value == "STOINK_ALLOW_LIVE_PORT":
            problems.append(f"line {node.lineno}: the live-port variable")
    if allowed_reads != 1:
        problems.append(f"expected exactly one allowed read, found {allowed_reads}")
    return problems


def test_connection_source_reads_the_environment_exactly_once() -> None:
    assert environment_problems(CONNECTION_SOURCE.read_text(encoding="utf-8")) == []


def test_the_source_check_catches_every_other_read() -> None:
    source = CONNECTION_SOURCE.read_text(encoding="utf-8")
    first = '    return "STOINK_BROKER_BLOCKED" in os.environ or "STOINK_BROKER_BLOCKED" in env\n'
    assert first in source
    read = '"STOINK_BROKER_BLOCKED" in os.environ'
    replacements = {
        "a second read in the same function": first.replace(" or ", " or os.environ.get('X') or "),
        "a second key": first.replace("in os.environ", "in os.environ and 'OTHER' in os.environ"),
        "another key instead": first.replace(read, '"OTHER" in os.environ'),
        "getenv": first.replace(read, 'os.getenv("STOINK_BROKER_BLOCKED")'),
        "no read at all": "    return False\n",
    }
    mutated = {label: source.replace(first, text) for label, text in replacements.items()}
    mutated["a read elsewhere"] = source + "\n\ndef _other() -> object:\n    return os.environ\n"
    for label, text in mutated.items():
        assert environment_problems(text), label
