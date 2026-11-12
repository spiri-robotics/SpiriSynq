//! Reading keywords out of a schema's value tree, with errors that say where.

use spirisynq_yaml::Value;

use crate::{Error, ErrorKind, TypeId};

pub(crate) type Map = [(Value, Value)];

/// `base` extended by one JSON Pointer reference token.
pub(crate) fn child(base: &str, token: &str) -> String {
    format!("{base}/{}", token.replace('~', "~0").replace('/', "~1"))
}

pub(crate) fn map<'a>(value: &'a Value, at: &str) -> Result<&'a Map, Error> {
    match value {
        Value::Map(m) => Ok(m),
        _ => Err(Error::new(at, ErrorKind::Expected("a mapping"))),
    }
}

/// The value under a string key, if there is one.
pub(crate) fn get<'a>(map: &'a Map, key: &str) -> Option<&'a Value> {
    map.iter()
        .find_map(|(k, v)| matches!(k, Value::String(s) if s == key).then_some(v))
}

/// A mapping's entries, which must all have string keys.
pub(crate) fn entries<'a>(value: &'a Value, at: &str) -> Result<Vec<(&'a str, &'a Value)>, Error> {
    map(value, at)?
        .iter()
        .map(|(k, v)| match k {
            Value::String(s) => Ok((s.as_str(), v)),
            _ => Err(Error::new(at, ErrorKind::Expected("string keys"))),
        })
        .collect()
}

pub(crate) fn list<'a>(value: &'a Value, at: &str) -> Result<&'a [Value], Error> {
    match value {
        Value::Seq(items) => Ok(items),
        _ => Err(Error::new(at, ErrorKind::Expected("a list"))),
    }
}

pub(crate) fn string<'a>(value: &'a Value, at: &str) -> Result<&'a str, Error> {
    match value {
        Value::String(s) => Ok(s),
        _ => Err(Error::new(at, ErrorKind::Expected("a string"))),
    }
}

pub(crate) fn boolean(value: &Value, at: &str) -> Result<bool, Error> {
    match value {
        Value::Bool(b) => Ok(*b),
        _ => Err(Error::new(at, ErrorKind::Expected("true or false"))),
    }
}

pub(crate) fn type_id(value: &Value, at: &str) -> Result<TypeId, Error> {
    string(value, at)?.parse().map_err(|e: Error| e.at(at))
}

/// The value of an optional boolean keyword, or `default`.
pub(crate) fn flag(map: &Map, key: &str, at: &str, default: bool) -> Result<bool, Error> {
    get(map, key).map_or(Ok(default), |v| boolean(v, &child(at, key)))
}
