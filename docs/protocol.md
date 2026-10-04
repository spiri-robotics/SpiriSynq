# Protocol Specification

This page describes what a node must implement to interoperate with SpiriSynq without using this library. Everything here is plain Zenoh (1.x) — there is no SpiriSynq-specific transport, framing or handshake. Every example payload on this page was captured from the reference implementation.

## Roles and what each must implement

A node can take part at three levels. Each level includes the one before it.

| Role | Must implement | Sections |
|---|---|---|
| **Observer** — watches an object | Subscribe to `<topic>/**`, decode [payloads](#payload-encoding), apply [field updates](#field-updates), handle [tombstones](#tombstones) | 1–4 |
| **Mirror** — reads and writes an object it doesn't own | Observer, plus fetch initial state with [`sr_rehydrate`](#topic-sr-rehydrate), publish field puts with a [`SourceInfo`](#echo-suppression-and-sequence-numbers), discard its own echoes, call [RPCs](#rpc-methods) | 1–7 |
| **Authoritative** — owns an object | Mirror, plus implement the three [built-in callables](#built-in-queryables), declare [RPC queryables](#rpc-methods), publish a tombstone on shutdown, describe itself with the [schema](#object-schema) | all |

Being authoritative doesn't give a node exclusive write access. **Any** node may publish field puts, and receivers apply them in arrival order (last writer wins). Authority only means that the node answers the queryables and owns the object's lifetime. The protocol doesn't detect two authoritative nodes on one topic: both answer every query, and clients take whichever reply comes first.

## 1. Key space

An object lives at an **absolute path**, a Zenoh key expression:

```
<base_topic>/<topic>          e.g.  myhost/probe
```

- `base_topic` defaults to the authoritative node's hostname (override with `SPIRI_SYNQ_BASE_TOPIC`). It's a naming convention only. On the wire there is just the absolute path.
- The path must be a valid Zenoh key expression **without wildcards**: no empty chunks, no leading or trailing `/`, no `*`, `**` or `$`. The chunks `.` and `..` are also rejected. The Python API uses a leading `./` locally to mean "relative to my base topic", and it's always expanded before anything goes on the wire.
- Under the absolute path, the following chunk names are reserved:

| Key | Purpose |
|---|---|
| `<path>/<field>[/<subfield>…]` | Field values ([§3](#field-updates)) |
| `<path>/sr_rehydrate` | Full state ([§6](#topic-sr-rehydrate)) |
| `<path>/sr_metadata`, `<path>/sr_metadata/<Tag>` | Discovery metadata ([§6](#topic-sr-metadata)) |
| `<path>/sr_object_schema`, `<path>/sr_type_schema/<Tag>` | Object schema ([§6](#topic-sr-object-schema), [§9](#object-schema)) |
| `<path>/<method>` | RPC methods ([§7](#rpc-methods)) |

Field names and method names share one namespace below the path, and names starting with `sr_` are reserved for the protocol.

(payload-encoding)=
## 2. Payload encoding

### YAML

Unless a field uses a [codec](#binary-and-codec-encoded-fields), every payload (field puts, RPC arguments, RPC replies and queryable replies) is a **UTF-8 YAML 1.1 document** sent with Zenoh encoding `application/yaml`. The reference implementation uses PyYAML.

- The document end marker (`\n...`) is stripped, so a scalar is sent as just `3.0` rather than `3.0\n...`. Receivers must accept documents both with and without it.
- **Local tags** name application types: `!ClassName` by default, or a class's custom `yaml_tag`. A tagged mapping is an object of that class:

  ```yaml
  !Gimbal
  pitch: 1.0
  yaw: 0.0
  ```

  A receiver that doesn't know a tag must not crash. It can load the node as an untagged mapping (the reference client does this for `sr_rehydrate`), or reject that one update.

- **Standard YAML tags** you will see:

| Python value | On the wire |
|---|---|
| `int`, `float`, `str`, `bool`, `None` | plain scalars (`42`, `3.0`, `hello`, `true`, `null`) |
| `list`, `tuple` | plain sequence (tuples aren't tagged, so they arrive as sequences) |
| `dict`, `TypedDict` | plain mapping |
| `set` | `!!set {5: null}` |
| `bytes` *(inside a larger document, e.g. rehydrate)* | `!!binary` with base64 text |
| `EventedList` / `EventedDict` / `EventedSet` | `!EventedList [...]`, `!EventedDict {...}`, `!EventedSet [...]` |

(binary-and-codec-encoded-fields)=
### Binary and codec-encoded fields

When a field's value has a registered codec, the field put uses the codec's raw bytes and its Zenoh encoding instead of YAML. The receiver chooses a decoder by matching the **sample's encoding**, not the field type. The only built-in codec is:

| Python type | Encoding | Payload |
|---|---|---|
| `bytes` | `zenoh/bytes` | raw bytes, unmodified |

Applications can register more, such as `image/jpeg`. Codecs apply only to field puts. Inside `sr_rehydrate` and RPC payloads, `bytes` values are still `!!binary` YAML. If a receiver gets an encoding it has no decoder for, it should treat the sample as undecodable and drop it.

(field-updates)=
## 3. Field updates

A change to one field is one Zenoh **put**:

```
key:          <path>/<field>              myhost/probe/speed
encoding:     application/yaml            (or a codec's encoding)
payload:      the field's new value       2.5
source_info:  see §5
```

- **Nested dataclass fields:** setting a nested dataclass field (`obj.gimbal.pitch = 3.0`) publishes at `<path>/gimbal/pitch`. Replacing the whole sub-object publishes the tagged object at `<path>/gimbal`.
- **Containers** (`EventedList`/`EventedDict`/`EventedSet`) are always sent whole, at the container's top-level field key, even after a single `append`. Mutating a plain `list`/`dict`/`set` in place publishes nothing, because only assignment does.
- **Subscribing:** subscribe to `<path>/**`. Remove the `<path>/` prefix to get the relative field path. Ignore any path that isn't a field of the object, and any value whose type doesn't match the field.
- **Missing parents:** an update to `gimbal/pitch` can arrive while `gimbal` is `null` locally. It can't be applied. The reference client drops it and calls `sr_rehydrate` (at most once every 5 s by default).
- **QoS:** priority, congestion control and express are chosen by the publisher for each field. They aren't part of the payload, and receivers need do nothing about them.
- **Custom framing:** an object can replace a field's encoding with its own framing (a publish hook), possibly as several puts per change. A receiver that doesn't understand that framing should treat those samples like any other undecodable update.

(tombstones)=
## 4. Tombstones

When an authoritative object shuts down, it sends a Zenoh **delete** (`SampleKind.DELETE`) on the key expression `<path>/**` (literally, with the wildcard). The delete is sent `RELIABLE` from the object's own publisher, so it carries the same entity ID as the object's field puts and the next `source_sn` in their sequence. A receiver that tracks sequence numbers therefore knows whether it missed any updates before the tombstone. When a receiver sees a delete whose key is `<path>/**` or `<path>`, it should mark the object deleted. Other delete samples under the path should be ignored.

(echo-suppression-and-sequence-numbers)=
## 5. Echo suppression and sequence numbers

Every field put and tombstone carries a Zenoh `SourceInfo`:

- `source_id`: the **entity global ID of the publisher that sent it**. This is the Zenoh session ID (`zid`) plus the publisher's entity ID (`eid`). It isn't just the session.
- `source_sn`: a counter that starts at 0 and goes up by 1 for each sample **from that publisher**. Each object has exactly one publisher and therefore one counter, which all of its field keys and its tombstone share. A subscriber on `<path>/**` can use it to detect dropped or reordered updates.

Zenoh delivers a node's own puts back to its own matching subscribers, so a node must **discard samples whose `(source_id.zid, source_id.eid)` matches the publisher it declared for that object**. Don't compare `zid` alone. Many objects (or an authoritative object and a mirror of another one) can share a session, and comparing only `zid` would make them ignore each other's genuine updates.

Applying a received update must not trigger a re-publish of that update.

(built-in-queryables)=
## 6. Built-in queryables

Every authoritative object implements **three callables**. Like any other method (see [§7](#rpc-methods)), each one is a queryable under the object's path, and two of them are also mounted at extra keys:

| Callable | Mounted at | Returns |
|---|---|---|
| `sr_rehydrate` | `<path>/sr_rehydrate` | the object's full state |
| `sr_metadata` | `<path>/sr_metadata`, plus `<path>/sr_metadata/<Tag>` for each tag in the class's MRO | address, tags and owner |
| `sr_object_schema` | `<path>/sr_object_schema`, plus `<path>/sr_type_schema/<Tag>` for each tag in the class's MRO | the object's [schema](#object-schema) |

Every key that a callable is mounted at runs the same code and returns the same reply. `<Tag>` is a YAML tag without its `!`. For example, a `Probe(SyncableObject)` at `myhost/probe` answers on `…/sr_metadata/Probe`, `…/sr_metadata/SyncableObject`, `…/sr_type_schema/Probe` and `…/sr_type_schema/SyncableObject`. The per-tag keys let a client filter by type, including parent types, with a single Zenoh wildcard query (see [§8](#discovery)).

All three reply with an `application/yaml` payload, using the queryable's own key as the reply key, so a wildcard query always shows which object answered. They take no parameters, and passing any is an error. They're listed in the object's own `x-rpc-endpoints` alongside its other methods (see [§9](#rpc-endpoints)), so a client can check what a node actually implements rather than relying on this page.

(topic-sr-rehydrate)=
### `sr_rehydrate`

Returns the full current state of the object: a mapping of every synced field, tagged with the object's YAML tag. The mapping also carries `synq_topic` and `synq_base_topic`. These are the object's address, they aren't in the schema, and clients should ignore them along with any other keys they don't know.

```yaml
!Probe
blob: !!binary |
  AAE=
gimbal: null
lit: a
maybe: null
name: x
pt: null
res:
- 1
- 2
speed: 0.0
st: !!set {}
synq_base_topic: myhost
synq_topic: probe
tags: []
```

A mirror calls this once when it attaches. It also calls it to recover from a [missing parent](#field-updates).

(topic-sr-metadata)=
### `sr_metadata`

```yaml
authoritive_node: abdc77db26c912dc6a6ada9fc4b056a3   # Zenoh session ZID (spelling is intentional)
classes:
- '!Probe'
- '!SyncableObject'
topic: myhost/probe
```

`classes` lists every YAML tag in the MRO, sorted.

(topic-sr-object-schema)=
(sr-type-schema)=
### `sr_object_schema` and `sr_type_schema/<Tag>`

Returns the object's schema, described in [§9](#object-schema). The `sr_type_schema/<Tag>` keys are the same callable, so `**/sr_type_schema/Gimbal` gets one reply from **each object** whose class is or inherits from `Gimbal`, keyed by that object's path. If two objects of the same type return different schemas (for example because they run different versions of the code), that's a real disagreement, and the client sees both.

Types that only appear nested inside an object, such as a dataclass field's type, don't get keys of their own. Their definitions are in the `$defs` of every object schema that uses them.

(rpc-methods)=
## 7. RPC methods

Each `@remote_method` is a queryable at `<path>/<method_name>`.

### Request

Arguments are passed **by name** as Zenoh selector parameters. Each value is a YAML document with the trailing `\n...` stripped:

```
myhost/probe/add?a=2;b=40
```

- **Parameters are separated by `;`, not `&`.** This is the Zenoh 1.x selector syntax. With `?a=2&b=40`, the server sees a single parameter `a` with the value `2&b=40`.
- `&`, `=`, `|` and `#` are all fine inside a value.
- The reference client sends every parameter, defaults included. Omitted parameters take the method's default. Unknown parameters are an error.
- The Zenoh-level `ConsolidationMode` must be `NONE` for [generator methods](#generator-methods). For other methods it doesn't matter.

### Response

The return value is a single reply, encoded as YAML with encoding `application/yaml`. A method that returns nothing replies `null`.

### Errors

An exception is sent as a Zenoh **error reply** (`reply_err`). Its payload is a UTF-8 string of the form `RPC error '<message>'`, sent with encoding `zenoh/bytes`. Clients should treat the payload as a human-readable string and not parse it.

(generator-methods)=
### Generator methods

A generator method sends **several replies** to a single query:

1. one `application/yaml` reply for each yielded value, in order;
2. a final reply with encoding **`x-spirisynq/generator-done`**. Its YAML payload is the generator's return value (`null` for async generators, which can't return a value).

```
GET myhost/probe/count?n=3     (ConsolidationMode.NONE)
  → application/yaml               0
  → application/yaml               1
  → application/yaml               2
  → x-spirisynq/generator-done     done
```

If the generator raises an exception partway through, the stream ends with an error reply instead of the done reply. A client **must** request `ConsolidationMode.NONE`. Zenoh's default consolidation keeps only the last reply for each key, which would drop every yielded value. The schema marks generator methods with `x-generator: true` (see [§9](#rpc-endpoints)).

(discovery)=
## 8. Discovery

| To find | Query (`ConsolidationMode.NONE`) | One reply per |
|---|---|---|
| every object | `**/sr_metadata` | object |
| objects of a type (or subtype) | `**/sr_metadata/<Tag>`, e.g. `**/sr_metadata/Counter` | matching object |
| schemas of every object of a type | `**/sr_type_schema/<Tag>` | matching object |
| anything under a prefix | `<prefix>/**/…` in place of `**/…` | |

Don't query `**/sr_metadata/` (with a trailing slash): it isn't a valid key expression. Don't use `**/sr_metadata/**` either, because it matches the bare key and every per-tag key, so you get each object once per tag. The same applies to `sr_type_schema`.

(object-schema)=
## 9. Object schema

`sr_object_schema` returns a **standard JSON Schema (draft 2020-12)** document describing one object. Every schema the reference implementation produces passes the 2020-12 meta-schema, and the test suite validates real `sr_rehydrate` payloads against it. SpiriSynq-specific information is carried in `x-` keywords, which standard validators ignore. The document is sent as YAML, but it contains only JSON values, so a consumer can treat it as JSON.

The schema does three jobs:
- it describes the **field values** that can arrive at `<path>/<field>` and inside `sr_rehydrate`;
- it says how each value is **encoded** on the wire (YAML tags and non-YAML encodings);
- it lists **every callable** the object serves, including the three built-ins, with their parameters and results, in `x-rpc-endpoints`.

Generic clients use it. `Session.from_topic_untyped` reads the `properties` names, the CLI's `synq topic rpc` reads `x-rpc-endpoints`, and `synq meta type_schema` prints `sr_type_schema` replies.

### Document structure

```yaml
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 2                  # format version, see below
title: <class name>
type: object
x-yaml-tag: '!<Tag>'                   # the tag the object is sent with
x-classes: ['!<Tag>', ...]             # every tag in the MRO, sorted (as in sr_metadata)
description: <class docstring>         # omitted if the class has none
properties:
  <field>: <value schema>              # one per synced top-level field
required: [<every field>]
x-rpc-endpoints:
  <callable>: <endpoint>               # see below
$defs:                                 # omitted when nothing is referenced
  <ClassName>: <object schema>
```

- **`properties`** lists every top-level field that can appear in field puts. It leaves out `_`-prefixed fields, `synq_*` settings and addressing (`synq_topic`, `synq_base_topic`), `ClassVar`s (so `<field>_qos` never appears) and anything in `synq_skip_sync`.
- **`required`** lists every field, because `sr_rehydrate` always sends all of them. Whether a field can be `null` is part of its value schema, not of `required`.
- There's no `additionalProperties: false`, because `sr_rehydrate` also carries the addressing fields (see [§6](#topic-sr-rehydrate)).
- **`description`** comes from the class's own docstring, cleaned with `inspect.cleandoc`. The signature-style docstring that `@dataclass` generates for an undocumented class is left out, and so are inherited docstrings. A field's `description` comes from `field(metadata={"help": "..."})`. Comments next to a field aren't visible at runtime, so put units and similar information in `help`.
- **`default`** on a property is the field's Python default, converted to JSON (see [Defaults](#defaults)).

**`x-spirisynq-schema`** is `2` for the format described here. Version 1, sent by releases up to 0.2.0, had no version marker and used non-standard `type` names. A consumer that finds no `x-spirisynq-schema` should treat the document as version 1 and not validate with it.

(type-mapping)=
### Type mapping

| Python annotation | Value schema |
|---|---|
| `int` / `float` / `str` / `bool` | `{type: integer}` / `number` / `string` / `boolean` |
| `X \| None` | `X` with `null` added: `{type: [integer, 'null']}`, or `{anyOf: [<X>, {type: 'null'}]}` when `X` is a `$ref` or a union |
| `X \| Y` | `{anyOf: [<X>, <Y>]}` |
| `Literal["a", "b"]` | `{type: string, enum: [a, b]}`. Mixed value types give a list of types. `Literal[...] \| None` adds `null` to both `type` and `enum` |
| `list[X]` | `{type: array, items: <X>}` |
| `tuple[X, Y]` | `{type: array, prefixItems: [<X>, <Y>], items: false, minItems: 2}` |
| `tuple[X, ...]` | `{type: array, items: <X>}` |
| `set[X]`, `frozenset[X]` | `{type: array, uniqueItems: true, items: <X>, x-yaml-tag: '!!set'}` |
| `dict[K, V]` | `{type: object, additionalProperties: <V>}` (keys aren't described) |
| `bytes` | `{type: string, contentEncoding: base64, x-yaml-tag: '!!binary'}` |
| `EventedList` / `EventedSet` / `EventedDict` | `array` / `array` with `uniqueItems` / `object`, with `x-yaml-tag: '!EventedList'` etc. |
| an `Enum` class | `{enum: [<member values>], x-python-type: <name>}` |
| a `str`/`int`/`float` subclass with a `yaml_tag` (e.g. `RootFrame`) | the base type plus `x-yaml-tag` |
| a dataclass (including `SubSyncableDataclass`) or `TypedDict` | `{$ref: '#/$defs/<ClassName>'}` |
| `Any`, `object`, or an annotation that can't be resolved | `{}` (any value) |
| any other class | `{x-python-type: <name>}`, plus `x-yaml-tag` if it has one: an opaque value that accepts anything |

**`$defs`** has one entry per referenced dataclass or `TypedDict`, keyed by class name. A dataclass entry is an object schema with `title`, `x-yaml-tag`, `properties` and `required` (every field). A `TypedDict` entry has no `x-yaml-tag`, because it's sent as a plain mapping, and its `required` follows the TypedDict's own required keys. Recursive types terminate, because each type is defined once.

### Wire annotations

These keywords tell a receiver how a value looks on the wire. Standard validators ignore them.

| Keyword | Meaning |
|---|---|
| `x-yaml-tag` | The YAML tag the value is sent with. On an object schema it's the object's own tag (`!Probe`). On a value schema it's the tag a nested value carries (`!Gimbal`, `!!set`, `!!binary`, `!EventedList`). |
| `x-encoding` | Field puts for this property **aren't YAML**. They use a codec with this Zenoh encoding (for example `zenoh/bytes` for `bytes`, see [§2](#binary-and-codec-encoded-fields)). It reflects the codecs registered on the serving node. Inside `sr_rehydrate` and RPC payloads the value is still YAML. |
| `x-python-type` | The Python class name of a value the schema can't describe structurally. Useful for display and code generation only. |
| `x-classes` | Root only: the object's tags across its MRO, the same list `sr_metadata` returns. |

(defaults)=
### Defaults

A `default` is included when the Python default can be expressed as JSON:
- primitives, and `None` as `null`;
- enum members, as their value;
- `bytes`, as base64;
- sets, as sorted arrays;
- frozen dataclass instances, as objects.

A `default_factory` is called only if it's a builtin container or scalar constructor (`list`, `dict`, `set`, `EventedList` and similar). Other factories are never run, and their fields simply have no `default`.

(rpc-endpoints)=
### `x-rpc-endpoints`

There's one entry for each callable the object serves, keyed by the name it's served under (the last key chunk). This includes `sr_rehydrate`, `sr_metadata` and `sr_object_schema`, so an object fully describes itself. Extra mount points (`sr_metadata/<Tag>`, `sr_type_schema/<Tag>`) aren't listed separately.

```yaml
<callable>:
  description: <docstring, cleaned>       # omitted if none
  parameters:                             # always present: a JSON Schema for the selector parameters
    type: object
    properties:
      <name>: <value schema>              # {} if unannotated; `default` if it has one
    required: [<parameters without a default>]
    additionalProperties: false           # unknown parameters are an error
  returns: <value schema>                 # {type: 'null'} for -> None; {} if unannotated
  x-generator: true                       # generator methods only (see §7)
  yields: <value schema>                  # generator methods only: each streamed value
```

- To validate a call, validate the selector parameters, decoded from YAML into a mapping, against `parameters`.
- For a generator, `returns` describes the payload of the final `x-spirisynq/generator-done` reply. It's taken from `Generator[Y, S, R]`. For `Iterator[Y]`, `AsyncGenerator[Y, S]` and other generators that can't return a value, it's `null`.
- `returns: {$ref: '#'}` means "an object of this schema" (`sr_rehydrate`). `returns: {$ref: 'https://json-schema.org/draft/2020-12/schema'}` means "a JSON Schema document" (`sr_object_schema`).
- `$ref`s in parameters and results point into the same top-level `$defs`.

### Worked example

This class:

```python
@dataclass
class Gimbal(SubSyncableDataclass):
    """A gimbal."""
    pitch: float = 0.0

@dataclass
class Probe(SyncableObject):
    """A probe."""
    speed: float = field(default=0.0, metadata={"help": "metres per second"})
    mode: Literal["idle", "run"] = "idle"
    count: int | None = None
    blob: bytes | None = None
    gimbal: Gimbal | None = None

    @remote_method()
    def set_speed(self, value: float, ramp: bool = False) -> None:
        """Set the speed."""

    @remote_method()
    def countdown(self, n: int) -> Generator[int, None, str]:
        """Count down from n."""
```

serves this from `myhost/probe/sr_object_schema`, and from `myhost/probe/sr_type_schema/Probe` and `…/SyncableObject`. The reply is shown here sorted by key and lightly reflowed:

```yaml
$defs:
  Gimbal:
    title: Gimbal
    type: object
    x-yaml-tag: '!Gimbal'
    description: A gimbal.
    properties:
      pitch: {default: 0.0, type: number}
    required: [pitch]
  SyncableObjectMetadata:                     # result of sr_metadata
    title: SyncableObjectMetadata
    type: object
    description: "Reply of sr_metadata: an object's address, YAML tags, and owner."
    properties:
      authoritive_node: {type: string}
      classes: {items: {type: string}, type: array}
      topic: {type: string}
    required: [authoritive_node, classes, topic]
$schema: https://json-schema.org/draft/2020-12/schema
description: A probe.
properties:
  blob:
    type: [string, 'null']
    contentEncoding: base64
    x-yaml-tag: '!!binary'
    x-encoding: zenoh/bytes                   # field puts are raw bytes, not YAML
    default: null
  count: {default: null, type: [integer, 'null']}
  gimbal:
    anyOf: [{$ref: '#/$defs/Gimbal'}, {type: 'null'}]
    default: null
  mode: {default: idle, enum: [idle, run], type: string}
  speed: {default: 0.0, description: metres per second, type: number}
required: [blob, count, gimbal, mode, speed]
title: Probe
type: object
x-classes: ['!Probe', '!SyncableObject']
x-rpc-endpoints:
  countdown:
    description: Count down from n.
    parameters:
      type: object
      properties: {n: {type: integer}}
      required: [n]
      additionalProperties: false
    x-generator: true
    yields: {type: integer}
    returns: {type: string}
  set_speed:
    description: Set the speed.
    parameters:
      type: object
      properties:
        ramp: {default: false, type: boolean}
        value: {type: number}
      required: [value]
      additionalProperties: false
    returns: {type: 'null'}
  sr_metadata:
    description: Returns topic path, YAML type tags, and authoritative node ID.
    parameters: {type: object, properties: {}, required: [], additionalProperties: false}
    returns: {$ref: '#/$defs/SyncableObjectMetadata'}
  sr_object_schema:
    description: Returns the JSON Schema for this object's syncable fields and RPC endpoints.
    parameters: {type: object, properties: {}, required: [], additionalProperties: false}
    returns: {$ref: 'https://json-schema.org/draft/2020-12/schema'}
  sr_rehydrate:
    description: Returns the full current state of this object.
    parameters: {type: object, properties: {}, required: [], additionalProperties: false}
    returns: {$ref: '#'}
x-spirisynq-schema: 2
x-yaml-tag: '!Probe'
```

## Implementer's checklist

Authoritative node, for one object at `P`:

- [ ] Implement `sr_rehydrate`, `sr_metadata` and `sr_object_schema`, and mount them at `P/sr_rehydrate`, `P/sr_metadata`, `P/sr_object_schema`, plus `P/sr_metadata/<Tag>` and `P/sr_type_schema/<Tag>` for each tag in the MRO
- [ ] Declare `P/<method>` for each RPC method. Parse `;`-separated selector parameters, decode each one as YAML, and reply `application/yaml`, or send a `reply_err` string
- [ ] For generator methods, send one reply per value, then a final `x-spirisynq/generator-done` reply
- [ ] Describe every callable, including the three built-ins, in `x-rpc-endpoints`
- [ ] Declare a publisher on `P/**`. Send each field change as a put at `P/<field path>` with `SourceInfo(publisher id, per-publisher counter)`
- [ ] On shutdown, send a reliable delete on `P/**` from the same publisher, with the next `SourceInfo` in its sequence
- [ ] Subscribe to `P/**`, apply puts from other nodes, and discard samples whose `(zid, eid)` is your publisher's

Mirror, for an object at `P`:

- [ ] Subscribe to `P/**` **before** you rehydrate, so no update is lost in between
- [ ] Query `P/sr_rehydrate`, then apply field puts as they arrive. Choose the decoder by the sample's encoding, using `x-encoding` from the schema to know which fields aren't YAML
- [ ] To write, put to `P/<field>` with your own `SourceInfo`, and discard your own echoes
- [ ] Mark the object deleted when a delete on `P/**` or `P` arrives
- [ ] Discover objects with `**/sr_metadata[/<Tag>]` and `ConsolidationMode.NONE`
