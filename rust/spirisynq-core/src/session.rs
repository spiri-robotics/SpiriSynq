//! The engine: one [`Session`] per zenoh session.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::iter;
use std::sync::Arc;

use spirisynq_schema::{
    RemoteMethod, Role as CodeRole, TypeId, TypeSchema, WasmEntry, check_topic,
};
use spirisynq_yaml::Value;

use crate::Error;
use crate::keyexpr;
use crate::notice::{Data, IncomingCall, Notice, ObjectId, Origin, ReplyError, RequestEnd};
use crate::store::{Store, Stored};
use crate::transport::{Command, EntityId, Event, Qos, QueryId, RequestId, Sample, Source, Zid};
use crate::wasm::{CallJob, CallSent, Named, Replies, WasmHost, WasmJob};
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
    /// Pass replies on as [`Data::Raw`], for an `after_call` hook to decode. Always so when the
    /// method has a WASM `after_call`.
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

    /// The type's WASM code `name`.
    fn code(&self, name: &str) -> Result<&WasmEntry, Error> {
        self.schema
            .wasm()
            .iter()
            .find(|c| c.name == name)
            .ok_or_else(|| Error::UnknownCode(name.to_owned()))
    }

    fn method(&self, name: &str) -> Result<&RemoteMethod, Error> {
        self.schema
            .method(name)
            .ok_or_else(|| Error::UnknownMethod(name.to_owned()))
    }

    /// A call's options: its replies are raw for a WASM `after_call`.
    fn call_options(&self, method: &str, mut options: CallOptions) -> CallOptions {
        options.raw_replies |= self.hooks(&CodeRole::AfterCall, method).next().is_some();
        options
    }

    /// The type's WASM hooks of `role` on `of`.
    fn hooks<'a>(&'a self, role: &'a CodeRole, of: &'a str) -> impl Iterator<Item = &'a WasmEntry> {
        self.schema
            .wasm()
            .iter()
            .filter(move |c| c.role == *role && c.of.iter().any(|o| o == of))
    }

    /// A job for `code`, with a snapshot of the fields.
    fn job(&self, object: ObjectId, code: &WasmEntry, args: Named) -> Result<WasmJob, Error> {
        let mut fields = Named::default();
        let mut paths = Vec::new();
        for field in self.schema.fields() {
            let path = &field.path;
            paths.push((path.clone(), self.options(path).raw));
            if let Some(Stored::Value(value)) = self.store.top(path) {
                fields
                    .insert(path, value)
                    .map_err(|e| Error::Wasm(format!("field `{path}`: {e}")))?;
            }
        }
        Ok(WasmJob::new(
            object,
            code.clone(),
            self.schema.clone(),
            fields,
            paths,
            args,
        ))
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
    Method(String),
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
        // A field with a WASM `before_send` or `after_receive` has its own encoding.
        for code in schema.wasm() {
            if matches!(code.role, CodeRole::BeforeSend | CodeRole::AfterReceive) {
                let path = code.of[0].clone();
                fields.entry(path).or_default().raw = true;
            }
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
            self.declare(object, Route::Method(method.name.clone()), queryable(key));
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

    /// The object's type schema.
    pub fn schema(&self, object: ObjectId) -> Result<&Arc<TypeSchema>, Error> {
        Ok(&self.object(object)?.schema)
    }

    /// A field's value, if it holds one: `None` before it's set, while a parent dataclass is
    /// null, or when it has its own encoding (the binding keeps those values).
    pub fn field(&self, object: ObjectId, path: &str) -> Result<Option<&Value>, Error> {
        let obj = self.object(object)?;
        obj.check_field(path)?;
        Ok(obj.store.get(path))
    }

    /// Get ready to run the object's WASM property or method `name`, from its type's `x-wasm`,
    /// with `args`: a mapping by name, or null for none. The job holds a snapshot of the
    /// object's fields; run it with [`WasmJob::run`], anywhere, then hand the output to
    /// [`Session::finish_wasm`]. [`Session::run_wasm`] does all three.
    pub fn wasm_job(&self, object: ObjectId, name: &str, args: &Value) -> Result<WasmJob, Error> {
        let obj = self.object(object)?;
        let code = obj.code(name)?;
        if !matches!(code.role, CodeRole::Property | CodeRole::Method) {
            return Err(Error::NotRunnable(name.to_owned()));
        }
        let mut named = Named::default();
        match args {
            Value::Null => {}
            Value::Map(entries) => {
                for (k, v) in entries {
                    let Value::String(k) = k else {
                        let why = "a mapping key that isn't a string has no JSON form";
                        return Err(Error::Wasm(format!("the arguments: {why}")));
                    };
                    named
                        .insert(k, v)
                        .map_err(|e| Error::Wasm(format!("the arguments: {e}")))?;
                }
            }
            _ => return Err(Error::Wasm("the arguments must be a mapping".into())),
        }
        let args = named;
        obj.job(object, code, args)
    }

    /// The names of the object's WASM hooks of `role` on `of` (a field's key, or a remote
    /// method's name), in the schema's order.
    pub fn hooks(&self, object: ObjectId, role: &CodeRole, of: &str) -> Result<Vec<String>, Error> {
        Ok(self
            .object(object)?
            .hooks(role, of)
            .map(|code| code.name.clone())
            .collect())
    }

    /// Get ready to run a mirror's `on_change_mirror` hook `name`, for a new `value` of `field`.
    /// It's called with the value, and also the field's key if it watches several. Fields it
    /// sets change only this mirror's copy.
    pub fn on_change_job(
        &self,
        object: ObjectId,
        name: &str,
        field: &str,
        value: &Value,
    ) -> Result<WasmJob, Error> {
        let obj = self.object(object)?;
        if let Role::Owner { .. } = obj.role {
            return Err(Error::NotMirror);
        }
        let code = obj.code(name)?;
        if code.role != CodeRole::OnChangeMirror || !code.of.iter().any(|f| f == field) {
            return Err(Error::Wasm(format!("`{name}` doesn't watch `{field}`")));
        }
        let mut args = Named::default();
        args.insert("value", value)
            .map_err(|e| Error::Wasm(format!("the value: {e}")))?;
        if code.of.len() > 1 {
            args.insert("field", &Value::String(field.to_owned()))
                .expect("a string has a JSON form");
        }
        Ok(obj.job(object, code, args)?.of(field))
    }

    /// Get ready to decode a sample that arrived for `field`, with its WASM `after_receive`;
    /// `None` if it has none. Finishing the job gives the field's value.
    pub fn after_receive_job(
        &self,
        object: ObjectId,
        field: &str,
        sample: Sample,
    ) -> Result<Option<WasmJob>, Error> {
        let obj = self.object(object)?;
        obj.check_field(field)?;
        let Some(code) = obj.hooks(&CodeRole::AfterReceive, field).next() else {
            return Ok(None);
        };
        let job = obj.job(object, code, Default::default())?;
        Ok(Some(job.of(field).with_sample(sample)))
    }

    /// Get ready to send a new `value` of `field` with its WASM `before_send`; `None` if it has
    /// none. Finishing the job publishes what the code sent, as [`Session::send_raw`] does.
    pub fn before_send_job(
        &self,
        object: ObjectId,
        field: &str,
        value: &Value,
    ) -> Result<Option<WasmJob>, Error> {
        let obj = self.object(object)?;
        obj.check_field(field)?;
        let Some(code) = obj.hooks(&CodeRole::BeforeSend, field).next() else {
            return Ok(None);
        };
        let mut args = Named::default();
        args.insert("value", value)
            .map_err(|e| Error::Wasm(format!("the value: {e}")))?;
        Ok(Some(obj.job(object, code, args)?.of(field)))
    }

    /// Finish a job, and give its result. If the code failed, or its result or a field it set
    /// can't be read, nothing is done. Otherwise, by its role:
    ///
    /// - a property or method sets the fields its code set, as [`Session::set_field`] does;
    /// - an `on_change_mirror` sets them on the mirror only, as [`Session::set_field_local`]
    ///   does;
    /// - an `after_receive`'s result is the field's value, for the binding to apply;
    /// - a `before_send` publishes what its code sent, as [`Session::send_raw`] does.
    pub fn finish_wasm(
        &mut self,
        job: WasmJob,
        output: Result<Vec<u8>, String>,
    ) -> Result<Value, Error> {
        let done = job.outcome(output)?;
        let object = done.object;
        for (path, value) in done.writes {
            if done.role == CodeRole::OnChangeMirror {
                self.set_field_local(object, &path, value)?;
            } else {
                self.set_field(object, &path, value)?;
            }
        }
        if let (Some(sample), Some(field)) = (done.sent, done.of) {
            self.send_raw(object, &field, sample)?;
        }
        Ok(done.result)
    }

    /// Run the object's WASM property or method `name` on `host`, and finish it: see
    /// [`Session::wasm_job`].
    pub fn run_wasm(
        &mut self,
        host: &mut dyn WasmHost,
        object: ObjectId,
        name: &str,
        args: &Value,
    ) -> Result<Value, Error> {
        let job = self.wasm_job(object, name, args)?;
        self.run_job(host, job)
    }

    fn run_job(&mut self, host: &mut dyn WasmHost, mut job: WasmJob) -> Result<Value, Error> {
        let output = job.run(host);
        self.finish_wasm(job, output)
    }

    /// Run every WASM `on_change_mirror` watching `field` on a mirror, in the schema's order,
    /// for its new `value`. Each sees what the ones before it set. Stops at the first that
    /// fails.
    pub fn run_on_change(
        &mut self,
        host: &mut dyn WasmHost,
        object: ObjectId,
        field: &str,
        value: &Value,
    ) -> Result<(), Error> {
        for name in self.hooks(object, &CodeRole::OnChangeMirror, field)? {
            let job = self.on_change_job(object, &name, field, value)?;
            self.run_job(host, job)?;
        }
        Ok(())
    }

    /// Decode a sample that arrived for `field` with its WASM `after_receive`: the field's
    /// value, or `None` if it has none.
    pub fn run_after_receive(
        &mut self,
        host: &mut dyn WasmHost,
        object: ObjectId,
        field: &str,
        sample: Sample,
    ) -> Result<Option<Value>, Error> {
        match self.after_receive_job(object, field, sample)? {
            Some(job) => self.run_job(host, job).map(Some),
            None => Ok(None),
        }
    }

    /// Send a new `value` of `field` with its WASM `before_send`. Whether it has one: if not,
    /// nothing is sent.
    pub fn run_before_send(
        &mut self,
        host: &mut dyn WasmHost,
        object: ObjectId,
        field: &str,
        value: &Value,
    ) -> Result<bool, Error> {
        match self.before_send_job(object, field, value)? {
            Some(job) => self.run_job(host, job).map(|_| true),
            None => Ok(false),
        }
    }

    /// Get ready to make a call to `method` with its WASM `before_call`, which gets `args` and
    /// sends the call; `None` if it has none. Finish it with [`Session::finish_call`].
    pub fn before_call_job(
        &self,
        object: ObjectId,
        method: &str,
        args: &[(String, Value)],
        options: CallOptions,
    ) -> Result<Option<CallJob>, Error> {
        let obj = self.object(object)?;
        obj.method(method)?;
        let Some(code) = obj.hooks(&CodeRole::BeforeCall, method).next() else {
            return Ok(None);
        };
        let mut map = Named::default();
        for (name, value) in args {
            map.insert(name, value)
                .map_err(|e| Error::Wasm(format!("`{name}`: {e}")))?;
        }
        let job = obj.job(object, code, map)?.of(method);
        Ok(Some(CallJob { job, options }))
    }

    /// Finish a `before_call`: make the call it sent, as [`Session::call`] does, or
    /// [`Session::call_raw`] for a raw send. If the code failed, or didn't send, nothing is
    /// sent.
    pub fn finish_call(
        &mut self,
        job: CallJob,
        output: Result<Vec<u8>, String>,
    ) -> Result<RequestId, Error> {
        let CallJob { job, mut options } = job;
        let done = job.outcome(output)?;
        let method = done.of.expect("set for a before_call");
        match done.call.expect("a before_call that finishes has sent") {
            CallSent::Args(args) => self.call(done.object, &method, args, options),
            CallSent::Raw {
                sample,
                parameters,
                key_args,
            } => {
                if !parameters.is_empty() {
                    options.parameters = parameters;
                }
                let key_args: Vec<_> = key_args
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.as_str()))
                    .collect();
                self.call_raw(done.object, &method, &key_args, Some(sample), options)
            }
        }
    }

    /// Call a remote method, with its WASM `before_call` if it has one, run on `host`; else as
    /// [`Session::call`] does.
    pub fn run_call(
        &mut self,
        host: &mut dyn WasmHost,
        object: ObjectId,
        method: &str,
        args: Vec<(String, Value)>,
        options: CallOptions,
    ) -> Result<RequestId, Error> {
        match self.before_call_job(object, method, &args, options.clone())? {
            Some(mut job) => {
                let output = job.run(host);
                self.finish_call(job, output)
            }
            None => self.call(object, method, args, options),
        }
    }

    /// Get ready to read a call's replies to `method` with its WASM `after_call`; `None` if it
    /// has none. The code reads them from `replies` as they arrive, so run it on a thread of its
    /// own while the binding feeds them in, then finish it with [`Session::finish_wasm`]: its
    /// result is the call's, for a method that replies with one value. A generator method's
    /// results go to `replies` as they're yielded. If the code let an error reply through,
    /// finishing gives [`Error::Reply`].
    pub fn after_call_job(
        &self,
        object: ObjectId,
        method: &str,
        replies: Box<dyn Replies>,
    ) -> Result<Option<WasmJob>, Error> {
        let obj = self.object(object)?;
        obj.method(method)?;
        let Some(code) = obj.hooks(&CodeRole::AfterCall, method).next() else {
            return Ok(None);
        };
        let job = obj.job(object, code, Default::default())?.of(method);
        Ok(Some(job.with_replies(replies)))
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
        let m = obj.method(method)?;
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
        let options = obj.call_options(method, options);
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
        let m = obj.method(method)?;
        let key = wire::key(&obj.topic, &m.key.fill(key_args)?);
        let options = obj.call_options(method, options);
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
        let accepts = |rel: &str| match &route {
            Route::Rehydrate => rel == REHYDRATE,
            Route::Metadata(key) | Route::Schema(key) => wire::key(&obj.topic, rel) == *key,
            Route::Method(name) => {
                (obj.schema.method_for_key(rel)).is_some_and(|(m, _)| m.name == *name)
            }
            _ => false,
        };
        let Some(rel) = member(&key, &obj.topic, accepts) else {
            // `**`, `robot/**` or a query for another member: nothing this queryable answers.
            self.commands.push_back(Command::Finish { query });
            return;
        };
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
            _ => {
                let rel = rel.to_owned();
                self.on_call(object, query, &rel, parameters, payload);
            }
        }
    }

    fn on_call(
        &mut self,
        object: ObjectId,
        query: QueryId,
        rel: &str,
        parameters: String,
        payload: Option<Sample>,
    ) {
        let obj = &self.objects[&object];
        let key = wire::key(&obj.topic, rel);
        let (method, key_args) = obj.schema.method_for_key(rel).expect("accepted");
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

/// The member key, under an object's `topic`, that a query asks for: the query must end with it
/// written out (wildcards stand only for the topic part), `accepts` must take it, and the
/// object's key for it must match the query. So `**/set_limit` and `fleet/*/battery/set_limit`
/// reach `set_limit`, and `**` or `robot/**` reaches nothing. The longest such key wins.
fn member<'a>(query: &'a str, topic: &str, accepts: impl Fn(&str) -> bool) -> Option<&'a str> {
    let starts = iter::once(0).chain(query.match_indices('/').map(|(i, _)| i + 1));
    starts
        .map(|i| &query[i..])
        .filter(|rel| !rel.contains('*'))
        .find(|rel| accepts(rel) && keyexpr::includes(query, &wire::key(topic, rel)))
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
