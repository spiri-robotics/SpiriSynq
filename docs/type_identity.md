# Type identity and schemas

## Schemas

Every syncable type has a JSON Schema (2020-12, with SpiriSynq's own `x-` keywords). SpiriSynq
builds it from the class: fields and their types, remote methods with their parameters and
return types, links to other syncable types, and WASM methods.

The schema is the only description of a type that leaves Python. SpiriSynq's core uses it to
work out which keys an object publishes and subscribes to. Other processes fetch it from the
object's owner, and bindings for other languages build their mirrors from it.

## `$id`

Each schema has an `$id` that names the type. You usually don't write it: SpiriSynq takes it from
the PyPI distribution the class belongs to.

```
urn:pypi:<distribution>:<version>:<import path>
```

For example, `Camera` in the `spiricamera` package, version 0.3.4:

```
urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera
```

The import path is the one you'd use to import the class. Unreleased code, such as an editable
install or local changes, uses the same form with whatever version the installed package
reports.

### Types that aren't in a PyPI package

A class that doesn't belong to a distribution on PyPI must carry its own identity: a UUID you
generate once and keep, and a version.

```python
@dataclass
class Gripper(SyncableObject):
    synq_type_id = "6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42:1"
    closed: bool = False
```

Generate the UUID with `python -c "import uuid; print(uuid.uuid4())"`. Change the version when
the type changes.

Creating an instance of a class that has neither raises `TypeError`, naming the class.

## Changing a type

SpiriSynq has no rules for evolving a type. Keeping a type compatible with older readers is up
to you; WASM properties can help, by standing in for fields you've removed (see
[Methods and properties](methods.md)).

If two processes use different versions of a type, SpiriSynq at most reports the difference.
