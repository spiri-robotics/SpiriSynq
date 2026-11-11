//! How a plain (unquoted, untagged) scalar is typed: YAML 1.2's JSON schema, plus `.inf`, `-.inf`
//! and `.nan`. Anything else is a string.

use crate::{ErrorKind, Value};

pub(crate) fn resolve_plain(text: &str) -> Result<Value, ErrorKind> {
    Ok(match text {
        "" => return Err(ErrorKind::EmptyValue),
        "null" => Value::Null,
        "true" => Value::Bool(true),
        "false" => Value::Bool(false),
        ".inf" => Value::Float(f64::INFINITY),
        "-.inf" => Value::Float(f64::NEG_INFINITY),
        ".nan" => Value::Float(f64::NAN),
        _ => match number_kind(text) {
            Some(Number::Int) => match text.parse::<i64>() {
                Ok(i) => Value::Int(i),
                Err(_) => return Err(ErrorKind::NumberOutOfRange(text.to_owned())),
            },
            Some(Number::Float) => match text.parse::<f64>() {
                Ok(f) if f.is_finite() => Value::Float(f),
                _ => return Err(ErrorKind::NumberOutOfRange(text.to_owned())),
            },
            None => Value::String(text.to_owned()),
        },
    })
}

/// Whether `text`, written plain, would be read as something other than a string (or rejected).
pub(crate) fn plain_is_not_string(text: &str) -> bool {
    !matches!(resolve_plain(text), Ok(Value::String(_)))
}

enum Number {
    Int,
    Float,
}

/// Matches the JSON schema's patterns:
/// int `-?(0|[1-9][0-9]*)`, float `-?(0|[1-9][0-9]*)(\.[0-9]*)?([eE][-+]?[0-9]+)?`.
fn number_kind(text: &str) -> Option<Number> {
    let b = text.as_bytes();
    let mut i = 0;
    if b.first() == Some(&b'-') {
        i += 1;
    }
    match b.get(i) {
        Some(b'0') => i += 1,
        Some(b'1'..=b'9') => {
            while matches!(b.get(i), Some(b'0'..=b'9')) {
                i += 1;
            }
        }
        _ => return None,
    }
    if i == b.len() {
        return Some(Number::Int);
    }
    if b[i] == b'.' {
        i += 1;
        while matches!(b.get(i), Some(b'0'..=b'9')) {
            i += 1;
        }
    }
    if matches!(b.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(b.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        let digits = i;
        while matches!(b.get(i), Some(b'0'..=b'9')) {
            i += 1;
        }
        if i == digits {
            return None;
        }
    }
    (i == b.len()).then_some(Number::Float)
}

/// Tag names: `A-Z a-z 0-9 _ . : / -`, at least one character.
pub(crate) fn valid_tag_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b':' | b'/' | b'-'))
}
