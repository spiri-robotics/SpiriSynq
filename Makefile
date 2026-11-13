# SpiriSynq tasks. Each mirrors a CI job, so `make check` passing locally
# means CI should too.

.PHONY: help check lint format test docs docs-serve pdf lint-docs clean rust-lint rust-test interpreters

help:
	@echo "make check       lint and test and build docs, as CI does"
	@echo "make lint        ruff format --check and ruff check"
	@echo "make format      apply ruff formatting and fixes"
	@echo "make test        run pytest"
	@echo "make rust-lint   cargo fmt --check and clippy, for the Rust core"
	@echo "make rust-test   Rust tests, failing below 100% line and function coverage"
	@echo "make interpreters rebuild the WASM interpreters spirisynq-extism bundles (needs Nix)"
	@echo "make docs        build HTML docs into docs/_build/html"
	@echo "make docs-serve  build docs and serve them on http://127.0.0.1:8000"
	@echo "make pdf         build the controlled PDF (needs LaTeX: latexmk, texlive, rsvg-convert)"
	@echo "make lint-docs   check the docs' prose with Vale"
	@echo "make clean       remove build output"

check: lint test docs rust-lint rust-test
lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .
	cd rust && cargo fmt

test:
	uv run pytest

rust-lint:
	cd rust && cargo fmt --check
	cd rust && cargo clippy --locked --all-targets -- -D warnings

# Needs Rust 1.96 or newer, cargo-llvm-cov, and LLVM tools matching rustc's LLVM (`rustc -vV`):
# rustup's llvm-tools-preview, or the flake's shell, which has them all:
#   nix develop .#rust -c make rust-test
# The extension module is measured by its Python tests (tests/core), with an instrumented
# build installed for the run; a plain build is put back afterwards.
rust-test:
	status=0; (cd rust && export LLVM_COV=$${LLVM_COV:-$$(command -v llvm-cov)} LLVM_PROFDATA=$${LLVM_PROFDATA:-$$(command -v llvm-profdata)} \
		&& { [ -n "$$LLVM_COV" ] || unset LLVM_COV; } && { [ -n "$$LLVM_PROFDATA" ] || unset LLVM_PROFDATA; } \
		&& eval "$$(cargo llvm-cov show-env --sh)" \
		&& cargo llvm-cov clean --workspace \
		&& cargo test --locked --workspace --exclude spirisynq-python \
		&& (cd .. && uvx maturin develop --uv --locked -q && uv run --no-sync pytest -q tests/core) \
		&& cargo llvm-cov report --lcov --output-path target/lcov.info \
		&& cargo llvm-cov report --json --output-path target/coverage.json \
		&& uv run --no-sync python coverage_gate.py target/lcov.info target/coverage.json) || status=$$?; \
	uvx maturin develop --uv --locked -q; exit $$status

docs:
	uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html

docs-serve: docs
	uv run python -m http.server --bind 127.0.0.1 --directory docs/_build/html 8000

# Named by spiri_docs for the document number and revision, as CI publishes it.
pdf:
	uv run --group docs sphinx-build -W --keep-going -b latex docs docs/_build/latex
	$(MAKE) -C docs/_build/latex all-pdf
	@echo "docs/_build/latex/$$(uv run --group docs python -m spiri_docs name).pdf"

lint-docs:
	vale sync
	vale docs/


clean:
	rm -rf docs/_build dist .pytest_cache .ruff_cache .coverage htmlcov rust/target

# The interpreters spirisynq-extism bundles, built from pinned sources with pinned tools
# (rust/spirisynq-extism/interpreters/default.nix) and committed. The build is reproducible: CI
# rebuilds them and fails if they differ from the committed ones.
interpreters:
	out=$$(nix build .#interpreters --no-link --print-out-paths) \
		&& install -m 644 $$out/*.wasm rust/spirisynq-extism/interpreters/
