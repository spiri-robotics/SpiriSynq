//! Carrying out the engine's commands on zenoh, and turning what zenoh delivers into events.

use std::borrow::Cow;
use std::collections::HashMap;
use std::time::Duration;

use spirisynq_core::{
    Bytes, Command, EntityId, Event, Notice, Payload, Qos, QueryId, RequestId, Sample, Source, Zid,
};
use zenoh::Wait;
use zenoh::bytes::ZBytes;
use zenoh::handlers::CallbackDrop;
use zenoh::matching::MatchingListener;
use zenoh::pubsub::Publisher;
use zenoh::qos::{CongestionControl, Priority, Reliability};
use zenoh::query::{ConsolidationMode, Query, QueryTarget, Reply, ReplyKeyExpr};
use zenoh::sample::{SampleKind, SourceInfo};
use zenoh::session::{EntityGlobalId, ZenohId};

/// A payload in zenoh's own container, shared memory included. Payloads that arrive are these;
/// a payload that is one goes back out as the same buffer, without a copy.
#[derive(Debug, Clone)]
pub struct ZenohBytes(pub ZBytes);

impl Bytes for ZenohBytes {
    fn bytes(&self) -> Cow<'_, [u8]> {
        self.0.to_bytes()
    }
}

impl From<ZenohBytes> for Payload {
    fn from(bytes: ZenohBytes) -> Self {
        Payload::new(bytes)
    }
}

fn wrap(bytes: &ZBytes) -> Payload {
    ZenohBytes(bytes.clone()).into()
}

/// The payload as zenoh bytes: the same buffer if it came from zenoh, else a copy.
fn unwrap(payload: &Payload) -> ZBytes {
    match payload.downcast_ref::<ZenohBytes>() {
        Some(bytes) => bytes.0.clone(),
        None => ZBytes::from(payload.bytes().into_owned()),
    }
}

fn sample(s: &zenoh::sample::Sample) -> Sample {
    Sample {
        payload: wrap(s.payload()),
        encoding: s.encoding().to_string(),
        attachment: s.attachment().map(wrap),
    }
}

fn query_payload(q: &Query) -> Option<Sample> {
    Some(Sample {
        payload: wrap(q.payload()?),
        encoding: q.encoding().map(ToString::to_string).unwrap_or_default(),
        attachment: q.attachment().map(wrap),
    })
}

/// The engine's priorities, in its order.
const PRIORITIES: [Priority; 7] = [
    Priority::RealTime,
    Priority::InteractiveHigh,
    Priority::InteractiveLow,
    Priority::DataHigh,
    Priority::Data,
    Priority::DataLow,
    Priority::Background,
];

/// Log an operation that failed. Nothing the engine asked for is fatal: the engine carries on,
/// and a failed get still ends (its handler is dropped).
pub(crate) fn report(what: &str, result: zenoh::Result<()>) {
    if let Err(e) = result {
        log::warn!("{what}: {e}");
    }
}

fn send(inbox: &flume::Sender<Input>, input: Input) {
    // Fails only once the node has stopped, when nothing more is wanted.
    let _ = inbox.send(input);
}

/// What to do with an input.
pub(crate) enum Step {
    Handle(Event),
    /// Nothing for the engine.
    Skip,
    /// The node is stopping.
    Stop,
}

/// Whether a reply is zenoh's own word that the get timed out, rather than a queryable's. The
/// getter's session sends it with no replier; a routing node that gave up on a query it forwarded
/// sends it as entity 0, which no queryable is. The get ends right after it either way.
fn is_timeout(reply: &Reply) -> bool {
    let from_zenoh = reply.replier_id().is_none_or(|id| id.eid() == 0);
    from_zenoh
        && reply
            .result()
            .is_err_and(|e| *e.payload().to_bytes() == *b"Timeout")
}

/// What zenoh delivered, queued by its callbacks for the driver.
pub(crate) enum Input {
    Sample(EntityId, zenoh::sample::Sample),
    Liveliness(EntityId, zenoh::sample::Sample),
    Query(EntityId, Query),
    Reply(RequestId, Reply),
    Done(RequestId),
    Matching(EntityId, bool),
    Stop,
}

struct Pub {
    /// Kept for its callback. `None` if it couldn't be declared, which only happens when the
    /// session closes as the publisher is declared. Before the publisher, so it is dropped first.
    _matching: Option<MatchingListener<()>>,
    publisher: Publisher<'static>,
    /// The sequence number of the last put, for its `SourceInfo`.
    sn: u32,
}

pub(crate) struct Net {
    zenoh: zenoh::Session,
    zid: ZenohId,
    inbox: flume::Sender<Input>,
    notify: flume::Sender<Notice>,
    publishers: HashMap<EntityId, Pub>,
    /// Subscribers, queryables and tokens: kept so they stay declared, dropped to undeclare.
    held: HashMap<EntityId, Box<dyn Send>>,
    /// Local publishers and queryables by zenoh entity id, to recognise our own samples and
    /// replies.
    local: HashMap<u32, EntityId>,
    queries: HashMap<QueryId, Query>,
    next_query: u64,
}

impl Net {
    pub(crate) fn new(
        zenoh: zenoh::Session,
        inbox: flume::Sender<Input>,
        notify: flume::Sender<Notice>,
    ) -> Self {
        Net {
            zid: zenoh.zid(),
            zenoh,
            inbox,
            notify,
            publishers: HashMap::new(),
            held: HashMap::new(),
            local: HashMap::new(),
            queries: HashMap::new(),
            next_query: 0,
        }
    }

    pub(crate) fn notify(&self, notice: Notice) {
        // Fails only once the node is dropped.
        let _ = self.notify.send(notice);
    }

    /// Drop everything declared, and every open query, so its getter learns no more replies come.
    pub(crate) fn clear(&mut self) {
        self.publishers.clear();
        self.held.clear();
        self.queries.clear();
    }

    fn hold(&mut self, id: EntityId, what: &str, declared: zenoh::Result<Box<dyn Send>>) {
        match declared {
            Ok(entity) => {
                self.held.insert(id, entity);
            }
            Err(e) => report(what, Err(e)),
        }
    }

    pub(crate) fn execute(&mut self, command: Command) {
        let inbox = self.inbox.clone();
        match command {
            Command::DeclarePublisher { id, key, qos } => self.declare_publisher(id, key, qos),
            Command::DeclareSubscriber { id, key } => {
                let declared = self
                    .zenoh
                    .declare_subscriber(key)
                    .callback(move |s| send(&inbox, Input::Sample(id, s)))
                    .wait();
                self.hold(
                    id,
                    "declaring a subscriber",
                    declared.map(|s| Box::new(s) as _),
                );
            }
            Command::DeclareQueryable { id, key } => {
                let declared = self
                    .zenoh
                    .declare_queryable(key)
                    .callback(move |q| send(&inbox, Input::Query(id, q)))
                    .wait();
                if let Ok(q) = &declared {
                    self.local.insert(q.id().eid(), id);
                }
                self.hold(
                    id,
                    "declaring a queryable",
                    declared.map(|q| Box::new(q) as _),
                );
            }
            Command::DeclareToken { id, key } => {
                let declared = self.zenoh.liveliness().declare_token(key).wait();
                self.hold(id, "declaring a token", declared.map(|t| Box::new(t) as _));
            }
            Command::DeclareLivelinessSubscriber { id, key } => {
                let declared = self
                    .zenoh
                    .liveliness()
                    .declare_subscriber(key)
                    .history(true)
                    .callback(move |s| send(&inbox, Input::Liveliness(id, s)))
                    .wait();
                let declared = declared.map(|s| Box::new(s) as _);
                self.hold(id, "declaring a liveliness subscriber", declared);
            }
            Command::Undeclare { id } => {
                self.publishers.remove(&id);
                self.held.remove(&id);
                self.local.retain(|_, e| *e != id);
            }
            Command::Put { publisher, sample } => {
                let Some(p) = self.publishers.get_mut(&publisher) else { return };
                p.sn = p.sn.wrapping_add(1);
                let put = p
                    .publisher
                    .put(unwrap(&sample.payload))
                    .encoding(sample.encoding)
                    .attachment(sample.attachment.as_ref().map(unwrap))
                    .source_info(SourceInfo::new(p.publisher.id(), p.sn));
                report("put", put.wait());
            }
            Command::Get {
                id,
                key,
                parameters,
                payload,
                any_reply_keys,
                timeout_ms,
            } => {
                let accept = match any_reply_keys {
                    true => ReplyKeyExpr::Any,
                    false => ReplyKeyExpr::MatchingQuery,
                };
                let mut get = self
                    .zenoh
                    .get(format!("{key}?{parameters}"))
                    .target(QueryTarget::All)
                    .consolidation(ConsolidationMode::None)
                    .accept_replies(accept);
                if let Some(sample) = payload {
                    get = get
                        .payload(unwrap(&sample.payload))
                        .encoding(sample.encoding)
                        .attachment(sample.attachment.as_ref().map(unwrap));
                }
                if let Some(ms) = timeout_ms {
                    get = get.timeout(Duration::from_millis(ms));
                }
                let done = inbox.clone();
                let handler = CallbackDrop {
                    callback: move |reply| send(&inbox, Input::Reply(id, reply)),
                    drop: move || send(&done, Input::Done(id)),
                };
                report("get", get.with(handler).wait());
            }
            Command::Reply { query, key, sample } => {
                let Some(q) = self.queries.get(&query) else { return };
                let reply = q
                    .reply(key, unwrap(&sample.payload))
                    .encoding(sample.encoding)
                    .attachment(sample.attachment.as_ref().map(unwrap));
                report("reply", reply.wait());
            }
            Command::ReplyErr { query, sample } => {
                let Some(q) = self.queries.get(&query) else { return };
                let reply = q
                    .reply_err(unwrap(&sample.payload))
                    .encoding(sample.encoding);
                report("error reply", reply.wait());
            }
            Command::Finish { query } => {
                self.queries.remove(&query);
            }
        }
    }

    fn declare_publisher(&mut self, id: EntityId, key: String, qos: Qos) {
        let (reliability, congestion) = match qos.reliable {
            true => (Reliability::Reliable, CongestionControl::Block),
            false => (Reliability::BestEffort, CongestionControl::Drop),
        };
        let declared = self
            .zenoh
            .declare_publisher(key)
            .priority(PRIORITIES[qos.priority as usize])
            .reliability(reliability)
            .congestion_control(congestion)
            .wait();
        let publisher = match declared {
            Ok(publisher) => publisher,
            Err(e) => return report("declaring a publisher", Err(e)),
        };
        let inbox = self.inbox.clone();
        let matching = publisher
            .matching_listener()
            .callback(move |s| send(&inbox, Input::Matching(id, s.matching())))
            .wait();
        self.local.insert(publisher.id().eid(), id);
        self.publishers.insert(
            id,
            Pub {
                publisher,
                _matching: matching.ok(),
                sn: 0,
            },
        );
    }

    /// Who sent a sample or a reply.
    fn source(&self, id: Option<&EntityGlobalId>) -> Source {
        match id {
            None => Source::Unknown,
            Some(id) if id.zid() == self.zid => {
                let local = self.local.get(&id.eid()).copied();
                local.map_or(Source::Unknown, Source::Local)
            }
            Some(id) => Source::Remote(Zid(id.zid().to_string())),
        }
    }

    /// What to do with what zenoh delivered.
    pub(crate) fn event(&mut self, input: Input) -> Step {
        Step::Handle(match input {
            Input::Sample(subscriber, s) => Event::Sample {
                subscriber,
                key: s.key_expr().to_string(),
                sample: sample(&s),
                source: self.source(s.source_info().map(SourceInfo::source_id)),
            },
            Input::Liveliness(subscriber, s) => Event::Liveliness {
                subscriber,
                key: s.key_expr().to_string(),
                alive: s.kind() == SampleKind::Put,
            },
            Input::Query(queryable, q) => {
                self.next_query += 1;
                let query = QueryId(self.next_query);
                let event = Event::Query {
                    queryable,
                    query,
                    key: q.key_expr().to_string(),
                    parameters: q.parameters().to_string(),
                    payload: query_payload(&q),
                };
                self.queries.insert(query, q);
                event
            }
            Input::Reply(_, reply) if is_timeout(&reply) => return Step::Skip,
            Input::Reply(request, reply) => {
                let replier = self.source(reply.replier_id().as_ref());
                let (key, result) = match reply.result() {
                    Ok(s) => (s.key_expr().to_string(), Ok(sample(s))),
                    Err(e) => {
                        let error = Sample::new(wrap(e.payload()), e.encoding().to_string());
                        (String::new(), Err(error))
                    }
                };
                Event::Reply {
                    request,
                    key,
                    result,
                    replier,
                }
            }
            Input::Done(request) => Event::RequestDone { request },
            Input::Matching(publisher, matched) => Event::Matching { publisher, matched },
            Input::Stop => return Step::Stop,
        })
    }
}
