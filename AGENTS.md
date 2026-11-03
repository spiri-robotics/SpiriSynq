# AGENTS.md

SpiriSynq keeps Python dataclass instances in sync across processes, machines and languages over
Zenoh. It requires no zenoh router: peers discover each other directly. Python >=3.13, managed
with `uv` (`uv.lock` is committed). Generated from the SpiriProjectTemplates copier template;
`uvx copier update` pulls in template changes.

## Commands

- Everything CI checks: `make check`.
- Format and autofix: `make format`.
- Tests: `uv run pytest`. Single test: `uv run pytest tests/test_session.py::test_name`.
  `test_performance.py` is heavier; run it separately when needed.
- Docs: `make docs` (Sphinx, MyST markdown; warnings are errors), `make pdf` for the controlled
  PDF, `make lint-docs` for prose (Vale).
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

## Tests: TCP-only zenoh network

- `tests/conftest.py` opens one seed zenoh session on a random local TCP port; all test sessions connect to it via `zenoh_test_config()` with **multicast scouting disabled**. Do not add UDP multicast or router-based tests.
- Each test gets a fresh `Session` via the autouse `zenoh_current_session` fixture (sets the `current_session` contextvar).
- Zenoh drops messages before a subscriber is ready. Do **not** `time.sleep()` before publish/wait; poll a predicate (`_wait_for(lambda: ...)`) or `obj.synq_publisher.matching_status.matching`.
- Use the `close_test_sessions` fixture pattern (teardown from `SpiriSynq.shutdown._live_sessions`) in tests that create `Session`s directly.

## Naming and API gotchas

- `synq_authoritive` is **intentionally misspelled** (public API, used everywhere) — never "fix" it.
- Wire format is plain Zenoh with YAML payloads; the protocol is documented in `docs/protocol.md` (three built-in callables `sr_rehydrate` / `sr_metadata` / `sr_object_schema`; `sr_metadata` is also mounted at `<topic>/sr_metadata/<Tag>` and `sr_object_schema` at `<topic>/sr_type_schema/<Tag>` per MRO tag — same code path, not separate implementations; schemas are standard JSON Schema 2020-12, format version `SCHEMA_VERSION` in `src/SpiriSynq/schema.py`; per-field puts at `<topic>/<field>`, RPCs at `<topic>/<method>` with `;`-separated params, echo suppression via zenoh `SourceInfo` zid+eid). `tests/test_protocol.py` checks the spec's wire claims with raw zenoh — update both together. CLI tests must assert on output (`_invoke_captured` in `tests/test_cli.py`), not just the exit code: commands exit 0 even when nothing answers.
- Serialization is PyYAML via `SessionSerializer` (`src/SpiriSynq/serializer.py`), not ruamel. Register custom types with `session.register_type_recursive(cls)`; str-subclasses need explicit `yaml_tag`/`to_yaml`/`from_yaml` to round-trip (see `RootFrame` in `src/SpiriSynq/example_types/position.py`).
- Authoritative objects prepend the hostname as topic prefix; override with env var `SPIRI_SYNQ_BASE_TOPIC`.

## Architecture map

- `src/SpiriSynq/syncable_objects.py` — `SyncableObject` and `SubSyncableDataclass` bases.
- `src/SpiriSynq/session.py` — `Session` + `current_session` contextvar (use `session.as_default()` for scoped defaults); module-level default session created at import.
- `src/SpiriSynq/remote_callables.py` — `@remote_method` RPC support, `RpcException`.
- `src/SpiriSynq/completion/` — tab completion: callbacks + shell scripts (`__init__.py`), and the `synq _complete-server` daemon the scripts talk to over a socket (`server.py`). Self-contained; `cli.py` only wires `autocompletion=` callbacks to it. Tests type Tab into real bash/zsh on a pty (`tests/test_completion.py`).
- `src/SpiriSynq/cli.py` — typer CLI; registered as **both** `synq` and `spirisynq` entry points. `click` is a direct dependency (needed to keep `uv run`/`uvx spirisynq` working regardless of what `typer` declares) — fine to import if needed, but prefer typer's own API where possible.
