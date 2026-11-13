"""Fail unless every line and function of the Rust code is covered.

Reads the reports cargo-llvm-cov writes for the whole workspace, covering the cargo tests and
the Python tests of the extension module: an lcov report for lines and a JSON export for
functions.

- A source file appears once per binary that uses it, so counts are added up across binaries.
  A function counts as covered if any binary ran it; functions are told apart by where they
  start in the source, since the same function has a different symbol in each binary.
- PyO3's generated wrappers are left out: the `__pymethod_*` functions it writes for each
  method, and the `#[pymethods]` and `#[pyclass]` lines they sit on. llvm-cov counts the
  wrappers of methods that take no arguments as never called, even when they are (checked with
  PyO3 0.30). The methods themselves are counted as usual.

Usage: python coverage_gate.py <lcov file> <json file>
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

WORKSPACE = str(Path(__file__).resolve().parent) + "/"


def generated_line(text: str) -> bool:
    return text.strip().startswith(("#[pymethods]", "#[pyclass"))


def missed_lines(lcov: str) -> tuple[int, list[str]]:
    counts: dict[tuple[str, int], int] = defaultdict(int)
    path = ""
    for record in Path(lcov).read_text().splitlines():
        kind, _, data = record.partition(":")
        if kind == "SF":
            path = data
        elif kind == "DA":
            line, count = (int(x) for x in data.split(",")[:2])
            counts[path, line] += count
    sources = {path: Path(path).read_text().splitlines() for path, _ in counts}
    lines = {k: c for k, c in counts.items() if not generated_line(sources[k[0]][k[1] - 1])}
    return len(lines), [f"{p}:{line}: never run" for (p, line), c in sorted(lines.items()) if not c]


def missed_functions(export: str) -> tuple[int, list[str]]:
    counts: dict[tuple[str, int, int], int] = defaultdict(int)
    for function in json.loads(Path(export).read_text())["data"][0]["functions"]:
        if "___pymethod_" in function["name"]:
            continue
        line, column, *_, file = function["regions"][0][:6]
        path = function["filenames"][file]
        # The workspace's own code: not the tests' helpers, nor dependencies.
        if path.startswith(WORKSPACE) and "/src/" in path:
            counts[path, line, column] += function["count"]
    missed = [f"{p}:{line}:{col}: function never called" for (p, line, col), c in counts.items()]
    return len(counts), sorted(m for m, c in zip(missed, counts.values(), strict=True) if not c)


def main(lcov: str, export: str) -> int:
    lines, missed = missed_lines(lcov)
    functions, more = missed_functions(export)
    missed += more
    for miss in missed:
        print(miss)
    print(f"{lines} lines and {functions} functions, {len(missed)} not covered")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
