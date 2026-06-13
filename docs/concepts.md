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

`@remote_method` turns a regular method into a descriptor that behaves differently depending on whether you're on the authoritative or mirror side:

- **Authoritative**: calls the method directly in the same process.
- **Mirror**: serialises arguments to YAML, sends a Zenoh query to `<topic>/<method_name>`, and deserialises the reply.

This is transparent to the caller. The same `robot.arm()` call works whether `robot` is authoritative or a mirror.

**Generators** — if the decorated function is a generator, the mirror receives a Python generator that yields values as each Zenoh reply arrives. Zenoh consolidation is disabled for these calls so no intermediate values are dropped.

**Async** — `@remote_method` works on `async def` functions and async generators. Use `.as_async()` on the bound method to get an awaitable or async generator on the mirror side.

**Timeouts** — use `.timeout(seconds)` to get a bound method with a custom timeout:

```python
result = robot.arm.timeout(5.0)(mode="manual")
```

## Lifecycle and garbage collection

Zenoh resources (publishers, subscribers, queryables) are tied to the `SyncableObject` instance. When the object is garbage collected, `__del__` calls `close()` to undeclare them. SpiriSynq uses weak references for internal callbacks so that objects are not kept alive by their own Zenoh subscriptions.

The `Session` object is similarly cleaned up on GC, and is also registered with a shutdown hook so that Zenoh's non-daemon threads don't prevent interpreter exit.

## Multiple sessions

Most applications use the default session created at import time and never touch `Session` directly. If you need to connect to multiple Zenoh networks in one process, you can create additional `Session` instances and pass them via `synq_session=`, or use `session.as_default()` as a context manager to set the default for a block of code.
