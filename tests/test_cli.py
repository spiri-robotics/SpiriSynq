"""Tests for the SpiriSynq CLI commands."""

import time
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from SpiriSynq.cli import app
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.remote_callables import remote_method

runner = CliRunner()


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


# ── Error-path tests (no network interaction needed) ──────────────────────────

def test_topic_put_raw_without_topic_is_error():
    """--input-type raw requires a topic argument; omitting it is an error."""
    result = runner.invoke(
        app, ["topic", "put", "--input-type", "raw"],
        input="some value\n",
    )
    assert result.exit_code == 1


def test_topic_put_stream_subpath_validation_error():
    """Stream path that isn't under the given topic prefix is rejected."""
    yaml_input = "path: other/prefix/topic\nvalue: hello\n---\n"
    result = runner.invoke(
        app, ["topic", "put", "test/myprefix"],
        input=yaml_input,
    )
    assert result.exit_code == 1


def test_topic_call_bad_kwarg_format():
    """A kwarg argument missing '=' is an error."""
    result = runner.invoke(app, ["topic", "call", "some/topic", "badarg"])
    assert result.exit_code == 1


def test_zenoh_scout_all_types_disabled_is_error():
    """Disabling all entity types in zenoh scout is an error."""
    result = runner.invoke(app, ["zenoh", "scout", "--no-routers", "--no-peers"])
    assert result.exit_code == 1


# ── Help ──────────────────────────────────────────────────────────────────────

def test_help_all_exits_zero():
    result = runner.invoke(app, ["help-all"])
    assert result.exit_code == 0


# ── topic list ────────────────────────────────────────────────────────────────

def test_topic_list_exits_zero():
    result = runner.invoke(app, ["topic", "list"])
    assert result.exit_code == 0


def test_topic_list_with_nonexistent_type_filter():
    """Filtering by a type with no matches should still exit zero."""
    result = runner.invoke(app, ["topic", "list", "--type", "TypeThatDoesNotExist999"])
    assert result.exit_code == 0


# ── topic schema ──────────────────────────────────────────────────────────────

def test_topic_schema_for_existing_object():
    @dataclass
    class CliSchemaObj(SyncableObject):
        speed: float = 0.0

    obj = CliSchemaObj("cli_test/schema_obj", synq_authoritive=True)
    time.sleep(0.1)

    result = runner.invoke(app, ["topic", "schema", obj.synq_absolute_path])
    assert result.exit_code == 0


def test_topic_schema_for_missing_topic():
    """schema for a non-existent topic exits zero (no results found)."""
    result = runner.invoke(app, ["topic", "schema", "cli_test/does_not_exist_12345"])
    assert result.exit_code == 0


# ── topic rehydrate ───────────────────────────────────────────────────────────

def test_topic_rehydrate_for_existing_object():
    @dataclass
    class CliRehydrateObj(SyncableObject):
        value: int = 0

    obj = CliRehydrateObj("cli_test/rehydrate_obj", synq_authoritive=True, value=42)
    time.sleep(0.1)

    result = runner.invoke(app, ["topic", "rehydrate", obj.synq_absolute_path])
    assert result.exit_code == 0


def test_topic_rehydrate_for_missing_topic():
    """rehydrate for a non-existent topic exits zero (no results found)."""
    result = runner.invoke(app, ["topic", "rehydrate", "cli_test/does_not_exist_12345"])
    assert result.exit_code == 0


# ── topic rpc ─────────────────────────────────────────────────────────────────

def test_topic_rpc_for_specific_topic():
    @dataclass
    class CliRpcListObj(SyncableObject):
        @remote_method()
        def ping(self) -> str:
            return "pong"

    obj = CliRpcListObj("cli_test/rpc_list_obj", synq_authoritive=True)
    time.sleep(0.1)

    result = runner.invoke(app, ["topic", "rpc", obj.synq_absolute_path])
    assert result.exit_code == 0


def test_topic_rpc_global_query_exits_zero():
    """topic rpc with no arguments queries all topics; should exit zero."""
    result = runner.invoke(app, ["topic", "rpc"])
    assert result.exit_code == 0


# ── topic call ────────────────────────────────────────────────────────────────

def test_topic_call_success():
    from SpiriSynq.cli import session as cli_session

    @dataclass
    class CliCallObj(SyncableObject):
        @remote_method()
        def add(self, a: int, b: int) -> int:
            return a + b

    obj = CliCallObj("cli_test/call_obj", synq_authoritive=True, synq_session=cli_session)
    time.sleep(0.1)

    result = runner.invoke(
        app,
        ["topic", "call", f"{obj.synq_absolute_path}/add", "a=3", "b=4", "--timeout", "5.0"],
    )
    assert result.exit_code == 0


def test_topic_call_no_reply_exits_2():
    """Calling a non-existent RPC endpoint exits with code 2 (no reply received)."""
    result = runner.invoke(
        app,
        ["topic", "call", "cli_test/does_not_exist/method", "--timeout", "0.5"],
    )
    assert result.exit_code == 2


def test_topic_call_rpc_exception_exits_1():
    """An RPC method that raises on the server propagates as exit code 1."""
    from SpiriSynq.cli import session as cli_session

    @dataclass
    class CliCrashObj(SyncableObject):
        @remote_method()
        def crash(self) -> None:
            raise ValueError("intentional")

    obj = CliCrashObj("cli_test/crash_obj", synq_authoritive=True, synq_session=cli_session)
    time.sleep(0.1)

    result = runner.invoke(
        app,
        ["topic", "call", f"{obj.synq_absolute_path}/crash", "--timeout", "5.0"],
    )
    assert result.exit_code == 1


def test_topic_call_generator_method():
    """A generator RPC method streams values and exits zero."""
    from SpiriSynq.cli import session as cli_session

    @dataclass
    class CliGenObj(SyncableObject):
        @remote_method()
        def count(self, n: int):
            for i in range(n):
                yield i
            return "done"

    obj = CliGenObj("cli_test/gen_obj", synq_authoritive=True, synq_session=cli_session)
    time.sleep(0.1)

    result = runner.invoke(
        app,
        ["topic", "call", f"{obj.synq_absolute_path}/count", "n=3", "--timeout", "5.0"],
    )
    assert result.exit_code == 0


# ── topic put ─────────────────────────────────────────────────────────────────

def test_topic_put_direct_value():
    """Publishing a value directly from command-line args exits zero."""
    result = runner.invoke(app, ["topic", "put", "cli_test/direct_put", "hello world"])
    assert result.exit_code == 0


def test_topic_put_stdin_bare_value():
    """Publishing a bare value from stdin exits zero."""
    result = runner.invoke(
        app, ["topic", "put", "cli_test/stdin_put"],
        input="hello from stdin\n",
    )
    assert result.exit_code == 0


def test_topic_put_stdin_yaml_with_path():
    """Publishing a YAML stream with embedded path: fields exits zero."""
    yaml_input = "path: cli_test/pathed_put\nvalue: some_data\n---\n"
    result = runner.invoke(
        app, ["topic", "put"],
        input=yaml_input,
    )
    assert result.exit_code == 0


def test_topic_put_stdin_raw_mode():
    """--input-type raw with a topic reads entire stdin as one value."""
    result = runner.invoke(
        app, ["topic", "put", "--input-type", "raw", "cli_test/raw_put"],
        input="raw string value\n",
    )
    assert result.exit_code == 0


def test_topic_put_stdin_subpath_valid():
    """A stream path that IS under the given prefix passes validation."""
    yaml_input = "path: test/myprefix/child\nvalue: hello\n---\n"
    result = runner.invoke(
        app, ["topic", "put", "test/myprefix"],
        input=yaml_input,
    )
    assert result.exit_code == 0


# ── zenoh info / scout ────────────────────────────────────────────────────────

def test_zenoh_info_exits_zero():
    result = runner.invoke(app, ["zenoh", "info"])
    assert result.exit_code == 0


def test_zenoh_scout_exits_zero():
    result = runner.invoke(app, ["zenoh", "scout", "--timeout", "0.1"])
    assert result.exit_code == 0


# ── bytes field / codec path ──────────────────────────────────────────────────
# bytes fields bypass YAML serialisation and are published as ZENOH_BYTES via
# BytesCodec (registered on every Session by default).  These tests verify that
# the CLI commands handle objects that carry bytes fields without crashing and
# that the round-trip through the codec system is intact.

def test_topic_schema_with_bytes_field():
    """topic schema exits zero for an object whose field type is bytes."""
    @dataclass
    class CliBytesSchemaObj(SyncableObject):
        data: bytes = b""

    obj = CliBytesSchemaObj("cli_test/bytes_schema_obj", synq_authoritive=True)
    time.sleep(0.1)

    result = runner.invoke(app, ["topic", "schema", obj.synq_absolute_path])
    assert result.exit_code == 0


def test_topic_rehydrate_with_bytes_field():
    """topic rehydrate exits zero for an object with non-empty bytes content.

    The bytes value is YAML-serialised as !!binary on the rehydrate path; this
    test ensures the CLI can emit that without crashing.
    """
    @dataclass
    class CliBytesRehydrateObj(SyncableObject):
        data: bytes = b""

    obj = CliBytesRehydrateObj(
        "cli_test/bytes_rehydrate_obj",
        synq_authoritive=True,
        data=b"\x00\x01\x02\xff",
    )
    time.sleep(0.1)

    result = runner.invoke(app, ["topic", "rehydrate", obj.synq_absolute_path])
    assert result.exit_code == 0


def test_topic_watch_count_exits_after_n_messages():
    """--count N causes topic watch to exit after receiving N messages."""
    import threading
    from SpiriSynq.cli import session as cli_session

    @dataclass
    class CliWatchCountObj(SyncableObject):
        value: int = 0

    obj = CliWatchCountObj("cli_test/watch_count_obj", synq_authoritive=True,
                           synq_session=cli_session, value=0)
    time.sleep(0.1)

    result_holder = []

    def run_watch():
        result_holder.append(runner.invoke(
            app,
            ["topic", "watch", f"{obj.synq_absolute_path}/**", "--count", "1",
             "--no-received-timestamp"],
        ))

    t = threading.Thread(target=run_watch, daemon=True)
    t.start()
    time.sleep(0.2)

    obj.value = 42

    t.join(timeout=5.0)
    assert not t.is_alive(), "topic watch did not exit after --count 1"
    assert result_holder[0].exit_code == 0


def test_topic_watch_bytes_field_decoded_as_binary():
    """topic watch emits !!binary YAML for ZENOH_BYTES-encoded fields (codec path).

    bytes fields bypass YAML serialisation and are published via BytesCodec as
    ZENOH_BYTES encoding.  topic watch must detect that encoding and emit the
    value as a base64 !!binary scalar rather than attempting a UTF-8 decode.
    """
    import threading
    from io import StringIO
    from rich.console import Console
    import SpiriSynq.cli as cli_module
    from SpiriSynq.cli import session as cli_session

    @dataclass
    class CliByteWatchObj(SyncableObject):
        data: bytes = b""

    obj = CliByteWatchObj("cli_test/bytes_watch_obj", synq_authoritive=True,
                          synq_session=cli_session, data=b"")
    time.sleep(0.1)

    buf = StringIO()
    original_console = cli_module.console_out
    cli_module.console_out = Console(file=buf, highlight=False, soft_wrap=True)

    result_holder = []

    def run_watch():
        result_holder.append(runner.invoke(
            app,
            ["topic", "watch", f"{obj.synq_absolute_path}/data", "--count", "1",
             "--no-received-timestamp"],
        ))

    try:
        t = threading.Thread(target=run_watch, daemon=True)
        t.start()
        time.sleep(0.2)

        obj.data = b"\xde\xad\xbe\xef"

        t.join(timeout=5.0)
    finally:
        cli_module.console_out = original_console

    assert not t.is_alive(), "topic watch did not exit after --count 1"
    assert result_holder[0].exit_code == 0
    assert "!!binary" in buf.getvalue()


# ── meta type_schema ──────────────────────────────────────────────────────────

def test_meta_type_schema_all_exits_zero():
    result = runner.invoke(app, ["meta", "type_schema"])
    assert result.exit_code == 0


def test_meta_type_schema_specific_type():
    """Querying a specific type name exits zero even when not found."""
    result = runner.invoke(app, ["meta", "type_schema", "SomeType"])
    assert result.exit_code == 0
