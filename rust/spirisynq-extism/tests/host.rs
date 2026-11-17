//! Real WASM code on Extism: the host functions, both loaders, the limits, and what's refused.

use std::sync::Arc;
use std::time::Duration;

use spirisynq_core::memory::MemoryNetwork;
use spirisynq_core::{Error, FieldOptions, ObjectId, ObjectOptions, Session};
use spirisynq_extism::{Host, Limits};
use spirisynq_schema::{Source, TypeSchema};
use spirisynq_yaml::Value;

/// A plug-in using every host function. Strings it passes are copied from its own memory into
/// Extism's (`$str`).
const PLUGIN: &str = r#"
(module
  (import "extism:host/env" "alloc" (func $alloc (param i64) (result i64)))
  (import "extism:host/env" "store_u8" (func $store_u8 (param i64 i32)))
  (import "extism:host/env" "length" (func $length (param i64) (result i64)))
  (import "extism:host/env" "input_length" (func $input_length (result i64)))
  (import "extism:host/env" "input_load_u8" (func $input_load_u8 (param i64) (result i32)))
  (import "extism:host/env" "output_set" (func $output_set (param i64 i64)))
  (import "extism:host/user" "field_get" (func $field_get (param i64) (result i64)))
  (import "extism:host/user" "field_get_i64" (func $field_get_i64 (param i64) (result i64)))
  (import "extism:host/user" "field_get_f64" (func $field_get_f64 (param i64) (result f64)))
  (import "extism:host/user" "field_set" (func $field_set (param i64 i64)))
  (import "extism:host/user" "field_set_i64" (func $field_set_i64 (param i64 i64)))
  (import "extism:host/user" "field_set_f64" (func $field_set_f64 (param i64 f64)))
  (import "extism:host/user" "arg_get" (func $arg_get (param i64) (result i64)))
  (import "extism:host/user" "arg_get_i64" (func $arg_get_i64 (param i64) (result i64)))
  (import "extism:host/user" "arg_get_f64" (func $arg_get_f64 (param i64) (result f64)))
  (memory 1)
  (data (i32.const 0) "voltage")
  (data (i32.const 16) "cells")
  (data (i32.const 32) "name")
  (data (i32.const 48) "by")
  (data (i32.const 64) "n")
  (data (i32.const 80) "value")
  (data (i32.const 96) "{")

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
  (func $out (param $h i64)
    (call $output_set (local.get $h) (call $length (local.get $h))))

  (func (export "echo") (result i32)
    (local $h i64) (local $i i64) (local $n i64)
    (local.set $n (call $input_length))
    (local.set $h (call $alloc (local.get $n)))
    (block $done (loop $next
      (br_if $done (i64.ge_u (local.get $i) (local.get $n)))
      (call $store_u8 (i64.add (local.get $h) (local.get $i))
                      (call $input_load_u8 (local.get $i)))
      (local.set $i (i64.add (local.get $i) (i64.const 1)))
      (br $next)))
    (call $out (local.get $h))
    (i32.const 0))
  (func (export "voltage") (result i32)
    (call $out (call $field_get (call $str (i32.const 0) (i32.const 7))))
    (i32.const 0))
  (func (export "halve") (result i32)
    (call $field_set_f64 (call $str (i32.const 0) (i32.const 7))
      (f64.div (call $field_get_f64 (call $str (i32.const 0) (i32.const 7)))
               (call $arg_get_f64 (call $str (i32.const 48) (i32.const 2)))))
    (i32.const 0))
  (func (export "add_cells") (result i32)
    (call $field_set_i64 (call $str (i32.const 16) (i32.const 5))
      (i64.add (call $field_get_i64 (call $str (i32.const 16) (i32.const 5)))
               (call $arg_get_i64 (call $str (i32.const 64) (i32.const 1)))))
    (i32.const 0))
  (func (export "rename") (result i32)
    (call $field_set (call $str (i32.const 32) (i32.const 4))
                     (call $arg_get (call $str (i32.const 80) (i32.const 5))))
    (i32.const 0))
  (func (export "set_not_json") (result i32)
    (call $field_set (call $str (i32.const 32) (i32.const 4)) (call $str (i32.const 96) (i32.const 1)))
    (i32.const 0))
  (func (export "set_nan") (result i32)
    (call $field_set_f64 (call $str (i32.const 0) (i32.const 7))
                         (f64.div (f64.const 0) (f64.const 0)))
    (i32.const 0))
  (func (export "voltage_as_int") (result i32)
    (drop (call $field_get_i64 (call $str (i32.const 0) (i32.const 7))))
    (i32.const 0))
  (func (export "name_as_number") (result i32)
    (drop (call $field_get_f64 (call $str (i32.const 32) (i32.const 4))))
    (i32.const 0))
  (func (export "bad_offset") (result i32)
    (drop (call $field_get (i64.const 999999)))
    (i32.const 0))
  (func (export "spin") (result i32)
    (loop $forever (br $forever))
    (i32.const 0))
  (func (export "grow") (result i32)
    (drop (memory.grow (i32.const 2000)))
    (i32.const 0))
)
"#;

/// A plug-in that needs a host function nobody provides.
const UNLINKED: &str = r#"
(module
  (import "extism:host/user" "launch" (func $launch))
  (func (export "main") (result i32) (call $launch) (i32.const 0)))
"#;

const BATTERY: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Battery
properties:
  voltage: {type: number}
  cells: {type: integer}
  name: {type: string}
  log: {type: string}
x-remote-methods:
  set_limit: {}
"##;

fn v(text: &str) -> Value {
    spirisynq_yaml::parse(text).unwrap()
}

fn s(text: &str) -> Value {
    Value::String(text.into())
}

/// An `x-wasm` entry.
fn entry(
    name: &str,
    role: &str,
    loader: &str,
    function: &str,
    source: Option<Source>,
) -> (Value, Value) {
    let hash = match &source {
        Some(source) => source.hash(),
        None => format!("sha256:{}", "0".repeat(64)),
    };
    let mut fields = vec![
        (s("role"), s(role)),
        (s("loader"), s(loader)),
        (s("entry"), s(function)),
        (s("hash"), s(&hash)),
    ];
    if role == "before_call" {
        fields.push((s("of"), s("set_limit")));
    }
    match source {
        Some(Source::Text(text)) => fields.push((s("source"), Value::String(text))),
        Some(Source::Binary(bytes)) => fields.push((s("source"), Value::Binary(bytes))),
        None => {}
    }
    (s(name), Value::Map(fields))
}

/// The battery's type, with every export of the plug-in as a method (and `voltage` also as a
/// property), plus entries that can't run.
fn schema() -> Arc<TypeSchema> {
    let text = || Some(Source::Text(PLUGIN.into()));
    let binary = Some(Source::Binary(wat::parse_str(PLUGIN).unwrap()));
    let mut code = vec![
        entry("voltage_now", "property", "wat", "voltage", text()),
        entry("set_in_property", "property", "wat", "halve", text()),
        entry("halve_compiled", "method", "wasm", "halve", binary),
        entry("check", "before_call", "wat", "echo", text()),
        entry("elsewhere", "method", "wat", "echo", None),
        entry(
            "cobol",
            "method",
            "cobol",
            "main",
            Some(Source::Text("pass".into())),
        ),
        entry(
            "broken",
            "method",
            "wat",
            "main",
            Some(Source::Text("(module".into())),
        ),
        entry("absent", "method", "wat", "absent", text()),
        entry(
            "unlinked",
            "method",
            "wat",
            "main",
            Some(Source::Text(UNLINKED.into())),
        ),
    ];
    for export in [
        "echo",
        "halve",
        "add_cells",
        "rename",
        "set_not_json",
        "set_nan",
        "voltage_as_int",
        "name_as_number",
        "bad_offset",
        "spin",
        "grow",
    ] {
        code.push(entry(export, "method", "wat", export, text()));
    }
    let Value::Map(mut root) = v(BATTERY) else { unreachable!() };
    root.push((s("x-wasm"), Value::Map(code)));
    Arc::new(TypeSchema::from_value(Value::Map(root)).unwrap())
}

/// An owned battery: 12.5 V, 4 cells, named "main".
fn battery() -> (MemoryNetwork, ObjectId) {
    let mut net = MemoryNetwork::new();
    net.add_session();
    let raw = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    let options = ObjectOptions {
        fields: vec![("log".into(), raw)],
        ..ObjectOptions::default()
    };
    let s = net.session(0);
    let owner = s.create_owner("robot/battery", schema(), options).unwrap();
    s.set_field(owner, "voltage", v("12.5")).unwrap();
    s.set_field(owner, "cells", v("4")).unwrap();
    s.set_field(owner, "name", v("main")).unwrap();
    (net, owner)
}

fn run(
    s: &mut Session,
    host: &mut Host,
    object: ObjectId,
    name: &str,
    args: &str,
) -> Result<Value, Error> {
    s.run_wasm(host, object, name, &v(args))
}

/// The error a run failed with.
fn failure(result: Result<Value, Error>) -> String {
    match result {
        Err(Error::Wasm(why)) => why,
        other => panic!("didn't fail: {other:?}"),
    }
}

#[test]
fn code_gets_its_input_and_gives_a_result() {
    let (mut net, owner) = battery();
    let mut host = Host::default();
    let s = net.session(0);
    let echoed = run(s, &mut host, owner, "echo", "{by: 2}").unwrap();
    assert_eq!(
        echoed,
        v("{self: {voltage: 12.5, cells: 4, name: main}, args: {by: 2}}")
    );
    assert_eq!(
        run(s, &mut host, owner, "voltage_now", "null"),
        Ok(v("12.5"))
    );
}

#[test]
fn code_reads_and_sets_fields_and_reads_arguments() {
    let (mut net, owner) = battery();
    let mut host = Host::default();
    let s = net.session(0);
    assert_eq!(
        run(s, &mut host, owner, "halve", "{by: 2}"),
        Ok(Value::Null)
    );
    assert_eq!(
        run(s, &mut host, owner, "halve_compiled", "{by: 2.5}"),
        Ok(Value::Null)
    );
    assert_eq!(s.field(owner, "voltage").unwrap(), Some(&v("2.5")));
    run(s, &mut host, owner, "add_cells", "{n: 2}").unwrap();
    assert_eq!(s.field(owner, "cells").unwrap(), Some(&v("6")));
    run(s, &mut host, owner, "rename", "{value: spare}").unwrap();
    assert_eq!(s.field(owner, "name").unwrap(), Some(&v("spare")));
}

#[test]
fn a_host_function_that_cant_do_its_job_stops_the_code() {
    let (mut net, owner) = battery();
    let mut host = Host::default();
    let s = net.session(0);
    let cases = [
        (
            "set_in_property",
            "{by: 2}",
            "a property can't set fields (`voltage`)",
        ),
        ("halve", "null", "no argument `by`"),
        ("set_not_json", "null", "not JSON"),
        ("set_nan", "null", "NaN has no JSON form"),
        ("voltage_as_int", "null", "`voltage` isn't an integer: 12.5"),
        ("name_as_number", "null", "`name` isn't a number: \"main\""),
        ("bad_offset", "null", "invalid memory offset"),
    ];
    for (name, args, why) in cases {
        let got = failure(run(s, &mut host, owner, name, args));
        assert!(got.contains(why), "{name}: {got}");
    }
    // The code runs again after a failure.
    assert_eq!(
        run(s, &mut host, owner, "voltage_now", "null"),
        Ok(v("12.5"))
    );
}

#[test]
fn code_is_limited_in_time_and_memory() {
    let (mut net, owner) = battery();
    let limits = Limits {
        timeout: Duration::from_millis(100),
        ..Limits::default()
    };
    let mut host = Host::new(limits);
    assert_eq!(host.limits(), limits);
    let s = net.session(0);
    let got = failure(run(s, &mut host, owner, "spin", "null"));
    assert!(got.contains("timeout"), "{got}");
    let got = failure(run(s, &mut host, owner, "grow", "null"));
    assert!(got.contains("oom"), "{got}");
}

#[test]
fn code_that_cant_be_loaded_says_why() {
    let (mut net, owner) = battery();
    let mut host = Host::default();
    let s = net.session(0);
    let cases = [
        ("cobol", "loader `cobol` isn't available yet"),
        ("broken", "expected"),
        ("absent", "absent"),
        ("unlinked", "launch"),
    ];
    for (name, why) in cases {
        let got = failure(run(s, &mut host, owner, name, "null"));
        assert!(got.contains(why), "{name}: {got}");
    }
    assert_eq!(
        run(s, &mut host, owner, "check", "null"),
        Err(Error::NotRunnable("check".into()))
    );
    // Code left out of the schema, which no owner has sent.
    assert_eq!(
        run(s, &mut host, owner, "elsewhere", "null"),
        Err(Error::CodeNotFetched("elsewhere".into()))
    );
}
