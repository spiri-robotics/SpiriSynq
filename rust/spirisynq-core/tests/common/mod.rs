#![allow(dead_code)]

use std::sync::Arc;

use spirisynq_core::memory::MemoryNetwork;
use spirisynq_core::{
    Command, Data, FieldOptions, Notice, ObjectId, ObjectOptions, Origin, Sample, Session,
};
use spirisynq_schema::TypeSchema;
use spirisynq_yaml::Value;

pub type Net = MemoryNetwork;

/// A robot battery with every kind of member the core handles.
pub const BATTERY: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Battery
x-bases: [urn:uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42:7]
properties:
  voltage: {type: number}
  charging: {type: boolean}
  image: {type: string, x-skip-rehydrate: true}
  gps: {anyOf: [{$ref: "#/$defs/Gps"}, {type: "null"}]}
  camera: {type: string, x-yaml-tag: "!Link", x-link: "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"}
$defs:
  Gps:
    type: object
    x-sync: fields
    properties:
      latitude: {type: number}
      fix: {$ref: "#/$defs/Fix"}
  Fix:
    type: object
    x-sync: fields
    properties:
      quality: {type: integer}
      raw: {type: string, x-skip-rehydrate: true}
x-remote-methods:
  set_limit:
    parameters: {type: object, properties: {percent: {type: integer}}}
    returns: {type: integer}
  count:
    parameters: {type: object, properties: {n: {type: integer}}}
    returns: {type: integer}
    x-generator: true
  read:
    x-key: read/{user}/{path:**}
    parameters: {type: object, properties: {user: {type: string}, path: {type: string}, start: {type: integer}}}
    returns: {type: string}
  snapshot: {}
"##;

pub fn schema_from(text: &str) -> Arc<TypeSchema> {
    let value = spirisynq_yaml::parse(text).expect("test YAML parses");
    Arc::new(TypeSchema::from_value(value).unwrap_or_else(|e| panic!("{e}")))
}

pub fn battery() -> Arc<TypeSchema> {
    schema_from(BATTERY)
}

pub fn v(text: &str) -> Value {
    spirisynq_yaml::parse(text).expect("test YAML parses")
}

pub fn yaml(text: &str) -> Sample {
    Sample::new(text.as_bytes().to_vec(), "application/yaml")
}

pub fn raw(bytes: &[u8], encoding: &str) -> Sample {
    Sample::new(bytes.to_vec(), encoding)
}

/// Every notice a session has, in order.
pub fn notices(session: &mut Session) -> Vec<Notice> {
    std::iter::from_fn(|| session.poll_notice()).collect()
}

/// Every command a session has, in order.
pub fn commands(session: &mut Session) -> Vec<Command> {
    std::iter::from_fn(|| session.poll_command()).collect()
}

pub fn incoming(object: ObjectId, path: &str, value: &str, origin: Origin) -> Notice {
    Notice::Incoming {
        object,
        path: path.into(),
        data: Data::Value(v(value)),
        origin,
    }
}

pub fn options(fields: &[(&str, FieldOptions)]) -> ObjectOptions {
    ObjectOptions {
        fields: fields.iter().map(|(p, o)| (p.to_string(), *o)).collect(),
        ..ObjectOptions::default()
    }
}

/// A network with an owned battery on session 0, at `robot/battery`.
pub fn with_owner(o: ObjectOptions) -> (Net, ObjectId) {
    let mut net = Net::new();
    net.add_session();
    let owner = net
        .session(0)
        .create_owner("robot/battery", battery(), o)
        .unwrap();
    net.run();
    (net, owner)
}

/// A battery owned on session 0, with voltage 12.5, and a mirror of it on session 1 that has
/// fetched its state. Both sessions' notices are drained.
pub fn pair() -> (Net, ObjectId, ObjectId) {
    let (mut net, owner) = with_owner(ObjectOptions::default());
    net.session(0)
        .set_field(owner, "voltage", v("12.5"))
        .unwrap();
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.clear_trace();
    (net, owner, mirror)
}

/// Run the network as a binding would: apply every incoming value (an owner publishes it on, a
/// mirror keeps it), until nothing is left to do. Every notice each session got.
pub fn settle(net: &mut Net) -> Vec<Vec<Notice>> {
    let mut all = vec![Vec::new(); net.sessions()];
    loop {
        net.run();
        let mut any = false;
        for (i, got) in all.iter_mut().enumerate() {
            for notice in notices(net.session(i)) {
                any = true;
                if let Notice::Incoming {
                    object,
                    path,
                    data: Data::Value(value),
                    origin,
                } = &notice
                {
                    let session = net.session(i);
                    match origin {
                        Origin::Write => session.set_field(*object, path, value.clone()),
                        _ => session.set_field_local(*object, path, value.clone()),
                    }
                    .unwrap();
                }
                got.push(notice);
            }
        }
        if !any {
            return all;
        }
    }
}

/// A value the YAML writer refuses: a mapping with a sequence for a key.
pub fn unwritable() -> Value {
    Value::Map(vec![(Value::Seq(vec![]), Value::Null)])
}
