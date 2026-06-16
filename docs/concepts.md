# Concepts

## Authoritative and mirror nodes

Field sync is **bidirectional** — every instance, whether authoritative or not, both publishes its own changes and receives changes from others on the same topic. `synq_authoritive` controls only the queryable side:

The authoritative instance:
- Registers Zenoh queryables for `@remote_method` RPCs and the built-in `sr_rehydrate`, `sr_metadata`, and `sr_object_schema` endpoints.
- Sets its base topic prefix automatically from the hostname.

Mirror instances (`synq_authoritive=False`):
- Publish and receive field changes just like an authoritative instance.
- Forward `@remote_method` calls to the authoritative node's queryable instead of running them locally.
- Do not register queryables, so they cannot answer rehydration or discovery requests.

SpiriSynq does not enforce a single-authoritative constraint at the protocol level. Running two authoritative instances on the same topic will cause conflicting queryable registrations and undefined rehydration behaviour.

## How field sync works

Each syncable field maps to a Zenoh key expression `<topic>/<field>`. When you assign a new value to a field on an authoritative object, psygnal fires a change signal, SpiriSynq serialises the new value to YAML, and publishes it to `<topic>/<field>`.

Mirrors have a subscriber on `<topic>/**`. When a put arrives, SpiriSynq:

1. Strips the topic prefix to get the relative field path.
2. Validates it against the class's `valid_sync_paths` (derived from field type annotations).
3. Deserialises the YAML payload.
4. Applies it as a DeepDiff delta — mutating the local instance in-place without firing the change signal again, so the update doesn't echo back.

Echo prevention uses Zenoh `SourceInfo`: each session tags its puts with its own Zenoh session ID, and mirrors discard updates from their own ID.

### Nested dataclasses

Nested dataclass fields sync at the field level, not the object level. A change to `robot.gps.latitude` publishes to `<topic>/gps/latitude`, not `<topic>/gps`.

For this to work, the nested type must fire change signals on mutation — either:
- Use `@dataclass(frozen=True)` and replace the whole object.
- Add a `SignalGroupDescriptor` (psygnal) to the nested class.

If neither condition holds, SpiriSynq logs a warning at startup.

### Fields excluded from sync

The `synq_skip_sync` class variable lists fields that are never published or received. All `synq_*` bookkeeping fields are in this set by default. Add your own fields to exclude them:

```python
@dataclass
class MyObject(SyncableObject):
    synq_skip_sync = SyncableObject.synq_skip_sync | {"local_cache"}
    value: int = 0
    local_cache: dict = field(default_factory=dict)
```

`synq_skip_rehydrate` additionally excludes fields from the full-state snapshot returned by `sr_rehydrate` — useful for large binary fields like raw images.

## Type registry

SpiriSynq serialises everything as YAML using [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/). Each `SyncableObject` subclass is registered with a YAML tag (`!ClassName` by default, or overridden via `yaml_tag`).

When you call `SyncableObject.from_topic()` or `session.register_type_recursive()`, SpiriSynq walks the class's field type annotations and registers every `SyncableObject` subclass it finds, recursively. You must register a type before you can receive it — calling `from_topic()` handles this for you.

## Remote methods

`@remote_method` turns a regular method into a descriptor that behaves differently depending on which side you're on:

- **Authoritative**: calls the method directly in the same process.
- **Mirror**: serialises arguments to YAML query parameters, sends a Zenoh `get` to `<topic>/<method_name>`, and deserialises the reply.

The call is transparent — `mirror.reset()` works identically whether the instance is authoritative or a mirror. The `Counter` from [Getting Started](getting_started.md) already shows this pattern:

```python
@dataclass
class Counter(SyncableObject):
    value: int = 0

    @remote_method()
    def reset(self, to: int = 0) -> int:
        self.value = to
        return self.value
```

```python
mirror = Counter.from_topic("myhost/myapp/counter")
result = mirror.reset(to=0)  # routed to the authoritative process
```

### Sync vs async

`@remote_method` works on regular, `async def`, generator, and async generator functions. The method signature drives the behaviour on both sides:

| Function type | Authoritative side | Mirror side (sync call) | Mirror side (`.as_async()`) |
|---|---|---|---|
| `def f()` | called directly | blocks until reply | `await`able coroutine |
| `async def f()` | awaited in a new event loop | blocks until reply | `await`able coroutine |
| `def f()` with `yield` | called directly, returns generator | returns generator; blocks per `next()` | async generator |
| `async def f()` with `yield` | iterated in a new event loop | returns generator; blocks per `next()` | async generator |

On the authoritative side, `async def` methods are always run to completion synchronously (via `asyncio.run`) when called from a sync context. On the mirror side, the Zenoh round-trip is always blocking by default — use `.as_async()` to avoid blocking an event loop.

```python
# Sync call — blocks until the authoritative node replies
result = mirror.reset(to=0)

# Async call — awaitable, doesn't block the event loop
result = await mirror.reset.as_async(to=0)
```

### Generator methods

If the decorated function uses `yield`, the mirror receives a regular Python generator that streams values as each Zenoh reply arrives. Zenoh consolidation is disabled for these calls so no intermediate values are dropped.

```python
@dataclass
class Counter(SyncableObject):
    value: int = 0

    @remote_method()
    def count_down(self, from_value: int = 10):
        for i in range(from_value, -1, -1):
            self.value = i
            yield i
        return "done"

# Mirror side — iterates as replies arrive
for value in mirror.count_down(from_value=5):
    print(value)

# Or async
async for value in mirror.count_down.as_async(from_value=5):
    print(value)
```

The return value of a generator method (the value passed to `StopIteration`) is transmitted as the final reply and is accessible as the `StopIteration` value if you iterate manually with `next()`.

Async generators cannot carry a return value — the final reply payload is `None`.

### Client-side transforms

`@method.client()` registers a post-processing function that runs on the mirror side after the RPC reply is received. It is never called on the authoritative side.

```python
@dataclass
class Camera(SyncableObject):

    @remote_method()
    def capture(self) -> bytes:
        return self._device.read_frame()

    @capture.client()
    def capture(self, result: bytes) -> np.ndarray:
        return decode_jpeg(result)
```

The client function receives `self` (the mirror instance) and the deserialized return value, and returns the transformed result. Having access to `self` lets you use instance state in the transform — a local calibration matrix, a unit preference, etc.

```python
mirror = Camera.from_topic("robot/camera")
frame = mirror.capture()  # returns np.ndarray on the caller, bytes never leaves the library
```

On the authoritative object, `capture()` returns `bytes` directly and the client function is not invoked.

For generator methods the client function is applied **per yielded item**:

```python
@dataclass
class Sensor(SyncableObject):

    @remote_method()
    def stream(self):
        while True:
            yield self._read_raw()

    @stream.client()
    def stream(self, item: bytes) -> float:
        return struct.unpack("f", item)[0]

# Mirror side — each item is already decoded
for value in mirror.stream():
    print(value)  # float
```

Client transforms also apply when using `.as_async()`.

#### raw=True — take the raw Zenoh reply

Pass `raw=True` to skip deserialization entirely and receive the raw `zenoh.Reply` object instead. The client function is then responsible for extracting and parsing whatever it needs.

```python
import zenoh

@dataclass
class Robot(SyncableObject):

    @remote_method()
    def get_state(self) -> Self:
        return self

    @get_state.client(raw=True)
    def get_state(self, reply: zenoh.Reply) -> dict:
        from ruamel.yaml import YAML
        plain = YAML()
        plain.constructor.add_multi_constructor(
            '', lambda loader, _tag, node: loader.construct_mapping(node, deep=True)
        )
        return plain.load(reply.ok.payload.to_string())
```

This is useful when the return type is a `SyncableObject` subclass and you want to avoid the full deserialisation cost (which would otherwise start a new Zenoh session), when you need to inspect the reply encoding before deciding how to decode, or when you need to apply the result to `self` in-place rather than returning a new object.

### Server-side transforms

`@method.server()` registers a function that intercepts the raw Zenoh query before the default dispatch logic runs. It receives `self` (the authoritative instance) and the raw `zenoh.Query` object, and is fully responsible for calling the underlying method, encoding the result, and sending the reply.

```python
import zenoh

@dataclass
class Robot(SyncableObject):

    @remote_method()
    def move(self, x: float, y: float) -> bool:
        return self._actuator.go(x, y)

    @move.server()
    def move(self, query: zenoh.Query):
        registry = self.synq_session.type_registry
        params = {k: registry.load(v) for k, v in dict(query.parameters).items()}
        if not self._auth.check(params.get("token")):
            query.reply_err("Unauthorized")
            return
        result = self.move.__wrapped__(self, **{k: v for k, v in params.items() if k != "token"})
        query.reply(query.key_expr, payload=registry.dumps(result), encoding=zenoh.Encoding.APPLICATION_YAML)
```

The server function is **only** invoked by the Zenoh callback — direct local calls on an authoritative instance skip it entirely and call the original method directly. This keeps local usage fast and unaffected.

Common uses:

- **Authentication / authorisation** — inspect query parameters or payload before dispatching.
- **Custom encoding** — accept or return binary payloads that the default YAML path cannot handle.
- **Streaming from external sources** — send multiple replies (e.g. from a hardware buffer) without requiring a generator on the Python side.

If the server function raises an exception, SpiriSynq catches it and calls `query.reply_err()` automatically, matching the behaviour of the default path.

### Errors

If the authoritative side raises an exception, the mirror receives an `RpcException` with the error message as its string. The original traceback is logged on the authoritative side.

### Timeouts

`.timeout(seconds)` returns a bound method with a custom timeout. The default is the Zenoh session default.

```python
result = robot.arm.timeout(5.0)(mode="manual")
await robot.arm.timeout(5.0).as_async(mode="manual")
```

## Lifecycle and garbage collection

Zenoh resources (publishers, subscribers, queryables) are tied to the `SyncableObject` instance. When the object is garbage collected, `__del__` calls `close()` to undeclare them. SpiriSynq uses weak references for internal callbacks so that objects are not kept alive by their own Zenoh subscriptions.

The `Session` object is similarly cleaned up on GC, and is also registered with a shutdown hook so that Zenoh's non-daemon threads don't prevent interpreter exit.

## Publish and receive flags

Four boolean fields on `SyncableObject` control whether an instance participates in the pub/sub exchange:

| Field | Default | Effect |
|---|---|---|
| `synq_publish` | `True` | Publish local field changes to Zenoh. Set to `False` for a receive-only instance. |
| `synq_receive` | `True` | Apply incoming changes from Zenoh. Set to `False` for a publish-only instance. |
| `sync_lazy_publish` | `False` | Skip publishing when there are no active subscribers. Reduces network traffic at the cost of subscribers that join late potentially missing updates. |
| `synq_auto_start` | `True` | Call `sync()` automatically in `__post_init__`. Set to `False` if you need to finish configuring the object before it begins synchronising. |

`synq_check_receive_types` (default `True`) validates that incoming values match the field's type annotation before applying them. A mismatch logs a warning and discards the update. Disable it only if you are deliberately receiving subtypes or loosely-typed values.

## Codecs

By default, SpiriSynq serialises every field value as YAML before putting it on the Zenoh network. For most types this is fine, but for binary data — images, audio, raw sensor buffers — YAML base64-encodes the bytes, adding roughly 33 % size overhead and a full YAML parse on receive.

Codecs let you register a custom serialiser/deserialiser pair on a `Session` for a specific Python type. When a codec is registered, it takes over from YAML for that type in both directions.

### How codec selection works

- **Encoding (publish)**: when a field value is about to be put to Zenoh, SpiriSynq walks the value's MRO and picks the first registered codec whose `python_type` matches. If none matches, it falls back to YAML.
- **Decoding (receive)**: when a sample arrives, SpiriSynq checks the sample's Zenoh encoding against each registered codec's `zenoh_schema`. If one matches, that codec's `decode` method is called instead of the YAML parser.

This means the encoding carried in the Zenoh message is the unambiguous signal for which decoder to use — no out-of-band negotiation required.

### Built-in codecs

`BytesCodec` is registered on every `Session` by default. It transmits `bytes` fields as raw `APPLICATION_OCTET_STREAM` payloads, bypassing YAML entirely:

```python
@dataclass
class Camera(SyncableObject):
    frame: bytes = b""  # published as raw binary, not base64 YAML
```

No configuration is needed — any `bytes`-typed field is automatically handled.

### Writing a custom codec

Subclass `Codec`, set `python_type` and `zenoh_schema` as class attributes, and implement `encode` and `decode`:

```python
import numpy as np
import zenoh
from SpiriSynq.codecs import Codec

class JpegCodec(Codec):
    python_type = np.ndarray
    zenoh_schema = zenoh.Encoding.IMAGE_JPEG

    def encode(self, value: np.ndarray) -> tuple[bytes, zenoh.Encoding]:
        ok, buf = cv2.imencode(".jpg", value)
        return buf.tobytes(), zenoh.Encoding.IMAGE_JPEG

    def decode(self, sample: zenoh.Sample) -> np.ndarray:
        buf = np.frombuffer(sample.payload.to_bytes(), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
```

Then register it on the session before any objects are created:

```python
from SpiriSynq.session import Session

session = Session()
session.register_codec(JpegCodec())

@dataclass
class Camera(SyncableObject):
    frame: np.ndarray | None = None  # encoded as JPEG, decoded back to ndarray
```

Codecs are checked in registration order; the first match wins. Built-in codecs are prepended, so user-registered codecs take priority over them.

### What codecs do not cover

Codecs apply only to the field pub/sub path. They do not affect:

- **RPC parameters and replies** — `@remote_method` arguments and return values are always YAML-serialised.
- **`sr_rehydrate`** — the full-state snapshot uses YAML. If a `bytes` field should be excluded from rehydration snapshots (e.g. a large image), add it to `synq_skip_rehydrate`.

## Multiple sessions

Most applications use the default session created at import time and never touch `Session` directly. If you need to connect to multiple Zenoh networks in one process, you can create additional `Session` instances and pass them via `synq_session=`, or use `session.as_default()` as a context manager to set the default for a block of code.
