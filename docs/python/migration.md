# Moving from v1

v2 keeps the shape of the v1 Python API: a dataclass that subclasses `SyncableObject`, owned with
`synq_authoritive=True` and mirrored with `from_topic`, with `@remote_method`s and signals on
`events`. Most classes move over unchanged. This page lists what doesn't.

v1 and v2 don't talk to each other: the wire protocol changed. Move every process that shares
objects at once.

## Type identity

Every syncable class has a type `$id`, which every process and language uses to name it. A class
in a package installed from PyPI gets one from its distribution and module. A class anywhere else
needs its own:

```python
@dataclass
class Battery(SyncableObject):
    synq_type_id = "3f1c2a7e-5b8d-4e61-9a0f-7c2d4b6e8a91:1"  # <uuid>:<version>
    voltage: float = 0.0
```

Without one, opening an object of the class raises `TypeError`, saying so. See
[Types that aren't in a PyPI package](../type_identity.md#types-that-arent-in-a-pypi-package).

YAML tags no longer name types on the wire. `sr_metadata/<Tag>` is now mounted under each type's
`$id`, and `list_topics()` reports `$id`s (`type`, `bases`) where it reported tags.
`list_topics(type_filter=...)` takes the class itself, a `$id`, or a class name.

## Values

v1 serialized values with PyYAML and a type registry. v2 converts a field's value by its
annotation, so there's nothing to register:

| v1 | v2 |
| --- | --- |
| `session.register_type_recursive(cls)` | Nothing: the annotation says. For a value whose annotation doesn't (`Any`, `object`), `SpiriSynq.values.register(cls)`. |
| `to_yaml` / `from_yaml` on a class | `synq_to_value(self)` and a classmethod `synq_from_value(cls, value)`. A `str`, `int` or `float` subclass with a `yaml_tag` needs neither. |
| `session.type_registry`, `SessionSerializer` | Gone. `SpiriSynq._core.yaml_load` / `yaml_dump` read and write the wire's YAML. |
| `synq_check_receive_types` | Always on: a value that doesn't fit the annotation is dropped and reported (`synq_signal_bad_data`). |

`bytes` fields travel as YAML (`!!binary`) unless the field has its own encoding: v1's built-in
`BytesCodec` is gone.

## Codecs become field hooks

v1's `Codec` classes, registered on the session and chosen by Python type, are replaced by a pair
of methods on the class, per field: `<field>_before_send` and `<field>_after_receive`. They choose
the payload, encoding and attachment themselves. See [Custom encoding](objects.md#custom-encoding).

| v1 | v2 |
| --- | --- |
| `session.register_codec(JpegCodec())` | `image_before_send(self, image, sample)` and `image_after_receive(self, sample)` on the class |
| `Codec.encode(value)` returns `(bytes, encoding)` | `before_send` calls `sample.send(payload, encoding=..., attachment=...)` |
| `Codec.decode(sample)` | `after_receive` reads `sample.payload`, `sample.encoding`, `sample.attachment` |

## Field and object settings

Settings are per field, as class attributes named after the field. See
[Field settings](objects.md#field-settings).

| v1 | v2 |
| --- | --- |
| `<field>_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_HIGH)` | `<field>_qos = qos.data_high`, a preset from `SpiriSynq.qos` |
| `SynqQoS(congestion_control=zenoh.CongestionControl.BLOCK)` | `<field>_reliability = qos.reliable` |
| `synq_publish = False` | Gone: an owner always publishes. Keep a field local with `synq_skip_sync`. |
| `synq_receive = False` | `<field>_ignore_incoming = True` on each field that should ignore mirrors' writes. |
| `synq_lazy_publish = True` | `<field>_lazy = True`: published only while someone listens. For streams, such as camera frames, `<field>_on_demand = True` (see [Streams: on-demand fields](objects.md#streams-on-demand-fields)). |
| `synq_skip_sync`, `synq_skip_rehydrate` | Unchanged. |
| `synq_session` default: the session at creation | `None` until the object starts syncing, then its session. |

## Signals and state

| v1 | v2 |
| --- | --- |
| `synq_signal_unknown_path`, `synq_signal_type_mismatch` | `synq_signal_bad_data(path, reason)` |
| `synq_signal_missing_parent` | Gone: a value for a nested dataclass that's `None` is bad data. |
| `synq_signal_tombstone`, `synq_is_deleted` | `synq_signal_alive(alive)` and `synq_alive`. See [Liveliness](liveliness.md). |
| `synq_mtime` | Gone. |
| Callbacks connected to `events` that set fields | Still work. For logic that should run however a field changes, `@on_change.owner` and `@on_change.mirror` (see [Reacting to changes](callbacks.md)). |

## Nested objects become links

A field that holds another `SyncableObject` holds a link to it: the child is an object of its own,
on its own topic, and a mirror of the parent holds a mirror of the child. Fields that hold plain
dataclasses still sync field by field, as in v1. See
[Nested objects and links](nested_objects.md).

## Remote methods

`@remote_method` methods move over unchanged. Their hooks don't:

| v1 | v2 |
| --- | --- |
| `@method.client()`: transform the result on the caller | `@method.after_call`: an async generator over the call's replies, yielding what the caller gets. |
| `@method.client(raw=True)`: take the `zenoh.Reply` | `@method.after_call`, reading each `Reply`'s `payload`, `encoding` and `attachment`. |
| `@method.server()`: take the `zenoh.Query` | `@remote_method_raw`: an async generator that gets the `Query` and yields `Reply`s. |
| `mirror.method.timeout(5)(...)` | `@remote_method(timeout=5)` |
| `mirror.method.retries(n)` | Gone. |
| `mirror.method.sync(...)` | `mirror.method(...)`; `mirror.method.as_async(...)` for asyncio. |
| `rpc_call(topic, session, kwargs)` | `obj.synq_call("method", **args)`, or `synq topic call`. |
| `encode_parameters` / `decode_parameters` | `format_parameters` / `parse_parameters`, and `Query.parameters` |
| `RpcException(message, topic)` | `RpcException(kind, message, topic)`: `kind` is the exception's type on the owner. |

The schema lists methods under `x-remote-methods`, not `x-rpc-endpoints`. See
[Remote methods](remote_methods.md).

## Sessions

| v1 | v2 |
| --- | --- |
| A default session opened on import | Opened on first use. |
| `Session(config=zenoh.Config(...))` | `Session(config)`, with zenoh's configuration as a dict or JSON5 text. |
| `session.zenoh_session` | Gone: SpiriSynq no longer uses the `eclipse-zenoh` package. For plain zenoh, see [Escape hatches](objects.md#escape-hatches). |
| `SpiriSynq.shutdown` | Gone: sessions close at exit by themselves, or with `session.close()`. |
| `session.source_info()` | Gone. |

## The command line

`synq` keeps its commands. `topic list --type` takes a class name or a type `$id`, `topic
rehydrate` prints the state as one document of fields by path, and `topic call` exits 1 when the
method raises and 2 when nothing answers. Regenerate the completion script after upgrading. See
[The `synq` command line](../cli.md).
