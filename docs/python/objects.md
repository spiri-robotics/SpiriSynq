# Objects and fields

## Defining an object

A syncable object is a dataclass that inherits from `SyncableObject`:

```python
from dataclasses import dataclass, field

from SpiriSynq.syncable_objects import SyncableObject


@dataclass
class Battery(SyncableObject):
    voltage: float = 0.0
    charging: bool = False
    cells: list[float] = field(default_factory=list)
```

The first argument to the constructor is the topic. `synq_authoritive=True` makes this process
the owner:

```python
battery = Battery("./battery", synq_authoritive=True, voltage=12.4)
```

Elsewhere, `Battery.from_topic("myhost/battery")` makes a mirror.

From the class's fields and type annotations, SpiriSynq builds a JSON Schema for it. That schema,
not the Python class, is what the core and other processes see. See
[Type identity and schemas](../type_identity.md).

## Topics

A topic is a slash-separated Zenoh key, such as `myhost/battery`. An authoritative object's topic
is placed under this machine's *base topic*, which comes from, in order:

1. the `SPIRI_SYNQ_BASE_TOPIC` environment variable;
1. the contents of `/etc/spirisynq_base_topic`;
1. the hostname.

A topic starting with `./` is relative to the base topic of the process that uses it, so on
`myhost`, `./battery` and `myhost/battery` name the same object. To name another machine's
object, write its topic in full. `obj.synq_absolute_path` is the topic an object actually uses.

In a container, the hostname belongs to the container. Bind-mount the machine's `/etc/hostname`
to `/etc/spirisynq_base_topic` so every container on the machine shares its base topic.

## Authoritative objects and mirrors

There is one authoritative instance of an object. It:

- answers remote method calls;
- serves the object's full state to mirrors when they start;
- serves the object's metadata and schema, for discovery;
- announces that the object is alive (see [Liveliness](liveliness.md)).

A mirror:

- fetches the full state from the owner when it starts, then follows changes;
- forwards remote method calls to the owner;
- can write fields, which the owner applies (see below).

SpiriSynq doesn't stop you running two authoritative instances on one topic, but the result is
undefined.

## How fields sync

Each field is published on its own key, `<topic>/<field>`. Assigning a field publishes the new
value; the change reaches every mirror and the owner.

```python
battery.voltage = 12.1   # published on myhost/battery/voltage
```

A field whose type is a plain dataclass is synced field by field, so
`robot.gps.latitude = 45.0` publishes on `<topic>/gps/latitude` alone. For that to work, the
nested dataclass must emit change events: inherit from `SubSyncableDataclass`. A frozen dataclass
can't be changed in place, so it is published whole each time the field is assigned.

Containers (`list`, `dict`, `set`) are published whole whenever they change. Prefer them for
small collections; a large or fast-changing collection is better as separate fields, or encoded
your own way (see [Custom encoding](#custom-encoding)).

A field that holds another syncable object is a link. See
[Nested objects and links](nested_objects.md).

### Subscriptions are exact

An object subscribes only to the keys its type defines. A mirror subscribes to every key in its
type's schema, and nothing else. A key that isn't in the schema is never delivered to the object.

### Writes from mirrors

Any process can set a field, and the owner applies it like a local change. To make a field
read-only from the network, turn off incoming writes for it on the authoritative object. The
owner then ignores writes from other processes to that field, and its own value is the only one.

```python
@dataclass
class Battery(SyncableObject):
    voltage: float = 0.0
    voltage_ignore_incoming = True
```

This is a field setting; see [Field settings](#field-settings).

### Fields that aren't synced

Fields in `synq_skip_sync` are never published or received:

```python
@dataclass
class Camera(SyncableObject):
    synq_skip_sync = SyncableObject.synq_skip_sync | {"local_cache"}
    exposure: float = 0.01
    local_cache: dict = field(default_factory=dict)
```

Fields in `synq_skip_rehydrate` are synced as they change, but left out of the full state a new
mirror fetches. Use it for large values, such as raw images, that a new mirror can wait for.

### Publishing only when someone is listening

An owner can skip publishing a field when no other process is subscribed to it. This only works
for fields with incoming writes turned off: an owner that accepts writes on a field is itself
subscribed to it, and Zenoh counts that subscription as a listener.

```python
@dataclass
class Camera(SyncableObject):
    image: bytes = b""
    image_ignore_incoming = True
    image_lazy = True
```

A lazy field is still sent to new mirrors as part of the object's full state.

### Streams: on-demand fields

Some fields are streams, not state: a camera's frames, a lidar's scans. Most mirrors don't want
them, and the owner shouldn't spend time making them when nobody does. Make such a field *on
demand*:

```python
@dataclass
class Jpeg(SubSyncableDataclass):
    quality: int = 80
    data: bytes = b""
    data_on_demand = True
    data_qos = qos.background

    def data_before_send(self, data: bytes, sample: Sample) -> None:
        sample.send(data, encoding="image/jpeg")

    def data_after_receive(self, sample: Sample) -> bytes:
        return sample.payload


@dataclass
class Camera(SyncableObject):
    jpeg: Jpeg = field(default_factory=Jpeg)
```

An on-demand field's key starts with `@`: `<topic>/jpeg/@data`. A zenoh wildcard never matches a
chunk starting with `@`, so nothing receives the field without asking for it by name:

- A mirror doesn't receive it until it subscribes, and the state a new mirror fetches leaves it
  out. `mirror.synq_subscribe("jpeg/data")` subscribes and fetches the field's last value;
  `mirror.synq_unsubscribe("jpeg/data")` lets go. Subscriptions are counted, so each part of a
  program can hold its own. A field with `synq_skip_rehydrate` isn't fetched on subscribing:
  useful for a video's frames between keyframes, which mean nothing alone.
- The owner publishes it only while someone is subscribed, and ignores writes to it from
  mirrors. `camera.synq_has_listeners("jpeg/data")` and `synq_signal_listeners` tell it when to
  start or stop producing.
- It isn't part of the value of the dataclass holding it: assigning `camera.jpeg` whole sends
  `quality`, not the frame.

The `@` is part of the type's schema, so mirrors in every language treat the field the same way.

## Field settings

A setting for a field is a class attribute named after the field: `<field>_<setting>`. Its value
is one of the presets SpiriSynq provides:

```python
from dataclasses import dataclass

from SpiriSynq import qos
from SpiriSynq.syncable_objects import SyncableObject


@dataclass
class Camera(SyncableObject):
    exposure: float = 0.01
    exposure_qos = qos.realtime

    image: bytes = b""
    image_qos = qos.background

    recording: bool = False
    recording_reliability = qos.reliable
```

Settings have no type annotation, so the dataclass doesn't make them fields. They are never
synced, and, except `_on_demand`, aren't part of the schema: they only affect how this process
handles the field. They
are fixed when the class is defined and can't be changed at run time.

| Setting | Values | Default |
| --- | --- | --- |
| `<field>_qos` | A priority preset, below. | `qos.data` |
| `<field>_reliability` | `qos.drop` or `qos.reliable`, below. | `qos.drop` |
| `<field>_ignore_incoming` | `True` to ignore writes from other processes. Owner only. | `False` |
| `<field>_lazy` | `True` to publish only when someone is listening. Owner only. | `False` |
| `<field>_on_demand` | `True` for a stream: see [Streams](#streams-on-demand-fields). Part of the type. | `False` |

A setting goes on the class that declares the field. For a field of a nested dataclass, put it on
the nested class.

### Priority: `<field>_qos`

From highest to lowest:

| Preset | Use it for |
| --- | --- |
| `qos.realtime` | Control loops and anything that must not wait. |
| `qos.interactive_high` | Values a person is waiting on. |
| `qos.interactive_low` | |
| `qos.data_high` | |
| `qos.data` | The default. |
| `qos.data_low` | |
| `qos.background` | Bulk data, such as images and logs. |

### Reliability: `<field>_reliability`

| Preset | Behaviour |
| --- | --- |
| `qos.drop` | Best effort: a value lost in transit stays lost. The default. |
| `qos.reliable` | A value lost in transit is sent again. |

Most fields are values that are replaced often, such as sensor readings, positions or video
frames. For those, a lost value is soon replaced by a newer one, and resending a stale value would
only get in the way.

Use `qos.reliable` for a field where every change matters, and that might not change again soon:
a mode, a setting, a flag such as `recording` above. If a change to `recording` were lost, mirrors
would show the old value until the next change, however long that takes.

A new mirror always gets the current value of every field, in the full state it fetches when it
starts, whatever the field's reliability.

## Custom encoding

Values go on the wire as YAML. YAML is readable and works for every type, but it's a poor fit for
some data. An image in YAML is a base64 string, a third bigger than the image itself, and the
camera has already compressed it once.

For a field like that, define two methods named after it, alongside its settings, and handle the
encoding yourself:

```python
@dataclass
class Camera(SyncableObject):
    image: Image | None = None
    image_qos = qos.background

    def image_before_send(self, image: Image, sample: Sample) -> None:
        sample.send(image.to_jpeg(quality=80), encoding="image/jpeg")

    def image_after_receive(self, sample: Sample) -> Image:
        return Image.from_jpeg(sample.payload)
```

- **`<field>_before_send`** runs in the process that publishes a new value: the owner when the
  field changes, or a mirror when it writes the field. It gets the value and a `Sample`, and sends
  it, exactly once, either raw with `sample.send(payload, encoding=..., attachment=...)` or the
  usual way with `sample.send_value(value)`.
- **`<field>_after_receive`** runs in each process that receives a value: the mirrors, and the
  owner when a mirror writes the field. It gets the `Sample`, with its `payload` (bytes),
  `encoding` and `attachment`, and returns the field's value. `sample.decode()` decodes it the
  usual way.

Each is optional, and works on its own. They are the field's counterpart of a remote method's
[`before_call` and `after_call`](remote_methods.md#hooks-on-the-caller).

The full state a new mirror fetches carries each field as its `before_send` sent it, and goes
through `after_receive` like any other value.

Uses:

- **Images and video frames**: JPEG or PNG, or raw pixels with a small header.
- **Compression**: a large text field, such as a log or a map, compressed with zstd.
- **Packed numbers**: a list of floats as binary, a fraction of its size as text.

To share an encoding between fields, write it once as a function and call it from each field's
hooks.

## Reacting to changes

See [Reacting to changes](callbacks.md): callbacks declared on the class, and signals on
`obj.events`.

## Bad incoming data

An incoming value that can't be applied is dropped, and the object emits a signal so you can log
it or recover. A value is bad when:

- it isn't YAML, and the field has no `after_receive` to decode it;
- it decodes, or its `after_receive` returns, the wrong type for the field;
- it is a link to an object of the wrong type (see
  [Nested objects and links](nested_objects.md)).

The core checks only routing. Decoding the value and checking it against the field's type is
done in Python.

```python
def on_bad_data(path: str, reason: str) -> None:
    print(f"dropped a value for {path}: {reason}")

mirror.synq_signal_bad_data.connect(on_bad_data)
```

## Escape hatches

Some data doesn't fit the model: payloads that aren't YAML, custom framing, keys you want to
handle by hand. For those, SpiriSynq exposes the Zenoh primitives it uses (publishers,
subscribers, queries, samples) through its own Python types, which share the core's Zenoh
session. Don't open a second Zenoh session with the `eclipse-zenoh` package for this: it
would be a separate Zenoh runtime.

:::{note}
The Python API of the escape hatches isn't designed yet. It will grow alongside the field
settings. Until then, the session's `node` has the plain zenoh calls the `synq` command line
uses, on the same zenoh session: `put(key, payload, encoding=..., attachment=...)`,
`watch(key)` (read samples with `recv(timeout)`) and `get(key, parameters=..., payload=...,
timeout=...)` (iterate over its replies). They may change before the API settles.
:::
