# Remote methods

A method marked `@remote_method` always runs on the object's **owner**. Calling it on a mirror
sends the call over the network and returns the owner's result. Calling it on the owner runs it
directly.

```python
from SpiriSynq.remote_callables import remote_method


@dataclass
class Battery(SyncableObject):
    charge_limit_percent: int = 100

    @remote_method
    def set_charge_limit(self, percent: int) -> int:
        self.charge_limit_percent = max(50, min(percent, 100))
        return self.charge_limit_percent


mirror = Battery.from_topic("robot1/battery")
mirror.set_charge_limit(80)   # 80
```

`@remote_method` and `@remote_method()` are the same; use the call form to pass options.

Remote methods are part of the type's schema, with their parameters and return type, so mirrors
in every language can call them. Arguments and return values must be types SpiriSynq can
serialize, unless the [hooks](#hooks-on-the-caller) or a
[raw remote method](#raw-remote-methods) encode them some other way.

A remote method answers on the key `<topic>/<method name>`, unless you give it a
[key template](#methods-that-own-part-of-the-key-tree).

## Async

`as_async()` returns an awaitable, so a call doesn't block the event loop:

```python
result = await mirror.set_charge_limit.as_async(80)
```

## Generators

A remote method that yields streams its values to the caller, which gets an iterator:

```python
@remote_method
def count_down(self, start: int = 10):
    for i in range(start, -1, -1):
        yield i


for value in mirror.count_down(start=3):
    print(value)
```

On the owner, a remote method can also be `async def`, or an async generator; SpiriSynq awaits it.
Callers can't tell the difference. On the caller, `as_async()` on a generator method returns an
async iterator:

```python
async for value in mirror.count_down.as_async(start=3):
    print(value)
```

Each yielded value is one Zenoh reply, so the caller gets values as the owner produces them.

## Errors and timeouts

An exception raised by the method on the owner is raised on the caller as `RpcException`,
carrying the original exception's type and message.

A call that gets no reply in time raises `TimeoutError`. The timeout is set on the method, in
seconds:

```python
@remote_method(timeout=30)
def calibrate(self) -> float:
    ...
```

Without one, the session's default applies.

## Methods that own part of the key tree

Some objects serve keys that nobody can list in advance. A file server, for example, serves every
file under its root. Give the method a key template, and the part of the key after the fixed
part becomes one of its arguments:

```python
from collections.abc import Iterator
from pathlib import Path


@dataclass
class FileTree(SyncableObject):
    synq_type_id = "3c8e1f2a-7d4b-4a9e-b1c6-5f0d2e8a9b47:1"
    root: str = "."
    root_ignore_incoming = True

    @remote_method(key="read/data/{path:**}")
    def read(self, path: str, start: int = 0) -> Iterator[bytes]:
        with open(Path(self.root) / path, "rb") as f:
            f.seek(start)
            while chunk := f.read(1024 * 1024):
                yield chunk

    @remote_method(key="read/list/{path:**}")
    def list(self, path: str) -> list[str]:
        return [p.name for p in (Path(self.root) / path).iterdir()]

    @remote_method(key="write/data/{path:**}")
    def write(self, path: str, offset: int, data: bytes) -> None:
        with open(Path(self.root) / path, "r+b") as f:
            f.seek(offset)
            f.write(data)
```

```python
files = FileTree.from_topic("robot1/files")
files.list("logs")                    # get on robot1/files/read/list/logs
for chunk in files.read("logs/today.txt"):
    ...                               # get on robot1/files/read/data/logs/today.txt
```

You call the method as usual; SpiriSynq builds the key. On the owner, the method answers every key
the template matches.

Why put the path in the key and not in an argument? Because Zenoh's access control works on keys.
With the path in the key, the router's ACL can do the permission checks for you:

- allow `robot1/files/read/**` for the ground station, and `robot1/files/write/**` only for the
  maintenance laptop;
- allow `robot1/files/read/list/**` to everyone, so anyone can browse, but
  `robot1/files/read/data/logs/**` only to some.

That's why the operations above are grouped by what they do (`read/…`, `write/…`) before the path:
one ACL rule can then cover a whole group.

### Templates

A template is the key after the topic, with placeholders in braces:

| Placeholder | Matches |
| --- | --- |
| `{name}` | Exactly one key chunk, e.g. `alice` |
| `{name:**}` | The rest of the key, any number of chunks, including none |

`{name:**}` must be the last thing in the template. Each placeholder is a `str` parameter of the
method with the same name. Other parameters are sent as usual. For example, a method that runs a
command as a given user:

```python
@remote_method(key="execute/{user}/{command:**}")
def run(self, user: str, command: str, args: list[str]) -> int:
    ...
```

`run("alice", "usr/bin/uptime", [])` is a call on `<topic>/execute/alice/usr/bin/uptime`.

`{path:**}` matching no chunks gives `path == ""`: in the file server, that's the root.

### Characters that can't go in a key

A value can contain characters Zenoh doesn't allow in a key: `*`, `$`, `?` and `#`. SpiriSynq
percent-encodes them, and `%` itself, when it builds the key, and decodes them before calling the
method. The method always sees the original string. A value with an empty chunk, such as
`"logs//today.txt"`, a leading `/` or a trailing `/`, can't be made into a key: the call raises
`ValueError` on the caller, and nothing is sent.

Keep this in mind when you write ACL rules: a file named `a*b` is the key chunk `a%2Ab`.

### Overlapping keys

No two members of a class may answer the same key. `read/{path:**}` and `read/list/{path:**}`
overlap, and so do `read/{path:**}` and a field called `read`, because `**` also matches no
chunks. A class with overlapping keys raises `TypeError` when it's defined.

### Large payloads

A call carries one payload each way, in one go. To send a large file, call `write` once per
chunk, with its offset. Each call returns when the owner has written the chunk, so a writer can't
get ahead of the owner. To receive a large file, make the method a generator, as `read` is above.

## Seeing the incoming query

A remote method can take a parameter annotated `Query`. On a call from the network it gets the
incoming query, with its parameters and attachment; on a call from the owner itself, it gets
`None`. The parameter isn't part of the method's signature for callers, or of the schema.

Use it for authorization, or anything else that depends on how the call arrived. Here `move` only
obeys callers that send a valid token:

```python
from SpiriSynq.remote_callables import Query


@dataclass
class Robot(SyncableObject):
    @remote_method
    def move(self, x: float, y: float, query: Query | None) -> bool:
        if query is not None and not self._auth.check(query.parameters.get("token")):
            raise PermissionError("unauthorized")
        return self._actuator.go(x, y)
```

`Query` is SpiriSynq's own type, one of its [escape hatches](objects.md#escape-hatches).

## Hooks on the caller

Two hooks run on the process that makes the call, around it:

| Hook | Gets | Does |
| --- | --- | --- |
| `@m.before_call` | A `Call`, and the arguments | Checks the arguments, and sends the call |
| `@m.after_call` | The stream of `Reply`s | Decodes them, and yields what the caller gets |

`m` is the remote method, and each hook is a method with the same name as it. Both are optional:
without `before_call`, SpiriSynq sends the arguments the usual way; without `after_call`, it
decodes each reply the usual way.

Both hooks are async. A call to a Zenoh queryable gets back a stream of replies, not one value,
so `after_call` is an async generator: it reads replies as they arrive and yields results. It
doesn't have to yield one result per reply. It can join several replies into one result, or split
one into several.

Each hook can take the usual path with a helper, or work with the raw Zenoh data:

| Hook | Usual path | Raw |
| --- | --- | --- |
| `before_call` | `await call.send_args(args)` encodes a dict of arguments the usual way | `await call.send(payload=..., parameters=..., encoding=..., attachment=...)` |
| `after_call` | `reply.decode()` decodes the payload the usual way | `reply.payload` (bytes), `reply.encoding`, `reply.attachment` |

The hooks only run for calls that cross the network: calling `m` on the owner itself runs the
method directly and skips them. The caller doesn't need to be async: `mirror.m(...)` runs the
hooks for you and blocks, and `mirror.m.as_async(...)` awaits them.

A mirror without the hooks, such as one that doesn't import your class, sends and decodes the
call the usual way.

### Example

The battery's owner keeps a history of its voltage. A dashboard asks for the last few minutes of
it:

```python
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from SpiriSynq.remote_callables import Call, Reply


@dataclass
class Battery(SyncableObject):
    synq_skip_sync = SyncableObject.synq_skip_sync | {"history"}
    history: list[float] = field(default_factory=list)  # owner-only: not synced

    @remote_method
    def voltage_history(self, minutes: int) -> list[float]:
        return self.history[-minutes * 60:]

    @voltage_history.before_call
    async def voltage_history(self, call: Call, minutes: int) -> None:
        if not 1 <= minutes <= 60:
            raise ValueError("minutes must be between 1 and 60")
        await call.send_args({"minutes": minutes})

    @voltage_history.after_call
    async def voltage_history(self, replies: AsyncIterator[Reply]) -> AsyncIterator[list[float]]:
        async for reply in replies:
            yield [round(v, 2) for v in reply.decode()]
```

```python
mirror.voltage_history(minutes=5)    # [12.31, 12.3, 12.3, ...]
mirror.voltage_history(minutes=600)  # ValueError, raised on the mirror; nothing is sent
```

- `before_call` gets a `Call` and the same arguments as the remote method. It sends the call
  exactly once, and returns nothing. If it raises, the call isn't sent: here a dashboard that asks
  for 600 minutes gets the error at once, without a round trip to the robot. If it returns without
  sending, the call raises `RuntimeError`.
- `after_call` gets the successful replies, in the order they arrive. An error reply is raised as
  `RpcException` from the `async for`; the hook can catch it, or let it reach the caller. For a
  method that returns one value, the caller gets the one value the hook yields; yielding none, or
  more than one, raises `RuntimeError`. For a generator method, the caller gets every value it
  yields.

## Raw remote methods

When a method needs control over its own wire format on the owner's side, make it a raw remote
method. It runs on the owner like any remote method, but it gets the incoming Zenoh query, and
yields the replies itself.

A camera's hardware encoder produces JPEG frames, with the capture time in a header of its own.
`snapshot` sends a frame on as the hardware made it, without decoding it, in chunks of 64 KiB as
the encoder hands them over, with the capture time in the first chunk's attachment. Each mirror
joins the chunks back into one `Snapshot`:

```python
import struct
from collections.abc import AsyncIterator

from SpiriSynq.remote_callables import Query, Reply, remote_method_raw


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    jpeg: bytes


@dataclass
class Camera(SyncableObject):
    @remote_method_raw
    async def snapshot(self, query: Query, quality: int = 90) -> AsyncIterator[Reply]:
        frame = await self._device.capture(quality)
        attachment = struct.pack("<d", frame.taken_at)
        async for chunk in frame.chunks(64 * 1024):
            yield Reply(chunk, encoding="image/jpeg", attachment=attachment)
            attachment = None

    @snapshot.after_call
    async def snapshot(self, replies: AsyncIterator[Reply]) -> AsyncIterator[Snapshot]:
        import struct
        taken_at, jpeg = None, b""
        async for reply in replies:
            if reply.attachment:
                (taken_at,) = struct.unpack("<d", reply.attachment)
            jpeg += reply.payload
        yield Snapshot(taken_at, jpeg)
```

```python
mirror.snapshot(quality=75)   # Snapshot(taken_at=1762890000.12, jpeg=b'\xff\xd8...')
```

1. The mirror calls `snapshot` like any remote method. SpiriSynq sends the query, encoding the
   arguments the usual way.
1. On the owner, `snapshot` gets the raw query, does whatever it needs to, and yields its replies.
1. On the mirror, `after_call` reads the raw replies and yields the result.

### Custom data in both directions

The caller's side can be custom too. A raw remote method's `before_call` sends whatever the owner's
side expects, and the owner reads it from the query. The two are written together, on the same
class, so they always agree.

The file server's `write`, sending each chunk compressed, with its offset in the attachment:

```python
import struct
import zlib


@dataclass
class FileTree(SyncableObject):
    @remote_method_raw(key="write/data/{path:**}")
    async def write(self, query: Query, path: str) -> AsyncIterator[Reply]:
        (offset,) = struct.unpack("<Q", query.attachment)
        with open(Path(self.root) / path, "r+b") as f:
            f.seek(offset)
            f.write(zlib.decompress(query.payload))
        yield Reply.encode(None)

    @write.before_call
    async def write(self, call: Call, path: str, offset: int, data: bytes) -> None:
        await call.send(
            payload=zlib.compress(data),
            encoding="application/zlib",
            attachment=struct.pack("<Q", offset),
        )
```

```python
files.write("logs/today.txt", 0, chunk)
```

Callers see `before_call`'s parameters: `path`, `offset` and `data`. `path` still comes from the
key template, so ACL rules on `write/data/**` still apply per file.

How a raw remote method is declared:

- It's an async generator. Each `Reply` it yields is sent as one Zenoh reply:
  `Reply(payload, encoding=..., attachment=...)` sends bytes as they are, and `Reply.encode(value)`
  encodes a value the usual way. If it raises, SpiriSynq sends the error as an error reply. When
  it returns, the call is finished.
- The first parameter after `self` is the `Query`. The parameters after it are the method's
  parameters, as callers see them and as the schema records them. SpiriSynq decodes them from the
  query the usual way, or from the key for placeholders in a key template, and passes them in.
- With a `before_call`, callers see `before_call`'s parameters instead, and the schema records
  those. Then the method declares only the placeholders in its key template after `query`, and
  reads the rest from the query itself.
- The return annotation is `AsyncIterator[Reply]`. The schema records the type callers get as
  `after_call`'s yield type, or, without an `after_call`, as any value.
- It takes the same options as `@remote_method`, such as `key=` and `timeout=`, and the same hooks.

A raw remote method runs only on the owner. Calling it on the owner itself raises `TypeError`,
since there's no query. Give the logic a plain method of its own if the owner needs it too.

A caller without the method's `after_call`, such as one that doesn't import your class, gets each
reply as a value if it's YAML (what `Reply.encode` sends), and as its payload, in bytes, if it
isn't.

## Chaining calls

:::{admonition} Not in 1.0.0rc1
:class: warning

`.pipe()` raises `NotImplementedError` in 1.0.0rc1. Call the method, then call the methods of the object it returns. This section describes chaining as it will be.
:::

A remote method can return a syncable object:

```python
@dataclass
class Robot(SyncableObject):
    @remote_method
    def get_camera(self, name: str) -> Camera:
        return self.cameras[name]
```

Called the usual way, it waits for the owner's reply, then fetches the camera's state, and gives
you a mirror: two round trips before you can use it.

To call methods on the result without waiting for it, call the method with `.pipe(...)`. This
line is one round trip to the robot, not three (call `get_camera`, fetch the camera's state, call
`snapshot`):

```python
snap = robot.get_camera.pipe("front").snapshot(quality=75)
```

That matters on a slow link. Over a radio with 100 ms of latency, the chain takes about 100 ms
instead of 300 ms.

### How it works

`.pipe(...)` works on a remote method whose return annotation is a syncable type. It sends the
call and returns a *pending link* at once, without waiting for the reply. A pending link is a link
whose topic isn't known yet; like any link, it stands in for the object (see
[Objects, mirrors and links are interchangeable](nested_objects.md#objects-mirrors-and-links-are-interchangeable)).
What you do with it is sent to the owner right behind the call, without waiting for the reply:

- **Calling a remote method on it** adds that call to the chain. Called the usual way, the method
  waits for its result as any call does. Called with `.pipe(...)`, it returns another pending link,
  and you can keep going: `robot.get_rig.pipe().get_camera.pipe("front").snapshot()`.
- **Reading a field on it** asks the owner for the object's state in the same round trip. The read
  blocks until the state arrives, and from then on the pending link is an ordinary mirror.
- **Awaiting it**, with `await link`, waits for the call and gives you the mirror.

The owner runs `get_camera`, and then runs the next call in the chain on the object it got back.
Only the last call's replies come back to the caller.

The call is sent as soon as you make it, whether or not you use the result. So
`robot.spawn.pipe("uptime")` starts the process even if you drop the pending link it returns.

### When the object lives somewhere else

A method can return a link to an object another process owns. The owner then makes the rest of
the chain's calls itself, as a caller of that object, and passes the replies back. The caller
still waits only one round trip.

Those calls go out under the owner's Zenoh identity, so the router's ACLs check the owner, not the
original caller. If the owner can't reach the object, because no process answers or its ACL
doesn't allow the call, it hands the chain back: the caller gets the object's topic, and makes the
rest of the calls itself. That costs one more round trip, and isn't an error.

### Hooks and errors in a chain

Every hook runs on the caller, as in an ordinary call:

- each call's `before_call` runs when you make the call, before anything is sent;
- the last call's `after_call` gets the replies;
- calls in the middle of the chain return objects, and their replies never come back to the
  caller, so their `after_call` doesn't run.

The caller finds each object's hooks through the schema: the method's return type says what type
the pending link will be.

An error at any step is raised where you use the result, as `RpcException`, naming the call that
failed. Calls after a failed step aren't run.

Only an object can be called through. A method that returns a plain value, and a
[raw remote method](#raw-remote-methods), end the chain.
