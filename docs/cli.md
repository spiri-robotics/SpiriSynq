# The `synq` command line

The Python package installs `synq` (also `spirisynq`), a tool for looking at a SpiriSynq network
and poking at it without writing code. It joins the network as any session does: zenoh's
defaults, finding peers by multicast.

Data goes to stdout as YAML, one document per record (`---` between them), so it can be piped;
progress and errors go to stderr. A command that finds nothing still exits 0, and says so on
stderr. Long lines are truncated on a terminal and left whole when piped; `--truncate` /
`--no-truncate` overrides that.

A topic argument may be relative: `./camera` is `<base topic>/camera`, and `.` is the base
topic itself. The base topic is `SPIRI_SYNQ_BASE_TOPIC`, else the machine's host name, as for
objects.

```bash
synq topic list --prefix .
synq topic watch './camera/**'
```

## Objects

`synq topic list` lists the objects on the network: each one's topic, owner (its session's
zenoh id), type `$id` and base types.

```bash
synq topic list
synq topic list --type Battery          # a class name, or a whole type $id
synq topic list --prefix ./drone --timeout 5
```

`--type` matches an object whose type, or one of whose base types, is the class named, as
`Session.list_topics` does.

`synq topic schema TOPIC` prints the schema an object serves. `synq meta type_schema [TYPE]`
prints the schema of every object of a type, as `{topic, schema}` documents; with no TYPE,
every object's.

`synq topic rehydrate TOPIC` prints an object's state, each field by its path, as its owner
holds it: what a new mirror fetches. Streams (`@` fields) and fields that skip the state fetch
aren't in it.

## Watching and putting

`synq topic watch KEY` prints what's put on a key, which may have wildcards. On-demand fields
are on `@` keys, which wildcards never match: watch one by its own key.

```bash
synq topic watch ./drone/battery/voltage
synq topic watch './drone/**' --count 10
synq topic watch './drone/**' --json
```

| Option | |
| --- | --- |
| `-p`, `--show-paths` | Include each sample's key. On by default when the key has a wildcard. |
| `-j`, `--json` | JSON lines, with each value as its text. |
| `-t`, `--timestamp` | Include when it was sent, if the sample carries a timestamp. |
| `-RT`, `--no-received-timestamp` | Leave out when it arrived (included by default). |
| `-n`, `--count` | Stop after this many samples. |

A payload that isn't YAML or UTF-8 text prints as `!!binary` and base64. With any of the
metadata options, each sample is a `path: … value: …` document.

`synq topic put KEY VALUE` writes a value to a field, as a mirror would: the owner takes it, and
publishes it if it accepts it. VALUE is read as YAML, or taken as text if it isn't YAML
(`--input-type yaml` refuses it instead, and `--input-type raw` always takes text). Without
VALUE, `put` reads stdin: YAML documents such as `topic watch --show-paths` writes, each put
on its `path:`. Given KEY too, every path must be under it, so a recording can't be replayed
onto the wrong tree.

```bash
synq topic put ./drone/battery/limit 80
synq topic watch './drone/**' -p > flight.yaml
synq topic put ./drone < flight.yaml
```

`synq topic bandwidth KEY` measures what's put on a key, in bytes and samples a second, every
`--interval` seconds. `--bytes` prints each interval's byte count to stdout instead.

## Remote methods

`synq topic rpc [TOPIC]` lists an object's remote methods, from its schema: or every object's,
under `--prefix` if given.

`synq topic call KEY [NAME=VALUE ...]` calls a method. KEY is the method's key: the object's
topic, then the method's name, or its key template filled in. Each value is read as YAML, or
taken as text if it isn't YAML. A generator's values print one document each.

```bash
synq topic call ./drone/battery/set_limit percent=80
synq topic call ./drone/camera/read/config/exposure
synq topic call ./drone/log/tail lines=20 --timeout 30
```

A method that raises exits with 1, printing the error's type and message. Exit status 2 means
nothing answered in time (`--timeout`, by default 10 seconds), or the answer was cut short.

## The zenoh network

`synq zenoh info` prints this session's zenoh id, where it listens, and the routers and peers it
is connected to.

`synq zenoh scout` looks for zenoh nodes by multicast scouting, for `--timeout` seconds:
routers and peers by default (`--routers/--no-routers`, `--peers/--no-peers`,
`--clients/--no-clients`). `--config` takes zenoh's configuration for scouting, as JSON5, to
choose the multicast address or interface.

## Tab completion

`synq completion bash|zsh|fish` prints a completion script. It completes topics, one `/` chunk
at a time, types, methods, and a method's arguments with the values their types allow.

```bash
# zsh, with ~/.zfunc in $fpath before compinit:
synq completion zsh > ~/.zfunc/_synq
# bash:
synq completion bash > ~/.local/share/bash-completion/completions/synq
```

A Tab can't wait for Python to start, so the script asks a server: a `synq` process that stays
open, keeps the list of objects current, and exits after ten minutes without a Tab. The first
Tab starts it, and gets topics from the server's cache (no methods) until it's up. Regenerate the
script after upgrading.
