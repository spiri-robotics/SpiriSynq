# AGENTS.md

SpiriSynq keeps Python dataclass instances in sync across processes, machines and languages over
Zenoh. It requires no zenoh router: peers discover each other directly. Python >=3.13, managed
with `uv` (`uv.lock` is committed). Generated from the SpiriProjectTemplates copier template;
`uvx copier update` pulls in template changes.

## Commands

- Everything CI checks: `make check` (in `nix develop .#rust` for the Rust half).
- Format and autofix: `make format`. ruff leaves Markdown alone: the docs' code samples are
  fragments it would rewrite.
- Tests: `uv run pytest`. Single test: `uv run pytest tests/test_objects.py::test_name`.
  Coverage must stay at 100%: `uv run pytest --cov=SpiriSynq` (the completion server's own
  process is measured too).
- The Rust core: `nix develop .#rust -c make rust-test` runs the cargo tests and `tests/core`,
  and fails below 100% line and function coverage; `make rust-lint` is fmt and clippy. After
  changing Rust, rebuild the extension with `uvx maturin develop --uv --locked` (in the nix
  shell) before running the Python tests.
- Docs: `make docs` (Sphinx, MyST markdown; warnings are errors; build with `-E` after toctree
  changes), `make pdf` for the controlled PDF, `make lint-docs` for prose (Vale).
  `autodoc_mock_imports` stubs the optional `examples` deps (PIL, cv2, nicegui, imagecodecs), so
  docs build without them. HTML is published to GitHub Pages from `main`.

## Conventions

- The docs are a controlled document (spiri-docs, as for every Spiri manual): `revisions.yaml`
  is the only place the document's revision is written. Revise it when what the docs say
  changes; it's separate from the package version.
- The template owns the versions it ships (base dependencies, workflow actions, the uv image)
  and bumps them weekly; take them with `uvx copier update` and `uv lock`, not by hand.
- Every change gets a categorized entry in `CHANGES.md` (Features / Fixes / Improvements /
  Tests / Internal) under `## Unreleased`; commits use conventional-commit style (`feat:`,
  `fix:`, `refactor:`).
- Version lives in `pyproject.toml` + `CHANGES.md`. A pushed `vX.Y.Z` tag publishes to PyPI
  (`.github/workflows/pypi-publish.yml`, trusted publishing via `environment: pypi`).
- Design decisions about the Rust core are recorded in `rust/ARCHITECTURE.md`, and any `unsafe`
  in its unsafe table.

## Tests: TCP-only zenoh network

- `tests/conftest.py` opens one seed `_core.Node` on a random local TCP port; every test session
  connects to it with **multicast scouting disabled**. Do not add UDP multicast or router-based
  tests. (`_core.scout` is tested with a fake node answering over loopback unicast UDP.)
- Each test gets a fresh default session (the autouse `session` fixture, base topic `host`);
  `other` is a second one, as another process would have, and `open_session()` opens more. All
  are closed after the test. `tests/core` tests `SpiriSynq._core` directly, with its own
  `network` fixture.
- zenoh drops messages before a subscriber or queryable is known. Do **not** `time.sleep()`;
  poll a predicate with `wait_for(lambda: ...)` from `tests/conftest.py`.
- An owner nobody holds a reference to is garbage-collected and closed: keep it in a variable.

## Naming and API gotchas

- `synq_authoritive` is **intentionally misspelled** (public API, used everywhere) — never "fix" it.
- The wire protocol is the Rust core's: keys, encodings and built-in queries are in
  `rust/spirisynq-core/src/wire.rs` and the "Wire protocol" section of `rust/ARCHITECTURE.md`.
  Payloads are YAML (`application/yaml`) in the core's own subset; schemas are JSON Schema
  2020-12 with SpiriSynq's `x-` keys (`SCHEMA_VERSION` in `src/SpiriSynq/schema.py`).
  `docs-v1/` is v1's documentation, kept for reference.
- Values convert by annotation (`src/SpiriSynq/values.py`): dataclasses travel tagged, syncable
  objects and `Link`s as `!Link`, primitive subclasses with a `yaml_tag` tagged. A class outside
  a PyPI package needs `synq_type_id = "<uuid>:<version>"`.
- Authoritative objects prepend the base topic: the hostname, or `SPIRI_SYNQ_BASE_TOPIC`.
- CLI tests must assert on output (`invoke` in `tests/test_cli.py`), not just the exit code:
  commands exit 0 even when nothing answers.

## Architecture map

- `rust/` — the core: `spirisynq-core` (the engine, sans I/O), `spirisynq-zenoh` (runs it over
  zenoh), `spirisynq-schema`, `spirisynq-yaml`, `spirisynq-extism` (WASM), and
  `spirisynq-python`, the extension module `SpiriSynq._core`.
- `src/SpiriSynq/session.py` — `Session` (a `_core.Node`, a pump thread and the "SpiriSynq
  network" callback thread) and `current_session` (`session.as_default()` for scoped defaults);
  the default session opens on first use.
- `src/SpiriSynq/syncable_objects.py` — `SyncableObject` and `SubSyncableDataclass`.
- `src/SpiriSynq/remote_callables.py` — `@remote_method`, `@remote_method_raw`, hooks,
  `RpcException`.
- `src/SpiriSynq/values.py`, `schema.py`, `link.py`, `qos.py`, `callbacks.py`, `wasm.py` — values,
  schemas, links, QoS presets, `@on_change`, `@wasm`.
- `src/SpiriSynq/completion/` — tab completion: callbacks + shell scripts (`__init__.py`), and the `synq _complete-server` daemon the scripts talk to over a socket (`server.py`). Self-contained; `cli.py` only wires `autocompletion=` callbacks to it. Tests type Tab into real bash/zsh on a pty (`tests/test_completion.py`).
- `src/SpiriSynq/cli.py` — typer CLI; registered as **both** `synq` and `spirisynq` entry points. `click` is a direct dependency (needed to keep `uv run`/`uvx spirisynq` working regardless of what `typer` declares) — fine to import if needed, but prefer typer's own API where possible.
