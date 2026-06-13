# Protocol Specification

This page describes what a node must implement to be a first-class SpiriSynq citizen without using this library. Everything here is plain Zenoh — no SpiriSynq-specific transport.

## Payload format

All payloads are **UTF-8 YAML strings** encoded as `application/yaml`. Binary field values may be base64-encoded within the YAML.

YAML tags identify types: `!ClassName`. A node receiving an unknown tag should either ignore the field or surface an error — it must not crash.

## Field updates

The authoritative node publishes field changes as individual Zenoh puts:

```
key:     <topic>/<field>
payload: YAML-encoded field value
encoding: application/yaml
```

For nested fields: `<topic>/<field>/<subfield>`, and so on.

Subscribers watch `<topic>/**` and apply updates field by field.

## The four mandatory queryables

Every syncable object must respond to four queryable key patterns.

### `<topic>/sr_rehydrate`

Returns the full current state of the object. Used by mirrors on first connect.

**Response:** a complete YAML representation of the object, tagged with the class's YAML tag.

```yaml
!MyObject
field_a: 42
field_b: hello
```

### `<topic>/sr_metadata/<TypeName>`

Returns metadata about this topic. The `<TypeName>` segment is the YAML tag (without `!`). Used by `list_topics()` for discovery.

**Response:**

```yaml
topic: myhost/myapp/counter
classes:
  - "!Counter"
  - "!SyncableObject"
authoritive_node: "abc123..."   # Zenoh session ZID
```

`classes` lists all YAML tags in the MRO, sorted. This lets clients filter by parent class.

### `<topic>/sr_object_schema`

Returns the field schema for this object. Used for tooling and cross-language code generation.

**Response:** a dict of field name → type descriptor. See `SpiriSynq.schema.get_schema` for the exact format.

### `**/sr_type_schema/<TypeName>`

Returns the type definition for a named type. Wildcarded so any node on the network can answer it, not just the object's own topic.

**Response:** same format as `sr_object_schema`.

## RPC methods

Arbitrary methods are exposed as Zenoh queryables at `<topic>/<method_name>`.

**Request:** arguments are passed as Zenoh query parameters, each YAML-encoded:

```
selector: myhost/myapp/counter/reset?value=42
```

**Response:** the return value as a YAML payload with `application/yaml` encoding.

**Errors:** reply with a Zenoh error payload (not a normal reply). The string content is the error message.

### Generator methods

Generator methods send multiple replies to a single query. Each yielded value is a normal `application/yaml` reply. The final reply — carrying the return value — uses the encoding `x-spirisynq/generator-done`. Clients must request `ConsolidationMode.NONE` to receive all replies.

## Discovery

To list all objects on the network, query:

```
**/sr_metadata/
```

To filter by type, query:

```
**/sr_metadata/Counter
```

The wildcard on the left matches any base topic prefix.

## Echo prevention

The authoritative node tags each put with a `SourceInfo` containing its Zenoh session ID and a per-path sequence number. Subscribers must discard updates whose `source_id.zid` matches their own session ID to prevent echo loops.
