#![allow(dead_code)]

use spirisynq_schema::{Error, TypeSchema};

/// The three keys every schema needs; tests append their own.
pub const HEADER: &str = "\
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Robot
";

/// A schema from YAML text, as written.
pub fn raw(text: &str) -> Result<TypeSchema, Error> {
    TypeSchema::from_value(spirisynq_yaml::parse(text).expect("test YAML parses"))
}

/// A schema from `HEADER` plus `body`.
pub fn schema(body: &str) -> Result<TypeSchema, Error> {
    raw(&format!("{HEADER}{body}"))
}

/// A schema that must load.
pub fn ok(body: &str) -> TypeSchema {
    schema(body).unwrap_or_else(|e| panic!("{e}"))
}

/// A schema that must be rejected.
pub fn reject(body: &str) -> Error {
    match schema(body) {
        Ok(s) => panic!("accepted: {s:?}"),
        Err(e) => e,
    }
}
