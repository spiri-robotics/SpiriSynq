//! The v2 wire protocol: keys, encodings, and the payloads the core writes and reads itself.

use spirisynq_schema::TypeId;
use spirisynq_yaml::Value;

use crate::notice::{Metadata, ReplyError};
use crate::transport::{Payload, Sample, Zid};

/// The encoding of every payload the core writes, and of every payload it decodes.
pub const YAML: &str = "application/yaml";

/// The encoding of the reply that ends a successful answer, with an empty payload. A stream that
/// ends without it was cut short.
pub const DONE: &str = "x-spirisynq/done";

/// The query an owner answers with its full state, one reply per field.
pub const REHYDRATE: &str = "sr_rehydrate";
/// The query an owner answers with its [`Metadata`]; also mounted under `sr_metadata/<type>`.
pub const METADATA: &str = "sr_metadata";
/// The query an owner answers with its type's schema.
pub const OBJECT_SCHEMA: &str = "sr_object_schema";
/// Mounts the schema under `sr_type_schema/<type>`.
pub const TYPE_SCHEMA: &str = "sr_type_schema";
/// Liveliness tokens: `<topic>/sr_alive/<zid of the owner's session>`.
pub const ALIVE: &str = "sr_alive";

pub(crate) fn key(topic: &str, rest: &str) -> String {
    format!("{topic}/{rest}")
}

/// The type mounts of `sr_metadata` or `sr_type_schema`: one per type the object is.
pub(crate) fn type_keys<'a>(
    topic: &'a str,
    base: &'a str,
    types: impl Iterator<Item = &'a TypeId> + 'a,
) -> impl Iterator<Item = String> + 'a {
    types.map(move |t| format!("{topic}/{base}/{}", t.key_chunk()))
}

pub(crate) fn yaml_sample(value: &Value) -> Result<Sample, spirisynq_yaml::Error> {
    let text = spirisynq_yaml::to_string(value)?;
    Ok(Sample::new(Payload::from(text.into_bytes()), YAML))
}

pub(crate) fn done_sample() -> Sample {
    Sample::new(Payload::from(Vec::new()), DONE)
}

/// A sample's value, if it is YAML.
pub(crate) fn decode(sample: &Sample) -> Result<Value, String> {
    if sample.encoding != YAML {
        return Err(format!("encoding `{}` isn't YAML", sample.encoding));
    }
    spirisynq_yaml::parse_bytes(&sample.payload.bytes()).map_err(|e| e.to_string())
}

pub(crate) fn map_get<'a>(map: &'a [(Value, Value)], name: &str) -> Option<&'a Value> {
    map.iter()
        .find_map(|(k, v)| matches!(k, Value::String(s) if s == name).then_some(v))
}

fn string(map: &[(Value, Value)], name: &str) -> Option<String> {
    match map_get(map, name) {
        Some(Value::String(s)) => Some(s.clone()),
        _ => None,
    }
}

/// An error reply's payload: `{type: <exception type>, message: <text>}`.
pub(crate) fn error_sample(kind: &str, message: &str) -> Sample {
    let value = Value::Map(vec![
        ("type".into(), kind.into()),
        ("message".into(), message.into()),
    ]);
    yaml_sample(&value).expect("strings always encode")
}

/// Read an error reply. One that isn't in the usual form is passed on as its text, as an
/// `RpcError`.
pub(crate) fn read_error(sample: &Sample) -> ReplyError {
    if let Ok(Value::Map(map)) = decode(sample)
        && let (Some(kind), Some(message)) = (string(&map, "type"), string(&map, "message"))
    {
        return ReplyError::Remote { kind, message };
    }
    ReplyError::Remote {
        kind: "RpcError".into(),
        message: String::from_utf8_lossy(&sample.payload.bytes()).into_owned(),
    }
}

/// `sr_metadata`'s reply.
pub(crate) fn metadata_value(topic: &str, owner: &Zid, id: &TypeId, bases: &[TypeId]) -> Value {
    Value::Map(vec![
        ("topic".into(), topic.into()),
        ("owner".into(), owner.0.as_str().into()),
        ("type".into(), id.as_str().into()),
        (
            "bases".into(),
            Value::Seq(bases.iter().map(|b| b.as_str().into()).collect()),
        ),
    ])
}

pub(crate) fn read_metadata(value: &Value) -> Option<Metadata> {
    let Value::Map(map) = value else { return None };
    let type_id = string(map, "type")?.parse().ok()?;
    let Some(Value::Seq(bases)) = map_get(map, "bases") else { return None };
    let bases = bases
        .iter()
        .map(|b| match b {
            Value::String(s) => s.parse().ok(),
            _ => None,
        })
        .collect::<Option<Vec<TypeId>>>()?;
    Some(Metadata {
        topic: string(map, "topic")?,
        owner: Zid(string(map, "owner")?),
        type_id,
        bases,
    })
}
