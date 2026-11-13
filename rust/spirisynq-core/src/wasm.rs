//! WASM code on objects: what the engine does around running it.
//!
//! The engine doesn't run WASM itself; a [`WasmHost`] does (Extism natively, the browser's own
//! engine in the browser). For a property or method in the type's `x-wasm`, the engine takes a
//! snapshot of the object's fields into a [`Frame`] ([`Session::wasm_job`](crate::Session::wasm_job)),
//! the host runs the code against it, and the engine applies the fields the code set and reads
//! its result ([`Session::finish_wasm`](crate::Session::finish_wasm)). Nothing is locked while
//! the code runs.
//!
//! Code sees values as JSON: tags are dropped and bytes are base64 strings, as the schema
//! describes them. What comes back is read the other way, guided by the schema: a string whose
//! schema says `x-yaml-tag: "!!binary"` becomes bytes, and a string or collection whose schema
//! has a local tag such as `!Link` becomes that tagged value, and a whole number whose schema
//! says `number` but not `integer` becomes a float. In an `anyOf`, the first branch whose `type`
//! fits the value guides it.
//!
//! The field hooks run the same way. An `after_receive` reads the [`Sample`] that arrived
//! ([`Frame::sample`], [`Frame::decode_sample`]) and returns the field's value; a `before_send`
//! gets the value and sends it exactly once ([`Frame::send`], [`Frame::send_value`]); an
//! `on_change_mirror` gets the value that arrived, and sets fields on that mirror only.
//!
//! So do the hooks on a remote method's caller. A `before_call` gets the caller's arguments and
//! sends the call exactly once ([`Frame::send_args`], [`Frame::send_call`]). An `after_call`
//! reads the call's replies as they arrive ([`Frame::next_reply`], which waits for each, from
//! the binding's [`Replies`]) and yields results ([`Frame::give`]).

use std::fmt;
use std::sync::Arc;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use serde_json::{Map, Number, Value as Json};
use spirisynq_schema::{Role, TypeSchema, WasmEntry};
use spirisynq_yaml::Value;

use crate::session::CallOptions;
use crate::transport::Sample;
use crate::{Error, ObjectId, ReplyError, wire};

/// Runs WASM code. See the module docs.
pub trait WasmHost {
    /// Run `code`'s entry function. Its input is [`Frame::input`]; host functions read and set
    /// fields and read arguments through `frame`. The entry's output is its result as JSON
    /// (empty for `null`), or why it failed.
    fn run(&mut self, code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String>;
}

/// A call's replies, for its `after_call` while it runs, and where the results it yields go.
/// The binding feeds it from the call's [`Notice::Reply`](crate::Notice::Reply)s, from
/// whichever thread it likes: the code runs on another, and waits in [`Replies::next`].
pub trait Replies: Send {
    /// The next reply, successful or not, waiting until it arrives; `None` once the call has
    /// ended. A call that was cut short can end with an error of the binding's choosing, such
    /// as a timeout.
    fn next(&mut self) -> Option<Result<Sample, ReplyError>>;
    /// A result the code yielded, as soon as it's yielded, for a method that may reply with any
    /// number of values (`x-generator`). A method that replies with one value has it as the
    /// job's result instead.
    fn give(&mut self, value: Value);
}

/// What a piece of code can see and change while it runs: the object's fields, as JSON, and its
/// arguments, and for a hook the sample or the call's replies. Fields it sets, and what it
/// sends, are applied when it finishes successfully, and not at all if it fails.
#[derive(Debug, Default)]
pub struct Frame {
    fields: Map<String, Json>,
    /// Every field key, and whether it has its own encoding (code can't use those).
    paths: Vec<(String, bool)>,
    args: Map<String, Json>,
    writes: Vec<(String, Json)>,
    /// The role, if it can't set fields.
    read_only: Option<String>,
    /// An `after_receive`'s sample.
    sample: Option<Sample>,
    /// A `before_send` or `before_call` sends; this is what it sent.
    sender: Option<Role>,
    sent: Option<Sent>,
    /// An `after_call`'s replies.
    stream: Option<Stream>,
}

/// What a `before_send` or `before_call` sent.
#[derive(Debug, Clone, PartialEq)]
enum Sent {
    Raw(Sample),
    Value(Json),
    Args(Map<String, Json>),
    Call { sample: Sample, parameters: String },
}

/// An `after_call`'s replies, and what it yields.
struct Stream {
    replies: Box<dyn Replies>,
    /// Every reply so far, by its index.
    received: Vec<Sample>,
    /// The error reply handed to the code, and whether it let it through.
    error: Option<ReplyError>,
    escaped: bool,
    ended: bool,
    schema: Arc<TypeSchema>,
    /// The method's result schema, and whether it yields any number of results.
    returns: Value,
    generator: bool,
    /// What it yielded, for a method that replies with one value.
    kept: Vec<Value>,
}

impl fmt::Debug for Stream {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Stream")
            .field("received", &self.received.len())
            .field("error", &self.error)
            .field("ended", &self.ended)
            .finish_non_exhaustive()
    }
}

/// What an `after_call` gets when it asks for the next reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NextReply {
    /// A successful reply: read it by its index with [`Frame::reply`].
    Reply(usize),
    /// An error reply, for the code to raise: its exception's type and message.
    Error { kind: String, message: String },
    /// The call has ended.
    Ended,
}

/// "a" or "an", for a role's name.
fn article(role: &str) -> &'static str {
    if role.starts_with(['a', 'e', 'i', 'o', 'u']) {
        "an"
    } else {
        "a"
    }
}

impl Frame {
    /// The entry function's input: `{"self": <fields>, "args": <arguments>}`. Fields without a
    /// value, and fields with their own encoding, are left out of `self`.
    pub fn input(&self) -> Vec<u8> {
        let mut input = Map::new();
        input.insert("self".into(), Json::Object(self.fields.clone()));
        input.insert("args".into(), Json::Object(self.args.clone()));
        Json::Object(input).to_string().into_bytes()
    }

    fn check(&self, path: &str) -> Result<(), String> {
        match self.paths.iter().find(|(p, _)| p == path) {
            Some((_, false)) => Ok(()),
            Some((_, true)) => Err(format!("field `{path}` has its own encoding")),
            None => Err(format!("no field `{path}`")),
        }
    }

    /// A field's value, as the code last set it: `null` if it has none.
    pub fn field(&self, path: &str) -> Result<Json, String> {
        self.check(path)?;
        let mut parts = path.split('/');
        let mut node = parts.next().and_then(|top| self.fields.get(top));
        for name in parts {
            node = node.and_then(Json::as_object).and_then(|m| m.get(name));
        }
        Ok(node.cloned().unwrap_or(Json::Null))
    }

    /// Set a field. Only a method and an `on_change_mirror` can.
    pub fn set_field(&mut self, path: &str, value: Json) -> Result<(), String> {
        if let Some(role) = &self.read_only {
            let a = article(role);
            return Err(format!("{a} {role} can't set fields (`{path}`)"));
        }
        self.check(path)?;
        let mut map = &mut self.fields;
        let mut names: Vec<&str> = path.split('/').collect();
        let last = names.pop().expect("split yields a chunk");
        for (i, name) in names.iter().enumerate() {
            map = match map.get_mut(*name).and_then(Json::as_object_mut) {
                Some(m) => m,
                None => {
                    let parent = names[..=i].join("/");
                    return Err(format!("`{parent}` isn't there to hold `{path}`"));
                }
            };
        }
        map.insert(last.to_owned(), value.clone());
        self.writes.retain(|(p, _)| p != path);
        self.writes.push((path.to_owned(), value));
        Ok(())
    }

    /// The arguments, by name.
    pub fn args(&self) -> &Map<String, Json> {
        &self.args
    }

    /// An argument's value.
    pub fn arg(&self, name: &str) -> Result<Json, String> {
        self.args
            .get(name)
            .cloned()
            .ok_or_else(|| format!("no argument `{name}`"))
    }

    /// The sample that arrived, for an `after_receive`.
    pub fn sample(&self) -> Result<&Sample, String> {
        self.sample
            .as_ref()
            .ok_or_else(|| "only an after_receive has a sample to read".into())
    }

    /// The sample decoded the usual way, from YAML, as JSON.
    pub fn decode_sample(&self) -> Result<Json, String> {
        decode(self.sample()?)
    }

    fn sending(&mut self, role: Role, sent: Sent) -> Result<(), String> {
        let what = match role {
            Role::BeforeSend => "a field's value",
            _ => "a call",
        };
        if self.sender.as_ref() != Some(&role) {
            return Err(format!("only a {} sends {what}", role.as_str()));
        }
        if self.sent.is_some() {
            return Err(format!("a {} sends exactly once", role.as_str()));
        }
        self.sent = Some(sent);
        Ok(())
    }

    /// Send the field's value as it is, for a `before_send`.
    pub fn send(&mut self, sample: Sample) -> Result<(), String> {
        self.sending(Role::BeforeSend, Sent::Raw(sample))
    }

    /// Send a value the usual way, as YAML, for a `before_send`.
    pub fn send_value(&mut self, value: Json) -> Result<(), String> {
        self.sending(Role::BeforeSend, Sent::Value(value))
    }

    /// Send the call with these arguments, by name, the usual way, for a `before_call`.
    pub fn send_args(&mut self, args: Json) -> Result<(), String> {
        let Json::Object(args) = args else {
            return Err("the arguments sent must be a mapping".into());
        };
        self.sending(Role::BeforeCall, Sent::Args(args))
    }

    /// Send the call as it is, for a `before_call`: this payload, and these selector
    /// parameters (empty for the caller's own). The before_call's arguments named by the
    /// method's key template still fill it.
    pub fn send_call(&mut self, sample: Sample, parameters: String) -> Result<(), String> {
        self.sending(Role::BeforeCall, Sent::Call { sample, parameters })
    }

    fn stream(&mut self) -> Result<&mut Stream, String> {
        self.stream
            .as_mut()
            .ok_or_else(|| "only an after_call reads replies".into())
    }

    /// The call's next reply, for an `after_call`, waiting until it arrives.
    pub fn next_reply(&mut self) -> Result<NextReply, String> {
        let stream = self.stream()?;
        if stream.ended {
            return Ok(NextReply::Ended);
        }
        Ok(match stream.replies.next() {
            Some(Ok(sample)) => {
                stream.received.push(sample);
                NextReply::Reply(stream.received.len() - 1)
            }
            Some(Err(error)) => {
                let (kind, message) = error.parts();
                let next = NextReply::Error {
                    kind: kind.to_owned(),
                    message: message.to_owned(),
                };
                stream.error = Some(error);
                stream.escaped = false;
                next
            }
            None => {
                stream.ended = true;
                NextReply::Ended
            }
        })
    }

    /// A reply the `after_call` has had, by its index.
    pub fn reply(&mut self, index: usize) -> Result<&Sample, String> {
        let stream = self.stream()?;
        stream
            .received
            .get(index)
            .ok_or_else(|| format!("no reply {index}"))
    }

    /// A reply decoded the usual way, from YAML, as JSON.
    pub fn decode_reply(&mut self, index: usize) -> Result<Json, String> {
        decode(self.reply(index)?)
    }

    /// Say the code failed because it let the last error reply through, uncaught: the caller
    /// then raises that error as it is.
    pub fn reply_escaped(&mut self) -> Result<(), String> {
        let stream = self.stream()?;
        if stream.error.is_none() {
            return Err("no error reply came".into());
        }
        stream.escaped = true;
        Ok(())
    }

    /// A result the `after_call` yields, for the caller.
    pub fn give(&mut self, json: Json) -> Result<(), String> {
        let stream = self
            .stream
            .as_mut()
            .ok_or("only an after_call yields results")?;
        let value = Shape::new(&stream.schema).value(json, &stream.returns, BUDGET)?;
        if stream.generator {
            stream.replies.give(value);
        } else {
            stream.kept.push(value);
        }
        Ok(())
    }
}

/// A sample decoded the usual way, from YAML, as JSON.
fn decode(sample: &Sample) -> Result<Json, String> {
    let value = wire::decode(sample)?;
    to_json(&value)
}

/// Code about to run: see [`Session::wasm_job`](crate::Session::wasm_job).
#[derive(Debug)]
pub struct WasmJob {
    pub(crate) object: ObjectId,
    pub(crate) code: WasmEntry,
    pub(crate) frame: Frame,
    pub(crate) schema: Arc<TypeSchema>,
    /// The field a field hook runs for, or the remote method a call hook runs for.
    pub(crate) of: Option<String>,
}

/// A `before_call` about to run: see [`Session::before_call_job`](crate::Session::before_call_job).
#[derive(Debug)]
pub struct CallJob {
    pub(crate) job: WasmJob,
    pub(crate) options: CallOptions,
}

impl CallJob {
    pub fn job(&self) -> &WasmJob {
        &self.job
    }

    /// Run the code on `host`: see [`WasmJob::run`].
    pub fn run(&mut self, host: &mut dyn WasmHost) -> Result<Vec<u8>, String> {
        self.job.run(host)
    }
}

/// What a job's code did, as values.
pub(crate) struct Outcome {
    pub object: ObjectId,
    pub role: Role,
    pub of: Option<String>,
    pub result: Value,
    /// The fields the code set, in the order it set them.
    pub writes: Vec<(String, Value)>,
    /// What a `before_send` sent.
    pub sent: Option<Sample>,
    /// What a `before_call` sent.
    pub call: Option<CallSent>,
}

/// What a `before_call` sent, ready for the session.
pub(crate) enum CallSent {
    Args(Vec<(String, Value)>),
    Raw {
        sample: Sample,
        parameters: String,
        key_args: Vec<(String, String)>,
    },
}

impl WasmJob {
    pub fn code(&self) -> &WasmEntry {
        &self.code
    }

    pub fn frame(&self) -> &Frame {
        &self.frame
    }

    /// Run the code on `host`. Takes no lock on the session: run it wherever is convenient.
    pub fn run(&mut self, host: &mut dyn WasmHost) -> Result<Vec<u8>, String> {
        host.run(&self.code, &mut self.frame)
    }

    pub(crate) fn new(
        object: ObjectId,
        code: WasmEntry,
        schema: Arc<TypeSchema>,
        fields: Map<String, Json>,
        paths: Vec<(String, bool)>,
        args: Map<String, Json>,
    ) -> Self {
        let read_only = match code.role {
            Role::Method | Role::OnChangeMirror => None,
            _ => Some(code.role.as_str().to_owned()),
        };
        let sender = match code.role {
            Role::BeforeSend | Role::BeforeCall => Some(code.role.clone()),
            _ => None,
        };
        let frame = Frame {
            fields,
            paths,
            args,
            read_only,
            sender,
            ..Frame::default()
        };
        WasmJob {
            object,
            code,
            frame,
            schema,
            of: None,
        }
    }

    /// For a hook: the field or remote method it runs for.
    pub(crate) fn of(mut self, of: &str) -> Self {
        self.of = Some(of.to_owned());
        self
    }

    /// For an `after_receive`: the sample it reads.
    pub(crate) fn with_sample(mut self, sample: Sample) -> Self {
        self.frame.sample = Some(sample);
        self
    }

    /// For an `after_call`: the call's replies.
    pub(crate) fn with_replies(mut self, replies: Box<dyn Replies>) -> Self {
        let method = self.of.as_deref().and_then(|m| self.schema.method(m));
        let method = method.expect("checked by the caller");
        self.frame.stream = Some(Stream {
            replies,
            received: Vec::new(),
            error: None,
            escaped: false,
            ended: false,
            returns: method.returns.clone(),
            generator: method.generator,
            schema: self.schema.clone(),
            kept: Vec::new(),
        });
        self
    }

    /// What the code did. Nothing is applied if anything is wrong.
    pub(crate) fn outcome(self, output: Result<Vec<u8>, String>) -> Result<Outcome, Error> {
        let output = match (output, self.frame.stream) {
            (
                Err(_),
                Some(Stream {
                    error: Some(error),
                    escaped: true,
                    ..
                }),
            ) => return Err(Error::Reply(error)),
            (output, Some(stream)) => {
                output.map_err(Error::Wasm)?;
                return after_call(self.object, self.of, stream);
            }
            (output, None) => output.map_err(Error::Wasm)?,
        };
        let shape = Shape::new(&self.schema);
        let field_schema = |path: &str| &self.schema.field(path).expect("checked").schema;
        let result = if output.iter().all(u8::is_ascii_whitespace) {
            Value::Null
        } else {
            let json: Json = serde_json::from_slice(&output)
                .map_err(|e| Error::Wasm(format!("the result isn't JSON: {e}")))?;
            // An `after_receive` returns the field's value.
            let returns = match (&self.code.role, &self.of) {
                (Role::AfterReceive, Some(path)) => field_schema(path),
                _ => self.code.returns.as_ref().unwrap_or(&Value::Null),
            };
            shape
                .value(json, returns, BUDGET)
                .map_err(|e| Error::Wasm(format!("the result: {e}")))?
        };
        let mut writes = Vec::new();
        for (path, json) in self.frame.writes {
            let value = shape
                .value(json, field_schema(&path), BUDGET)
                .map_err(|e| Error::Wasm(format!("field `{path}`: {e}")))?;
            writes.push((path, value));
        }
        let role = self.code.role;
        if self.frame.sender.is_some() && self.frame.sent.is_none() {
            let role = role.as_str();
            return Err(Error::Wasm(format!("the {role} didn't send")));
        }
        let (mut sent, mut call) = (None, None);
        match (self.frame.sent, &self.of) {
            (Some(Sent::Raw(sample)), _) => sent = Some(sample),
            (Some(Sent::Value(json)), Some(path)) => {
                let value = shape
                    .value(json, field_schema(path), BUDGET)
                    .map_err(|e| Error::Wasm(format!("the value sent: {e}")))?;
                sent = Some(wire::yaml_sample(&value)?);
            }
            (Some(Sent::Args(args)), Some(method)) => {
                let params = &self.schema.method(method).expect("checked").parameters;
                let args = shape
                    .entries(args, params)
                    .map_err(|e| Error::Wasm(format!("the arguments sent: {e}")))?;
                call = Some(CallSent::Args(args));
            }
            (Some(Sent::Call { sample, parameters }), Some(method)) => {
                let m = self.schema.method(method).expect("checked");
                let mut key_args = Vec::new();
                for name in m.key.placeholders() {
                    match self.frame.args.get(name) {
                        Some(Json::String(s)) => key_args.push((name.to_owned(), s.clone())),
                        Some(_) => return Err(Error::NotAString(name.to_owned())),
                        None => {}
                    }
                }
                call = Some(CallSent::Raw {
                    sample,
                    parameters,
                    key_args,
                });
            }
            _ => {}
        }
        Ok(Outcome {
            object: self.object,
            role,
            of: self.of,
            result,
            writes,
            sent,
            call,
        })
    }
}

/// What an `after_call` did: its one result, for a method that replies with one value.
fn after_call(object: ObjectId, of: Option<String>, stream: Stream) -> Result<Outcome, Error> {
    let result = match (stream.generator, <[Value; 1]>::try_from(stream.kept)) {
        (true, _) => Value::Null,
        (false, Ok([value])) => value,
        (false, Err(kept)) => {
            let method = of.as_deref().unwrap_or_default();
            let n = kept.len();
            return Err(Error::Wasm(format!(
                "`{method}` replies with one value, and the after_call yielded {n}"
            )));
        }
    };
    Ok(Outcome {
        object,
        role: Role::AfterCall,
        of,
        result,
        writes: Vec::new(),
        sent: None,
        call: None,
    })
}

/// A value as code sees it.
pub(crate) fn to_json(value: &Value) -> Result<Json, String> {
    Ok(match value {
        Value::Null => Json::Null,
        Value::Bool(b) => Json::Bool(*b),
        Value::Int(i) => Json::from(*i),
        Value::Float(f) => match Number::from_f64(*f) {
            Some(n) => Json::Number(n),
            None => return Err(format!("{f} has no JSON form")),
        },
        Value::String(s) => Json::String(s.clone()),
        Value::Binary(bytes) => Json::String(BASE64.encode(bytes)),
        Value::Seq(items) => Json::Array(items.iter().map(to_json).collect::<Result<_, _>>()?),
        Value::Map(entries) => Json::Object(to_json_map(entries)?),
        Value::Tagged(t) => to_json(&t.value)?,
    })
}

/// A mapping as code sees it.
pub(crate) fn to_json_map(entries: &[(Value, Value)]) -> Result<Map<String, Json>, String> {
    let mut map = Map::new();
    for (k, v) in entries {
        let Value::String(k) = k else {
            return Err("a mapping key that isn't a string has no JSON form".into());
        };
        map.insert(k.clone(), to_json(v)?);
    }
    Ok(map)
}

/// How many `$ref`s and `anyOf`s reading one value may follow without going deeper into the
/// value: a schema that refers to itself stops guiding instead of looping.
const BUDGET: usize = 64;

/// Reads JSON as values, guided by schemas that may refer to the type's `$defs`.
struct Shape<'a> {
    defs: Option<&'a Value>,
}

/// A schema's keyword.
fn get<'a>(schema: &'a Value, key: &str) -> Option<&'a Value> {
    let Value::Map(entries) = schema else { return None };
    entries
        .iter()
        .find(|(k, _)| matches!(k, Value::String(s) if s == key))
        .map(|(_, v)| v)
}

fn text(value: &Value) -> Option<&str> {
    match value {
        Value::String(s) => Some(s),
        _ => None,
    }
}

/// Whether a JSON value is of a schema's `type`, if it gives one.
fn fits(json: &Json, schema: &Value) -> bool {
    let kind = |t: &Value| match (text(t), json) {
        (Some("null"), Json::Null)
        | (Some("boolean"), Json::Bool(_))
        | (Some("number"), Json::Number(_))
        | (Some("string"), Json::String(_))
        | (Some("array"), Json::Array(_))
        | (Some("object"), Json::Object(_)) => true,
        (Some("integer"), Json::Number(n)) => n.is_i64() || n.is_u64(),
        _ => false,
    };
    match get(schema, "type") {
        None => true,
        Some(Value::Seq(types)) => types.iter().any(kind),
        Some(t) => kind(t),
    }
}

/// Whether a schema's `type` says a number but not an integer: `3` is then `3.0`, as code in a
/// language without integers (JavaScript) writes it.
fn wants_float(schema: &Value) -> bool {
    let named = |t: &str| match get(schema, "type") {
        Some(Value::Seq(types)) => types.iter().any(|x| text(x) == Some(t)),
        Some(other) => text(other) == Some(t),
        None => false,
    };
    named("number") && !named("integer")
}

impl<'a> Shape<'a> {
    fn new(schema: &'a TypeSchema) -> Self {
        Shape {
            defs: get(schema.raw(), "$defs"),
        }
    }

    /// The schema a `$ref` chain ends at.
    fn resolve(&self, mut schema: &'a Value, budget: &mut usize) -> &'a Value {
        while *budget > 0 {
            let target = get(schema, "$ref")
                .and_then(text)
                .and_then(|r| r.strip_prefix("#/$defs/"))
                .map(|name| name.replace("~1", "/").replace("~0", "~"));
            let Some(def) = target.and_then(|name| get(self.defs?, &name)) else { break };
            schema = def;
            *budget -= 1;
        }
        schema
    }

    /// A mapping's entries, each read against its property's schema.
    fn properties(
        &self,
        map: Map<String, Json>,
        schema: &'a Value,
        budget: usize,
    ) -> Result<Vec<(String, Value)>, String> {
        let props = get(schema, "properties");
        let other = get(schema, "additionalProperties").unwrap_or(&Value::Null);
        let mut entries = Vec::new();
        for (k, j) in map {
            let sub = props.and_then(|p| get(p, &k)).unwrap_or(other);
            let v = self.value(j, sub, budget)?;
            entries.push((k, v));
        }
        Ok(entries)
    }

    /// A mapping of arguments, read against an object schema of parameters.
    fn entries(
        &self,
        map: Map<String, Json>,
        schema: &'a Value,
    ) -> Result<Vec<(String, Value)>, String> {
        let mut budget = BUDGET;
        let schema = self.resolve(schema, &mut budget);
        self.properties(map, schema, budget)
    }

    fn value(&self, json: Json, schema: &'a Value, mut budget: usize) -> Result<Value, String> {
        let mut schema = self.resolve(schema, &mut budget);
        while budget > 0
            && let Some(Value::Seq(branches)) = get(schema, "anyOf")
        {
            budget -= 1;
            let mut fitting = branches.iter().map(|b| self.resolve(b, &mut budget));
            schema = fitting.find(|b| fits(&json, b)).unwrap_or(&Value::Null);
        }
        let tag = get(schema, "x-yaml-tag").and_then(text);
        let inner = match json {
            Json::String(s) if tag == Some("!!binary") => {
                return BASE64
                    .decode(&s)
                    .map(Value::Binary)
                    .map_err(|e| format!("not base64: {e}"));
            }
            Json::Null => Value::Null,
            Json::Bool(b) => Value::Bool(b),
            Json::Number(n) => match n.as_i64() {
                Some(i) if !wants_float(schema) => Value::Int(i),
                _ => Value::Float(n.as_f64().unwrap_or(f64::NAN)),
            },
            Json::String(s) => Value::String(s),
            Json::Array(items) => {
                let item = get(schema, "items").unwrap_or(&Value::Null);
                let items = items.into_iter().map(|j| self.value(j, item, budget));
                Value::Seq(items.collect::<Result<_, _>>()?)
            }
            Json::Object(map) => {
                let entries = self.properties(map, schema, budget)?;
                Value::Map(
                    entries
                        .into_iter()
                        .map(|(k, v)| (Value::String(k), v))
                        .collect(),
                )
            }
        };
        // Only strings and collections take a local tag: a null where a link may be stays null.
        let taggable = matches!(inner, Value::String(_) | Value::Seq(_) | Value::Map(_));
        Ok(match tag.and_then(|t| t.strip_prefix('!')) {
            Some(local) if taggable && !local.starts_with('!') => Value::tagged(local, inner),
            _ => inner,
        })
    }
}
