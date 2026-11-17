//! Runs SpiriSynq's WASM code natively, with Extism (wasmtime): the [`WasmHost`] for the native
//! bindings.
//!
//! Code is an Extism plug-in. Its entry function takes the frame's input (`{"self": …,
//! "args": …}`, JSON) and outputs its result as JSON, through Extism's own input and output
//! functions. It can also use these host functions, imported from `extism:host/user`. Names
//! and JSON text are Extism memory blocks (an `i64` offset), as Extism passes strings:
//!
//! | Function | Does |
//! | --- | --- |
//! | `field_get(name) -> json` | A field's value (`null` if it has none). `name` is its key: `voltage`, `gps/latitude`. |
//! | `field_get_i64(name) -> i64`, `field_get_f64(name) -> f64` | The same, for a field holding an integer or a number. |
//! | `field_set(name, json)` | Set a field. Not in a property. |
//! | `field_set_i64(name, i64)`, `field_set_f64(name, f64)` | The same, with an integer or a number. |
//! | `arg_get(name) -> json`, `arg_get_i64`, `arg_get_f64` | An argument's value. |
//! | `sample_payload() -> bytes`, `sample_encoding() -> text`, `sample_attachment() -> bytes` | In an `after_receive`: the sample that arrived. No attachment is block 0. |
//! | `sample_decode() -> json` | In an `after_receive`: the sample decoded the usual way, from YAML. |
//! | `sample_send(payload, encoding, attachment)` | In a `before_send`: send the field's value as these bytes. `attachment` 0 for none. |
//! | `sample_send_value(json)` | In a `before_send`: send a value the usual way, as YAML. |
//! | `call_send_args(json)` | In a `before_call`: send the call with these arguments, a mapping by name, the usual way. |
//! | `call_send(payload, parameters, encoding, attachment)` | In a `before_call`: send the call as these bytes, with these selector parameters (0 for the caller's). |
//! | `reply_next() -> json` | In an `after_call`: wait for the next reply. `{"reply": <index>}`, `{"error": {"kind": …, "message": …}}` for an error reply, or `null` once the call has ended. |
//! | `reply_payload(index) -> bytes`, `reply_encoding(index) -> text`, `reply_attachment(index) -> bytes`, `reply_decode(index) -> json` | In an `after_call`: a reply, as for a sample. `index` is an `i64`. |
//! | `reply_escaped()` | In an `after_call`, about to fail: the error reply it was handed is why, so the caller gets that error as it is. |
//! | `call_yield(json)` | In an `after_call`: yield a result to the caller. |
//! | `marks() -> json` | Where the JSON `field_get`, `arg_get`, `sample_decode` or `reply_decode` last returned holds bytes, as base64 strings, and integer keys, as strings: `{"bytes": [...], "int_keys": [...]}`, each a list of paths of keys and indexes (an integer key's is its entry's), left out if empty. Block 0 if there are none. |
//! | `mark(json)` | Mark where the code put bytes and integer keys, the same way, in the JSON it hands over next (to `field_set`, `sample_send_value`, `call_send_args` or `call_yield`, or as its output if nothing else first). |
//!
//! The input has its marks too, as `"marks"`, with paths from the input's root such as
//! `["self", "image"]`, left out if there are none.
//!
//! Block 0 is empty bytes (Extism gives an empty block offset 0), so an empty attachment is no
//! attachment. A host function that can't do what it's asked (no such field, not an integer, a
//! property setting a field, a `before_send` sending twice) stops the code with an error saying
//! why.
//!
//! The time limit counts only while code runs: not while an `after_call` waits for a reply.
//!
//! The `python`, `lua` and `javascript` loaders run source in a bundled interpreter, itself an Extism plug-in (see
//! `interpreters/`): its input is `{"source": …, "entry": …, "args": [...]}`, the arguments in
//! the order of the entry's `params` (or as given, without `params`), and the source's entry
//! function is called with `self` first. `self` reads and sets fields through the host
//! functions. A hook's arguments go in the order the engine gives them (its `params` are
//! ignored), and an `after_receive` or `before_send` also gets a sample object, last, marked by
//! `"sample": "in"` or `"out"` in the input. A `before_call`'s arguments go in the order of its
//! `params` too, after a call object, marked by `"call": "send"`; an `after_call` gets the
//! call's replies as its one argument, marked by `"call": "replies"`, and each result it yields
//! goes to the caller. Its `"marks"` say where its bytes and integer keys are, as for any input
//! (`["args", 0]`), so the runner hands them to the code as the language's own; the runner marks
//! them in what it hands back with `mark`.
//!
//! Code is sandboxed: it gets WASI with no files, no environment and no output (its stdout and
//! stderr go nowhere), and no network. Each call is limited in time and memory ([`Limits`]),
//! and starts afresh: nothing code keeps from one call to the next. Modules are compiled once
//! per [`Host`], each interpreter once for all the code it runs.

#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use extism::{
    CancelHandle, CompiledPlugin, CurrentPlugin, Function, Manifest, PTR, Plugin, PluginBuilder,
    UserData, Val, ValType, Wasm,
};
use serde_json::{Number, Value as Json, json};
use spirisynq_core::{Frame, NextReply, Payload, Sample, WasmHost};
use spirisynq_schema::{Loader, Role, Source, WasmEntry};
use spirisynq_yaml::Value;

/// The bundled interpreters, built by `make interpreters`: MicroPython, Lua 5.4 and QuickJS-ng.
const PYTHON: &[u8] = include_bytes!("../interpreters/python.wasm");
const LUA: &[u8] = include_bytes!("../interpreters/lua.wasm");
const JAVASCRIPT: &[u8] = include_bytes!("../interpreters/javascript.wasm");

/// What one call of a piece of code may use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// How long it may run.
    pub timeout: Duration,
    /// How much memory it may have, in 64 KiB WebAssembly pages.
    pub memory_pages: u32,
}

impl Default for Limits {
    /// One second and 64 MiB.
    fn default() -> Self {
        Limits {
            timeout: Duration::from_secs(1),
            memory_pages: 1024,
        }
    }
}

/// The frame of the code that's running, for the host functions.
type Slot = UserData<Option<Frame>>;

/// The time limit. Extism's own counts the wall clock, waiting included, so the host keeps its
/// own: it runs only while code runs, and cancels the plug-in when it's used up.
#[derive(Default)]
struct Clock {
    watch: Mutex<Watch>,
    wake: Condvar,
}

#[derive(Default)]
struct Watch {
    /// The running plug-in.
    plugin: Option<CancelHandle>,
    /// When its time is up; none while it waits.
    deadline: Option<Instant>,
    /// What's left of it while it waits.
    left: Duration,
    stop: bool,
}

/// How soon a cancel is sent again, in case the first came before the call had started.
const RETRY: Duration = Duration::from_millis(10);

impl Clock {
    fn watch(&self) -> MutexGuard<'_, Watch> {
        self.watch.lock().expect("nothing panics holding it")
    }

    /// The watcher's thread: cancels the running plug-in when its time is up.
    fn run(&self) {
        let mut watch = self.watch();
        while !watch.stop {
            let now = Instant::now();
            watch = match watch.deadline {
                Some(deadline) if now >= deadline => {
                    if let Some(plugin) = &watch.plugin {
                        let _ = plugin.cancel();
                    }
                    watch.deadline = Some(now + RETRY);
                    watch
                }
                Some(deadline) => {
                    let wait = deadline - now;
                    let woken = self.wake.wait_timeout(watch, wait);
                    woken.expect("nothing panics holding it").0
                }
                None => self.wake.wait(watch).expect("nothing panics holding it"),
            };
        }
    }

    fn start(&self, plugin: CancelHandle, limit: Duration) {
        let mut watch = self.watch();
        watch.plugin = Some(plugin);
        watch.deadline = Some(Instant::now() + limit);
        self.wake.notify_one();
    }

    fn finish(&self) {
        let mut watch = self.watch();
        watch.plugin = None;
        watch.deadline = None;
    }

    fn pause(&self) {
        let mut watch = self.watch();
        if let Some(deadline) = watch.deadline.take() {
            watch.left = deadline.saturating_duration_since(Instant::now());
        }
    }

    fn resume(&self) {
        let mut watch = self.watch();
        watch.deadline = Some(Instant::now() + watch.left);
        self.wake.notify_one();
    }
}

/// Runs WASM code with Extism. See the crate docs.
pub struct Host {
    limits: Limits,
    slot: Slot,
    clock: Arc<Clock>,
    watcher: Option<JoinHandle<()>>,
    /// By the code's hash, or by the interpreter's name.
    compiled: HashMap<String, CompiledPlugin>,
}

impl Host {
    pub fn new(limits: Limits) -> Self {
        let clock = Arc::new(Clock::default());
        let watching = clock.clone();
        Host {
            limits,
            slot: UserData::new(None),
            clock,
            watcher: Some(std::thread::spawn(move || watching.run())),
            compiled: HashMap::new(),
        }
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// A fresh plug-in for `code`, compiled on first use.
    fn plugin(&mut self, code: &WasmEntry) -> Result<Plugin, String> {
        let (key, module) = match interpreter(&code.loader) {
            Some((name, module)) => (name.to_owned(), Module::Bundled(module)),
            None => (code.hash.clone(), Module::Own(code)),
        };
        if !self.compiled.contains_key(&key) {
            let manifest = Manifest::new([Wasm::data(module.bytes()?)])
                .with_memory_max(self.limits.memory_pages);
            let compiled = PluginBuilder::new(manifest)
                .with_wasi(true)
                .with_cache_disabled()
                .with_functions(functions(&self.slot, &self.clock))
                .compile()
                .map_err(cause)?;
            self.compiled.insert(key.clone(), compiled);
        }
        Plugin::new_from_compiled(&self.compiled[&key]).map_err(cause)
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        self.clock.watch().stop = true;
        self.clock.wake.notify_one();
        if let Some(watcher) = self.watcher.take() {
            let _ = watcher.join();
        }
    }
}

impl Default for Host {
    fn default() -> Self {
        Host::new(Limits::default())
    }
}

impl WasmHost for Host {
    fn run(&mut self, code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String> {
        // An interpreter's export is `run`; it calls the entry itself.
        let (export, input) = match interpreter(&code.loader) {
            Some(_) => ("run", interpreter_input(code, &mut *frame)?),
            None => (code.entry.as_str(), frame.input()),
        };
        let slot = self.slot.get().expect("a Rust value");
        // The frame is in the slot whenever WASM runs, starting the plug-in included (a
        // module's start function runs then).
        *lock(&slot) = Some(std::mem::take(frame));
        let output = self.plugin(code).and_then(|mut plugin| {
            self.clock
                .start(plugin.cancel_handle(), self.limits.timeout);
            let output = plugin.call::<&[u8], &[u8]>(export, &input);
            self.clock.finish();
            output.map(<[u8]>::to_vec).map_err(cause)
        });
        *frame = lock(&slot).take().expect("put there above");
        output
    }
}

fn lock(slot: &Mutex<Option<Frame>>) -> std::sync::MutexGuard<'_, Option<Frame>> {
    slot.lock().expect("nothing panics holding it")
}

/// The bundled interpreter a loader runs its source in, if it's one of them.
fn interpreter(loader: &Loader) -> Option<(&'static str, &'static [u8])> {
    match loader {
        Loader::Python => Some(("python", PYTHON)),
        Loader::Lua => Some(("lua", LUA)),
        Loader::JavaScript => Some(("javascript", JAVASCRIPT)),
        _ => None,
    }
}

enum Module<'a> {
    Bundled(&'static [u8]),
    Own(&'a WasmEntry),
}

impl Module<'_> {
    fn bytes(&self) -> Result<Vec<u8>, String> {
        let code = match self {
            Module::Bundled(bytes) => return Ok(bytes.to_vec()),
            Module::Own(code) => code,
        };
        match (&code.loader, source(code)) {
            (Loader::Wasm, Source::Binary(bytes)) => Ok(bytes.clone()),
            (Loader::Wat, Source::Text(text)) => wat::parse_str(text).map_err(describe),
            (loader, _) => Err(format!("loader `{}` isn't available yet", loader.as_str())),
        }
    }
}

/// The code: the engine fills it in, from the schema or fetched by its hash.
fn source(code: &WasmEntry) -> &Source {
    code.source
        .as_ref()
        .expect("the engine gives jobs their code")
}

/// An interpreter's input: the source, the function to call, and the arguments in order.
fn interpreter_input(code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String> {
    // The schema checks that a text loader has text.
    let text = String::from_utf8_lossy(source(code).as_bytes());
    let declared = match code.role {
        Role::Property | Role::Method | Role::BeforeCall => {
            code.params.as_ref().and_then(property_names)
        }
        _ => None,
    };
    let names = declared.unwrap_or_else(|| frame.args().keys().cloned().collect());
    let (mut args, mut marks) = (Vec::new(), serde_json::Map::new());
    for (i, name) in names.iter().enumerate() {
        args.push(frame.arg(name)?);
        let Some(Json::Object(given)) = frame.given() else { continue };
        for (kind, paths) in given {
            let list = marks.entry(kind).or_insert_with(|| json!([]));
            for path in paths.as_array().into_iter().flatten() {
                let mut at = vec![json!("args"), json!(i)];
                at.extend(path.as_array().into_iter().flatten().cloned());
                list.as_array_mut()
                    .expect("made above")
                    .push(Json::Array(at));
            }
        }
    }
    let mut input = serde_json::Map::new();
    input.insert("source".into(), Json::String(text.into_owned()));
    input.insert("entry".into(), Json::String(code.entry.clone()));
    input.insert("args".into(), Json::Array(args));
    if !marks.is_empty() {
        input.insert("marks".into(), Json::Object(marks));
    }
    let sample = match code.role {
        Role::AfterReceive => Some("in"),
        Role::BeforeSend => Some("out"),
        _ => None,
    };
    if let Some(sample) = sample {
        input.insert("sample".into(), Json::from(sample));
    }
    let call = match code.role {
        Role::BeforeCall => Some("send"),
        Role::AfterCall => Some("replies"),
        _ => None,
    };
    if let Some(call) = call {
        input.insert("call".into(), Json::from(call));
    }
    Ok(Json::Object(input).to_string().into_bytes())
}

/// The names in an object schema's `properties`, in order.
fn property_names(schema: &Value) -> Option<Vec<String>> {
    let Value::Map(entries) = schema else { return None };
    let (_, props) = entries
        .iter()
        .find(|(k, _)| matches!(k, Value::String(s) if s == "properties"))?;
    let Value::Map(props) = props else { return None };
    let names = props.iter().filter_map(|(k, _)| match k {
        Value::String(s) => Some(s.clone()),
        _ => None,
    });
    Some(names.collect())
}

/// A host function's body: the plug-in, its arguments, its results, and the running frame.
type Body = fn(&mut CurrentPlugin, &[Val], &mut [Val], &mut Frame) -> Result<(), String>;

fn define(
    slot: &Slot,
    name: &str,
    params: &[ValType],
    results: &[ValType],
    body: Body,
) -> Function {
    Function::new(
        name,
        params.iter().cloned(),
        results.iter().cloned(),
        slot.clone(),
        move |plugin, inputs, outputs, slot| {
            let slot = slot.get()?;
            let mut frame = lock(&slot);
            let frame = frame.as_mut().expect("set whenever WASM runs");
            body(plugin, inputs, outputs, frame).map_err(extism::Error::msg)
        },
    )
}

/// `reply_next`, which waits: the clock stops meanwhile.
fn reply_next_function(slot: &Slot, clock: &Arc<Clock>) -> Function {
    let clock = clock.clone();
    Function::new(
        "reply_next",
        [],
        [PTR],
        slot.clone(),
        move |plugin, _, outputs, slot| {
            let slot = slot.get()?;
            let mut frame = lock(&slot);
            let frame = frame.as_mut().expect("set whenever WASM runs");
            clock.pause();
            let next = frame.next_reply();
            clock.resume();
            reply_next(plugin, next, &mut outputs[0]).map_err(extism::Error::msg)
        },
    )
}

fn reply_next(
    p: &mut CurrentPlugin,
    next: Result<NextReply, String>,
    out: &mut Val,
) -> Result<(), String> {
    let next = match next? {
        NextReply::Reply(index) => json!({"reply": index}),
        NextReply::Error { kind, message } => {
            json!({"error": {"kind": kind, "message": message}})
        }
        NextReply::Ended => Json::Null,
    };
    give(p, &next, out)
}

fn functions(slot: &Slot, clock: &Arc<Clock>) -> Vec<Function> {
    use ValType::{F64, I64};
    vec![
        define(slot, "field_get", &[PTR], &[PTR], field_get),
        define(slot, "field_get_i64", &[PTR], &[I64], field_get_i64),
        define(slot, "field_get_f64", &[PTR], &[F64], field_get_f64),
        define(slot, "field_set", &[PTR, PTR], &[], field_set),
        define(slot, "field_set_i64", &[PTR, I64], &[], field_set_i64),
        define(slot, "field_set_f64", &[PTR, F64], &[], field_set_f64),
        define(slot, "arg_get", &[PTR], &[PTR], arg_get),
        define(slot, "arg_get_i64", &[PTR], &[I64], arg_get_i64),
        define(slot, "arg_get_f64", &[PTR], &[F64], arg_get_f64),
        define(slot, "sample_payload", &[], &[PTR], sample_payload),
        define(slot, "sample_encoding", &[], &[PTR], sample_encoding),
        define(slot, "sample_attachment", &[], &[PTR], sample_attachment),
        define(slot, "sample_decode", &[], &[PTR], sample_decode),
        define(slot, "sample_send", &[PTR, PTR, PTR], &[], sample_send),
        define(slot, "sample_send_value", &[PTR], &[], sample_send_value),
        define(slot, "call_send_args", &[PTR], &[], call_send_args),
        define(slot, "call_send", &[PTR, PTR, PTR, PTR], &[], call_send),
        reply_next_function(slot, clock),
        define(slot, "reply_payload", &[I64], &[PTR], reply_payload),
        define(slot, "reply_encoding", &[I64], &[PTR], reply_encoding),
        define(slot, "reply_attachment", &[I64], &[PTR], reply_attachment),
        define(slot, "reply_decode", &[I64], &[PTR], reply_decode),
        define(slot, "reply_escaped", &[], &[], reply_escaped),
        define(slot, "call_yield", &[PTR], &[], call_yield),
        define(slot, "marks", &[], &[PTR], marks),
        define(slot, "mark", &[PTR], &[], mark),
    ]
}

/// Why Extism failed, without its WASM backtrace.
fn cause(error: extism::Error) -> String {
    error.root_cause().to_string()
}

fn describe(error: impl std::fmt::Display) -> String {
    error.to_string()
}

/// A string the code passed.
fn text(plugin: &mut CurrentPlugin, val: &Val) -> Result<String, String> {
    plugin.memory_get_val::<String>(val).map_err(describe)
}

/// Bytes the code passed: block 0 is empty.
fn bytes(plugin: &mut CurrentPlugin, val: &Val) -> Result<Vec<u8>, String> {
    if val.unwrap_i64() == 0 {
        return Ok(Vec::new());
    }
    let bytes: &[u8] = plugin.memory_get_val(val).map_err(describe)?;
    Ok(bytes.to_vec())
}

/// Hand bytes to the code: empty bytes are block 0.
fn hand(plugin: &mut CurrentPlugin, bytes: &[u8], out: &mut Val) -> Result<(), String> {
    if bytes.is_empty() {
        *out = Val::I64(0);
        return Ok(());
    }
    plugin.memory_set_val(out, bytes).map_err(describe)
}

/// JSON text the code passed.
fn parse(json: &str) -> Result<Json, String> {
    serde_json::from_str(json).map_err(|e| format!("not JSON: {e}"))
}

/// Hand JSON to the code.
fn give(plugin: &mut CurrentPlugin, value: &Json, out: &mut Val) -> Result<(), String> {
    let text = value.to_string();
    plugin.memory_set_val(out, text.as_str()).map_err(describe)
}

fn int(value: Json, name: &str) -> Result<i64, String> {
    value
        .as_i64()
        .ok_or_else(|| format!("`{name}` isn't an integer: {value}"))
}

fn float(value: Json, name: &str) -> Result<f64, String> {
    value
        .as_f64()
        .ok_or_else(|| format!("`{name}` isn't a number: {value}"))
}

fn field_get(p: &mut CurrentPlugin, i: &[Val], o: &mut [Val], f: &mut Frame) -> Result<(), String> {
    let value = f.field(&text(p, &i[0])?)?;
    give(p, &value, &mut o[0])
}

fn field_get_i64(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    o[0] = Val::I64(int(f.field(&name)?, &name)?);
    Ok(())
}

fn field_get_f64(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    o[0] = Val::F64(float(f.field(&name)?, &name)?.to_bits());
    Ok(())
}

fn field_set(p: &mut CurrentPlugin, i: &[Val], _: &mut [Val], f: &mut Frame) -> Result<(), String> {
    let name = text(p, &i[0])?;
    let json = text(p, &i[1])?;
    let value = parse(&json)?;
    f.set_field(&name, value)
}

fn field_set_i64(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    f.set_field(&name, Json::from(i[1].unwrap_i64()))
}

fn field_set_f64(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    let x = i[1].unwrap_f64();
    let value = Number::from_f64(x).ok_or(format!("{x} has no JSON form"))?;
    f.set_field(&name, Json::Number(value))
}

fn arg_get(p: &mut CurrentPlugin, i: &[Val], o: &mut [Val], f: &mut Frame) -> Result<(), String> {
    let value = f.arg(&text(p, &i[0])?)?;
    give(p, &value, &mut o[0])
}

fn arg_get_i64(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    o[0] = Val::I64(int(f.arg(&name)?, &name)?);
    Ok(())
}

fn arg_get_f64(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let name = text(p, &i[0])?;
    o[0] = Val::F64(float(f.arg(&name)?, &name)?.to_bits());
    Ok(())
}

fn payload(p: &mut CurrentPlugin, sample: &Sample, out: &mut Val) -> Result<(), String> {
    let payload = sample.payload.bytes().into_owned();
    hand(p, &payload, out)
}

fn encoding(p: &mut CurrentPlugin, sample: &Sample, out: &mut Val) -> Result<(), String> {
    let encoding = sample.encoding.clone();
    hand(p, encoding.as_bytes(), out)
}

fn attachment(p: &mut CurrentPlugin, sample: &Sample, out: &mut Val) -> Result<(), String> {
    let attachment = sample.attachment.as_ref().map(|a| a.bytes().into_owned());
    hand(p, &attachment.unwrap_or_default(), out)
}

/// Bytes, an encoding and an attachment the code passed, as a sample.
fn sample(
    p: &mut CurrentPlugin,
    payload: &Val,
    encoding: &Val,
    attachment: &Val,
) -> Result<Sample, String> {
    let payload = bytes(p, payload)?;
    let encoding = String::from_utf8(bytes(p, encoding)?).map_err(describe)?;
    let attachment = bytes(p, attachment)?;
    Ok(Sample {
        payload: Payload::from(payload),
        encoding,
        attachment: (!attachment.is_empty()).then(|| Payload::from(attachment)),
    })
}

fn sample_payload(
    p: &mut CurrentPlugin,
    _: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    payload(p, f.sample()?, &mut o[0])
}

fn sample_encoding(
    p: &mut CurrentPlugin,
    _: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    encoding(p, f.sample()?, &mut o[0])
}

fn sample_attachment(
    p: &mut CurrentPlugin,
    _: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    attachment(p, f.sample()?, &mut o[0])
}

fn sample_decode(
    p: &mut CurrentPlugin,
    _: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let value = f.decode_sample()?;
    give(p, &value, &mut o[0])
}

fn sample_send(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let sample = sample(p, &i[0], &i[1], &i[2])?;
    f.send(sample)
}

fn sample_send_value(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let json = text(p, &i[0])?;
    let value = parse(&json)?;
    f.send_value(value)
}

fn call_send_args(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let json = text(p, &i[0])?;
    let value = parse(&json)?;
    f.send_args(value)
}

fn call_send(p: &mut CurrentPlugin, i: &[Val], _: &mut [Val], f: &mut Frame) -> Result<(), String> {
    let sample = sample(p, &i[0], &i[2], &i[3])?;
    let parameters = String::from_utf8(bytes(p, &i[1])?).map_err(describe)?;
    f.send_call(sample, parameters)
}

/// A reply's index the code passed.
fn index(val: &Val) -> Result<usize, String> {
    let i = val.unwrap_i64();
    usize::try_from(i).map_err(|_| format!("no reply {i}"))
}

fn reply_payload(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    payload(p, f.reply(index(&i[0])?)?, &mut o[0])
}

fn reply_encoding(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    encoding(p, f.reply(index(&i[0])?)?, &mut o[0])
}

fn reply_attachment(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    attachment(p, f.reply(index(&i[0])?)?, &mut o[0])
}

fn reply_decode(
    p: &mut CurrentPlugin,
    i: &[Val],
    o: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let value = f.decode_reply(index(&i[0])?)?;
    give(p, &value, &mut o[0])
}

fn reply_escaped(
    _: &mut CurrentPlugin,
    _: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    f.reply_escaped()
}

fn call_yield(
    p: &mut CurrentPlugin,
    i: &[Val],
    _: &mut [Val],
    f: &mut Frame,
) -> Result<(), String> {
    let json = text(p, &i[0])?;
    let value = parse(&json)?;
    f.give(value)
}

fn marks(p: &mut CurrentPlugin, _: &[Val], o: &mut [Val], f: &mut Frame) -> Result<(), String> {
    match f.given() {
        Some(marks) => give(p, &marks, &mut o[0]),
        None => hand(p, &[], &mut o[0]),
    }
}

fn mark(p: &mut CurrentPlugin, i: &[Val], _: &mut [Val], f: &mut Frame) -> Result<(), String> {
    let json = text(p, &i[0])?;
    f.mark(parse(&json)?)
}
