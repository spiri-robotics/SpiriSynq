"""Tab completion for ``synq``: topics, type tags, RPC methods and their arguments.

A Tab press can't wait for Python to start, let alone for a fresh zenoh
session to learn what's out there. So the completion script asks a server
instead: ``synq _complete-server`` (server.py), one warm process with a
zenoh session open, that runs this same completion code and exits after
``IDLE`` seconds without a Tab. The shell talks to it itself, so no Python
starts: zsh over a Unix socket (``zsh/net/socket``), bash over TCP on
127.0.0.1 with a token, as bash can't open a Unix socket.

When the server isn't running the script falls back to running ``synq``
the way Typer's does, which answers from the topics and tags the server
last wrote to ``~/.cache/spirisynq/topics`` (no RPC methods) and starts the
server for the next Tab.

What completes:

- a topic, one ``/`` chunk at a time, from every object answering
  ``**/sr_metadata``; ``./`` stands for the base topic, as ``resolve_topic``
  reads it.
- a type tag (``--type``, ``meta type_schema``), from the same replies.
- ``topic call``'s method, below an object's topic, and its ``key=value``
  arguments: the keys from the method's ``parameters`` in the object's
  schema, the values from each one's ``enum``, ``boolean`` or ``null``
  type and its default.

Typer's bash and zsh classes are replaced:

- bash splits words at ``=``, so ``mode=fa`` reaches the completion
  function as ``mode`` ``=`` ``fa``. Ours hands Click the raw line instead
  and trims each answer back to bash's last word.
- zsh adds a space after a single match. A candidate ending in ``/`` or
  ``=`` is waiting for more, so it gets none (bash: ``compopt -o nospace``).
- Nothing synq completes is a local file, so neither shell falls back to
  files.
"""
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

import click
import yaml
from click.shell_completion import add_completion_class, get_completion_class, split_arg_string
# Typer's shell classes: private, but what --install-completion itself uses.
from typer import _completion_classes as typer_shells

PROGS = ("synq", "spirisynq")
PROTOCOL = "1"
"""First field of a request. A server that doesn't speak it closes the connection, and the shell falls back."""

START_GAP = 5.0
"""Seconds after starting a server before the fallback starts another, so a burst of Tabs starts one."""

MORE = ("/", "=")
"""A candidate ending in one of these is waiting for more: no space after it."""


def cache_path(environ: Mapping[str, str] = os.environ) -> Path:
    base = environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "spirisynq" / "topics"


def runtime_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """Where the server's socket lives: ``$XDG_RUNTIME_DIR/spirisynq``, else next to the cache.

    The scripts work it out the same way, as
    ``${XDG_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}}/spirisynq``.
    """
    base = environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "spirisynq" if base else cache_path(environ).parent


def read_topics(path: Path) -> dict[str, list[str]]:
    """The cache: each topic with its tags, one ``topic<TAB>Tag Tag`` per line."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    topics = {}
    for line in lines:
        topic, _, tags = line.partition("\t")
        topics[topic] = tags.split()
    return topics


def write_topics(path: Path, topics: Mapping[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text("".join(f"{topic}\t{' '.join(topics[topic])}\n" for topic in sorted(topics)))
    os.replace(tmp, path)


# ── What the callbacks complete from ────────────────────────────────────

class CachedSource:
    """Without the server: the topics and tags it last wrote, and no schemas."""

    def topics(self) -> dict[str, list[str]]:
        return read_topics(cache_path())

    def base_topic(self) -> str:
        from SpiriSynq.session import _default_base_topic
        return _default_base_topic()

    def schema(self, topic: str) -> dict | None:
        return None


source = CachedSource()
"""The server swaps in its own (server.LiveSource)."""


def chunks(keys, incomplete: str) -> list[str]:
    """``keys`` completed one ``/`` chunk past ``incomplete``: ``a/b`` → ``a/bc`` and ``a/bd/``.

    A key that's also a prefix of others gives both. ``sr_`` chunks only when
    asked for by name, as shells hide dotfiles.
    """
    head = incomplete[:incomplete.rfind("/") + 1]
    hidden = not incomplete[len(head):].startswith("sr_")
    out = set()
    for key in keys:
        if key.startswith(incomplete):
            chunk, slash, _ = key[len(head):].partition("/")
            if chunk and not (hidden and chunk.startswith("sr_")):
                out.add(head + chunk + slash)
    return sorted(out)


def relative(incomplete: str, complete) -> list:
    """``complete`` for a topic that may start with ``./``, which stands for the base topic."""
    base = source.base_topic()
    if incomplete in ("", "."):
        under = any(t.startswith(f"{base}/") for t in source.topics())
        return (["./"] if under else []) + ([] if incomplete else complete(""))
    if not incomplete.startswith("./"):
        return complete(incomplete)
    out = []
    for c in complete(base + incomplete[1:]):
        value, help = c if isinstance(c, tuple) else (c, None)
        if value.startswith(f"{base}/"):
            value = "." + value[len(base):]
            out.append((value, help) if help is not None else value)
    return out


def complete_topic(incomplete: str) -> list[str]:
    """Autocompletion for a topic or a key prefix."""
    return relative(incomplete, lambda inc: chunks(source.topics(), inc))


def complete_tag(incomplete: str) -> list[str]:
    """Autocompletion for a type tag, without its ``!``."""
    tags = {tag.lstrip("!") for tags in source.topics().values() for tag in tags}
    return sorted(t for t in tags if t.startswith(incomplete))


def endpoints(topic: str) -> dict:
    schema = source.schema(topic)
    return (schema or {}).get("x-rpc-endpoints") or {}


def first_line(text: str | None) -> str:
    return (text or "").strip().partition("\n")[0]


def complete_method(incomplete: str) -> list:
    """Autocompletion for ``topic call``'s ``TOPIC/METHOD``: topics, then the methods below an object's."""
    def complete(inc: str) -> list:
        topics = source.topics()
        found: list = chunks((f"{t}/" for t in topics), inc)    # a topic alone isn't a method
        topic = inc[:inc.rfind("/")]
        if topic in topics:
            methods = endpoints(topic)
            names = chunks((f"{topic}/{name}" for name in methods), inc)
            found = [v for v in found if v not in names]
            found += [(v, first_line(methods[v.rpartition("/")[2]].get("description"))) for v in names]
        return found
    return relative(incomplete, complete)


def describe(schema: dict, defs: dict) -> str:
    """A value schema in a few words: ``fast | slow``, ``number | null``, ``Pose``."""
    if "$ref" in schema:
        return schema["$ref"].rpartition("/")[2]
    if "x-python-type" in schema:
        return schema["x-python-type"]
    if "enum" in schema:
        return " | ".join(scalar(v) for v in schema["enum"])
    if "const" in schema:
        return scalar(schema["const"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(describe(s, defs) for s in schema[key])
    types = schema.get("type", "any")
    return " | ".join(types) if isinstance(types, list) else types


def scalar(value) -> str:
    """``value`` as it's written in ``key=value``: YAML, as the server parses it."""
    return yaml.safe_dump(value, default_flow_style=True, width=1 << 16).removesuffix("\n...\n").strip()


def values(schema: dict, defs: dict) -> list[str]:
    """The values a value schema names: its enum or const, true/false for a boolean, null, its default."""
    if "$ref" in schema:
        schema = defs.get(schema["$ref"].rpartition("/")[2], {}) | {k: v for k, v in schema.items() if k != "$ref"}
    found = [scalar(v) for v in schema.get("enum", [])]
    if "const" in schema:
        found.append(scalar(schema["const"]))
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    if "boolean" in types:
        found += ["true", "false"]
    if "null" in types:
        found.append("null")
    for key in ("anyOf", "oneOf"):
        for sub in schema.get(key, []):
            found += values(sub, defs)
    if "default" in schema:
        found.append(scalar(schema["default"]))
    return list(dict.fromkeys(found))


def complete_arguments(ctx: click.Context, incomplete: str) -> list:
    """Autocompletion for ``topic call``'s ``key=value`` arguments, from the method's parameters."""
    target = ctx.params.get("topic") or ""
    if target.startswith("./"):
        target = source.base_topic() + target[1:]
    topic, _, method = target.rpartition("/")
    schema = source.schema(topic) if topic else None
    endpoint = ((schema or {}).get("x-rpc-endpoints") or {}).get(method)
    if not endpoint:
        return []
    defs = schema.get("$defs", {})
    parameters = endpoint.get("parameters", {})
    properties = parameters.get("properties", {})
    name, eq, value = incomplete.partition("=")
    if eq:
        if name not in properties:
            return []
        return [f"{name}={v}" for v in values(properties[name], defs) if v.startswith(value)]
    given = {kv.partition("=")[0] for kv in ctx.params.get("kwargs") or []}
    required = parameters.get("required", [])
    out = []
    for pname in sorted(properties, key=lambda p: p not in required):   # required ones first
        if pname.startswith(name) and pname not in given:
            help = describe(properties[pname], defs)
            out.append((f"{pname}=", help + (" (required)" if pname in required else "")))
    return out


# ── Starting the server ─────────────────────────────────────────────────

def server_running(run: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(run / "complete.sock"))
        except OSError:
            return False
    return True


def start_server(environ: Mapping[str, str] = os.environ) -> None:
    """Start ``_complete-server`` detached, unless it's running or was just started."""
    run = runtime_dir(environ)
    stamp = run / "complete.started"
    try:
        if time.time() - stamp.stat().st_mtime < START_GAP:
            return
    except FileNotFoundError:
        run.mkdir(mode=0o700, parents=True, exist_ok=True)
    if server_running(run):
        return
    stamp.touch()
    subprocess.Popen(
        [sys.executable, "-m", "SpiriSynq", "_complete-server"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd="/", start_new_session=True,
    )


# ── Shells ──────────────────────────────────────────────────────────────

RUNTIME_DIR_SH = "${XDG_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}}/spirisynq"
# Click names its variable after the program it runs as, so the fallback sets both. Its
# stderr (the session's log line) would land on the command line being typed.
FALLBACK_ENV = " ".join(f"_{prog.upper()}_COMPLETE=complete_%(shell)s" for prog in PROGS)

BASH = """\
%(complete_func)s() {
    local line=${COMP_LINE:0:COMP_POINT} cur=${COMP_WORDS[COMP_CWORD]}
    local dir=""" + RUNTIME_DIR_SH + """ port token fd reply
    COMPREPLY=()
    if read -r port token 2>/dev/null <"$dir/complete.tcp" &&
       { exec {fd}<>"/dev/tcp/127.0.0.1/$port"; } 2>/dev/null; then
        printf '""" + PROTOCOL + """\\t%%s\\tbash\\t%%s\\t%%s\\t%%s\\n' "$token" "$SPIRI_SYNQ_BASE_TOPIC" "$cur" "$line" >&$fd
        while IFS= read -r -t 2 -u $fd reply; do COMPREPLY+=("$reply"); done
        exec {fd}>&-
    else
        local IFS=$'\\n'
        COMPREPLY=( $(env COMP_LINE="$line" COMP_CUR="$cur" """ + FALLBACK_ENV % {"shell": "bash"} + """ "$1" 2>/dev/null) )
    fi
    if [[ ${#COMPREPLY[@]} == 1 && ${COMPREPLY[0]} == *[/=] ]]; then
        compopt -o nospace
    fi
    return 0
}

complete -F %(complete_func)s """ + " ".join(PROGS) + """
"""

ZSH = """\
#compdef """ + " ".join(PROGS) + """

%(complete_func)s() {
    local dir=""" + RUNTIME_DIR_SH + """ reply fd
    if zmodload zsh/net/socket 2>/dev/null && zsocket "$dir/complete.sock" 2>/dev/null; then
        fd=$REPLY
        print -rn -u $fd -- $'""" + PROTOCOL + """\\t\\tzsh\\t'"$SPIRI_SYNQ_BASE_TOPIC"$'\\t\\t'"${words[1,$CURRENT]}"$'\\n'
        read -r -d '' -t 2 -u $fd reply
        exec {fd}>&-
    else
        reply="$(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" """ + FALLBACK_ENV % {"shell": "zsh"} + """ "${words[1]}" 2>/dev/null)"
    fi
    eval "$reply"
}

# Autoloaded from $fpath, this file is the body of _synq and runs on every Tab
# unless later Tabs go straight to the function it defines.
compdef %(complete_func)s """ + " ".join(PROGS) + """
if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    %(complete_func)s "$@"
fi
"""


class BashComplete(typer_shells.BashComplete):
    """Takes the raw line up to the cursor, and answers relative to bash's last word.

    ``request`` is ``(line, cur)``: the server sets it, the fallback reads them from
    the environment. A script from Typer's ``--install-completion`` sets neither,
    and gets Typer's own handling.
    """
    source_template = BASH
    request: tuple[str, str] | None = None
    from_typer = False

    def get_request(self) -> tuple[str, str] | None:
        if self.request is None and not self.from_typer:
            start_server()
            if "COMP_LINE" in os.environ:
                self.request = os.environ["COMP_LINE"], os.environ.get("COMP_CUR", "")
            else:
                self.from_typer = True
        return self.request

    def get_completion_args(self) -> tuple[list[str], str]:
        request = self.get_request()
        if request is None:
            return super().get_completion_args()
        line, _ = request
        words = split_arg_string(line)
        incomplete = "" if not line or line[-1].isspace() else words.pop()
        return words[1:], incomplete

    def complete(self) -> str:
        args, incomplete = self.get_completion_args()
        _, cur = self.get_request() or ("", incomplete)
        # What bash split off before its word: "mode=" for "mode=fa", whose word is "fa".
        split_off = incomplete[:len(incomplete) - len(cur)] if incomplete.endswith(cur) else ""
        values = [item.value for item in self.get_completions(args, incomplete)]
        return "\n".join(v.removeprefix(split_off) for v in values if v.startswith(split_off))


class ZshComplete(typer_shells.ZshComplete):
    """No space after a ``/`` or ``=``, and no local files when nothing matches.

    ``request`` is the line: the server sets it, the fallback reads ``_TYPER_COMPLETE_ARGS``.
    """
    source_template = ZSH
    request: str | None = None

    def get_completion_args(self) -> tuple[list[str], str]:
        if self.request is None:
            start_server()
            line = os.environ.get("_TYPER_COMPLETE_ARGS", "")
        else:
            line = self.request
        words = split_arg_string(line)[1:]
        if words and not line.endswith(" "):
            return words[:-1], words[-1]
        return words, ""

    def complete(self) -> str:
        args, incomplete = self.get_completion_args()
        items = self.get_completions(args, incomplete)
        if not items:
            return ""
        if not any(item.value.endswith(MORE) for item in items):
            formatted = "\n".join(self.format_completion(item) for item in items)
            return f"_arguments '*: :(({formatted}))'"
        # Shown without what's typed up to its last '/' or '=', as zsh shows files.
        hidden = incomplete[:max(incomplete.rfind(c) for c in MORE) + 1]
        more = [i for i in items if i.value.endswith(MORE)]
        done = [i for i in items if not i.value.endswith(MORE)]
        lines = [self.add(more, hidden, "-S ''")]
        if done:
            lines.append(self.add(done, hidden))
        return "; ".join(lines)

    @staticmethod
    def add(items, hidden: str, *options: str) -> str:
        """Code adding ``items``, without ``hidden`` in front: ``_describe`` when any has help, to show it."""
        options = " ".join(["-p", shlex.quote(hidden), *options])
        values = [i.value.removeprefix(hidden) for i in items]
        if not any(i.help for i in items):
            return f"compadd {options} -- {shlex.join(values)}"
        described = [v.replace(":", "\\:") + (f":{i.help}" if i.help else "") for v, i in zip(values, items)]
        return f"local -a described=({shlex.join(described)}); _describe -t values '' described {options}"


class FishComplete(typer_shells.FishComplete):
    """Typer's, plus starting the server, which keeps the topics cache fresh."""

    def complete(self) -> str:
        start_server()
        return super().complete()


class Shell(StrEnum):
    bash = "bash"
    zsh = "zsh"
    fish = "fish"


def register() -> None:
    """Typer's shell classes, with ours. Typer only registers its own with add_completion."""
    typer_shells.completion_init()
    add_completion_class(BashComplete, "bash")
    add_completion_class(ZshComplete, "zsh")
    add_completion_class(FishComplete, "fish")


def script(shell: str, prog_name: str = "synq") -> str:
    """The completion script to source in ``shell``. Bash's and zsh's cover both program names."""
    register()
    complete_var = f"_{prog_name.replace('-', '_').upper()}_COMPLETE"
    return get_completion_class(shell)(None, {}, prog_name, complete_var).source()


def answer(cli, shell: str, line: str, cur: str = "") -> str:
    """What the script expects back for ``line``, for the server: ``COMPREPLY`` lines for bash, code to eval for zsh."""
    if shell == "bash":
        complete = BashComplete(cli, {}, "synq", "_SYNQ_COMPLETE")
        complete.request = (line, cur)
    elif shell == "zsh":
        complete = ZshComplete(cli, {}, "synq", "_SYNQ_COMPLETE")
        complete.request = line
    else:
        raise ValueError(f"no server completion for {shell!r}")
    return complete.complete()
