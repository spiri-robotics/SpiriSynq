//! An in-memory network for tests: several [`Session`]s in one process, connected as zenoh would
//! connect them.
//!
//! It delivers every put to every subscriber whose key intersects (the sender's own included),
//! routes gets to every intersecting queryable, ends a get when every queryable that got it has
//! finished, and reports liveliness tokens and publisher matching. It never loses or reorders
//! anything, and keeps no time: [`MemoryNetwork::expire_requests`] stands in for timeouts and
//! [`MemoryNetwork::disconnect`] for a process that dies.

use std::collections::{HashMap, VecDeque};

use crate::Session;
use crate::transport::{Command, EntityId, Event, QueryId, RequestId, Source, Zid};
use crate::wire::YAML;

/// Whether two key expressions made of plain chunks, `*` and `**` have a key in common.
pub fn intersects(a: &str, b: &str) -> bool {
    let a: Vec<&str> = a.split('/').collect();
    let b: Vec<&str> = b.split('/').collect();
    chunks_intersect(&a, &b)
}

fn chunks_intersect(a: &[&str], b: &[&str]) -> bool {
    match (a.split_first(), b.split_first()) {
        (None, None) => true,
        (Some((&"**", rest)), _) => {
            chunks_intersect(rest, b) || (!b.is_empty() && chunks_intersect(a, &b[1..]))
        }
        (_, Some((&"**", rest))) => {
            chunks_intersect(a, rest) || (!a.is_empty() && chunks_intersect(&a[1..], b))
        }
        (Some((x, a_rest)), Some((y, b_rest))) => {
            (x == y || *x == "*" || *y == "*") && chunks_intersect(a_rest, b_rest)
        }
        _ => false,
    }
}

#[derive(Debug)]
struct Node {
    session: Session,
    connected: bool,
    publishers: HashMap<EntityId, String>,
    matched: HashMap<EntityId, bool>,
    subscribers: HashMap<EntityId, String>,
    queryables: HashMap<EntityId, String>,
    tokens: HashMap<EntityId, String>,
    liveliness: HashMap<EntityId, String>,
    inbox: VecDeque<Event>,
}

#[derive(Debug)]
struct Query {
    getter: usize,
    request: RequestId,
    key: String,
    any_reply_keys: bool,
    replier: usize,
    queryable: EntityId,
}

/// Sessions connected in memory. Each is known by its index.
#[derive(Debug)]
pub struct MemoryNetwork {
    nodes: Vec<Node>,
    next_query: u64,
    queries: HashMap<QueryId, Query>,
    pending: HashMap<(usize, RequestId), usize>,
    trace: Vec<String>,
}

impl Default for MemoryNetwork {
    fn default() -> Self {
        Self::new()
    }
}

impl MemoryNetwork {
    pub fn new() -> Self {
        MemoryNetwork {
            nodes: Vec::new(),
            next_query: 0,
            queries: HashMap::new(),
            pending: HashMap::new(),
            trace: Vec::new(),
        }
    }

    /// Add a session, with zid `z<index>`. Its index.
    pub fn add_session(&mut self) -> usize {
        let index = self.nodes.len();
        self.nodes.push(Node {
            session: Session::new(Zid(format!("z{index}"))),
            connected: true,
            publishers: HashMap::new(),
            matched: HashMap::new(),
            subscribers: HashMap::new(),
            queryables: HashMap::new(),
            tokens: HashMap::new(),
            liveliness: HashMap::new(),
            inbox: VecDeque::new(),
        });
        index
    }

    /// How many sessions there are.
    pub fn sessions(&self) -> usize {
        self.nodes.len()
    }

    pub fn session(&mut self, index: usize) -> &mut Session {
        &mut self.nodes[index].session
    }

    /// What went over the network, one line per put, query, reply or end of a get, oldest first.
    /// YAML payloads are written out; others show their size.
    pub fn trace(&self) -> &[String] {
        &self.trace
    }

    pub fn clear_trace(&mut self) {
        self.trace.clear();
    }

    /// Carry out every command and deliver every event, until nothing is left to do.
    pub fn run(&mut self) {
        loop {
            let mut busy = false;
            for index in 0..self.nodes.len() {
                while let Some(command) = self.nodes[index].session.poll_command() {
                    busy = true;
                    if self.nodes[index].connected {
                        self.execute(index, command);
                    }
                }
            }
            for node in &mut self.nodes {
                while let Some(event) = node.inbox.pop_front() {
                    busy = true;
                    node.session.handle(event);
                }
            }
            if !busy {
                return;
            }
        }
    }

    /// End every get still waiting for replies, as a timeout would. Replies to them that come
    /// later are dropped.
    pub fn expire_requests(&mut self) {
        let mut pending: Vec<(usize, RequestId)> = self.pending.drain().map(|(k, _)| k).collect();
        pending.sort();
        for (getter, request) in pending {
            self.end_get(getter, request);
        }
        self.queries.clear();
    }

    /// Cut a session off, as if its process died: its tokens go away, its subscribers stop
    /// counting, and gets it was answering end. It sends and receives nothing more.
    pub fn disconnect(&mut self, index: usize) {
        let node = &mut self.nodes[index];
        node.connected = false;
        node.publishers.clear();
        node.subscribers.clear();
        node.queryables.clear();
        node.liveliness.clear();
        node.inbox.clear();
        let mut tokens: Vec<(EntityId, String)> = node.tokens.drain().collect();
        tokens.sort();
        for (_, key) in tokens {
            self.announce(&key, false);
        }
        let mut answering: Vec<QueryId> = self
            .queries
            .iter()
            .filter(|(_, q)| q.replier == index)
            .map(|(id, _)| *id)
            .collect();
        answering.sort();
        for query in answering {
            self.finish(query);
        }
        self.queries.retain(|_, q| q.getter != index);
        self.pending.retain(|(getter, _), _| *getter != index);
        self.update_matching();
    }

    fn source(&self, from: usize, to: usize, entity: EntityId) -> Source {
        if from == to {
            Source::Local(entity)
        } else {
            Source::Remote(self.nodes[from].session.zid().clone())
        }
    }

    fn describe(sample: &crate::Sample) -> String {
        let bytes = sample.payload.bytes();
        let body = if sample.encoding == YAML {
            String::from_utf8_lossy(&bytes)
                .trim_end()
                .replace('\n', "\\n")
        } else {
            format!("<{} bytes>", bytes.len())
        };
        format!("{} {body}", sample.encoding)
    }

    fn execute(&mut self, index: usize, command: Command) {
        match command {
            Command::DeclarePublisher { id, key, .. } => {
                self.nodes[index].publishers.insert(id, key);
                self.update_matching();
            }
            Command::DeclareSubscriber { id, key } => {
                self.nodes[index].subscribers.insert(id, key);
                self.update_matching();
            }
            Command::DeclareQueryable { id, key } => {
                self.nodes[index].queryables.insert(id, key);
            }
            Command::DeclareToken { id, key } => {
                self.nodes[index].tokens.insert(id, key.clone());
                self.announce(&key, true);
            }
            Command::DeclareLivelinessSubscriber { id, key } => {
                let mut alive: Vec<String> = self
                    .nodes
                    .iter()
                    .flat_map(|n| n.tokens.values())
                    .filter(|t| intersects(t, &key))
                    .cloned()
                    .collect();
                alive.sort();
                let node = &mut self.nodes[index];
                for token in alive {
                    node.inbox.push_back(Event::Liveliness {
                        subscriber: id,
                        key: token,
                        alive: true,
                    });
                }
                node.liveliness.insert(id, key);
            }
            Command::Undeclare { id } => {
                let node = &mut self.nodes[index];
                node.publishers.remove(&id);
                node.matched.remove(&id);
                node.subscribers.remove(&id);
                node.queryables.remove(&id);
                node.liveliness.remove(&id);
                if let Some(key) = node.tokens.remove(&id) {
                    self.announce(&key, false);
                }
                self.update_matching();
            }
            Command::Put {
                publisher,
                sample,
                qos,
            } => {
                let key = self.nodes[index].publishers[&publisher].clone();
                let qos = qos.map(|q| format!(" ({q:?})")).unwrap_or_default();
                self.trace.push(format!(
                    "{index} put {key} {}{qos}",
                    Self::describe(&sample)
                ));
                for to in 0..self.nodes.len() {
                    let source = self.source(index, to, publisher);
                    let node = &mut self.nodes[to];
                    let mut subscribers: Vec<EntityId> = node
                        .subscribers
                        .iter()
                        .filter(|(_, k)| intersects(k, &key))
                        .map(|(id, _)| *id)
                        .collect();
                    subscribers.sort();
                    for subscriber in subscribers {
                        node.inbox.push_back(Event::Sample {
                            subscriber,
                            key: key.clone(),
                            sample: sample.clone(),
                            source: source.clone(),
                        });
                    }
                }
            }
            Command::Get {
                id,
                key,
                parameters,
                payload,
                any_reply_keys,
                ..
            } => {
                let shown = payload.as_ref().map(Self::describe).unwrap_or_default();
                self.trace.push(
                    format!("{index} get {key}?{parameters} {shown}")
                        .trim_end()
                        .to_owned(),
                );
                let mut targets: Vec<(usize, EntityId)> = Vec::new();
                for (n, node) in self.nodes.iter().enumerate() {
                    for (q, k) in &node.queryables {
                        if intersects(k, &key) {
                            targets.push((n, *q));
                        }
                    }
                }
                targets.sort();
                if targets.is_empty() {
                    self.end_get(index, id);
                    return;
                }
                self.pending.insert((index, id), targets.len());
                for (replier, queryable) in targets {
                    self.next_query += 1;
                    let query = QueryId(self.next_query);
                    self.queries.insert(
                        query,
                        Query {
                            getter: index,
                            request: id,
                            key: key.clone(),
                            any_reply_keys,
                            replier,
                            queryable,
                        },
                    );
                    self.nodes[replier].inbox.push_back(Event::Query {
                        queryable,
                        query,
                        key: key.clone(),
                        parameters: parameters.clone(),
                        payload: payload.clone(),
                    });
                }
            }
            Command::Reply { query, key, sample } => self.reply(query, key, Ok(sample)),
            Command::ReplyErr { query, sample } => {
                let key = self
                    .queries
                    .get(&query)
                    .map(|q| q.key.clone())
                    .unwrap_or_default();
                self.reply(query, key, Err(sample));
            }
            Command::Finish { query } => self.finish(query),
        }
    }

    fn reply(&mut self, query: QueryId, key: String, result: Result<crate::Sample, crate::Sample>) {
        let Some(q) = self.queries.get(&query) else { return };
        assert!(
            q.any_reply_keys || intersects(&key, &q.key),
            "reply on {key} to a get on {}",
            q.key
        );
        let (kind, sample) = match &result {
            Ok(s) => ("reply", s),
            Err(s) => ("error", s),
        };
        self.trace.push(format!(
            "{} {kind} {key} {}",
            q.replier,
            Self::describe(sample)
        ));
        let replier = self.source(q.replier, q.getter, q.queryable);
        let (getter, request) = (q.getter, q.request);
        self.nodes[getter].inbox.push_back(Event::Reply {
            request,
            key,
            result,
            replier,
        });
    }

    fn finish(&mut self, query: QueryId) {
        let Some(q) = self.queries.remove(&query) else { return };
        let left = self
            .pending
            .get_mut(&(q.getter, q.request))
            .expect("counted when sent");
        *left -= 1;
        if *left == 0 {
            self.pending.remove(&(q.getter, q.request));
            self.end_get(q.getter, q.request);
        }
    }

    fn end_get(&mut self, getter: usize, request: RequestId) {
        self.trace.push(format!("{getter} done {request:?}"));
        self.nodes[getter]
            .inbox
            .push_back(Event::RequestDone { request });
    }

    fn announce(&mut self, token: &str, alive: bool) {
        for node in &mut self.nodes {
            let mut subscribers: Vec<EntityId> = node
                .liveliness
                .iter()
                .filter(|(_, k)| intersects(token, k))
                .map(|(id, _)| *id)
                .collect();
            subscribers.sort();
            for subscriber in subscribers {
                node.inbox.push_back(Event::Liveliness {
                    subscriber,
                    key: token.to_owned(),
                    alive,
                });
            }
        }
    }

    fn update_matching(&mut self) {
        let subscribed: Vec<String> = self
            .nodes
            .iter()
            .flat_map(|n| n.subscribers.values().cloned())
            .collect();
        for node in &mut self.nodes {
            let mut publishers: Vec<(&EntityId, &String)> = node.publishers.iter().collect();
            publishers.sort();
            for (id, key) in publishers {
                let matched = subscribed.iter().any(|s| intersects(s, key));
                let was = node.matched.insert(*id, matched).unwrap_or(false);
                if was != matched {
                    node.inbox.push_back(Event::Matching {
                        publisher: *id,
                        matched,
                    });
                }
            }
        }
    }
}
