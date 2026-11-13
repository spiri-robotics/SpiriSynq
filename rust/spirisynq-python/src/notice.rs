//! The engine's notices, as Python classes: one class per kind, with the same fields.
//!
//! A value that arrived is a Python value, or a [`Sample`] if it's in its own encoding. An origin
//! is `"owner"`, `"write"` or `"rehydrate"`; how a request ended is `"done"`, `"failed"` or
//! `"cut_short"`. A failed reply's `error` is `(kind, message)`: the exception's type and
//! message, or `"Undecodable"` and why.

use pyo3::prelude::*;
use spirisynq_core::{self as core, Data, Notice, Origin, ReplyError, RequestEnd};

use crate::{ObjectId, RequestId, Sample};

/// A field value arrived. Not applied yet: see `Node.set_field` and `Node.set_field_local`.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Incoming {
    object: ObjectId,
    path: String,
    data: Py<PyAny>,
    origin: &'static str,
}

/// A field value arrived that can't be decoded, and was dropped.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct BadData {
    object: ObjectId,
    path: String,
    reason: String,
}

/// A mirror's state fetch ended; not `complete` if some fields may not have arrived.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Rehydrated {
    object: ObjectId,
    complete: bool,
}

/// A mirror's owner went away or came back.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Alive {
    object: ObjectId,
    alive: bool,
}

/// Whether anyone listens to an owner's field changed.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Listeners {
    object: ObjectId,
    path: String,
    listening: bool,
}

/// A remote call arrived at an owner. Answer it with `Node.reply` and `Node.finish`, or
/// `Node.reply_error`.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Call {
    query: u64,
    object: ObjectId,
    method: String,
    /// The owner's own key for the call.
    key: String,
    /// The key's placeholders: a dict of strings.
    key_args: Py<PyAny>,
    /// The other arguments: a dict by name. Empty for a raw method.
    args: Py<PyAny>,
    /// The query's payload, for a raw method.
    payload: Option<Sample>,
    parameters: String,
}

/// A reply to a remote call: a `value`, or an `error`.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Reply {
    request: RequestId,
    value: Py<PyAny>,
    error: Option<(String, String)>,
}

/// An object answered a discovery query.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct Discovered {
    request: RequestId,
    topic: String,
    /// The owner's session id.
    owner: String,
    type_id: String,
    bases: Vec<String>,
}

/// A schema fetch's answer: the schema as a value, or an `error`.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct SchemaFetched {
    request: RequestId,
    value: Py<PyAny>,
    error: Option<(String, String)>,
}

/// A remote call, discovery query or schema fetch ended.
#[pyclass(frozen, get_all, module = "SpiriSynq._core")]
pub struct RequestEnded {
    request: RequestId,
    end: &'static str,
}

fn data(py: Python<'_>, data: Data) -> PyResult<Py<PyAny>> {
    match data {
        Data::Value(value) => Ok(crate::to_py(py, &value)?.unbind()),
        Data::Raw(sample) => Ok(Bound::new(py, Sample(sample))?.into_any().unbind()),
    }
}

fn reply_error(error: ReplyError) -> (String, String) {
    let (kind, message) = error.parts();
    (kind.to_owned(), message.to_owned())
}

/// A failed reply's exception type and message.
type Failure = Option<(String, String)>;

/// A value or an error, as `(value, error)`.
fn result<T>(
    py: Python<'_>,
    result: Result<T, ReplyError>,
    value: impl FnOnce(T) -> PyResult<Py<PyAny>>,
) -> PyResult<(Py<PyAny>, Failure)> {
    match result {
        Ok(v) => Ok((value(v)?, None)),
        Err(e) => Ok((py.None(), Some(reply_error(e)))),
    }
}

/// The Python object for a notice.
pub fn to_py(py: Python<'_>, notice: Notice) -> PyResult<Py<PyAny>> {
    let object = |id: core::ObjectId| ObjectId(id);
    let made = match notice {
        Notice::Incoming {
            object: id,
            path,
            data: d,
            origin,
        } => {
            let origin = match origin {
                Origin::Owner => "owner",
                Origin::Write => "write",
                Origin::Rehydrate => "rehydrate",
            };
            let data = data(py, d)?;
            let incoming = Incoming {
                object: object(id),
                path,
                data,
                origin,
            };
            Bound::new(py, incoming)?.into_any()
        }
        Notice::BadData {
            object: id,
            path,
            reason,
        } => {
            let bad = BadData {
                object: object(id),
                path,
                reason,
            };
            Bound::new(py, bad)?.into_any()
        }
        Notice::Rehydrated {
            object: id,
            complete,
        } => {
            let object = object(id);
            Bound::new(py, Rehydrated { object, complete })?.into_any()
        }
        Notice::Alive { object: id, alive } => {
            let object = object(id);
            Bound::new(py, Alive { object, alive })?.into_any()
        }
        Notice::Listeners {
            object: id,
            path,
            listening,
        } => {
            let listeners = Listeners {
                object: object(id),
                path,
                listening,
            };
            Bound::new(py, listeners)?.into_any()
        }
        Notice::Call(call) => {
            let key_args = pyo3::types::PyDict::new(py);
            for (k, v) in &call.key_args {
                key_args.set_item(k, v)?;
            }
            let call = Call {
                query: call.query.0,
                object: object(call.object),
                method: call.method,
                key: call.key,
                key_args: key_args.into_any().unbind(),
                args: crate::to_py(py, &call.args)?.unbind(),
                payload: call.payload.map(Sample),
                parameters: call.parameters,
            };
            Bound::new(py, call)?.into_any()
        }
        Notice::Reply { request, result: r } => {
            let (value, error) = result(py, r, |d| data(py, d))?;
            let reply = Reply {
                request: RequestId(request),
                value,
                error,
            };
            Bound::new(py, reply)?.into_any()
        }
        Notice::Discovered { request, metadata } => {
            let bases = metadata.bases.iter().map(|b| b.as_str().to_owned());
            let discovered = Discovered {
                request: RequestId(request),
                topic: metadata.topic,
                owner: metadata.owner.0,
                type_id: metadata.type_id.as_str().to_owned(),
                bases: bases.collect(),
            };
            Bound::new(py, discovered)?.into_any()
        }
        Notice::Schema { request, result: r } => {
            let (value, error) = result(py, r, |v| Ok(crate::to_py(py, &v)?.unbind()))?;
            let schema = SchemaFetched {
                request: RequestId(request),
                value,
                error,
            };
            Bound::new(py, schema)?.into_any()
        }
        Notice::RequestEnded { request, end } => {
            let end = match end {
                RequestEnd::Done => "done",
                RequestEnd::Failed => "failed",
                RequestEnd::CutShort => "cut_short",
            };
            let request = RequestId(request);
            Bound::new(py, RequestEnded { request, end })?.into_any()
        }
    };
    Ok(made.unbind())
}
