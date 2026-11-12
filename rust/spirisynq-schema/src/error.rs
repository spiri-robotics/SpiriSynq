use std::fmt;

/// Why a schema was rejected.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ErrorKind {
    /// `$schema` is missing or isn't [`crate::DIALECT`].
    Dialect,
    /// `x-spirisynq-schema` is missing or isn't [`crate::FORMAT_VERSION`].
    FormatVersion,
    /// A required keyword is missing.
    Missing,
    /// A keyword holds the wrong kind of value; says what was expected.
    Expected(&'static str),
    /// A string that should be a type id isn't one. Holds the string.
    TypeId(String),
    /// A field or remote method name that can't be a key chunk. Holds the reason.
    Name(String),
    /// A `#/$defs/...` reference to a definition that doesn't exist. Holds the reference.
    Ref(String),
    /// A dataclass synced field by field contains itself. Holds the definition's name.
    Cycle(String),
    /// A field that could hold more than one dataclass synced field by field.
    Union,
    /// A key template that can't be parsed. Holds the reason.
    Template(String),
    /// A key template placeholder that isn't a string parameter of the method. Holds its name.
    Placeholder(String),
    /// A member key in the space SpiriSynq reserves: its first chunk starts with `sr_` or is a
    /// placeholder. Holds the member.
    Reserved(String),
    /// Two members answer overlapping keys. Holds both members.
    Overlap(String, String),
    /// A WASM entry's `of` is wrong for its role. Holds the reason.
    Hook(String),
    /// A WASM entry whose `source` is the wrong kind for its loader: text loaders take a string,
    /// `wasm` takes bytes (`!!binary`).
    Source,
    /// A WASM `hash` that isn't `sha256:` followed by 64 lowercase hex digits.
    Hash,
    /// A WASM entry whose `source` doesn't match its `hash`.
    HashMismatch,
    /// A WASM property or method with the same name as a field or remote method. Holds the name.
    NameTaken(String),
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        use ErrorKind::*;
        match self {
            Dialect => write!(f, "`$schema` must be {}", crate::DIALECT),
            FormatVersion => write!(f, "`x-spirisynq-schema` must be {}", crate::FORMAT_VERSION),
            Missing => write!(f, "required, but missing"),
            Expected(what) => write!(f, "expected {what}"),
            TypeId(text) => write!(f, "`{text}` isn't a type id"),
            Name(reason) => write!(f, "bad name: {reason}"),
            Ref(target) => write!(f, "no definition for `{target}`"),
            Cycle(name) => write!(f, "`{name}` is synced field by field and contains itself"),
            Union => write!(
                f,
                "a field can hold at most one dataclass synced field by field"
            ),
            Template(reason) => write!(f, "bad key template: {reason}"),
            Placeholder(name) => write!(
                f,
                "placeholder `{name}` must be a string parameter of the method"
            ),
            Reserved(member) => write!(
                f,
                "{member} claims a reserved key (first chunk starting with `sr_`, or a placeholder)"
            ),
            Overlap(a, b) => write!(f, "{a} and {b} answer overlapping keys"),
            Hook(reason) => write!(f, "bad `of`: {reason}"),
            Source => write!(
                f,
                "`source` must be a string for text loaders and bytes for `wasm`"
            ),
            Hash => write!(f, "`hash` must be `sha256:` and 64 lowercase hex digits"),
            HashMismatch => write!(f, "`source` doesn't match `hash`"),
            NameTaken(name) => write!(f, "`{name}` is already a field or remote method"),
        }
    }
}

/// A rejected schema, with a JSON Pointer to where in it the problem is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    /// Where in the schema, as a JSON Pointer (`/properties/gps`); empty for the whole schema
    /// or for a lone type id.
    pub pointer: String,
    /// What went wrong.
    pub kind: ErrorKind,
}

impl Error {
    pub(crate) fn new(pointer: impl Into<String>, kind: ErrorKind) -> Self {
        Error {
            pointer: pointer.into(),
            kind,
        }
    }

    pub(crate) fn at(mut self, pointer: &str) -> Self {
        self.pointer = pointer.to_owned();
        self
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.pointer.is_empty() {
            write!(f, "{}", self.kind)
        } else {
            write!(f, "{}: {}", self.pointer, self.kind)
        }
    }
}

impl std::error::Error for Error {}

/// Why a key couldn't be built from a template, or an incoming key couldn't be decoded.
///
/// When building a key, this is the caller's mistake (Python raises `ValueError`). When decoding
/// one, it's bad incoming data.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum KeyError {
    /// No value was given for a placeholder. Holds its name.
    MissingArgument(String),
    /// A placeholder's value would make an empty chunk: empty for `{name}`; a leading, trailing
    /// or doubled `/` for `{name:**}`. Holds its name.
    EmptyChunk(String),
    /// An incoming chunk has a `%` escape the encoder never writes, or a raw `*`, `$`, `?` or
    /// `#`. Holds the placeholder's name.
    BadEscape(String),
}

impl fmt::Display for KeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeyError::MissingArgument(name) => write!(f, "no value for `{name}`"),
            KeyError::EmptyChunk(name) => write!(f, "the value for `{name}` makes an empty chunk"),
            KeyError::BadEscape(name) => write!(f, "the value for `{name}` is badly escaped"),
        }
    }
}

impl std::error::Error for KeyError {}
