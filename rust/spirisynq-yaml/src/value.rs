//! The value tree every binding converts to and from.

/// One parsed YAML value.
///
/// Equality means "would be written as the same text": floats compare by their bits, except that
/// every NaN equals every other NaN. So `0.0 != -0.0`, and setting a field to NaN twice is not a
/// change.
#[derive(Debug, Clone)]
pub enum Value {
    /// `null`
    Null,
    /// `true` or `false`
    Bool(bool),
    /// An integer that fits in an `i64`.
    Int(i64),
    /// A float, including `.inf`, `-.inf` and `.nan`.
    Float(f64),
    /// A string. Every scalar that isn't one of the other kinds is a string.
    String(String),
    /// Bytes, written as `!!binary` (base64).
    Binary(Vec<u8>),
    /// A sequence.
    Seq(Vec<Value>),
    /// A mapping, in the order its keys were written. Keys are scalars and are unique.
    Map(Vec<(Value, Value)>),
    /// A value with a local tag such as `!Link`.
    Tagged(Box<Tagged>),
}

/// A value with a local tag: `!Link robot/camera` is `Tagged { tag: "Link", value: "robot/camera" }`.
///
/// A tagged scalar is always a [`Value::String`], as YAML says: the tag, not the text, decides what
/// it means. A tagged collection is a [`Value::Seq`] or [`Value::Map`].
#[derive(Debug, Clone, PartialEq)]
pub struct Tagged {
    /// The tag without its leading `!`.
    pub tag: String,
    /// The tagged value.
    pub value: Value,
}

impl Value {
    /// A tagged value. `tag` is given without its leading `!`.
    pub fn tagged(tag: impl Into<String>, value: Value) -> Self {
        Value::Tagged(Box::new(Tagged {
            tag: tag.into(),
            value,
        }))
    }

    /// Whether this is a scalar (anything but a sequence, a mapping or a tagged collection).
    pub fn is_scalar(&self) -> bool {
        match self {
            Value::Seq(_) | Value::Map(_) => false,
            Value::Tagged(t) => t.value.is_scalar(),
            _ => true,
        }
    }
}

impl PartialEq for Value {
    fn eq(&self, other: &Self) -> bool {
        use Value::*;
        match (self, other) {
            (Null, Null) => true,
            (Bool(a), Bool(b)) => a == b,
            (Int(a), Int(b)) => a == b,
            (Float(a), Float(b)) => same_float(*a, *b),
            (String(a), String(b)) => a == b,
            (Binary(a), Binary(b)) => a == b,
            (Seq(a), Seq(b)) => a == b,
            (Map(a), Map(b)) => a == b,
            (Tagged(a), Tagged(b)) => a == b,
            _ => false,
        }
    }
}

pub(crate) fn same_float(a: f64, b: f64) -> bool {
    (a.is_nan() && b.is_nan()) || a.to_bits() == b.to_bits()
}

impl From<bool> for Value {
    fn from(v: bool) -> Self {
        Value::Bool(v)
    }
}

impl From<i64> for Value {
    fn from(v: i64) -> Self {
        Value::Int(v)
    }
}

impl From<f64> for Value {
    fn from(v: f64) -> Self {
        Value::Float(v)
    }
}

impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Value::String(v.to_owned())
    }
}

impl From<String> for Value {
    fn from(v: String) -> Self {
        Value::String(v)
    }
}
