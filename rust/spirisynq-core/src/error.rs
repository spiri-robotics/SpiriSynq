//! Errors from the binding-facing API.

use std::fmt;

use spirisynq_schema::KeyError;

use crate::ReplyError;

/// Why a call into the [`Session`](crate::Session) was refused. Nothing was sent.
#[derive(Debug, Clone, PartialEq)]
#[non_exhaustive]
pub enum Error {
    /// No open object has this id.
    UnknownObject,
    /// No pending incoming call has this id.
    UnknownQuery,
    /// The object's type has no field with this key.
    UnknownField(String),
    /// The object's type has no remote method with this name.
    UnknownMethod(String),
    /// Not a topic an object can have; says why.
    Topic(String),
    /// Only mirrors can do this.
    NotMirror,
    /// Only authoritative objects can do this.
    NotOwner,
    /// A lazy field must also ignore incoming writes.
    LazyNeedsIgnoreIncoming(String),
    /// The field is in a dataclass the object doesn't hold: its parent is null, or missing.
    ParentMissing(String),
    /// The call's key couldn't be built.
    Key(KeyError),
    /// A placeholder argument that isn't a string.
    NotAString(String),
    /// The value can't be written as YAML.
    Encode(spirisynq_yaml::Error),
    /// The object's type has no WASM code with this name.
    UnknownCode(String),
    /// This WASM code is a hook, run by the engine when its moment comes, not a property or
    /// method to run.
    NotRunnable(String),
    /// WASM code failed, or what it gave back can't be read; says why.
    Wasm(String),
    /// The code isn't in the schema, and hasn't been fetched by its hash yet: a mirror fetches
    /// it from the owner when it opens, and again when an owner appears.
    CodeNotFetched(String),
    /// An `after_call` let one of the call's error replies through: the caller raises it as it
    /// is.
    Reply(ReplyError),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnknownObject => f.write_str("no such object"),
            Error::UnknownQuery => f.write_str("no such incoming call"),
            Error::UnknownField(path) => write!(f, "no field `{path}`"),
            Error::UnknownMethod(name) => write!(f, "no remote method `{name}`"),
            Error::Topic(why) => write!(f, "bad topic: {why}"),
            Error::NotMirror => f.write_str("only a mirror can do this"),
            Error::NotOwner => f.write_str("only an authoritative object can do this"),
            Error::LazyNeedsIgnoreIncoming(path) => {
                write!(f, "lazy field `{path}` must also ignore incoming writes")
            }
            Error::ParentMissing(path) => write!(f, "`{path}` isn't there to hold the field"),
            Error::Key(e) => e.fmt(f),
            Error::NotAString(name) => write!(f, "`{name}` is in the key, so it must be a string"),
            Error::Encode(e) => e.fmt(f),
            Error::UnknownCode(name) => write!(f, "no WASM code `{name}`"),
            Error::NotRunnable(name) => write!(f, "`{name}` is a hook, not a property or method"),
            Error::Wasm(why) => write!(f, "WASM: {why}"),
            Error::CodeNotFetched(name) => {
                write!(
                    f,
                    "the code of `{name}` hasn't been fetched from the owner yet"
                )
            }
            Error::Reply(error) => {
                let (kind, message) = error.parts();
                write!(f, "{kind}: {message}")
            }
        }
    }
}

impl std::error::Error for Error {}

impl From<KeyError> for Error {
    fn from(e: KeyError) -> Self {
        Error::Key(e)
    }
}

impl From<spirisynq_yaml::Error> for Error {
    fn from(e: spirisynq_yaml::Error) -> Self {
        Error::Encode(e)
    }
}
