"""`synq`: look at a SpiriSynq network, and poke at it.

Data goes to stdout as YAML (or JSON, where asked), one document per record, so it can be piped;
progress and errors go to stderr. Commands exit 0 even when nothing answers, and say so on
stderr.
"""

import base64
import json
import sys
import threading
import time
from datetime import UTC, datetime
from enum import StrEnum

import typer
from rich.console import Console
from rich.syntax import Syntax

from SpiriSynq import _core, completion
from SpiriSynq.session import Session, current_session

YAML = "application/yaml"
DONE = "x-spirisynq/done"

# Completion scripts come from `synq completion`; Typer's own install would replace our shell
# classes.
app = typer.Typer(add_completion=False)
completion.register()
topic_app = typer.Typer(help="Objects and the keys under them.")
app.add_typer(topic_app, name="topic")
zenoh_app = typer.Typer(help="The zenoh network itself.")
app.add_typer(zenoh_app, name="zenoh")
meta_app = typer.Typer(help="Types.")
app.add_typer(meta_app, name="meta")

console_out = Console(file=sys.stdout, highlight=False)
"""Data, for pipes."""
console_err = Console(file=sys.stderr)
"""Progress and errors."""


def session() -> Session:
    """The session commands use: opened on first use, so `synq completion` opens none."""
    return current_session.get()


@app.callback()
def term_callback(
    truncate: bool = typer.Option(
        None,
        "--truncate/--no-truncate",
        help="Truncate long lines (default: yes on a terminal, no when piped)",
    ),
):
    if not (truncate if truncate is not None else sys.stdout.isatty()):
        console_out.soft_wrap = True


def fail(message: str, code: int = 1):
    console_err.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)


def resolve(topic: str) -> str:
    """A topic argument, with `./` for the base topic; exits on one that isn't a topic. A
    trailing `/`, as completion leaves one, is dropped."""
    try:
        return session().resolve_topic(topic.rstrip("/") or topic)
    except ValueError as e:
        fail(str(e))


def out_overflow():
    """Ellipsis for long lines when truncating. An explicit overflow overrides soft wrapping,
    so it's only passed when truncating."""
    return None if console_out.soft_wrap else "ellipsis"


def show(text: str, lexer: str = "yaml") -> None:
    console_out.print(
        Syntax(text.rstrip("\n"), lexer, theme="ansi_dark", background_color="default"),
        overflow=out_overflow(),
    )


def dump(value) -> str:
    return _core.yaml_dump(value).decode()


def document(value) -> None:
    """One record of a multi-document YAML stream."""
    console_out.print("---")
    show(dump(value))


def payload_text(message) -> tuple[str, bool]:
    """A payload as text, and whether it's binary: a payload that isn't YAML or UTF-8 text is
    `!!binary` and base64."""
    if message.encoding == YAML or message.encoding.startswith("text/"):
        try:
            return message.payload.decode().strip(), False
        except UnicodeDecodeError:
            pass
    return f"!!binary {base64.b64encode(message.payload).decode('ascii')}", True


def payload_value(message):
    """A payload as a value: YAML decoded, anything else as bytes."""
    if message.encoding == YAML:
        try:
            return _core.yaml_load(message.payload)
        except _core.SynqError:
            pass
    return message.payload


def interactive() -> bool:
    """Whether stdin is a terminal, not a pipe."""
    return sys.stdin.isatty()


def parse(text: str):
    """A value written on the command line: YAML, else the text itself."""
    try:
        return _core.yaml_load(text.encode())
    except _core.SynqError:
        return text


# --- topic ---------------------------------------------------------------------------------


@topic_app.command("list")
def topic_list(
    type_filter: str = typer.Option(
        "",
        "--type",
        "-t",
        help="Only objects of this type: a class name or a type $id",
        autocompletion=completion.complete_tag,
    ),
    prefix: str = typer.Option(
        None,
        "--prefix",
        "-p",
        help="Only topics under this",
        autocompletion=completion.complete_topic,
    ),
    timeout: float = typer.Option(2.0, "--timeout", help="Seconds to wait for answers"),
):
    """List the objects on the network: each one's topic, owner, type and base types."""
    found = 0
    for meta in session().list_topics(type_filter, resolve(prefix) if prefix else "", timeout):
        meta.pop("classes")
        document(meta)
        found += 1
    console_err.print(f"[dim]{found} object(s)[/dim]")


@topic_app.command("watch")
def topic_watch(
    topic: str = typer.Argument(
        ...,
        help="Key expression to watch (wildcards work)",
        autocompletion=completion.complete_topic,
    ),
    show_paths: bool = typer.Option(
        None, "--show-paths/--no-show-paths", "-p/-P", help="Include each sample's key"
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="NDJSON, with the value as its text"
    ),
    show_timestamp: bool = typer.Option(
        False, "--timestamp/--no-timestamp", "-t/-T", help="Include when it was sent, if known"
    ),
    show_received_timestamp: bool = typer.Option(
        True,
        "--received-timestamp/--no-received-timestamp",
        "-rt/-RT",
        help="Include when it arrived",
    ),
    count: int = typer.Option(0, "--count", "-n", help="Stop after N samples (0: never)"),
):
    """Watch what's put on a topic, as a YAML stream, or JSON lines.

    Streams (`@` fields) aren't matched by wildcards: watch them by their key.
    """
    topic = resolve_pattern(topic)
    if show_paths is None:
        show_paths = "*" in topic
    console_err.print(f"[dim]Watching [bold]{topic}[/bold] (Ctrl+C to stop)[/dim]")
    watch = session().node.watch(topic)
    received = 0
    try:
        while not count or received < count:
            message = watch.recv()
            text, binary = payload_text(message)
            meta = {}
            if show_paths:
                meta["path"] = message.key
            if show_timestamp:
                stamp = message.timestamp
                meta["timestamp"] = (
                    None if stamp is None else datetime.fromtimestamp(stamp, UTC).isoformat()
                )
            if show_received_timestamp:
                meta["received"] = datetime.now(UTC).isoformat()
            if json_output:
                record = {**meta, "value": text} if meta else text
                show(json.dumps(record, separators=(",", ":")), "json")
            elif meta:
                lines = [f"{k}: {'null' if v is None else v}" for k, v in meta.items()]
                if "\n" in text and not binary:
                    indented = "\n".join(f"  {line}" for line in text.splitlines())
                    lines.append(f"value: |-\n{indented}")
                else:
                    lines.append(f"value: {text}")
                show("\n".join(lines))
                console_out.print("---")
            else:
                show(text)
            received += 1
    except KeyboardInterrupt:
        console_err.print("\n[dim]Stopped.[/dim]")
    finally:
        watch.close()


def resolve_pattern(topic: str) -> str:
    """A key expression argument: `./` for the base topic, wildcards allowed."""
    if topic == "." or topic.startswith("./"):
        return session().base_topic + topic[1:]
    return topic


def rate(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}/s"
        n /= 1024
    return f"{n:.1f} TB/s"


@topic_app.command("bandwidth")
def topic_bandwidth(
    topic: str = typer.Argument(
        ...,
        help="Key expression to watch (wildcards work)",
        autocompletion=completion.complete_topic,
    ),
    interval: float = typer.Option(1.0, "--interval", "-i", help="Seconds between reports"),
    raw_bytes: bool = typer.Option(
        False, "--bytes", help="Print each interval's byte count to stdout instead"
    ),
    reports: int = typer.Option(0, "--count", "-n", help="Stop after N reports (0: never)"),
):
    """Measure what's put on a topic, in bytes and samples per second."""
    topic = resolve_pattern(topic)
    console_err.print(f"[dim]Watching [bold]{topic}[/bold] (Ctrl+C to stop)[/dim]")
    watch = session().node.watch(topic)
    counts = [0, 0]
    lock = threading.Lock()
    stop = threading.Event()

    def count():
        while not stop.is_set():
            message = watch.recv(0.1)
            if message is not None:
                with lock:
                    counts[0] += len(message.payload)
                    counts[1] += 1

    counter = threading.Thread(target=count, daemon=True)
    counter.start()
    done = 0
    try:
        while not reports or done < reports:
            time.sleep(interval)
            with lock:
                (size, samples), counts[:] = counts, [0, 0]
            if raw_bytes:
                console_out.print(size)
            else:
                console_err.print(f"{rate(size / interval)}  ({samples / interval:.1f} msg/s)")
            done += 1
    except KeyboardInterrupt:
        console_err.print("\n[dim]Stopped.[/dim]")
    finally:
        stop.set()
        counter.join()
        watch.close()


class InputType(StrEnum):
    auto = "auto"
    yaml = "yaml"
    raw = "raw"


@topic_app.command("put")
def topic_put(
    topic: str = typer.Argument(
        None,
        help=(
            "Key to put on. Optional if the input has `path:` fields; with them, it's a prefix "
            "every path must be under."
        ),
        autocompletion=completion.complete_topic,
    ),
    value: str = typer.Argument(None, help="Value to put (omit to read stdin)"),
    input_type: InputType = typer.Option(
        InputType.auto,
        "--input-type",
        "-i",
        help=(
            "How to read the value: 'yaml' must be YAML, 'auto' is YAML if it reads as YAML "
            "and text otherwise, 'raw' is all of stdin as one text value."
        ),
    ),
):
    """Put a value on a key, as a field write: from the command line, or a stream on stdin.

    The stream is YAML documents, such as `topic watch` writes. A document with a `path:` is
    put on that path, its `value:` the value; any other is a value for TOPIC.
    """
    if topic is not None:
        topic = resolve(topic)

    def put(key: str, value) -> None:
        text = dump(value)
        console_err.print(f"[dim]Putting {key}: {text.strip()}[/dim]")
        session().node.put(key, text.encode())

    def read(text: str):
        if input_type == InputType.raw:
            return text
        try:
            return _core.yaml_load(text.encode())
        except _core.SynqError as e:
            if input_type == InputType.yaml:
                fail(f"not YAML: {e}")
            return text

    if value is not None:  # TOPIC comes first, so it's given too
        put(topic, read(value))
        return
    if interactive():
        fail("no value given, and stdin is a terminal")
    if input_type == InputType.raw:
        if topic is None:
            fail("--input-type raw needs a topic")
        put(topic, sys.stdin.read().strip())
        return

    def put_document(text: str) -> None:
        if not text.strip():
            return
        parsed = read(text)
        if isinstance(parsed, dict) and "path" in parsed:
            path = resolve(parsed["path"])
            if topic is not None and path != topic and not path.startswith(f"{topic}/"):
                fail(f"{path} isn't under {topic}")
            put(path, parsed.get("value"))
        elif topic is not None:
            put(topic, parsed)
        else:
            console_err.print(f"[red]No path and no topic, skipped:[/red] {text.strip()}")

    buffer: list[str] = []
    for line in sys.stdin:
        if line.strip() == "---":
            put_document("".join(buffer))
            buffer = []
        else:
            buffer.append(line)
    put_document("".join(buffer))


def schemas(topic: str | None, prefix: str | None, type_filter: str = ""):
    """`(topic, schema)` of the object at `topic`, or of every object under `prefix`."""
    s = session()
    if topic is not None:
        topics = [resolve(topic)]
    else:
        found = s.list_topics(type_filter, resolve(prefix) if prefix else "")
        topics = sorted(meta["topic"] for meta in found)
    for t in topics:
        try:
            yield t, s.fetch_schema(t)
        except LookupError as e:
            console_err.print(f"[yellow]{e}[/yellow]")


@topic_app.command("rpc")
def topic_rpc(
    topic: str = typer.Argument(
        None,
        help="One object's topic (omit to list every object's)",
        autocompletion=completion.complete_topic,
    ),
    prefix: str = typer.Option(
        None,
        "--prefix",
        "-p",
        help="Only objects under this",
        autocompletion=completion.complete_topic,
    ),
):
    """List remote methods: each object's, from its schema."""
    found = 0
    for t, schema in schemas(topic, prefix):
        methods = schema.get("x-remote-methods")
        if methods:
            document({"topic": t, "methods": methods})
            found += 1
    console_err.print(f"[dim]{found} object(s) with remote methods[/dim]")


@topic_app.command("call")
def topic_call(
    topic: str = typer.Argument(
        ...,
        help="The method's key: the object's topic, then the method (my/robot/move_to)",
        autocompletion=completion.complete_method,
    ),
    kwargs: list[str] = typer.Argument(
        default=None,
        help="Arguments, as key=value; each value is read as YAML",
        autocompletion=completion.complete_arguments,
    ),
    timeout: float = typer.Option(None, "--timeout", "-t", help="Seconds to wait for replies"),
):
    """Call a remote method, and print what it returns.

    A generator's values come one document each.

        synq topic call my/robot/move_to x=1.0 y=2.0
    """
    args = {}
    for kv in kwargs or []:
        name, eq, text = kv.partition("=")
        if not eq:
            fail(f"argument '{kv}' isn't key=value")
        args[name] = parse(text)
    key = resolve(topic)
    console_err.print(f"[dim]Calling {key}[/dim]")
    s = session()
    replies = s.node.get(key, payload=_core.yaml_dump(args), timeout=timeout or s.call_timeout)
    answered = False
    for reply in replies:
        answered = True
        if not reply.ok:
            error = payload_value(reply)
            if isinstance(error, dict) and {"type", "message"} <= error.keys():
                error = f"{error['type']}: {error['message']}"
            fail(f"{key}: {error}")
        if reply.encoding == DONE:
            break
        text, _ = payload_text(reply)
        console_out.print("---")
        show(text)
    else:
        if answered:
            fail(f"{key}: the call was cut short", 2)
    if not answered:
        fail("no reply: nothing serves that method, or it didn't answer in time", 2)
    console_err.print("[dim]done[/dim]")


@topic_app.command("schema")
def topic_schema(
    topic: str = typer.Argument(
        ..., help="The object's topic", autocompletion=completion.complete_topic
    ),
):
    """Print the schema an object serves."""
    try:
        schema = session().fetch_schema(resolve(topic))
    except LookupError as e:
        console_err.print(f"[yellow]No schema: {e}[/yellow]")
        return
    show(dump(schema))


@topic_app.command("rehydrate")
def topic_rehydrate(
    topic: str = typer.Argument(
        ..., help="The object's topic", autocompletion=completion.complete_topic
    ),
):
    """Print an object's state, as its owner holds it: each field by its path.

    What a new mirror fetches. Streams (`@` fields) and fields that skip the state aren't in it.
    """
    topic = resolve(topic)
    s = session()
    state = {}
    for reply in s.node.get(f"{topic}/sr_rehydrate", timeout=s.call_timeout):
        if reply.ok and reply.encoding != DONE:
            state[reply.key.removeprefix(f"{topic}/")] = payload_value(reply)
    if not state:
        console_err.print(f"[yellow]No state from {topic}[/yellow]")
        return
    show(dump(state))


# --- zenoh ---------------------------------------------------------------------------------


@zenoh_app.command("info")
def zenoh_info():
    """Show this session's zenoh id, where it listens, and who it's connected to."""
    node = session().node
    show(
        dump(
            {
                "zid": node.zid,
                "locators": node.locators(),
                "routers": node.routers(),
                "peers": node.peers(),
            }
        )
    )


@zenoh_app.command("scout")
def zenoh_scout(
    timeout: float = typer.Option(1.0, "--timeout", "-t", help="Seconds to scout for"),
    routers: bool = typer.Option(True, "--routers/--no-routers", help="Look for routers"),
    peers: bool = typer.Option(True, "--peers/--no-peers", help="Look for peers"),
    clients: bool = typer.Option(False, "--clients/--no-clients", help="Look for clients"),
    config: str = typer.Option(
        "{}", "--config", "-c", help="zenoh configuration for scouting, as JSON5"
    ),
):
    """Find zenoh routers and peers, by multicast scouting."""
    what = "|".join(
        kind
        for kind, wanted in [("router", routers), ("peer", peers), ("client", clients)]
        if wanted
    )
    if not what:
        fail("look for at least one of --routers, --peers, --clients")
    console_err.print(f"[dim]Scouting ({timeout}s)...[/dim]")
    found = _core.scout(what, timeout, config)
    for zid, kind, locators in found:
        document({"zid": zid, "whatami": kind, "locators": locators})
    console_err.print(f"[dim]{len(found)} node(s) found[/dim]")


# --- meta ----------------------------------------------------------------------------------


@meta_app.command("type_schema")
def meta_type_schema(
    type_filter: str = typer.Argument(
        "",
        help="A class name or type $id (omit for every type)",
        autocompletion=completion.complete_tag,
    ),
    prefix: str = typer.Option(
        None,
        "--prefix",
        "-p",
        help="Only objects under this",
        autocompletion=completion.complete_topic,
    ),
):
    """Print the schema of every object of a type, or of its subtypes: a `{topic, schema}`
    document each."""
    found = 0
    for t, schema in schemas(None, prefix, type_filter):
        document({"topic": t, "schema": schema})
        found += 1
    if not found:
        console_err.print(f"[yellow]No objects of type '{type_filter}'[/yellow]")


# --- completion ----------------------------------------------------------------------------


@app.command("completion")
def completion_script(shell: completion.Shell):
    """Print the tab-completion script for SHELL. It completes topics, types, remote methods
    and their arguments.

    zsh: synq completion zsh > ~/.zfunc/_synq, with ~/.zfunc in $fpath before compinit.
    bash: synq completion bash > ~/.local/share/bash-completion/completions/synq, or source the
    script from ~/.bashrc. Both cover synq and spirisynq. Regenerate it after upgrading.
    """
    typer.echo(completion.script(shell), nl=False)


@app.command("_complete-server", hidden=True)
def complete_server(
    idle: float = typer.Option(600.0, "--idle", help="Exit after this many seconds without a Tab."),
    connect: list[str] = typer.Option(
        [], "--connect", help="zenoh endpoint to connect to, without scouting."
    ),
):
    """Answer tab completion over a socket, with a session kept open. Started by the
    completion script."""
    from loguru import logger

    from SpiriSynq.completion.server import serve

    logger.remove()
    serve(connect, idle=idle)


if __name__ == "__main__":  # pragma: no cover
    app()
