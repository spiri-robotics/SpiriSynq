//! WASM properties and methods, run on a scripted host: what code sees, what it sets, how its
//! result is read, and what's refused.

mod common;

use common::*;
use serde_json::{Value as Json, json};
use spirisynq_core::{Error, FieldOptions, Frame, ObjectOptions, WasmHost};
use spirisynq_schema::WasmEntry;
use spirisynq_yaml::Value;

const HASH: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

const BATTERY_CODE: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Battery
properties:
  voltage: {type: number}
  charging: {type: boolean}
  photo: {type: string, x-yaml-tag: "!!binary"}
  camera: {type: string, x-yaml-tag: "!Link", x-link: "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"}
  gps: {anyOf: [{$ref: "#/$defs/Gps"}, {type: "null"}]}
  log: {type: string}
$defs:
  Gps:
    type: object
    x-sync: fields
    properties:
      latitude: {type: number}
      fix: {$ref: "#/$defs/Fix"}
  Fix: {type: object, x-sync: fields, properties: {quality: {type: integer}}}
  Either: {anyOf: [{type: [integer, "null"]}, {type: string, x-yaml-tag: "!Name"}]}
  Loop: {$ref: "#/$defs/Loop"}
  Knot: {anyOf: [{$ref: "#/$defs/Knot"}]}
  Kinds:
    anyOf:
      - {type: 7}
      - {type: "null", x-yaml-tag: "!N"}
      - {type: boolean, x-yaml-tag: "!B"}
      - {type: integer, x-yaml-tag: "!I"}
      - {type: number, x-yaml-tag: "!F"}
      - {type: string, x-yaml-tag: "!S"}
      - {type: array, x-yaml-tag: "!A"}
      - {type: object, x-yaml-tag: "!O"}
x-remote-methods:
  set_limit: {}
x-wasm:
  cell_voltage: {role: property, loader: wat, entry: cell_voltage, hash: HASH}
  charge: {role: method, loader: wat, entry: charge, hash: HASH}
  check: {role: before_call, of: set_limit, loader: wat, entry: check, hash: HASH}
  shaped:
    role: method
    loader: wat
    entry: shaped
    hash: HASH
    returns:
      type: object
      properties:
        blob: {type: string, x-yaml-tag: "!!binary"}
        link: {type: string, x-yaml-tag: "!Link"}
        links: {type: array, items: {type: string, x-yaml-tag: "!Link"}}
        gps: {anyOf: [{type: "null"}, {$ref: "#/$defs/Gps"}]}
        int: {$ref: "#/$defs/Either"}
        name: {$ref: "#/$defs/Either"}
        float: {$ref: "#/$defs/Either"}
        loop: {$ref: "#/$defs/Loop"}
        knot: {$ref: "#/$defs/Knot"}
        str: {type: string, x-yaml-tag: "!!str"}
        missing: {$ref: "#/$defs/Nope"}
        away: {$ref: "other.json"}
        flag: {type: boolean}
        whole: {type: number}
        either_number: {type: [number, "null"]}
        count: {type: [integer, number]}
        kinds: {type: array, items: {$ref: "#/$defs/Kinds"}}
      additionalProperties: {type: string, x-yaml-tag: "!Extra"}
"##;

/// A host that runs a script instead of WASM.
type Body = Box<dyn FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String>>;

struct Script(Body);

impl WasmHost for Script {
    fn run(&mut self, code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String> {
        (self.0)(code, frame)
    }
}

fn host(f: impl FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String> + 'static) -> Script {
    Script(Box::new(f))
}

/// A host whose code returns `output`.
fn returning(output: &str) -> Script {
    let output = output.as_bytes().to_vec();
    host(move |_, _| Ok(output.clone()))
}

/// A network with a battery owned on session 0 and a mirror of it on session 1. The owner's
/// `log` field has its own encoding.
fn batteries() -> (Net, spirisynq_core::ObjectId, spirisynq_core::ObjectId) {
    let schema = schema_from(&BATTERY_CODE.replace("HASH", HASH));
    let mut net = Net::new();
    net.add_session();
    let raw = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    let owner = net
        .session(0)
        .create_owner("robot/battery", schema.clone(), options(&[("log", raw)]))
        .unwrap();
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", schema, ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    (net, owner, mirror)
}

fn input(frame: &Frame) -> Json {
    serde_json::from_slice(&frame.input()).unwrap()
}

#[test]
fn code_sees_the_fields_and_arguments_as_json() {
    let (mut net, owner, _) = batteries();
    let s = net.session(0);
    s.set_field(owner, "voltage", v("12.5")).unwrap();
    s.set_field(owner, "gps", v("{latitude: 45.5, fix: {quality: 3}}"))
        .unwrap();
    s.set_field(owner, "camera", v("!Link robot/camera"))
        .unwrap();
    s.set_field(owner, "photo", v("!!binary AAEC")).unwrap();
    let mut code = host(|code, frame| {
        assert_eq!(code.name, "cell_voltage");
        assert_eq!(
            input(frame),
            json!({
                "self": {
                    "voltage": 12.5,
                    "photo": "AAEC",
                    "camera": "robot/camera",
                    "gps": {"latitude": 45.5, "fix": {"quality": 3}},
                },
                "args": {"cells": 4, "tag": "AAE=", "none": null, "flag": true, "list": [1]},
            })
        );
        assert_eq!(frame.field("gps/fix/quality"), Ok(json!(3)));
        assert_eq!(frame.field("charging"), Ok(Json::Null));
        assert_eq!(frame.field("nope"), Err("no field `nope`".into()));
        assert_eq!(
            frame.field("log"),
            Err("field `log` has its own encoding".into())
        );
        assert_eq!(frame.arg("cells"), Ok(json!(4)));
        assert_eq!(frame.arg("nope"), Err("no argument `nope`".into()));
        let volts = frame.field("voltage")?.as_f64().unwrap();
        let cells = frame.arg("cells")?.as_f64().unwrap();
        Ok((volts / cells).to_string().into_bytes())
    });
    let args = v("{cells: 4, tag: !!binary AAE=, none: null, flag: true, list: [1]}");
    let result = s.run_wasm(&mut code, owner, "cell_voltage", &args);
    assert_eq!(result, Ok(v("3.125")));
}

#[test]
fn a_property_cant_set_fields() {
    let (mut net, owner, _) = batteries();
    let mut code = host(|_, frame| {
        frame.set_field("voltage", json!(1.0))?;
        Ok(Vec::new())
    });
    let result = net
        .session(0)
        .run_wasm(&mut code, owner, "cell_voltage", &Value::Null);
    assert_eq!(
        result,
        Err(Error::Wasm(
            "a property can't set fields (`voltage`)".into()
        ))
    );
}

#[test]
fn a_method_on_the_owner_sets_fields_and_they_publish() {
    let (mut net, owner, mirror) = batteries();
    let mut code = host(|_, frame| {
        frame.set_field("voltage", json!(13))?;
        frame.set_field("gps", json!({"latitude": 1.5}))?;
        frame.set_field("gps/latitude", json!(2.5))?;
        frame.set_field("voltage", json!(13.5))?;
        frame.set_field("photo", json!("AAEC"))?;
        // Code reads back what it set.
        assert_eq!(frame.field("gps/latitude"), Ok(json!(2.5)));
        assert_eq!(
            frame.set_field("gps/fix/quality", json!(1)),
            Err("`gps/fix` isn't there to hold `gps/fix/quality`".into())
        );
        assert_eq!(
            frame.set_field("log", json!("x")),
            Err("field `log` has its own encoding".into())
        );
        Ok(b" \n".to_vec())
    });
    let result = net
        .session(0)
        .run_wasm(&mut code, owner, "charge", &v("{}"));
    assert_eq!(result, Ok(Value::Null));
    settle(&mut net);
    for (path, value) in [
        ("voltage", "13.5"),
        ("gps", "{latitude: 2.5}"),
        ("photo", "!!binary AAEC"),
    ] {
        let want = Some(v(value));
        assert_eq!(net.session(0).field(owner, path).unwrap(), want.as_ref());
        assert_eq!(net.session(1).field(mirror, path).unwrap(), want.as_ref());
    }
}

#[test]
fn a_method_on_a_mirror_writes_to_the_owner() {
    let (mut net, owner, mirror) = batteries();
    let mut code = host(|_, frame| {
        frame.set_field("voltage", json!(11.0))?;
        Ok(b"null".to_vec())
    });
    net.session(1)
        .run_wasm(&mut code, mirror, "charge", &Value::Null)
        .unwrap();
    settle(&mut net);
    assert_eq!(
        net.session(0).field(owner, "voltage").unwrap(),
        Some(&v("11.0"))
    );
}

#[test]
fn nothing_is_set_unless_the_code_succeeds() {
    let (mut net, owner, _) = batteries();
    let s = net.session(0);
    s.set_field(owner, "voltage", v("12.0")).unwrap();
    let mut failing = host(|_, frame| {
        frame.set_field("voltage", json!(1.0))?;
        Err("trapped".into())
    });
    let result = s.run_wasm(&mut failing, owner, "charge", &Value::Null);
    assert_eq!(result, Err(Error::Wasm("trapped".into())));
    let mut not_json = host(|_, frame| {
        frame.set_field("voltage", json!(1.0))?;
        Ok(b"{".to_vec())
    });
    let result = s.run_wasm(&mut not_json, owner, "charge", &Value::Null);
    assert!(matches!(result, Err(Error::Wasm(why)) if why.starts_with("the result isn't JSON")));
    let mut bad_field = host(|_, frame| {
        frame.set_field("voltage", json!(1.0))?;
        frame.set_field("photo", json!("not base64!"))?;
        Ok(Vec::new())
    });
    let result = s.run_wasm(&mut bad_field, owner, "charge", &Value::Null);
    assert!(
        matches!(result, Err(Error::Wasm(why)) if why.starts_with("field `photo`: not base64"))
    );
    assert_eq!(s.field(owner, "voltage").unwrap(), Some(&v("12.0")));
}

#[test]
fn the_result_is_read_as_its_schema_says() {
    let (mut net, owner, _) = batteries();
    let output = r#"{
        "blob": "AAEC",
        "link": "robot/camera",
        "links": ["a", "b"],
        "gps": {"latitude": 1.5, "fix": {"quality": 2}},
        "int": 3,
        "name": "ann",
        "float": 3.5,
        "loop": "l",
        "knot": "k",
        "str": "s",
        "missing": "m",
        "away": "a",
        "flag": true,
        "whole": 3,
        "either_number": 4,
        "count": 5,
        "kinds": [null, true, 1, 1.5, "s", [], {}, "x"],
        "extra": "e",
        "none": null
    }"#;
    let result = net
        .session(0)
        .run_wasm(&mut returning(output), owner, "shaped", &Value::Null);
    let want = v(r#"{
        blob: !!binary AAEC,
        link: !Link robot/camera,
        links: [!Link a, !Link b],
        gps: {latitude: 1.5, fix: {quality: 2}},
        int: 3,
        name: !Name ann,
        float: 3.5,
        loop: l,
        knot: k,
        str: s,
        missing: m,
        away: a,
        flag: true,
        whole: 3.0,
        either_number: 4.0,
        count: 5,
        kinds: [null, true, 1, 1.5, !S s, !A [], !O {}, !S x],
        extra: !Extra e,
        none: null
    }"#);
    // Only strings and collections take a local tag.
    assert_eq!(result, Ok(want));
    let without_schema =
        net.session(0)
            .run_wasm(&mut returning("[1, 2.5]"), owner, "charge", &Value::Null);
    assert_eq!(without_schema, Ok(v("[1, 2.5]")));
    let bad = net.session(0).run_wasm(
        &mut returning(r#"{"blob": "%"}"#),
        owner,
        "shaped",
        &Value::Null,
    );
    assert!(matches!(bad, Err(Error::Wasm(why)) if why.starts_with("the result: not base64")));
}

#[test]
fn what_cant_run_is_refused() {
    let (mut net, owner, _) = batteries();
    let s = net.session(0);
    let refused = |s: &mut spirisynq_core::Session, name: &str, args: &str| {
        s.wasm_job(owner, name, &v(args)).map(|_| ()).unwrap_err()
    };
    assert_eq!(
        refused(s, "nope", "null"),
        Error::UnknownCode("nope".into())
    );
    assert_eq!(
        refused(s, "check", "null"),
        Error::NotRunnable("check".into())
    );
    assert_eq!(
        refused(s, "charge", "[1]"),
        Error::Wasm("the arguments must be a mapping".into())
    );
    assert_eq!(
        refused(s, "charge", "{1: 2}"),
        Error::Wasm("the arguments: a mapping key that isn't a string has no JSON form".into())
    );
    s.set_field(owner, "voltage", v(".nan")).unwrap();
    assert_eq!(
        refused(s, "charge", "null"),
        Error::Wasm("field `voltage`: NaN has no JSON form".into())
    );
    for (error, text) in [
        (Error::UnknownCode("x".into()), "no WASM code `x`"),
        (
            Error::NotRunnable("x".into()),
            "`x` is a hook, not a property or method",
        ),
        (Error::Wasm("trapped".into()), "WASM: trapped"),
    ] {
        assert_eq!(error.to_string(), text);
    }
}

#[test]
fn a_job_runs_without_the_session() {
    let (mut net, owner, _) = batteries();
    let mut job = net
        .session(0)
        .wasm_job(owner, "charge", &Value::Null)
        .unwrap();
    assert_eq!(job.code().name, "charge");
    assert_eq!(input(job.frame()), json!({"self": {}, "args": {}}));
    let mut code = host(|_, frame| {
        frame.set_field("voltage", json!(9.0))?;
        Ok(b"1".to_vec())
    });
    let output = job.run(&mut code);
    // The object went away while its code ran: the field can't be set.
    net.session(0).close(owner).unwrap();
    let result = net.session(0).finish_wasm(job, output);
    assert_eq!(result, Err(Error::UnknownObject));
}
