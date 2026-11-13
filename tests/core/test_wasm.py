"""WASM properties and methods through SpiriSynq._core, run by Extism."""

import hashlib

import pytest

from SpiriSynq import _core

from .conftest import BATTERY, wait_for

TOPIC = "robot/battery"

# A property that returns the voltage, and a method that divides it by its `by` argument.
PLUGIN = r"""
(module
  (import "extism:host/env" "alloc" (func $alloc (param i64) (result i64)))
  (import "extism:host/env" "store_u8" (func $store_u8 (param i64 i32)))
  (import "extism:host/env" "length" (func $length (param i64) (result i64)))
  (import "extism:host/env" "output_set" (func $output_set (param i64 i64)))
  (import "extism:host/user" "field_get" (func $field_get (param i64) (result i64)))
  (import "extism:host/user" "field_get_f64" (func $field_get_f64 (param i64) (result f64)))
  (import "extism:host/user" "field_set_f64" (func $field_set_f64 (param i64 f64)))
  (import "extism:host/user" "arg_get_f64" (func $arg_get_f64 (param i64) (result f64)))
  (memory 1)
  (data (i32.const 0) "voltage")
  (data (i32.const 16) "by")
  (func $str (param $at i32) (param $len i32) (result i64)
    (local $h i64) (local $i i32)
    (local.set $h (call $alloc (i64.extend_i32_u (local.get $len))))
    (block $done (loop $next
      (br_if $done (i32.ge_u (local.get $i) (local.get $len)))
      (call $store_u8
        (i64.add (local.get $h) (i64.extend_i32_u (local.get $i)))
        (i32.load8_u (i32.add (local.get $at) (local.get $i))))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br $next)))
    (local.get $h))
  (func (export "voltage") (result i32)
    (local $h i64)
    (local.set $h (call $field_get (call $str (i32.const 0) (i32.const 7))))
    (call $output_set (local.get $h) (call $length (local.get $h)))
    (i32.const 0))
  (func (export "divide") (result i32)
    (call $field_set_f64 (call $str (i32.const 0) (i32.const 7))
      (f64.div (call $field_get_f64 (call $str (i32.const 0) (i32.const 7)))
               (call $arg_get_f64 (call $str (i32.const 16) (i32.const 2)))))
    (i32.const 0))
)
"""


def code(role: str, entry: str) -> dict:
    digest = hashlib.sha256(PLUGIN.encode()).hexdigest()
    return {
        "role": role,
        "loader": "wat",
        "entry": entry,
        "source": PLUGIN,
        "hash": f"sha256:{digest}",
    }


PER_CELL = "def per_cell(self, cells):\n    return self.voltage / cells\n"


def python(entry: str, source: str) -> dict:
    digest = hashlib.sha256(source.encode()).hexdigest()
    return {
        "role": "method",
        "loader": "python",
        "entry": entry,
        "source": source,
        "hash": f"sha256:{digest}",
    }


@pytest.fixture
def battery() -> _core.Schema:
    return _core.Schema(
        {
            **BATTERY,
            "x-wasm": {
                "volts": code("property", "voltage"),
                "divide": code("method", "divide"),
                "check": {**code("before_call", "voltage"), "of": "set_limit"},
                "per_cell": python("per_cell", PER_CELL),
            },
        }
    )


def test_a_property_reads_the_objects_fields(network, battery):
    (a,) = network(1)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.5)
    assert a.run_wasm(owner, "volts") == 12.5


def test_a_method_on_a_mirror_writes_to_the_owner(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.0)
    copy = b.create_mirror(TOPIC, battery)
    got = wait_for(b, _core.Incoming, lambda n: n.path == "voltage")
    b.set_field_local(copy, "voltage", got.data)
    assert b.run_wasm(copy, "divide", {"by": 4}) is None
    assert b.field(copy, "voltage") == 3.0
    got = wait_for(a, _core.Incoming, lambda n: n.path == "voltage")
    assert (got.object, got.data, got.origin) == (owner, 3.0, "write")


def test_python_source_runs_in_micropython(network, battery):
    (a,) = network(1)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.0)
    assert a.run_wasm(owner, "per_cell", {"cells": 4}) == 3.0


def test_code_that_fails_or_cant_run_raises(network, battery):
    (a,) = network(1)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.0)
    with pytest.raises(_core.SynqError, match="no argument `by`"):
        a.run_wasm(owner, "divide")
    with pytest.raises(_core.SynqError, match="is a hook"):
        a.run_wasm(owner, "check")
    with pytest.raises(_core.SynqError, match="no WASM code `nope`"):
        a.run_wasm(owner, "nope")
    assert a.field(owner, "voltage") == 12.0
