//! What the interpreter tests share: a battery whose type carries the test's code.

#![allow(dead_code)]

use std::sync::Arc;

use spirisynq_core::memory::MemoryNetwork;
use spirisynq_core::{Data, Error, Notice, ObjectId, ObjectOptions, Sample, Session};
use spirisynq_extism::Host;
use spirisynq_schema::{Source, TypeSchema};
use spirisynq_yaml::Value;

const BATTERY: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Battery
properties:
  voltage: {type: number}
  cells: {type: integer}
  name: {type: string}
  readings: {type: array, items: {type: number}}
  info: {type: object}
  smoothed: {type: number}
  blob_a: {type: string, x-yaml-tag: "!!binary"}
  blob_b: {type: string, x-yaml-tag: "!!binary"}
  blob_c: {type: string, x-yaml-tag: "!!binary"}
  blob_d: {type: string, x-yaml-tag: "!!binary"}
  blob_e: {type: string, x-yaml-tag: "!!binary"}
  frames: {type: array, items: {type: string, x-yaml-tag: "!!binary"}}
  either: {anyOf: [{type: string, x-yaml-tag: "!!binary"}, {type: string}, {type: integer}]}
  anything: {}
x-remote-methods:
  history:
    parameters: {type: object, properties: {minutes: {type: integer}}}
    returns: {type: array, items: {type: number}}
  count:
    returns: {type: number}
    x-generator: true
  summary:
    returns: {type: string}
  write:
    x-key: write/{path:**}
    parameters: {type: object, properties: {path: {type: string}}}
"##;

/// One piece of code: (entry, role, params, source). A hook's role says what it hooks:
/// `after_receive:blob_a`, `on_change_mirror:voltage,cells`.
pub type Code = (&'static str, &'static str, &'static str, &'static str);

pub fn v(text: &str) -> Value {
    spirisynq_yaml::parse(text).unwrap()
}

pub fn text(text: &str) -> Value {
    Value::String(text.into())
}

/// The battery's type, with `code` in `x-wasm` for `loader`, each entry under its own name.
fn schema(loader: &str, code: &[Code]) -> Arc<TypeSchema> {
    let code = code.iter().map(|(entry, role, params, source)| {
        let (role, of) = match role.split_once(':') {
            Some((role, of)) => (role, Some(of)),
            None => (*role, None),
        };
        let mut fields = vec![
            (text("role"), text(role)),
            (text("loader"), text(loader)),
            (text("entry"), text(entry)),
            (text("hash"), text(&Source::Text(source.to_string()).hash())),
            (text("source"), text(source)),
        ];
        if !params.is_empty() {
            fields.push((text("params"), v(params)));
        }
        if let Some(of) = of {
            let of = match of.split_once(',') {
                Some(_) => Value::Seq(of.split(',').map(text).collect()),
                None => text(of),
            };
            fields.push((text("of"), of));
        }
        (text(entry), Value::Map(fields))
    });
    let Value::Map(mut root) = v(BATTERY) else { unreachable!() };
    root.push((text("x-wasm"), Value::Map(code.collect())));
    Arc::new(TypeSchema::from_value(Value::Map(root)).unwrap())
}

/// An owned battery: 12.5 V, 4 cells, named "main", readings [1, 2.5], info {name: main}.
/// `write` is a raw method.
pub fn battery(loader: &str, code: &[Code]) -> (MemoryNetwork, ObjectId) {
    let mut net = MemoryNetwork::new();
    net.add_session();
    let s = net.session(0);
    let options = ObjectOptions {
        raw_methods: vec!["write".into()],
        ..ObjectOptions::default()
    };
    let owner = s
        .create_owner("robot/battery", schema(loader, code), options)
        .unwrap();
    for (path, value) in [
        ("voltage", "12.5"),
        ("cells", "4"),
        ("name", "main"),
        ("readings", "[1, 2.5]"),
        ("info", "{name: main}"),
    ] {
        s.set_field(owner, path, v(value)).unwrap();
    }
    (net, owner)
}

/// [`battery`], and a mirror of it on session 1 that hasn't fetched the owner's state.
pub fn pair(loader: &str, code: &[Code]) -> (MemoryNetwork, ObjectId, ObjectId) {
    let (mut net, owner) = battery(loader, code);
    net.add_session();
    let options = ObjectOptions {
        rehydrate: false,
        ..ObjectOptions::default()
    };
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", schema(loader, code), options)
        .unwrap();
    net.run();
    for i in 0..2 {
        while net.session(i).poll_notice().is_some() {}
    }
    (net, owner, mirror)
}

/// The samples session 1 got for `field`, as they arrived.
pub fn arrived(net: &mut MemoryNetwork, field: &str) -> Vec<Sample> {
    net.run();
    let notices = std::iter::from_fn(|| net.session(1).poll_notice());
    notices
        .filter_map(|n| match n {
            Notice::Incoming {
                path,
                data: Data::Raw(sample),
                ..
            } if path == field => Some(sample),
            _ => None,
        })
        .collect()
}

/// A sample with an optional attachment.
pub fn sample(payload: &[u8], encoding: &str, attachment: Option<&[u8]>) -> Sample {
    Sample {
        payload: payload.to_vec().into(),
        encoding: encoding.into(),
        attachment: attachment.map(|a| a.to_vec().into()),
    }
}

pub fn run(
    s: &mut Session,
    host: &mut Host,
    object: ObjectId,
    name: &str,
    args: &Value,
) -> Result<Value, Error> {
    s.run_wasm(host, object, name, args)
}

/// What a run failed with.
pub fn failure(result: Result<Value, Error>) -> String {
    match result {
        Err(Error::Wasm(why)) => why,
        other => panic!("didn't fail: {other:?}"),
    }
}

/// A mapping's value for `key`.
pub fn get<'a>(map: &'a Value, key: &str) -> &'a Value {
    let Value::Map(entries) = map else { panic!("not a mapping: {map:?}") };
    &entries.iter().find(|(k, _)| *k == text(key)).unwrap().1
}

pub fn seq(value: &Value) -> &[Value] {
    match value {
        Value::Seq(items) => items,
        other => panic!("not a sequence: {other:?}"),
    }
}
