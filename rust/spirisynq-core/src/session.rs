//! The engine: one [`Session`] per zenoh session.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::iter;
use std::sync::Arc;

use spirisynq_schema::{TypeId, TypeSchema, check_topic};
use spirisynq_yaml::Value;

use crate::Error;
use crate::notice::{Data, IncomingCall, Notice, ObjectId, Origin, ReplyError, RequestEnd};
use crate::store::{Store, Stored};
use crate::transport::{Command, EntityId, Event, Qos, QueryId, RequestId, Sample, Source, Zid};
use crate::wire::{self, ALIVE, DONE, METADATA, OBJECT_SCHEMA, REHYDRATE, TYPE_SCHEMA};

/// How this process handles one field: the Python `<field>_<setting>` class attributes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FieldOptions {
    pub qos: Qos,
    /// Owner only: ignore writes from other processes. The owner then doesn't subscribe to the
    /// field.
    pub ignore_incoming: bool,
    /// Owner only: publish only while someone listens. Needs `ignore_incoming`, since the owner's
    /// own subscription would count as a listener.
    pub lazy: bool,
    /// The field has its own encoding: incoming values arrive as [`Data::Raw`], for the binding's
    /// `after_receive` to decode.
    pub raw: bool,
}

/// How this process handles an object.
#[derive(Debug, Clone, PartialEq)]
pub struct ObjectOptions {
    /// Per field key; fields not listed get the defaults.
    pub fields: Vec<(String, FieldOptions)>,
    /// Owner only: remote methods that are raw (`@remote_method_raw`). Their calls arrive with the
    /// query's payload untouched, and no decoded arguments.
    pub raw_methods: Vec<String>,
    /// Mirror only: fetch the owner's full state when the mirror starts. On by default.
    pub rehydrate: bool,
}

impl Default for ObjectOptions {
    fn default() -> Self {
        ObjectOptions {
            fields: Vec::new(),
            raw_methods: Vec::new(),
            rehydrate: true,
        }
    }
}

/// How to make a remote call.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CallOptions {
    /// Pass replies on as [`Data::Raw`], for an `after_call` hook to decode.
    pub raw_replies: bool,
    /// Selector parameters, sent as they are.
    pub parameters: String,
    pub timeout_ms: Option<u64>,
}

/// A mirror's state fetch under way, and the fields that arrived live meanwhile.
type Fetch = Option<(RequestId, HashSet<String>)>;

type MirrorMut<'a> = (
    &'a mut Option<Zid>,
    &'a mut Fetch,
    &'a mut bool,
    &'a mut bool,
);

#[derive(Debug)]
enum Role {
    Owner {
        listening: HashMap<String, bool>,
    },
    Mirror {
        owner: Option<Zid>,
        tokens: BTreeSet<Zid>,
        rehydrating: Fetch,
        /// A fetch was wanted and none has completed since: fetch again when a token appears.
        stale: bool,
        /// A token appeared during the fetch under way: fetch again if it doesn't complete.
        again: bool,
    },
}

impl Role {
    /// A mirror's owner, state fetch, and whether to fetch again (`stale`, `again`).
    fn mirror_mut(&mut self) -> Option<MirrorMut<'_>> {
        match self {
            Role::Mirror {
                owner,
                rehydrating,
                stale,
                again,
                ..
            } => Some((owner, rehydrating, stale, again)),
            Role::Owner { .. } => None,
        }
    }
}

#[derive(Debug)]
struct Object {
    topic: String,
    schema: Arc<TypeSchema>,
    options: HashMap<String, FieldOptions>,
    raw_methods: HashSet<String>,
    store: Store,
    publishers: HashMap<String, EntityId>,
    role: Role,
}

impl Object {
    fn check_field(&self, path: &str) -> Result<(), Error> {
        match self.schema.field(path) {
            Some(_) => Ok(()),
            None => Err(Error::UnknownField(path.to_owned())),
        }
    }

    fn options(&self, path: &str) -> FieldOptions {
        self.options.get(path).copied().unwrap_or_default()
    }

    /// An incoming sample as the binding gets it.
    fn notice(&self, object: ObjectId, path: &str, sample: Sample, origin: Origin) -> Notice {
        let path = path.to_owned();
        if self.options(&path).raw {
            return Notice::Incoming {
                object,
                path,
                data: Data::Raw(sample),
                origin,
            };
        }
        match wire::decode(&sample) {
            Ok(value) => Notice::Incoming {
                object,
                path,
                data: Data::Value(value),
                origin,
            },
            Err(reason) => Notice::BadData {
                object,
                path,
                reason,
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
enum Route {
    FieldSub(String),
    FieldPub(String),
    Rehydrate,
    Metadata(String),
    Schema(String),
    Method,
    Token,
    Alive,
}

#[derive(Debug)]
enum RequestKind {
    Rehydrate(ObjectId),
    Call { raw: bool },
    Discover,
    Schema,
}

#[derive(Debug)]
struct Request {
    kind: RequestKind,
    done: bool,
    failed: bool,
}

/// The SpiriSynq engine for one zenoh session.
///
/// It does no I/O. The transport takes [`Command`]s from [`Session::poll_command`] and carries
/// them out, and hands what happens on the network to [`Session::handle`] as [`Event`]s. The
/// binding opens objects, sets fields and makes calls through the other methods, and takes what
/// it needs to know from [`Session::poll_notice`]. Nothing here blocks, spawns or keeps time, so
/// the same engine runs on Tokio, in a browser's event loop, or in a test.
#[derive(Debug)]
pub struct Session {
    zid: Zid,
    next: u64,
    objects: HashMap<ObjectId, Object>,
    routes: HashMap<EntityId, (ObjectId, Route)>,
    requests: HashMap<RequestId, Request>,
    queries: HashMap<QueryId, (ObjectId, String)>,
    commands: VecDeque<Command>,
    notices: VecDeque<Notice>,
}

impl Session {
    /// An engine for the zenoh session `zid`, as the transport writes session ids.
    pub fn new(zid: Zid) -> Self {
        Session {
            zid,
            next: 0,
            objects: HashMap::new(),
            routes: HashMap::new(),
            requests: HashMap::new(),
            queries: HashMap::new(),
            commands: VecDeque::new(),
            notices: VecDeque::new(),
        }
    }

    pub fn zid(&self) -> &Zid {
        &self.zid
    }

    /// The next thing for the transport to do.
    pub fn poll_command(&mut self) -> Option<Command> {
        self.commands.pop_front()
    }

    /// The next thing for the binding to know.
    pub fn poll_notice(&mut self) -> Option<Notice> {
        self.notices.pop_front()
    }

    fn fresh(&mut self) -> u64 {
        self.next += 1;
        self.next
    }

    fn object(&self, object: ObjectId) -> Result<&Object, Error> {
        self.objects.get(&object).ok_or(Error::UnknownObject)
    }

    fn object_mut(&mut self, object: ObjectId) -> Result<&mut Object, Error> {
        self.objects.get_mut(&object).ok_or(Error::UnknownObject)
    }

    fn declare(
        &mut self,
        object: ObjectId,
        route: Route,
        command: impl FnOnce(EntityId) -> Command,
    ) -> EntityId {
        let id = EntityId(self.fresh());
        self.routes.insert(id, (object, route));
        self.commands.push_back(command(id));
        id
    }

    fn open(
        &mut self,
        topic: &str,
        schema: Arc<TypeSchema>,
        options: ObjectOptions,
        role: Role,
    ) -> Result<ObjectId, Error> {
        check_topic(topic).map_err(Error::Topic)?;
        let mut fields = HashMap::new();
        for (path, field) in options.fields {
            if schema.field(&path).is_none() {
                return Err(Error::UnknownField(path));
            }
            fields.insert(path, field);
        }
        let mut raw_methods = HashSet::new();
        for name in options.raw_methods {
            if schema.method(&name).is_none() {
                return Err(Error::UnknownMethod(name));
            }
            raw_methods.insert(name);
        }
        let object = ObjectId(self.fresh());
        self.objects.insert(
            object,
            Object {
                topic: topic.to_owned(),
                schema,
                options: fields,
                raw_methods,
                store: Store::new(),
                publishers: HashMap::new(),
                role,
            },
        );
        Ok(object)
    }

    /// Open an authoritative object on `topic`: declare its publishers and subscribers, serve its
    /// state, metadata, schema and remote methods, and announce that it's alive. Set its fields'
    /// first values with [`Session::set_field`].
    pub fn create_owner(
        &mut self,
        topic: &str,
        schema: Arc<TypeSchema>,
        options: ObjectOptions,
    ) -> Result<ObjectId, Error> {
        if let Some((path, _)) = options
            .fields
            .iter()
            .find(|(_, o)| o.lazy && !o.ignore_incoming)
        {
            return Err(Error::LazyNeedsIgnoreIncoming(path.clone()));
        }
        let role = Role::Owner {
            listening: HashMap::new(),
        };
        let object = self.open(topic, schema.clone(), options, role)?;
        let topic = topic.to_owned();
        for field in schema.fields() {
            let path = field.path.clone();
            let key = wire::key(&topic, &path);
            let o = self.objects[&object].options(&path);
            let k = key.clone();
            let publisher = self.declare(object, Route::FieldPub(path.clone()), |id| {
                Command::DeclarePublisher {
                    id,
                    key: k,
                    qos: o.qos,
                }
            });
            let obj = self.objects.get_mut(&object).expect("just opened");
            obj.publishers.insert(path.clone(), publisher);
            if !o.ignore_incoming {
                self.declare(object, Route::FieldSub(path), |id| {
                    Command::DeclareSubscriber { id, key }
                });
            }
        }
        self.declare(
            object,
            Route::Rehydrate,
            queryable(wire::key(&topic, REHYDRATE)),
        );
        let types = || iter::once(schema.id()).chain(schema.bases());
        let mounts = [(METADATA, METADATA), (OBJECT_SCHEMA, TYPE_SCHEMA)];
        for (bare, per_type) in mounts {
            let keys = iter::once(wire::key(&topic, bare))
                .chain(wire::type_keys(&topic, per_type, types()))
                .collect::<Vec<_>>();
            for key in keys {
                let route = if bare == METADATA {
                    Route::Metadata(key.clone())
                } else {
                    Route::Schema(key.clone())
                };
                self.declare(object, route, queryable(key));
            }
        }
        for method in schema.methods() {
            let key = wire::key(&topic, &method.key.key_expr());
            self.declare(object, Route::Method, queryable(key));
        }
        let key = format!("{topic}/{ALIVE}/{}", self.zid);
        self.declare(object, Route::Token, |id| Command::DeclareToken { id, key });
        Ok(object)
    }

    /// Open a mirror of the object on `topic`: follow its owner's liveliness, subscribe to every
    /// field, and fetch the owner's full state unless `options.rehydrate` is off.
    pub fn create_mirror(
        &mut self,
        topic: &str,
        schema: Arc<TypeSchema>,
        options: ObjectOptions,
    ) -> Result<ObjectId, Error> {
        let rehydrate = options.rehydrate;
        let role = Role::Mirror {
            owner: None,
            tokens: BTreeSet::new(),
            rehydrating: None,
            stale: false,
            again: false,
        };
        let object = self.open(topic, schema.clone(), options, role)?;
        let key = format!("{topic}/{ALIVE}/*");
        self.declare(object, Route::Alive, |id| {
            Command::DeclareLivelinessSubscriber { id, key }
        });
        for field in schema.fields() {
            let key = wire::key(topic, &field.path);
            self.declare(object, Route::FieldSub(field.path.clone()), |id| {
                Command::DeclareSubscriber { id, key }
            });
        }
        if rehydrate {
            self.start_rehydrate(object);
        }
        Ok(object)
    }

    fn start_rehydrate(&mut self, object: ObjectId) {
        let key = wire::key(&self.objects[&object].topic, REHYDRATE);
        let request = self.send_get(key, String::new(), None, true, None, || {
            RequestKind::Rehydrate(object)
        });
        let obj = self
            .objects
            .get_mut(&object)
            .expect("checked by the caller");
        let (_, rehydrating, stale, again) = obj.role.mirror_mut().expect("only mirrors fetch");
        (*stale, *again) = (true, false);
        if let Some((old, _)) = rehydrating.replace((request, HashSet::new())) {
            self.requests.remove(&old);
        }
    }

    /// Fetch a mirror's full state again. Values arrive as [`Notice::Incoming`] with
    /// [`Origin::Rehydrate`], then [`Notice::Rehydrated`]. A fetch already under way is dropped.
    ///
    /// Until a fetch completes, the mirror fetches again by itself whenever an owner's token
    /// appears: a mirror opened before its owner, or while the owner's declarations are still on
    /// their way, catches up once the owner is reachable.
    pub fn rehydrate(&mut self, object: ObjectId) -> Result<(), Error> {
        match self.object(object)?.role {
            Role::Mirror { .. } => {
                self.start_rehydrate(object);
                Ok(())
            }
            Role::Owner { .. } => Err(Error::NotMirror),
        }
    }

    /// Close an object: undeclare everything it declared, so an owner's token goes away. Calls
    /// it hasn't answered get an error reply. Calls it made carry on.
    pub fn close(&mut self, object: ObjectId) -> Result<(), Error> {
        self.objects.remove(&object).ok_or(Error::UnknownObject)?;
        let mut ids: Vec<EntityId> = self
            .routes
            .iter()
            .filter(|(_, (o, _))| *o == object)
            .map(|(id, _)| *id)
            .collect();
        ids.sort();
        for id in ids {
            self.routes.remove(&id);
            self.commands.push_back(Command::Undeclare { id });
        }
        let mut queries: Vec<QueryId> = self
            .queries
            .iter()
            .filter(|(_, (o, _))| *o == object)
            .map(|(q, _)| *q)
            .collect();
        queries.sort();
        for query in queries {
            self.end_with_error(query, "RuntimeError", "object closed");
        }
        self.requests
            .retain(|_, r| !matches!(r.kind, RequestKind::Rehydrate(o) if o == object));
        Ok(())
    }

    /// Set a field, and publish it if it changed: on an owner to the mirrors (unless it's lazy and
    /// nobody listens), on a mirror as a write to the owner. Whether it changed.
    ///
    /// On an owner, this is also how a write from another process is applied, once the binding
    /// has checked it.
    pub fn set_field(&mut self, object: ObjectId, path: &str, value: Value) -> Result<bool, Error> {
        self.object(object)?.check_field(path)?;
        let sample = wire::yaml_sample(&value)?;
        let changed = self.object_mut(object)?.store.set(path, value)?;
        if changed {
            self.publish(object, path, sample);
        }
        Ok(changed)
    }

    /// Set a mirror's field without publishing it: apply a value from the owner, or a mirror
    /// callback's write. Whether it changed.
    pub fn set_field_local(
        &mut self,
        object: ObjectId,
        path: &str,
        value: Value,
    ) -> Result<bool, Error> {
        let obj = self.object_mut(object)?;
        obj.check_field(path)?;
        if obj.role.mirror_mut().is_none() {
            return Err(Error::NotMirror);
        }
        obj.store.set(path, value)
    }

    /// Publish a field in its own encoding, as its `before_send` produced it. An owner keeps the
    /// sample, to send to new mirrors as the field's state.
    pub fn send_raw(&mut self, object: ObjectId, path: &str, sample: Sample) -> Result<(), Error> {
        let obj = self.object_mut(object)?;
        obj.check_field(path)?;
        if let Role::Owner { .. } = obj.role {
            obj.store.set_raw(path, sample.clone());
        }
        self.publish(object, path, sample);
        Ok(())
    }

    fn publish(&mut self, object: ObjectId, path: &str, sample: Sample) {
        let obj = &self.objects[&object];
        let publisher = match &obj.role {
            Role::Owner { listening } => {
                if obj.options(path).lazy && !listening.get(path).copied().unwrap_or(false) {
                    return;
                }
                obj.publishers[path]
            }
            Role::Mirror { .. } => match obj.publishers.get(path) {
                Some(id) => *id,
                None => {
                    let key = wire::key(&obj.topic, path);
                    let qos = obj.options(path).qos;
                    let id = self.declare(object, Route::FieldPub(path.to_owned()), |id| {
                        Command::DeclarePublisher { id, key, qos }
                    });
                    let obj = self
                        .objects
                        .get_mut(&object)
                        .expect("checked by the caller");
                    obj.publishers.insert(path.to_owned(), id);
                    id
                }
            },
        };
        self.commands.push_back(Command::Put { publisher, sample });
    }

    /// A field's value, if it holds one: `None` before it's set, while a parent dataclass is
    /// null, or when it has its own encoding (the binding keeps those values).
    pub fn field(&self, object: ObjectId, path: &str) -> Result<Option<&Value>, Error> {
        let obj = self.object(object)?;
        obj.check_field(path)?;
        Ok(obj.store.get(path))
    }

    /// Whether a mirror's owner is alive.
    pub fn alive(&self, object: ObjectId) -> Result<bool, Error> {
        match &self.object(object)?.role {
            Role::Mirror { tokens, .. } => Ok(!tokens.is_empty()),
            Role::Owner { .. } => Err(Error::NotMirror),
        }
    }

    /// Whether anyone listens to an owner's field, or to any of its fields.
    pub fn has_listeners(&self, object: ObjectId, path: Option<&str>) -> Result<bool, Error> {
        let obj = self.object(object)?;
        let Role::Owner { listening } = &obj.role else { return Err(Error::NotOwner) };
        match path {
            Some(path) => {
                obj.check_field(path)?;
                Ok(listening.get(path).copied().unwrap_or(false))
            }
            None => Ok(listening.values().any(|l| *l)),
        }
    }

    fn send_get(
        &mut self,
        key: String,
        parameters: String,
        payload: Option<Sample>,
        any_reply_keys: bool,
        timeout_ms: Option<u64>,
        kind: impl FnOnce() -> RequestKind,
    ) -> RequestId {
        let id = RequestId(self.fresh());
        self.requests.insert(
            id,
            Request {
                kind: kind(),
                done: false,
                failed: false,
            },
        );
        self.commands.push_back(Command::Get {
            id,
            key,
            parameters,
            payload,
            any_reply_keys,
            timeout_ms,
        });
        id
    }

    /// Call a remote method on the object's owner. Arguments named by the key template's
    /// placeholders go in the key, and must be strings; the rest go in the payload, as one YAML
    /// mapping. Replies arrive as [`Notice::Reply`], then [`Notice::RequestEnded`].
    pub fn call(
        &mut self,
        object: ObjectId,
        method: &str,
        args: Vec<(String, Value)>,
        options: CallOptions,
    ) -> Result<RequestId, Error> {
        let obj = self.object(object)?;
        let m = obj
            .schema
            .method(method)
            .ok_or_else(|| Error::UnknownMethod(method.to_owned()))?;
        let names: Vec<&str> = m.key.placeholders().collect();
        let mut key_args = Vec::new();
        let mut rest = Vec::new();
        for (name, value) in &args {
            match (names.contains(&name.as_str()), value) {
                (true, Value::String(s)) => key_args.push((name.as_str(), s.as_str())),
                (true, _) => return Err(Error::NotAString(name.clone())),
                (false, _) => rest.push((Value::String(name.clone()), value.clone())),
            }
        }
        let key = wire::key(&obj.topic, &m.key.fill(&key_args)?);
        let payload = wire::yaml_sample(&Value::Map(rest))?;
        Ok(self.call_key(key, Some(payload), options))
    }

    /// Call a remote method with a payload of the caller's own: a `before_call` hook's raw send.
    /// `key_args` fill the key template's placeholders.
    pub fn call_raw(
        &mut self,
        object: ObjectId,
        method: &str,
        key_args: &[(&str, &str)],
        payload: Option<Sample>,
        options: CallOptions,
    ) -> Result<RequestId, Error> {
        let obj = self.object(object)?;
        let m = obj
            .schema
            .method(method)
            .ok_or_else(|| Error::UnknownMethod(method.to_owned()))?;
        let key = wire::key(&obj.topic, &m.key.fill(key_args)?);
        Ok(self.call_key(key, payload, options))
    }

    fn call_key(&mut self, key: String, payload: Option<Sample>, o: CallOptions) -> RequestId {
        let raw = o.raw_replies;
        self.send_get(key, o.parameters, payload, false, o.timeout_ms, || {
            RequestKind::Call { raw }
        })
    }

    fn query_key(&self, query: QueryId) -> Result<String, Error> {
        match self.queries.get(&query) {
            Some((_, key)) => Ok(key.clone()),
            None => Err(Error::UnknownQuery),
        }
    }

    /// Send one value in reply to an incoming call.
    pub fn reply(&mut self, query: QueryId, value: &Value) -> Result<(), Error> {
        let key = self.query_key(query)?;
        let sample = wire::yaml_sample(value)?;
        self.commands
            .push_back(Command::Reply { query, key, sample });
        Ok(())
    }

    /// Send a reply as it is: a raw method's `Reply`.
    pub fn reply_raw(&mut self, query: QueryId, sample: Sample) -> Result<(), Error> {
        let key = self.query_key(query)?;
        self.commands
            .push_back(Command::Reply { query, key, sample });
        Ok(())
    }

    /// End an incoming call with an error: the exception's type and message.
    pub fn reply_error(&mut self, query: QueryId, kind: &str, message: &str) -> Result<(), Error> {
        self.query_key(query)?;
        self.end_with_error(query, kind, message);
        Ok(())
    }

    /// End an incoming call successfully, once every value has been sent.
    pub fn finish(&mut self, query: QueryId) -> Result<(), Error> {
        let key = self.query_key(query)?;
        self.queries.remove(&query);
        self.answer_done(query, key);
        Ok(())
    }

    fn end_with_error(&mut self, query: QueryId, kind: &str, message: &str) {
        self.queries.remove(&query);
        let sample = wire::error_sample(kind, message);
        self.commands.push_back(Command::ReplyErr { query, sample });
        self.commands.push_back(Command::Finish { query });
    }

    fn answer_done(&mut self, query: QueryId, key: String) {
        let sample = wire::done_sample();
        self.commands
            .push_back(Command::Reply { query, key, sample });
        self.commands.push_back(Command::Finish { query });
    }

    /// Ask every object under `prefix` (everywhere, without one), or every object of a type,
    /// whatever its version, for its metadata. Answers arrive as [`Notice::Discovered`], then
    /// [`Notice::RequestEnded`]. Answers that can't be read are skipped.
    pub fn discover(
        &mut self,
        prefix: Option<&str>,
        type_id: Option<&TypeId>,
    ) -> Result<RequestId, Error> {
        let mut key = match prefix {
            Some(prefix) => {
                check_topic(prefix).map_err(Error::Topic)?;
                format!("{prefix}/**/{METADATA}")
            }
            None => format!("**/{METADATA}"),
        };
        if let Some(t) = type_id {
            key = format!("{key}/{}", t.key_chunk());
        }
        Ok(self.send_get(key, String::new(), None, false, None, || {
            RequestKind::Discover
        }))
    }

    /// Fetch the schema of the object on `topic` from its owner. The answer arrives as
    /// [`Notice::Schema`], then [`Notice::RequestEnded`].
    pub fn fetch_schema(&mut self, topic: &str) -> Result<RequestId, Error> {
        check_topic(topic).map_err(Error::Topic)?;
        let key = wire::key(topic, OBJECT_SCHEMA);
        Ok(self.send_get(key, String::new(), None, false, None, || {
            RequestKind::Schema
        }))
    }

    /// Take in something that happened on the network.
    pub fn handle(&mut self, event: Event) {
        match event {
            Event::Sample {
                subscriber,
                sample,
                source,
                ..
            } => self.on_sample(subscriber, sample, source),
            Event::Query {
                queryable,
                query,
                key,
                payload,
                parameters,
            } => self.on_query(queryable, query, key, parameters, payload),
            Event::Reply {
                request,
                key,
                result,
                replier,
            } => self.on_reply(request, key, result, replier),
            Event::RequestDone { request } => self.on_request_done(request),
            Event::Liveliness {
                subscriber,
                key,
                alive,
            } => self.on_liveliness(subscriber, &key, alive),
            Event::Matching { publisher, matched } => self.on_matching(publisher, matched),
        }
    }

    fn on_sample(&mut self, subscriber: EntityId, sample: Sample, source: Source) {
        let Some((object, Route::FieldSub(path))) = self.routes.get(&subscriber).cloned() else {
            return;
        };
        let obj = &self.objects[&object];
        let origin = match &obj.role {
            Role::Owner { .. } => {
                if let Source::Local(e) = &source
                    && self.routes.get(e).is_some_and(|(o, _)| *o == object)
                {
                    return;
                }
                Origin::Write
            }
            Role::Mirror { owner, .. } => {
                let from_owner = match (&source, owner) {
                    (Source::Remote(z), Some(owner)) => z == owner,
                    (Source::Local(e), Some(owner)) => {
                        *owner == self.zid && self.is_owner_of(*e, &obj.topic)
                    }
                    _ => false,
                };
                if !from_owner {
                    return;
                }
                Origin::Owner
            }
        };
        let notice = obj.notice(object, &path, sample, origin);
        if let Role::Mirror {
            rehydrating: Some((_, live)),
            ..
        } = &mut self.objects.get_mut(&object).expect("routed").role
        {
            live.insert(path);
        }
        self.notices.push_back(notice);
    }

    /// Whether a local entity belongs to the authoritative object on `topic`.
    fn is_owner_of(&self, entity: EntityId, topic: &str) -> bool {
        self.routes.get(&entity).is_some_and(|(o, _)| {
            let obj = &self.objects[o];
            matches!(obj.role, Role::Owner { .. }) && obj.topic == topic
        })
    }

    fn on_query(
        &mut self,
        queryable: EntityId,
        query: QueryId,
        key: String,
        parameters: String,
        payload: Option<Sample>,
    ) {
        let Some((object, route)) = self.routes.get(&queryable).cloned() else {
            self.commands.push_back(Command::Finish { query });
            return;
        };
        let obj = &self.objects[&object];
        match route {
            Route::Rehydrate => {
                let topic = obj.topic.clone();
                let mut replies = Vec::new();
                for field in obj.schema.fields() {
                    if field.path.contains('/') || !field.rehydrate {
                        continue;
                    }
                    let sample = match obj.store.top(&field.path) {
                        Some(Stored::Value(v)) => {
                            wire::yaml_sample(v).expect("it was encoded once")
                        }
                        Some(Stored::Raw(s)) => s.clone(),
                        None => continue,
                    };
                    replies.push((field.path.clone(), sample));
                }
                for (path, sample) in obj.store.raw_subs() {
                    if obj.schema.field(path).is_some_and(|f| f.rehydrate) {
                        replies.push((path.clone(), sample.clone()));
                    }
                }
                for (path, sample) in replies {
                    let key = wire::key(&topic, &path);
                    self.commands
                        .push_back(Command::Reply { query, key, sample });
                }
                self.answer_done(query, wire::key(&topic, REHYDRATE));
            }
            Route::Metadata(key) => {
                let schema = &obj.schema;
                let value =
                    wire::metadata_value(&obj.topic, &self.zid, schema.id(), schema.bases());
                let sample = wire::yaml_sample(&value).expect("strings always encode");
                self.commands.push_back(Command::Reply {
                    query,
                    key: key.clone(),
                    sample,
                });
                self.answer_done(query, key);
            }
            Route::Schema(key) => match wire::yaml_sample(obj.schema.raw()) {
                Ok(sample) => {
                    self.commands.push_back(Command::Reply {
                        query,
                        key: key.clone(),
                        sample,
                    });
                    self.answer_done(query, key);
                }
                Err(e) => self.end_with_error(query, "RuntimeError", &format!("schema: {e}")),
            },
            Route::Method => self.on_call(object, query, key, parameters, payload),
            _ => self.commands.push_back(Command::Finish { query }),
        }
    }

    fn on_call(
        &mut self,
        object: ObjectId,
        query: QueryId,
        key: String,
        parameters: String,
        payload: Option<Sample>,
    ) {
        let obj = &self.objects[&object];
        let routed = key
            .strip_prefix(&format!("{}/", obj.topic))
            .filter(|rel| !rel.contains(['*', '$']))
            .and_then(|rel| obj.schema.method_for_key(rel));
        let Some((method, key_args)) = routed else {
            // A wildcard query that happens to reach this queryable isn't a call.
            self.commands.push_back(Command::Finish { query });
            return;
        };
        let name = method.name.clone();
        let raw = obj.raw_methods.contains(&name);
        let key_args = match key_args {
            Ok(args) => args,
            Err(e) => return self.end_with_error(query, "ValueError", &e.to_string()),
        };
        let (args, payload) = if raw {
            (Value::Map(Vec::new()), payload)
        } else {
            match read_args(payload.as_ref(), &key_args) {
                Ok(args) => (args, None),
                Err(why) => return self.end_with_error(query, "ValueError", &why),
            }
        };
        self.queries.insert(query, (object, key.clone()));
        self.notices.push_back(Notice::Call(IncomingCall {
            query,
            object,
            method: name,
            key,
            key_args,
            args,
            payload,
            parameters,
        }));
    }

    fn on_reply(
        &mut self,
        request: RequestId,
        key: String,
        result: Result<Sample, Sample>,
        replier: Source,
    ) {
        let Some(req) = self.requests.get_mut(&request) else { return };
        if req.failed {
            return;
        }
        let sample = match result {
            Ok(s) if s.encoding == DONE => {
                req.done = true;
                return;
            }
            Ok(s) => s,
            Err(s) => {
                if matches!(req.kind, RequestKind::Discover) {
                    return;
                }
                req.failed = true;
                let error = wire::read_error(&s);
                let notice = match req.kind {
                    RequestKind::Call { .. } => Notice::Reply {
                        request,
                        result: Err(error),
                    },
                    RequestKind::Schema => Notice::Schema {
                        request,
                        result: Err(error),
                    },
                    _ => return,
                };
                self.notices.push_back(notice);
                return;
            }
        };
        let notice = match req.kind {
            RequestKind::Call { raw: true } => Notice::Reply {
                request,
                result: Ok(Data::Raw(sample)),
            },
            RequestKind::Call { raw: false } => Notice::Reply {
                request,
                result: wire::decode(&sample)
                    .map(Data::Value)
                    .map_err(ReplyError::Undecodable),
            },
            RequestKind::Schema => Notice::Schema {
                request,
                result: wire::decode(&sample).map_err(ReplyError::Undecodable),
            },
            RequestKind::Discover => {
                match wire::decode(&sample)
                    .ok()
                    .as_ref()
                    .and_then(wire::read_metadata)
                {
                    Some(metadata) => Notice::Discovered { request, metadata },
                    None => return,
                }
            }
            RequestKind::Rehydrate(object) => {
                let Some(notice) = self.rehydrate_reply(object, &key, sample, replier) else {
                    return;
                };
                notice
            }
        };
        self.notices.push_back(notice);
    }

    fn rehydrate_reply(
        &mut self,
        object: ObjectId,
        key: &str,
        sample: Sample,
        replier: Source,
    ) -> Option<Notice> {
        let local = self.zid.clone();
        let obj = self
            .objects
            .get_mut(&object)
            .expect("closing drops its requests");
        let path = key.strip_prefix(&format!("{}/", obj.topic));
        let live = obj
            .role
            .mirror_mut()
            .is_some_and(|(owner, rehydrating, _, _)| {
                if owner.is_none() {
                    *owner = match replier {
                        Source::Remote(z) => Some(z),
                        Source::Local(_) => Some(local),
                        Source::Unknown => None,
                    };
                }
                rehydrating
                    .as_ref()
                    .zip(path)
                    .is_some_and(|((_, seen), path)| seen.contains(path))
            });
        let path = path?;
        if obj.schema.field(path).is_none() || live {
            return None;
        }
        Some(obj.notice(object, path, sample, Origin::Rehydrate))
    }

    fn on_request_done(&mut self, request: RequestId) {
        let Some(req) = self.requests.remove(&request) else { return };
        let end = match (req.failed, req.done) {
            (true, _) => RequestEnd::Failed,
            (false, true) => RequestEnd::Done,
            (false, false) => RequestEnd::CutShort,
        };
        let notice = match req.kind {
            RequestKind::Rehydrate(object) => {
                let obj = self
                    .objects
                    .get_mut(&object)
                    .expect("closing drops its requests");
                let complete = end == RequestEnd::Done;
                let (_, rehydrating, stale, again) =
                    obj.role.mirror_mut().expect("only mirrors fetch");
                *rehydrating = None;
                *stale = !complete;
                let retry = !complete && *again;
                self.notices
                    .push_back(Notice::Rehydrated { object, complete });
                if retry {
                    self.start_rehydrate(object);
                }
                return;
            }
            _ => Notice::RequestEnded { request, end },
        };
        self.notices.push_back(notice);
    }

    fn on_liveliness(&mut self, subscriber: EntityId, key: &str, alive: bool) {
        let Some((object, Route::Alive)) = self.routes.get(&subscriber).cloned() else { return };
        let obj = self.objects.get_mut(&object).expect("routed");
        let Role::Mirror { owner, tokens, .. } = &mut obj.role else { return };
        let zid = Zid(key.rsplit('/').next().unwrap_or_default().to_owned());
        let was = !tokens.is_empty();
        if alive {
            tokens.insert(zid.clone());
            *owner = Some(zid);
        } else {
            tokens.remove(&zid);
            if owner.as_ref() == Some(&zid)
                && let Some(next) = tokens.first()
            {
                *owner = Some(next.clone());
            }
        }
        let now = !tokens.is_empty();
        if was != now {
            self.notices.push_back(Notice::Alive { object, alive: now });
        }
        // A mirror still without the owner's state fetches it now that an owner is reachable.
        let (_, rehydrating, stale, again) = obj.role.mirror_mut().expect("matched above");
        let fetch = alive && *stale && rehydrating.is_none();
        *again |= alive && *stale && rehydrating.is_some();
        if fetch {
            self.start_rehydrate(object);
        }
    }

    fn on_matching(&mut self, publisher: EntityId, matched: bool) {
        let Some((object, Route::FieldPub(path))) = self.routes.get(&publisher).cloned() else {
            return;
        };
        let obj = self.objects.get_mut(&object).expect("routed");
        let Role::Owner { listening } = &mut obj.role else { return };
        let was = listening.insert(path.clone(), matched).unwrap_or(false);
        if was != matched {
            self.notices.push_back(Notice::Listeners {
                object,
                path,
                listening: matched,
            });
        }
    }
}

fn queryable(key: String) -> impl FnOnce(EntityId) -> Command {
    move |id| Command::DeclareQueryable { id, key }
}

/// A call's arguments from its payload: one YAML mapping by name, or nothing.
fn read_args(payload: Option<&Sample>, key_args: &[(String, String)]) -> Result<Value, String> {
    let Some(sample) = payload.filter(|s| !s.payload.bytes().is_empty()) else {
        return Ok(Value::Map(Vec::new()));
    };
    let Value::Map(map) = wire::decode(sample)? else {
        return Err("the arguments must be a mapping".into());
    };
    for (name, _) in &map {
        let Value::String(name) = name else {
            return Err("argument names must be strings".into());
        };
        if key_args.iter().any(|(k, _)| k == name) {
            return Err(format!(
                "`{name}` is in the key, so it can't be in the payload too"
            ));
        }
    }
    Ok(Value::Map(map))
}
