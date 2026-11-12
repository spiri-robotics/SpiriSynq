//! What passes between the core and a transport: [`Command`]s out, [`Event`]s in.

use std::any::Any;
use std::borrow::Cow;
use std::fmt;
use std::sync::Arc;

/// Bytes in whatever container holds them: a `Vec<u8>` the core wrote, or a transport's own
/// buffer, such as a zenoh `ZBytes` backed by shared memory.
pub trait Bytes: Any + fmt::Debug + Send + Sync {
    /// The bytes, borrowed when they are contiguous.
    fn bytes(&self) -> Cow<'_, [u8]>;
}

impl Bytes for Vec<u8> {
    fn bytes(&self) -> Cow<'_, [u8]> {
        Cow::Borrowed(self)
    }
}

/// A payload, shared and never copied by the core.
///
/// A field with its own encoding, a raw remote method and a raw reply pass the transport's
/// payload through untouched, so a zenoh transport can hand the core a shared-memory buffer and
/// get the same buffer back with [`Payload::downcast_ref`]. The core only reads the payloads it
/// decodes as YAML. Payloads are equal when their bytes are.
#[derive(Clone)]
pub struct Payload(Arc<dyn Bytes>);

impl Payload {
    pub fn new(bytes: impl Bytes) -> Self {
        Payload(Arc::new(bytes))
    }

    pub fn bytes(&self) -> Cow<'_, [u8]> {
        self.0.bytes()
    }

    /// The container, if it's a `T`.
    pub fn downcast_ref<T: Bytes>(&self) -> Option<&T> {
        let any: &dyn Any = &*self.0;
        any.downcast_ref()
    }
}

impl From<Vec<u8>> for Payload {
    fn from(bytes: Vec<u8>) -> Self {
        Payload::new(bytes)
    }
}

impl PartialEq for Payload {
    fn eq(&self, other: &Self) -> bool {
        self.bytes() == other.bytes()
    }
}

impl fmt::Debug for Payload {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

/// A payload with its zenoh encoding and optional attachment: what a put, a query or a reply
/// carries.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub payload: Payload,
    pub encoding: String,
    pub attachment: Option<Payload>,
}

impl Sample {
    /// A sample with no attachment.
    pub fn new(payload: impl Into<Payload>, encoding: impl Into<String>) -> Self {
        Sample {
            payload: payload.into(),
            encoding: encoding.into(),
            attachment: None,
        }
    }
}

/// A zenoh session id, as the transport writes it.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Zid(pub String);

impl fmt::Display for Zid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// A publisher, subscriber, queryable or liveliness token the core declared. The core picks the
/// ids; the transport maps them to its own entities.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EntityId(pub(crate) u64);

/// A get the core sent: a remote call, a state fetch, a discovery query or a schema fetch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RequestId(pub(crate) u64);

/// A query that arrived at one of the core's queryables. The transport picks the ids.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct QueryId(pub u64);

/// Who sent a sample or a reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Source {
    /// This session: the publisher, or the queryable that replied.
    Local(EntityId),
    /// Another session.
    Remote(Zid),
    /// The sample carried no source information.
    Unknown,
}

/// A field's priority, highest first. The presets of `qos.*` in Python.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum Priority {
    RealTime,
    InteractiveHigh,
    InteractiveLow,
    DataHigh,
    #[default]
    Data,
    DataLow,
    Background,
}

/// How a field's values are sent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct Qos {
    pub priority: Priority,
    /// Resend a value lost in transit. Off by default: most fields are soon replaced by a newer
    /// value, and resending a stale one would only get in the way.
    pub reliable: bool,
}

/// Something the core asks the transport to do. Take them with
/// [`Session::poll_command`](crate::Session::poll_command), in order.
#[derive(Debug, Clone, PartialEq)]
pub enum Command {
    /// Declare a publisher on an exact key. Report its matching status as [`Event::Matching`]
    /// whenever it changes; it starts unmatched.
    DeclarePublisher { id: EntityId, key: String, qos: Qos },
    /// Subscribe to an exact key, reporting [`Event::Sample`]s.
    DeclareSubscriber { id: EntityId, key: String },
    /// Declare a queryable, reporting [`Event::Query`]s. `key` may hold `*` and `**`.
    DeclareQueryable { id: EntityId, key: String },
    /// Declare a liveliness token.
    DeclareToken { id: EntityId, key: String },
    /// Subscribe to liveliness tokens, `key` holding `*`, including the tokens already alive.
    DeclareLivelinessSubscriber { id: EntityId, key: String },
    /// Undeclare any of the above.
    Undeclare { id: EntityId },
    /// Put a sample on a publisher's key.
    Put { publisher: EntityId, sample: Sample },
    /// Send a get. Consolidation is always off: every reply counts. Report replies as
    /// [`Event::Reply`], then [`Event::RequestDone`] once no more can come, timeouts included.
    Get {
        id: RequestId,
        key: String,
        parameters: String,
        payload: Option<Sample>,
        /// Accept replies on keys that don't intersect `key`.
        any_reply_keys: bool,
        timeout_ms: Option<u64>,
    },
    /// Reply to a query.
    Reply {
        query: QueryId,
        key: String,
        sample: Sample,
    },
    /// Send an error reply to a query.
    ReplyErr { query: QueryId, sample: Sample },
    /// The core is done with a query: drop it, so its getter learns that no more replies come.
    Finish { query: QueryId },
}

/// Something that happened on the network. Hand them to
/// [`Session::handle`](crate::Session::handle).
#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Sample {
        subscriber: EntityId,
        key: String,
        sample: Sample,
        source: Source,
    },
    Query {
        queryable: EntityId,
        query: QueryId,
        key: String,
        parameters: String,
        payload: Option<Sample>,
    },
    Reply {
        request: RequestId,
        key: String,
        /// `Err` for an error reply.
        result: Result<Sample, Sample>,
        replier: Source,
    },
    /// No more replies will come for this get.
    RequestDone {
        request: RequestId,
    },
    Liveliness {
        subscriber: EntityId,
        key: String,
        alive: bool,
    },
    Matching {
        publisher: EntityId,
        matched: bool,
    },
}
