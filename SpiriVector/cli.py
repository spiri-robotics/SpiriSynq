import sys
import typer
from SpiriVector.session import Session
from rich.console import Console
from rich.syntax import Syntax

app = typer.Typer()
topic_app = typer.Typer()
app.add_typer(topic_app, name="topic")

session = Session()

# stdout console for data output (pipeable)
console_out = Console(file=sys.stdout, highlight=False)
# stderr console for logging/status
console_err = Console(file=sys.stderr)


@topic_app.command("list")
def topic_list(
    _type: str = typer.Option("", "--type", "-t", help="Filter by type"),
    prefix: str = typer.Option(None, "--prefix", "-p", help="Key prefix"),
):
    """List all topics"""
    query_topic = f"{prefix}/**/sv_metadata/{_type}" if prefix else f"**/sv_metadata/{_type}"
    query_topic = query_topic.strip("/").removesuffix("/")

    console_err.print(f"[dim]Querying: {query_topic}[/dim]")

    replies = session.zenoh_session.get(query_topic)

    found = 0
    for reply in replies:
        if reply.ok:
            raw = reply.ok.payload.to_bytes().decode("utf-8")
            # Pygments-highlighted YAML via Rich Syntax, force_terminal keeps
            # colour codes in the stream but they're ignored by non-TTY consumers
            syntax = Syntax(raw, "yaml", theme="ansi_dark", background_color="default")
            console_out.print(syntax)
            found += 1
        else:
            console_err.print(f"[red]Error reply:[/red] {reply.err}")

    console_err.print(f"[dim]{found} result(s)[/dim]")


if __name__ == "__main__":
    app()
