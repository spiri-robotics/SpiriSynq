# Overview

SpiriSynq keeps objects in sync across processes, machines and languages. You write a Python
dataclass, give an instance a topic, and every other process that mirrors that topic sees its
fields change. Other processes can call its methods, too.

SpiriSynq runs over [Zenoh](https://zenoh.io). It doesn't need a router: peers on a network find
each other directly.

## The pieces

**Objects.** A syncable object is a dataclass that lives at a *topic*, a slash-separated name
such as `myhost/robot`. Each field is published on its own key under that topic, e.g.
`myhost/robot/battery`.

**Authoritative objects and mirrors.** Exactly one process owns an object: its instance is
*authoritative*. It answers remote method calls and serves the object's full state to newcomers.
Every other process holds a *mirror*, which stays up to date and forwards method calls to the
owner. Field writes go both ways: a mirror can set a field, and the owner applies it, unless the
owner has turned off incoming writes for that field.

**Links.** A field can hold another syncable object. On the wire that field is a link to the
other object's topic, so each object is published once, at its own topic, however many objects
refer to it.

**Liveliness.** Each authoritative object announces that it's alive. Mirrors can tell when the
owner goes away and when it comes back.

**Code on the object.** Code you put on a syncable object runs in one of three places:

| Trigger | Wherever it's used | On the owner | On mirrors |
| --- | --- | --- | --- |
| Read | `@property` | | |
| Called | Methods | | |
| Remote call | | `@remote_method`, `@remote_method_raw` | `@m.before_call`, `@m.after_call` |
| Field changes | | `@on_change.owner` | `@on_change.mirror` |

**WASM.** Any of these can be written in WebAssembly instead of plain Python. Then it ships with the type, and runs the same way in every language SpiriSynq supports.

**Types.** Every object type has a JSON Schema, with a stable identity (`$id`). The schema is
what other processes, other languages and SpiriSynq's own core know about your type.

## The Rust core

The protocol is implemented once, in a Rust library. The Python package is a binding over it. As
a Python user you write ordinary dataclasses; the binding turns each class into a JSON Schema
and hands it to the core, which works out the object's keys from the schema and does the
networking. Bindings for other languages use the same core and the same schemas, so a Python
object can be mirrored from any of them.
