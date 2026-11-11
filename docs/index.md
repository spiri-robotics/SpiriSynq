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
:numbered:

overview
getting_started
objects
nested_objects
liveliness
methods
remote_methods
callbacks
wasm
type_identity
```

```{toctree}
:maxdepth: 1
:caption: Document control

revisions
```
