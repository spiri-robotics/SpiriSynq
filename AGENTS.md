# AGENTS.md

SpiriSynq synchronizes Python dataclass instances across processes/machines over a Zenoh pub/sub network. Python >=3.13, managed with `uv` (`uv.lock`). Requires no zenoh router — peers discover each other directly.

## Commands

- Tests: `uv run pytest` (config in `pyproject.toml`: `testpaths=["tests"]`, `pythonpath=["."]`). Single test: `uv run pytest tests/test_session.py::test_name`.
- `test_performance.py` is heavier; run separately when needed.
- Docs: `make -C docs` (Sphinx, uses `uv run sphinx-build`). `autodoc_mock_imports` stubs the optional `examples` deps (PIL, cv2, nicegui, imagecodecs), so docs build without them.
- No formatter/linter/typecheck config exists; don't invent one.

## Tests: TCP-only zenoh network

- `tests/conftest.py` opens one seed zenoh session on a random local TCP port; all test sessions connect to it via `zenoh_test_config()` with **multicast scouting disabled**. Do not add UDP multicast or router-based tests.
- Each test gets a fresh `Session` via the autouse `zenoh_current_session` fixture (sets the `current_session` contextvar).
- Zenoh drops messages before a subscriber is ready. Do **not** `time.sleep()` before publish/wait; poll a predicate (`_wait_for(lambda: ...)`) or `obj.synq_publisher.matching_status.matching`.
- Use the `close_test_sessions` fixture pattern (teardown from `SpiriSynq.shutdown._live_sessions`) in tests that create `Session`s directly.

## Naming and API gotchas

- `synq_authoritive` is **intentionally misspelled** (public API, used everywhere) — never "fix" it.
- Wire format is plain Zenoh with YAML payloads; the protocol is documented in `docs/protocol.md` (four mandatory queryables `sr_rehydrate` / `sr_metadata` / `sr_object_schema` / `sr_type_schema`, per-field puts at `<topic>/<field>`, RPCs at `<topic>/<method>`, echo suppression via zenoh `SourceInfo` ZID).
- Serialization is PyYAML via `SessionSerializer` (`SpiriSynq/serializer.py`), not ruamel. Register custom types with `session.register_type_recursive(cls)`; str-subclasses need explicit `yaml_tag`/`to_yaml`/`from_yaml` to round-trip (see `RootFrame` in `SpiriSynq/example_types/position.py`).
- Authoritative objects prepend the hostname as topic prefix; override with env var `SPIRI_SYNQ_BASE_TOPIC`.

## Architecture map

- `SpiriSynq/syncable_objects.py` — `SyncableObject` and `SubSyncableDataclass` bases.
- `SpiriSynq/session.py` — `Session` + `current_session` contextvar (use `session.as_default()` for scoped defaults); module-level default session created at import.
- `SpiriSynq/remote_callables.py` — `@remote_method` RPC support, `RpcException`.
- `SpiriSynq/cli.py` — typer CLI; registered as **both** `synq` and `spirisynq` entry points. `click` is a direct dependency (needed to keep `uv run`/`uvx spirisynq` working regardless of what `typer` declares) — fine to import if needed, but prefer typer's own API where possible.

## Release conventions

- Every change gets a categorized entry in `CHANGES.md` (Features / Fixes / Improvements / Tests / Internal) under `## Unreleased`, and commits use conventional-commit style (`feat:`, `fix:`, `refactor:`).
- Version lives in `pyproject.toml` + `CHANGES.md`. Publishing is automatic: GitHub Actions publishes to PyPI when a `v*` git tag is pushed (build with `python -m build`, trusted publishing via `environment: pypi`).
