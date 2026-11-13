//! What the core tells its binding: [`Notice`]s.

use spirisynq_schema::{Args, TypeId};
use spirisynq_yaml::Value;

use crate::transport::{QueryId, RequestId, Sample, Zid};

/// An open object: an authoritative object or a mirror.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ObjectId(pub(crate) u64);

/// A value that arrived.
#[derive(Debug, Clone, PartialEq)]
pub enum Data {
    /// Decoded from YAML.
    Value(Value),
    /// As it arrived, for the binding to decode: the field has its own encoding, or the caller
    /// asked for raw replies.
    Raw(Sample),
}

impl Data {
    /// As a sample: a value is written as YAML, as it went over the wire. A value YAML can't
    /// hold is [`ReplyError::Undecodable`].
    pub fn into_sample(self) -> Result<Sample, ReplyError> {
        match self {
            Data::Value(value) => {
                crate::wire::yaml_sample(&value).map_err(|e| ReplyError::Undecodable(e.to_string()))
            }
            Data::Raw(sample) => Ok(sample),
        }
    }
}

/// Where a field value came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// On a mirror: the owner published it.
    Owner,
    /// On an owner: another process wrote it.
    Write,
    /// On a mirror: the owner's full state, fetched when the mirror started or on request.
    Rehydrate,
}

/// A failed reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplyError {
    /// The method raised: the exception's type and message. An error reply that isn't in this
    /// form arrives as kind `RpcError`, with its payload as the message.
    Remote { kind: String, message: String },
    /// A reply that should have been YAML and wasn't; says why.
    Undecodable(String),
}

impl ReplyError {
    /// The exception's type and message, as the caller raises it: an undecodable reply is
    /// kind `Undecodable`.
    pub fn parts(&self) -> (&str, &str) {
        match self {
            ReplyError::Remote { kind, message } => (kind, message),
            ReplyError::Undecodable(why) => ("Undecodable", why),
        }
    }
}

/// How a request ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestEnd {
    /// The answer was complete.
    Done,
    /// An error reply ended it.
    Failed,
    /// It ended before the answer was complete: the owner went away, or the call timed out, or
    /// nobody answered.
    CutShort,
}

/// An object found by [`Session::discover`](crate::Session::discover).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Metadata {
    pub topic: String,
    /// The owner's session.
    pub owner: Zid,
    pub type_id: TypeId,
    pub bases: Vec<TypeId>,
}

/// A remote call that arrived at an owner. Answer it with
/// [`Session::reply`](crate::Session::reply) (once per value), then
/// [`Session::finish`](crate::Session::finish), or with
/// [`Session::reply_error`](crate::Session::reply_error).
#[derive(Debug, Clone, PartialEq)]
pub struct IncomingCall {
    pub query: QueryId,
    pub object: ObjectId,
    pub method: String,
    /// The key it arrived on.
    pub key: String,
    /// The key template's placeholders, decoded.
    pub key_args: Args,
    /// The other arguments, decoded from the query's payload: a mapping by name. Empty for a raw
    /// method.
    pub args: Value,
    /// The query's raw payload, for a raw method.
    pub payload: Option<Sample>,
    /// The selector parameters, untouched.
    pub parameters: String,
}

/// Something the binding needs to know. Take them with
/// [`Session::poll_notice`](crate::Session::poll_notice), in order.
#[derive(Debug, Clone, PartialEq)]
pub enum Notice {
    /// A field value arrived. The core hasn't applied it: the binding checks it, and applies it
    /// with [`Session::set_field`](crate::Session::set_field) on an owner (which publishes it to
    /// the mirrors) or [`Session::set_field_local`](crate::Session::set_field_local) on a mirror.
    Incoming {
        object: ObjectId,
        path: String,
        data: Data,
        origin: Origin,
    },
    /// A field value arrived that can't be decoded. It was dropped.
    BadData {
        object: ObjectId,
        path: String,
        reason: String,
    },
    /// A mirror's state fetch ended. Not `complete` if it was cut short or failed: some fields
    /// may not have arrived.
    Rehydrated { object: ObjectId, complete: bool },
    /// A mirror's owner went away (`false`) or came back.
    Alive { object: ObjectId, alive: bool },
    /// Whether anyone listens to an owner's field changed.
    Listeners {
        object: ObjectId,
        path: String,
        listening: bool,
    },
    /// A remote call arrived at an owner.
    Call(IncomingCall),
    /// A reply to a remote call.
    Reply {
        request: RequestId,
        result: Result<Data, ReplyError>,
    },
    /// An object answered a discovery query.
    Discovered {
        request: RequestId,
        metadata: Metadata,
    },
    /// A schema fetch's answer.
    Schema {
        request: RequestId,
        result: Result<Value, ReplyError>,
    },
    /// A remote call, discovery query or schema fetch ended.
    RequestEnded { request: RequestId, end: RequestEnd },
}
