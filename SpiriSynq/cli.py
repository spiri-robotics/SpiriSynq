import sys
import typer
from SpiriSynq.session import Session
from rich.console import Console
from rich.syntax import Syntax
import base64
import zenoh

app = typer.Typer()
topic_app = typer.Typer()
app.add_typer(topic_app, name="topic")

session = Session()

# stdout console for data output (pipeable)
console_out = Console(file=sys.stdout, highlight=False)
# stderr console for logging/status
console_err = Console(file=sys.stderr)

@app.callback()
def term_callback(
    ctx: typer.Context,
    truncate: bool = typer.Option(None, "--truncate/--no-truncate", help="Truncate long lines (default: yes on TTY, no when piped)"),
):
    if not (truncate if truncate is not None else sys.stdout.isatty()):
        console_err.print("[dim]stdout truncation disabled[/dim]")
        console_out.soft_wrap = True


@topic_app.command("list")
def topic_list(
    _type: str = typer.Option("", "--type", "-t", help="Filter by type"),
    prefix: str = typer.Option(None, "--prefix", "-p", help="Key prefix"),
):
    """List all topics"""
    query_topic = f"{prefix}/**/sr_metadata/{_type}" if prefix else f"**/sr_metadata/{_type}"
    query_topic = query_topic.strip("/").removesuffix("/")

    console_err.print(f"[dim]Querying: {query_topic}[/dim]")

    replies = session.zenoh_session.get(query_topic)

    found = 0
    for reply in replies:
        if reply.ok:
            raw = reply.ok.payload.to_bytes().decode("utf-8")
            syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    console_err.print(f"[dim]{found} result(s)[/dim]")

@topic_app.command("watch")
def topic_watch(
    topic: str = typer.Argument(..., help="Topic key expression to subscribe to (wildcards supported)"),
    show_paths: bool = typer.Option(None, "--show-paths/--no-show-paths", "-p/-P", help="Include key path in output"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as NDJSON (value kept as raw string)"),
    show_timestamp: bool = typer.Option(False, "--timestamp/--no-timestamp", "-t/-T", help="Include Zenoh message timestamp in output"),
    show_received_timestamp: bool = typer.Option(True, "--received-timestamp/--no-received-timestamp", "-rt/-RT", help="Include when we received the message"),
    count: int = typer.Option(0, "--count", "-n", help="Exit after receiving N messages (0 = unlimited)"),
):
    """Watch a topic for incoming messages as a newline-delimited YAML or JSON stream."""
    import time
    import json
    import threading
    from datetime import datetime, timezone

    # Default to showing paths when using wildcards, since the key helps identify which topic fired
    if show_paths is None:
        show_paths = "*" in topic

    console_err.print(f"[dim]Subscribing to: [bold]{topic}[/bold] (Ctrl+C to stop)[/dim]")

    # --- Helpers ---

    def decode_payload(sample) -> tuple[str, bool]:
        """Return (raw_string, is_binary) for a sample's payload."""
        if sample.encoding == zenoh.Encoding.ZENOH_BYTES:
            b64 = base64.b64encode(sample.payload.to_bytes()).decode("ascii")
            return f"!!binary {b64}", True
        return sample.payload.to_bytes().decode("utf-8").strip(), False

    def build_metadata(key: str, sample) -> dict:
        """Collect any requested metadata fields into an ordered dict."""
        meta = {}
        if show_paths:
            meta["path"] = key
        if show_timestamp:
            meta["timestamp"] = (
                sample.timestamp.time.isoformat() if sample.timestamp is not None else None
            )
        if show_received_timestamp:
            meta["received"] = datetime.now(timezone.utc).isoformat()
        return meta

    def emit_json(meta: dict, raw: str, is_binary: bool):
        # Binary values are already base64-encoded in raw; pass them through as-is
        value_out = raw if is_binary else raw

        if meta:
            # Merge metadata and value into a single JSON object
            line = json.dumps({**meta, "value": value_out}, separators=(",", ":"))
        else:
            # No metadata — emit the bare value
            line = json.dumps(value_out, separators=(",", ":"))

        console_out.print(Syntax(line, "json", theme="ansi_dark", background_color="default"),overflow="ellipsis")

    def emit_yaml(meta: dict, raw: str, is_binary: bool):
        if meta:
            parts = [f"{k}: {v}" for k, v in meta.items()]

            if is_binary:
                parts.append(f"value: {raw}")
            else:
                if "\n" in raw:
                    indented = "\n".join(f"  {line}" for line in raw.splitlines())
                    parts.append(f"value: |-\n{indented}")
                else:
                    parts.append(f"value: {raw}")

            out = "\n".join(parts)
        else:
            out = raw

        console_out.print(Syntax(out, "yaml", theme="ansi_dark", background_color="default"),overflow="ellipsis")

        # Separate records with a YAML document delimiter when metadata is present
        if meta:
            console_out.print("---")

    # --- Subscriber callback ---

    done = threading.Event()
    received = [0]

    def on_sample(sample):
        key = str(sample.key_expr)
        raw, is_binary = decode_payload(sample)
        meta = build_metadata(key, sample)

        if json_output:
            emit_json(meta, raw, is_binary)
        else:
            emit_yaml(meta, raw, is_binary)

        if count:
            received[0] += 1
            if received[0] >= count:
                done.set()

    # --- Main loop ---

    subscriber = session.zenoh_session.declare_subscriber(topic, on_sample)

    try:
        if count:
            done.wait()
        else:
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        console_err.print("\n[dim]Stopped.[/dim]")
    finally:
        # Always undeclare the subscriber to cleanly release the Zenoh resource
        subscriber.undeclare()

from enum import Enum

class InputType(str, Enum):
    auto = "auto"
    yaml = "yaml"   # parse as YAML/JSON (YAML is a superset)
    raw  = "raw"    # treat entire stdin as a single raw string value, no parsing


@topic_app.command("put")
def topic_put(
    topic: str = typer.Argument(None, help=(
        "Topic key to publish to. "
        "Optional if the stream contains 'path:' fields. "
        "If provided alongside a stream with 'path:' fields, the stream path "
        "must be a subpath of this topic (used as a prefix guard)."
    )),
    value: str = typer.Argument(None, help="Value to publish directly (omit to read from stdin)"),
    input_type: InputType = typer.Option(
        InputType.auto,
        "--input-type", "-i",
        help=(
            "How to interpret stdin. "
            "'auto' detects YAML/JSON vs raw by attempting a parse. "
            "'yaml' forces YAML/JSON parsing (YAML is a superset of JSON). "
            "'raw' treats the entire input as a single opaque string value."
        ),
    ),
):
    """Publish to a topic.

    Accepts either a direct topic + value, or a piped stream from `topic watch`.
    The stream may be YAML (default) or JSON — both are handled transparently
    since YAML is a superset of JSON.

    Stream path validation:
        If `topic` is provided and the stream contains `path:` fields, each
        stream path must be a subpath of `topic`. This prevents accidentally
        replaying a stream to the wrong part of the topic tree.
    """
    import sys

    def put(path: str, raw: str):
        console_err.print(f"[dim]Publishing {path}: {raw}[/dim]")
        session.zenoh_session.put(path, raw)

    def validate_subpath(stream_path: str, prefix: str):
        """Raise if stream_path is not a subpath of prefix.

        Ensures that a stream captured from one part of the topic tree
        cannot be accidentally replayed to an unrelated topic.
        """
        if not stream_path.startswith(prefix.rstrip("/") + "/") and stream_path != prefix:
            console_err.print(
                f"[red]Error:[/red] stream path [bold]{stream_path}[/bold] "
                f"is not a subpath of [bold]{prefix}[/bold]"
            )
            raise typer.Exit(1)

    # --- Direct argument mode ---
    # Both topic and value provided on the command line; publish immediately.
    if value is not None:
        if topic is None:
            console_err.print("[red]Error: topic is required when providing a value directly.[/red]")
            raise typer.Exit(1)
        put(topic, value)
        return

    # --- Stdin stream mode ---
    if sys.stdin.isatty():
        console_err.print("[red]Error: no value provided and stdin is a TTY.[/red]")
        raise typer.Exit(1)

    # raw mode: treat entire stdin as a single opaque value, no parsing.
    # Requires topic to be provided since there is no path in the stream.
    if input_type == InputType.raw:
        if topic is None:
            console_err.print("[red]Error: topic is required when using --input-type raw.[/red]")
            raise typer.Exit(1)
        put(topic, sys.stdin.read().strip())
        return

    def process_document(doc: str):
        """Parse and publish a single YAML document from the stream.

        In 'auto' mode, attempts to parse as YAML. If the document contains
        a 'path' key (i.e. came from `topic watch --show-paths`), that path
        is used as the publish target. If a topic prefix was provided as an
        argument, the stream path is validated against it first.

        If no 'path' key is present, the topic argument is used directly
        and the raw document string is published as-is.

        In 'yaml' mode, behaves identically to 'auto' but skips the fallback
        to raw — a parse failure is treated as an error.
        """
        doc = doc.strip()
        if not doc:
            return

        parsed = None
        try:
            parsed = session.type_registry.load(doc)
        except Exception as e:
            if input_type == InputType.yaml:
                console_err.print(f"[red]Error: failed to parse YAML document:[/red] {e}")
                raise typer.Exit(1)
            # auto mode: fall through and treat as raw string

        if isinstance(parsed, dict) and "path" in parsed:
            # Stream came from `topic watch --show-paths`
            stream_path = parsed["path"]
            if topic is not None:
                validate_subpath(stream_path, topic)
            raw = str(parsed.get("value", ""))
            put(stream_path, raw)
        elif topic is not None:
            # Bare value stream — no path in document, use topic argument
            put(topic, doc)
        else:
            console_err.print(
                f"[red]Error: no path in document and no topic argument given, skipping:[/red] {doc}"
            )

    # Buffer lines and split on --- document separators
    buffer = []
    for line in sys.stdin:
        if line.strip() == "---":
            process_document("".join(buffer))
            buffer = []
        else:
            buffer.append(line)

    # Handle final document with no trailing ---
    if buffer:
        process_document("".join(buffer))


@topic_app.command("rpc")
def topic_rpc(
    prefix: str = typer.Option(None, "--prefix", "-p", help="Key prefix to search under"),
    topic: str = typer.Argument(None, help="Specific topic path (omit to search all topics)"),
):
    """List all RPC endpoints across topics.

    Queries sr_object_schema for each topic and extracts x-rpc-endpoints.
    Use --prefix to narrow the search, or pass a specific topic path as an argument.
    """
    if topic:
        query_topic = f"{topic}/sr_object_schema"
    elif prefix:
        query_topic = f"{prefix}/**/sr_object_schema"
    else:
        query_topic = "**/sr_object_schema"

    console_err.print(f"[dim]Querying: {query_topic}[/dim]")

    replies = session.zenoh_session.get(query_topic)

    found = 0
    for reply in replies:
        if reply.ok:
            topic_path = str(reply.ok.key_expr).removesuffix("/sr_object_schema")
            raw = reply.ok.payload.to_bytes().decode("utf-8")
            try:
                schema = session.type_registry.load(raw)
            except Exception as e:
                console_err.print(f"[red]Failed to parse schema for {topic_path}:[/red] {e}")
                continue

            if not isinstance(schema, dict):
                continue

            rpc_endpoints = schema.get("x-rpc-endpoints", {})
            if not rpc_endpoints:
                continue

            record = {"topic": topic_path, "endpoints": rpc_endpoints}
            out = session.type_registry.dumps(record).strip()
            syntax = Syntax(out, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            console_out.print("---")
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    console_err.print(f"[dim]{found} topic(s) with RPC endpoints[/dim]")


@topic_app.command("call")
def topic_call(
    topic: str = typer.Argument(..., help="Full RPC path including method name (e.g. my/topic/method_name)"),
    kwargs: list[str] = typer.Argument(default=None, help="Arguments as key=value pairs (values parsed as YAML)"),
    timeout: float = typer.Option(None, "--timeout", "-t", help="Timeout in seconds"),
):
    """Call an RPC endpoint and display the result.

    Arguments are passed as key=value pairs where values are interpreted as YAML literals.
    Generator endpoints stream each yielded value separated by ---.

    Examples:

        synq topic call my/robot/move_to x=1.0 y=2.0

        synq topic call my/sensor/read_frames count=10
    """
    import zenoh as _zenoh
    from SpiriSynq.remote_callables import GENERATOR_DONE_ENCODING

    parsed_kwargs = {}
    for kv in (kwargs or []):
        if "=" not in kv:
            console_err.print(f"[red]Error: argument '{kv}' must be in key=value format[/red]")
            raise typer.Exit(1)
        k, v = kv.split("=", 1)
        parsed_kwargs[k] = v

    selector = _zenoh.Selector(topic, _zenoh.Parameters(parsed_kwargs))
    console_err.print(f"[dim]Calling: {selector}[/dim]")

    get_kwargs: dict = dict(consolidation=_zenoh.QueryConsolidation(_zenoh.ConsolidationMode.NONE))
    if timeout is not None:
        get_kwargs["timeout"] = timeout

    item_count = 0
    for reply in session.zenoh_session.get(selector, **get_kwargs):
        if reply.err:
            console_err.print(f"[red]RPC error:[/red] {reply.err.payload.to_string()}")
            raise typer.Exit(1)

        raw = reply.ok.payload.to_string().strip()

        if reply.ok.encoding == GENERATOR_DONE_ENCODING:
            if raw and raw not in ("null", "~"):
                if item_count > 0:
                    console_out.print("---")
                syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
                console_out.print(syntax)
            break

        if item_count > 0:
            console_out.print("---")
        syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
        console_out.print(syntax)
        item_count += 1

    console_err.print(f"[dim]done[/dim]")


@topic_app.command("schema")
def topic_schema(
    topic: str = typer.Argument(..., help="Topic path to retrieve schema for"),
):
    """Retrieve and display the schema for a topic."""
    query_path = f"{topic}/sr_object_schema"
    console_err.print(f"[dim]Querying: {query_path}[/dim]")

    replies = session.zenoh_session.get(query_path)

    found = 0
    for reply in replies:
        if reply.ok:
            raw = reply.ok.payload.to_bytes().decode("utf-8").strip()
            syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    if found == 0:
        console_err.print(f"[yellow]No schema found for {topic}[/yellow]")
    else:
        console_err.print(f"[dim]{found} result(s)[/dim]")


@topic_app.command("rehydrate")
def topic_rehydrate(
    topic: str = typer.Argument(..., help="Topic path to rehydrate current state for"),
):
    """Retrieve the current state of a topic via the rehydrate queryable.

    This is useful when you suspect your local state is out of sync, or when
    connecting to a network without a caching router. Queries the topic path
    directly rather than the metadata or schema sub-paths.

    Emits a full yaml object instead of changes to sub topics.
    """
    topic = f"{topic}/sr_rehydrate"
    console_err.print(f"[dim]Querying: {topic}[/dim]")

    replies = session.zenoh_session.get(topic)

    found = 0
    for reply in replies:
        if reply.ok:
            raw = reply.ok.payload.to_bytes().decode("utf-8").strip()
            syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    if found == 0:
        console_err.print(f"[yellow]No response from {topic}[/yellow]")
    else:
        console_err.print(f"[dim]{found} result(s)[/dim]")

zenoh_app = typer.Typer()
app.add_typer(zenoh_app, name="zenoh")

@zenoh_app.command("info")
def zenoh_info():
    """Show information about the current zenoh session."""
    info = session.zenoh_session.info

    record = {
        "zid": str(info.zid()),
        "routers": [str(r) for r in info.routers_zid()],
        "peers": [str(p) for p in info.peers_zid()],
    }

    out = session.type_registry.dumps(record).strip()
    syntax = Syntax(out, "yaml", theme="ansi_dark", background_color="default")
    console_out.print(syntax)

@zenoh_app.command("scout")
def zenoh_scout(
    timeout: float = typer.Option(1.0, "--timeout", "-t", help="Scouting duration in seconds"),
    routers: bool = typer.Option(True, "--routers/--no-routers", help="Scout for routers"),
    peers: bool = typer.Option(True, "--peers/--no-peers", help="Scout for peers"),
    clients: bool = typer.Option(False, "--clients/--no-clients", help="Scout for clients"),
):
    """Discover zenoh routers and peers on the network."""
    import zenoh
    import threading

    what = None
    for enabled, variant in [
        (routers, zenoh.WhatAmI.ROUTER),
        (peers,   zenoh.WhatAmI.PEER),
        (clients, zenoh.WhatAmI.CLIENT),
    ]:
        if enabled:
            what = variant if what is None else what | variant

    if what is None:
        console_err.print("[red]Error: at least one of --routers, --peers, --clients must be enabled.[/red]")
        raise typer.Exit(1)

    console_err.print(f"[dim]Scouting ({timeout}s)...[/dim]")

    found = 0
    scout = zenoh.scout(what=what)

    # Stop scouting after the timeout
    timer = threading.Timer(timeout, scout.stop)
    timer.start()

    try:
        for hello in scout:
            record = {
                "zid":      str(hello.zid),
                "whatami":  str(hello.whatami),
                "locators": list(hello.locators),
            }
            out = session.type_registry.dumps(record).strip().removesuffix("\n...")
            syntax = Syntax(out, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            console_out.print("---")
            found += 1
    finally:
        timer.cancel()

    console_err.print(f"[dim]{found} node(s) found[/dim]")

meta_app = typer.Typer()
app.add_typer(meta_app, name="meta")

@meta_app.command("type_schema")
def meta_type_schema(
    _type: str = typer.Argument("*", help="Type name to retrieve schema for (defaults to all types)"),
    prefix: str = typer.Option("**", "--prefix", "-p", help="Key prefix (defaults to entire tree)"),
):
    """Retrieve and display the schema for a registered type.

    If no type is given, lists all known types across the network.
    Queries <prefix>/sr_type_schema/<type> across the network.
    """
    query_path = f"{prefix}/sr_type_schema/{_type}"
    console_err.print(f"[dim]Querying: {query_path}[/dim]")

    replies = session.zenoh_session.get(query_path)

    found = 0
    for reply in replies:
        if reply.ok:
            raw = reply.ok.payload.to_bytes().decode("utf-8").strip()
            syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    if found == 0:
        console_err.print(f"[yellow]No schema found for type '{_type}'[/yellow]")
    else:
        console_err.print(f"[dim]{found} result(s)[/dim]")

import click
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

console = Console()

def _print_all_help(cmd: click.Command, ctx: click.Context):
    """Recursively print help for all commands."""
    # Print section header
    console.print()
    console.print(Rule(Text(ctx.command_path, style="bold cyan"),characters="#"))

    # Format and print help text
    formatter = ctx.make_formatter()
    cmd.format_help(ctx, formatter)
    console.print(formatter.getvalue())

    # Recurse into subcommands
    if isinstance(cmd, click.Group):
        for sub_name, sub_cmd in cmd.commands.items():
            sub_ctx = click.Context(sub_cmd, parent=ctx, info_name=sub_name)
            _print_all_help(sub_cmd, sub_ctx)


@app.command("help-all")
def help_cmd():
    """Show help for all commands (like a man page)."""
    click_app = typer.main.get_command(app)
    root_ctx = click.Context(click_app, info_name="synq")
    _print_all_help(click_app, root_ctx)


if __name__ == "__main__":
    app()
