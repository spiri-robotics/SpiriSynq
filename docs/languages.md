# Other languages

SpiriSynq's languages come in two tiers:

- **Python is tier 1.** Types are defined in Python, and its documentation is the most complete:
  the rest of these docs are written for Python users.
- **Rust, TypeScript and C/C++ are tier 2.** They mirror objects whose types are defined in
  Python. They read and set fields, follow changes, know whether the owner is there, and call
  remote methods. WASM code on a type runs the same way in each of them as in Python. Defining
  types in a tier-2 language comes later.

Every language uses the same Rust core. They all speak the same protocol and read the same
schemas, so a Python object can be mirrored from any of them, and a mirror behaves the same in
each.

## Where each stands

| Language | How it works | Status |
| --- | --- | --- |
| Rust | The core's own crates, with a typed API over them. | The engine works; the typed API isn't built yet. |
| TypeScript | In the browser: the core compiled to WebAssembly, speaking zenoh over a WebSocket, with TypeScript types over it. | Not built yet. |
| C/C++ | A C API, with a header-only C++ wrapper over it. | Not built yet. |

Each language gets its own section of these docs once its binding is built.
