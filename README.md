# Technical overview

SpiriSynq is designed to keep objects in sync. Currently is only supports
python dataclasses, as we can leverage the type hints to make a better "contract".

SpiriSync is a set of extentions on top of zenoh, specific querables and conventions
that allow us to do auto-discovery of capabilities. It is compatible with regular zenoh,
you could implement a syncable object in pure-c on a microcontroller by adding a few
extra queryables.

# SpiriSynq — Low-Level Protocol Specification

This document describes what a node must implement to be a first-class SpiriSynq citizen, with no library assistance.

## The Four Mandatory Queryables

Every syncable object must respond to four key patterns. These are plain Zenoh queryables — nothing magic.

| Queryable Key | Trigger | Must Return |
|---|---|---|
| `<topic>/sr_metadata/<TypeName>` | `topic list` | YAML metadata blob |
| `<topic>/sr_object_schema` | `topic schema` | YAML field definitions |
| `**/sr_type_schema/<TypeName>` | `meta type_schema` | YAML type definition |
| `<topic>` (GET) | `topic rehydrate` | Full YAML state snapshot |

Plus: publish field changes as individual puts to `<topic>/<field>`.

---

## Payload Format

All payloads are **UTF-8 YAML strings**. Keep it simple — no binary encoding required unless a field value is itself binary.

```

```

We support optionally sending data binary encoded, important for things like raw image or audio data where we don't want the overhead of a yaml binary field.

# Errata

You must handle all your exceptions, or the zenoh thread will will deadlock.