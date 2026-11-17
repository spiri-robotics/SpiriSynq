---
orphan: false
---

# SpiriSynq

:::{admonition} Document control
:class: note

| | |
| --- | --- |
| Document number | **{{ document_number }}** |
| Revision | **{{ revision }}** |
| Status | {{ document_status }} |
| Effective date | {{ effective_date }} |
| Issued by | Spiri Robotics |

{{ distribution_statement }}
:::

Keeps Python dataclass instances in sync across processes, machines and languages over Zenoh

:::{admonition} These docs describe SpiriSynq v2, which doesn't exist yet
:class: warning

SpiriSynq v2 moves the protocol into a Rust core, with Python as a binding on top. These pages
describe v2 as a Python user will see it. They are written ahead of the code, as the
specification it has to meet. The few parts that aren't designed yet say so.
:::

```{toctree}
:maxdepth: 2

overview
```

```{toctree}
:maxdepth: 2
:numbered:
:caption: Python

python/getting_started
python/objects
python/nested_objects
python/liveliness
python/methods
python/remote_methods
python/callbacks
python/wasm
```

```{toctree}
:maxdepth: 2
:caption: Other languages

languages
```

```{toctree}
:maxdepth: 2
:caption: Reference

type_identity
```

```{toctree}
:maxdepth: 1
:caption: Document control

revisions
```
