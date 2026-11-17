//! `SpiriSynq._core`: the SpiriSynq engine, over zenoh, for Python.
//!
//! A thin layer: [`Node`] is a [`spirisynq_zenoh::Node`], its methods are the engine's, and its
//! notices come out of [`Node::next_notice`] as the classes in [`notice`]. The Python package
//! builds the user API on top. Field values convert as [`value`] says.

#![forbid(unsafe_code)]

mod hooks;
mod notice;
mod raw;
mod value;

use std::sync::Arc;
use std::time::{Duration, Instant};

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBytes, PyDict};
use spirisynq_core::{self as core, CallOptions, Priority, Qos, QueryId, Session};
use spirisynq_extism::Limits;
use spirisynq_schema::{Role as CodeRole, TypeId, TypeSchema};
use spirisynq_zenoh::zenoh::{self, Wait};

use crate::hooks::{Inner, Refused, lock};
use crate::value::{Tagged, from_py, to_py};

create_exception!(
    _core,
    SynqError,
    PyException,
    "The engine refused a call, or zenoh failed."
);

fn err(message: String) -> PyErr {
    SynqError::new_err(message)
}

/// zenoh's error, or any other that's only text.
fn zerr(e: impl std::fmt::Display) -> PyErr {
    err(e.to_string())
}

/// A timeout in seconds, as a duration. `ValueError` if it's negative or not a number.
fn seconds(timeout: Option<f64>) -> PyResult<Option<Duration>> {
    let timeout = timeout.map(Duration::try_from_secs_f64).transpose();
    timeout.map_err(|e| PyValueError::new_err(e.to_string()))
}

impl From<Refused> for PyErr {
    fn from(e: Refused) -> Self {
        err(e.to_string())
    }
}

/// An open object: an authoritative object or a mirror.
#[pyclass(frozen, eq, hash, from_py_object, module = "SpiriSynq._core")]
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct ObjectId(core::ObjectId);

/// A request this node sent: a remote call, a discovery query or a schema fetch.
#[pyclass(frozen, eq, hash, from_py_object, module = "SpiriSynq._core")]
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct RequestId(core::RequestId);

/// A payload with its zenoh encoding and optional attachment, for fields and methods with their
/// own encoding.
#[pyclass(frozen, eq, module = "SpiriSynq._core")]
#[derive(Clone, PartialEq)]
pub struct Sample(core::Sample);

#[pymethods]
impl Sample {
    #[new]
    #[pyo3(signature = (payload, encoding, attachment=None))]
    fn new(payload: PyBackedBytes, encoding: String, attachment: Option<PyBackedBytes>) -> Self {
        Sample(core::Sample {
            payload: payload.to_vec().into(),
            encoding,
            attachment: attachment.map(|a| a.to_vec().into()),
        })
    }

    #[getter]
    fn payload<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.0.payload.bytes())
    }

    #[getter]
    fn encoding(&self) -> &str {
        &self.0.encoding
    }

    #[getter]
    fn attachment<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let attachment = self.0.attachment.as_ref();
        attachment.map(|a| PyBytes::new(py, &a.bytes()))
    }
}

/// A type's schema, checked once and shared by every object of the type.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Schema(Arc<TypeSchema>);

#[pymethods]
impl Schema {
    #[new]
    fn new(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let schema = TypeSchema::from_value(from_py(value)?);
        Ok(Schema(Arc::new(schema.map_err(|e| err(e.to_string()))?)))
    }

    /// The `$id`.
    #[getter]
    fn id(&self) -> &str {
        self.0.id().as_str()
    }

    /// The `$id`s of the types it derives from.
    #[getter]
    fn bases(&self) -> Vec<&str> {
        self.0.bases().iter().map(TypeId::as_str).collect()
    }
}

/// How this process handles one field. `priority` is one of `real_time`, `interactive_high`,
/// `interactive_low`, `data_high`, `data`, `data_low` and `background`.
#[pyclass(frozen, from_py_object, module = "SpiriSynq._core")]
#[derive(Clone)]
pub struct FieldOptions(core::FieldOptions);

const PRIORITIES: [(&str, Priority); 7] = [
    ("real_time", Priority::RealTime),
    ("interactive_high", Priority::InteractiveHigh),
    ("interactive_low", Priority::InteractiveLow),
    ("data_high", Priority::DataHigh),
    ("data", Priority::Data),
    ("data_low", Priority::DataLow),
    ("background", Priority::Background),
];

#[pymethods]
impl FieldOptions {
    #[new]
    #[pyo3(signature = (*, priority="data", reliable=false, ignore_incoming=false, lazy=false, raw=false))]
    fn new(
        priority: &str,
        reliable: bool,
        ignore_incoming: bool,
        lazy: bool,
        raw: bool,
    ) -> PyResult<Self> {
        let Some(&(_, priority)) = PRIORITIES.iter().find(|(name, _)| *name == priority) else {
            return Err(PyValueError::new_err(format!("no priority `{priority}`")));
        };
        Ok(FieldOptions(core::FieldOptions {
            qos: Qos { priority, reliable },
            ignore_incoming,
            lazy,
            raw,
        }))
    }
}

fn field_options(
    fields: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<(String, core::FieldOptions)>> {
    let Some(fields) = fields else { return Ok(Vec::new()) };
    let fields = fields.iter().map(|(k, v)| {
        let options: FieldOptions = v.extract()?;
        Ok((k.extract()?, options.0))
    });
    fields.collect()
}

fn string_pairs(dict: Option<&Bound<'_, PyDict>>) -> PyResult<Vec<(String, String)>> {
    let Some(dict) = dict else { return Ok(Vec::new()) };
    dict.iter()
        .map(|(k, v)| Ok((k.extract()?, v.extract()?)))
        .collect()
}

/// A zenoh session with an engine on it. Its objects' WASM hooks run by themselves, as
/// [`hooks`] says.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Node {
    inner: Arc<Inner>,
    notices: flume::Receiver<core::Notice>,
    /// What the hooks make.
    made: flume::Receiver<core::Notice>,
    zid: String,
}

impl Node {
    /// Run `f` on the engine. Its commands go out when it returns.
    fn with<R>(&self, f: impl FnOnce(&mut Session) -> Result<R, core::Error>) -> PyResult<R> {
        Ok(self.inner.with(f)?)
    }

    /// The zenoh session, for what the engine doesn't do.
    fn zenoh(&self) -> PyResult<zenoh::Session> {
        let node = lock(&self.inner.node);
        let node = node.as_ref().ok_or(Refused::Closed)?;
        Ok(node.zenoh().clone())
    }
}

impl Drop for Node {
    fn drop(&mut self) {
        drop(self.inner.close());
    }
}

#[pymethods]
impl Node {
    /// Open a zenoh session, configured by `config`: zenoh's JSON5 configuration, `{}` for its
    /// defaults. Each run of WASM code may take `wasm_timeout` seconds, not counting the time an
    /// `after_call` waits for replies, and `wasm_memory_mib` MiB.
    #[new]
    #[pyo3(signature = (config, *, wasm_timeout=1.0, wasm_memory_mib=64))]
    fn new(
        py: Python<'_>,
        config: &str,
        wasm_timeout: f64,
        wasm_memory_mib: u32,
    ) -> PyResult<Self> {
        let timeout = Duration::try_from_secs_f64(wasm_timeout);
        let timeout = timeout.map_err(|e| PyValueError::new_err(e.to_string()))?;
        let limits = Limits {
            timeout,
            memory_pages: wasm_memory_mib.saturating_mul(16),
        };
        let config = zenoh::Config::from_json5(config).map_err(|e| err(e.to_string()))?;
        let node = py.detach(|| spirisynq_zenoh::Node::open(config));
        let node = node.map_err(|e| err(e.to_string()))?;
        let (made, made_out) = flume::unbounded();
        Ok(Node {
            notices: node.notices().clone(),
            made: made_out,
            zid: node.zid().0,
            inner: Arc::new(Inner::new(node, limits, made)),
        })
    }

    /// This node's zenoh session id.
    #[getter]
    fn zid(&self) -> &str {
        &self.zid
    }

    /// Where this node listens, as zenoh endpoints.
    fn locators(&self) -> PyResult<Vec<String>> {
        let locators = self.zenoh()?.info().locators().wait();
        Ok(locators.iter().map(ToString::to_string).collect())
    }

    /// The session ids of the peers this node is connected to.
    fn peers(&self) -> PyResult<Vec<String>> {
        let peers = self.zenoh()?.info().peers_zid().wait();
        Ok(peers.map(|zid| zid.to_string()).collect())
    }

    /// The session ids of the routers this node is connected to.
    fn routers(&self) -> PyResult<Vec<String>> {
        let routers: Vec<_> = self.zenoh()?.info().routers_zid().wait().collect();
        Ok(routers.iter().map(ToString::to_string).collect())
    }

    /// Put `payload` on `key`, with no object behind it.
    #[pyo3(signature = (key, payload, encoding="application/yaml", attachment=None))]
    fn put(
        &self,
        key: &str,
        payload: PyBackedBytes,
        encoding: &str,
        attachment: Option<PyBackedBytes>,
    ) -> PyResult<()> {
        raw::put(&self.zenoh()?, key, &payload, encoding, attachment.as_ref())
    }

    /// Watch what's put on the keys `key` matches.
    fn watch(&self, key: &str) -> PyResult<raw::Watch> {
        raw::Watch::declare(&self.zenoh()?, key)
    }

    /// Query `key` with `parameters`, and a payload in `encoding` if given. Every queryable
    /// that matches answers; the replies end after `timeout` seconds at the latest.
    #[pyo3(signature = (key, parameters="", payload=None, encoding="application/yaml", timeout=10.0))]
    fn get(
        &self,
        key: &str,
        parameters: &str,
        payload: Option<PyBackedBytes>,
        encoding: &str,
        timeout: f64,
    ) -> PyResult<raw::Replies> {
        let timeout = seconds(Some(timeout))?.unwrap_or_default();
        let payload = payload.as_ref().map(|p| (&p[..], encoding));
        raw::Replies::get(&self.zenoh()?, key, parameters, payload, timeout)
    }

    /// Start an authoritative object.
    #[pyo3(signature = (topic, schema, fields=None, raw_methods=Vec::new()))]
    fn create_owner(
        &self,
        topic: &str,
        schema: &Schema,
        fields: Option<&Bound<'_, PyDict>>,
        raw_methods: Vec<String>,
    ) -> PyResult<ObjectId> {
        let options = core::ObjectOptions {
            fields: field_options(fields)?,
            raw_methods,
            rehydrate: false,
        };
        let schema = schema.0.clone();
        self.with(|s| s.create_owner(topic, schema, options))
            .map(ObjectId)
    }

    /// Start a mirror.
    #[pyo3(signature = (topic, schema, fields=None, rehydrate=true))]
    fn create_mirror(
        &self,
        topic: &str,
        schema: &Schema,
        fields: Option<&Bound<'_, PyDict>>,
        rehydrate: bool,
    ) -> PyResult<ObjectId> {
        let options = core::ObjectOptions {
            fields: field_options(fields)?,
            raw_methods: Vec::new(),
            rehydrate,
        };
        let schema = schema.0.clone();
        self.with(|s| s.create_mirror(topic, schema, options))
            .map(ObjectId)
    }

    /// Run the object's WASM property or method `name`, from its type's `x-wasm`, with `args`
    /// by name, and give its result. Fields the code sets are set as `set_field` sets them, once
    /// it has succeeded. The code runs without the GIL, and without holding up the engine.
    #[pyo3(signature = (object, name, args=None))]
    fn run_wasm<'py>(
        &self,
        py: Python<'py>,
        object: ObjectId,
        name: &str,
        args: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let args = match args {
            Some(args) => from_py(args)?,
            None => spirisynq_yaml::Value::Null,
        };
        let job = self.with(|s| s.wasm_job(object.0, name, &args))?;
        let result = py.detach(|| self.inner.run_job(job))?;
        to_py(py, &result)
    }

    /// Fetch a mirror's full state again.
    fn rehydrate(&self, object: ObjectId) -> PyResult<()> {
        self.with(|s| s.rehydrate(object.0))
    }

    fn close_object(&self, object: ObjectId) -> PyResult<()> {
        self.with(|s| s.close(object.0))
    }

    /// Set a field. On an owner, publishes it if it changed; on a mirror, writes it to the
    /// owner. Says whether it changed. A field with a WASM `before_send` is sent as it sends
    /// it, every time.
    fn set_field(
        &self,
        py: Python<'_>,
        object: ObjectId,
        path: &str,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let value = from_py(value)?;
        match self.with(|s| s.before_send_job(object.0, path, &value))? {
            Some(job) => Ok(py.detach(|| self.inner.run_job(job)).map(|_| true)?),
            None => self.with(|s| s.set_field(object.0, path, value)),
        }
    }

    /// Set a mirror's copy of a field, without sending anything. Says whether it changed; if
    /// it did, the field's WASM `on_change_mirror` hooks run, in the schema's order. What they
    /// set is set as this sets it, without running hooks.
    fn set_field_local(
        &self,
        py: Python<'_>,
        object: ObjectId,
        path: &str,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let value = from_py(value)?;
        let changed = self.with(|s| s.set_field_local(object.0, path, value.clone()))?;
        if changed {
            for name in self.with(|s| s.hooks(object.0, &CodeRole::OnChangeMirror, path))? {
                let job = self.with(|s| s.on_change_job(object.0, &name, path, &value))?;
                py.detach(|| self.inner.run_job(job))?;
            }
        }
        Ok(changed)
    }

    /// Subscribe a mirror to an on-demand field (a key chunk starting with `@`), fetching its
    /// last value. Holds are counted. Whether this started the subscription.
    fn subscribe(&self, object: ObjectId, path: &str) -> PyResult<bool> {
        self.with(|s| s.subscribe(object.0, path))
    }

    /// Let go of one hold on an on-demand field. Whether this ended the subscription.
    fn unsubscribe(&self, object: ObjectId, path: &str) -> PyResult<bool> {
        self.with(|s| s.unsubscribe(object.0, path))
    }

    /// Send a field's value in its own encoding.
    fn send_raw(&self, object: ObjectId, path: &str, sample: &Sample) -> PyResult<()> {
        let sample = sample.0.clone();
        self.with(|s| s.send_raw(object.0, path, sample))
    }

    /// A field's value as the engine holds it, or `default` if it has none.
    #[pyo3(signature = (object, path, default=None))]
    fn field<'py>(
        &self,
        py: Python<'py>,
        object: ObjectId,
        path: &str,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let value = self.with(|s| Ok(s.field(object.0, path)?.cloned()))?;
        match value {
            Some(value) => to_py(py, &value),
            None => Ok(default.unwrap_or_else(|| py.None().into_bound(py))),
        }
    }

    /// Whether a mirror's owner is alive.
    fn alive(&self, object: ObjectId) -> PyResult<bool> {
        self.with(|s| s.alive(object.0))
    }

    /// Whether anyone listens to an owner's field, or to any of its fields.
    #[pyo3(signature = (object, path=None))]
    fn has_listeners(&self, object: ObjectId, path: Option<&str>) -> PyResult<bool> {
        self.with(|s| s.has_listeners(object.0, path))
    }

    /// Call a remote method. Replies come as `Reply` notices, then a `RequestEnded`. A WASM
    /// `before_call` makes the call; with a WASM `after_call`, the replies are what it yields.
    #[pyo3(signature = (object, method, args=None, *, raw_replies=false, parameters=String::new(), timeout_ms=None))]
    #[allow(clippy::too_many_arguments)]
    fn call(
        &self,
        py: Python<'_>,
        object: ObjectId,
        method: &str,
        args: Option<&Bound<'_, PyDict>>,
        raw_replies: bool,
        parameters: String,
        timeout_ms: Option<u64>,
    ) -> PyResult<RequestId> {
        let mut pairs = Vec::new();
        for (k, v) in args.into_iter().flatten() {
            pairs.push((k.extract()?, from_py(&v)?));
        }
        let options = CallOptions {
            raw_replies,
            parameters,
            timeout_ms,
        };
        let job = self.with(|s| s.before_call_job(object.0, method, &pairs, options.clone()))?;
        let request = match job {
            Some(mut job) => {
                let output = py.detach(|| self.inner.run(|host| job.run(host)));
                self.inner
                    .calling(object.0, method, |s| s.finish_call(job, output))
            }
            None => self.inner.calling(object.0, method, |s| {
                s.call(object.0, method, pairs, options)
            }),
        };
        Ok(RequestId(request?))
    }

    /// Call a remote method with a payload in its own encoding. `key_args` fill the method's
    /// key. A WASM `before_call` doesn't run; a WASM `after_call` does, as for `call`.
    #[pyo3(signature = (object, method, key_args=None, payload=None, *, raw_replies=false, parameters=String::new(), timeout_ms=None))]
    #[allow(clippy::too_many_arguments)]
    fn call_raw(
        &self,
        object: ObjectId,
        method: &str,
        key_args: Option<&Bound<'_, PyDict>>,
        payload: Option<&Sample>,
        raw_replies: bool,
        parameters: String,
        timeout_ms: Option<u64>,
    ) -> PyResult<RequestId> {
        let key_args = string_pairs(key_args)?;
        let key_args: Vec<(&str, &str)> = key_args
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        let payload = payload.map(|p| p.0.clone());
        let options = CallOptions {
            raw_replies,
            parameters,
            timeout_ms,
        };
        let call = |s: &mut Session| s.call_raw(object.0, method, &key_args, payload, options);
        Ok(RequestId(self.inner.calling(object.0, method, call)?))
    }

    /// Answer an incoming call with one value.
    fn reply(&self, query: u64, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = from_py(value)?;
        self.with(|s| s.reply(QueryId(query), &value))
    }

    /// Answer an incoming call with one value in its own encoding.
    fn reply_raw(&self, query: u64, sample: &Sample) -> PyResult<()> {
        let sample = sample.0.clone();
        self.with(|s| s.reply_raw(QueryId(query), sample))
    }

    /// Answer an incoming call with an error, which ends it.
    fn reply_error(&self, query: u64, kind: &str, message: &str) -> PyResult<()> {
        self.with(|s| s.reply_error(QueryId(query), kind, message))
    }

    /// End an incoming call.
    fn finish(&self, query: u64) -> PyResult<()> {
        self.with(|s| s.finish(QueryId(query)))
    }

    /// Find objects under `prefix`, of the type `type_id` names if given. Each answers with a
    /// `Discovered` notice.
    #[pyo3(signature = (prefix=None, type_id=None))]
    fn discover(&self, prefix: Option<&str>, type_id: Option<&str>) -> PyResult<RequestId> {
        let type_id = type_id.map(str::parse::<TypeId>).transpose();
        let type_id = type_id.map_err(|e| err(e.to_string()))?;
        self.with(|s| s.discover(prefix, type_id.as_ref()))
            .map(RequestId)
    }

    /// Fetch the schema of the object at `topic`, as a `Schema` notice.
    fn fetch_schema(&self, topic: &str) -> PyResult<RequestId> {
        self.with(|s| s.fetch_schema(topic)).map(RequestId)
    }

    /// The next notice, or `None` if none came within `timeout` seconds or the node is closed.
    /// Waits for ever without a timeout. Doesn't hold the GIL while it waits, or while a WASM
    /// `after_receive` decodes a field's sample.
    #[pyo3(signature = (timeout=None))]
    fn next_notice(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<Py<PyAny>>> {
        let deadline = seconds(timeout)?.map(|t| Instant::now() + t);
        // Wake up now and then to let Python handle signals, such as Ctrl-C.
        let slice = Duration::from_millis(100);
        loop {
            let wait = deadline.map_or(slice, |d| slice.min(d - d.min(Instant::now())));
            let got = py.detach(|| {
                flume::Selector::new()
                    .recv(&self.made, Result::ok)
                    .recv(&self.notices, Result::ok)
                    .wait_timeout(wait)
            });
            match got {
                Ok(Some(notice)) => {
                    if let Some(notice) = self.inner.feed(notice) {
                        let notice = py.detach(|| self.inner.receive(notice));
                        return notice::to_py(py, notice).map(Some);
                    }
                }
                Ok(None) => return Ok(None),
                Err(flume::select::SelectError::Timeout) => py.check_signals()?,
            }
            if deadline.is_some_and(|d| Instant::now() >= d) {
                return Ok(None);
            }
        }
    }

    /// Close every object and the zenoh session. Does nothing if already closed.
    fn close(&self, py: Python<'_>) {
        let node = self.inner.close();
        py.detach(|| drop(node));
    }
}

/// A value read from YAML in the wire's subset, as fields arrive. Raises `SynqError` if the
/// text isn't in it.
#[pyfunction]
fn yaml_load<'py>(py: Python<'py>, data: PyBackedBytes) -> PyResult<Bound<'py, PyAny>> {
    let value = spirisynq_yaml::parse_bytes(&data).map_err(|e| err(e.to_string()))?;
    to_py(py, &value)
}

/// A value written as YAML, as fields are sent, in UTF-8.
#[pyfunction]
fn yaml_dump<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let text = spirisynq_yaml::to_string(&from_py(value)?).map_err(|e| err(e.to_string()))?;
    Ok(PyBytes::new(py, text.as_bytes()))
}

#[pymodule]
mod _core {
    #[pymodule_export]
    use super::{
        FieldOptions, Node, ObjectId, RequestId, Sample, Schema, SynqError, Tagged, yaml_dump,
        yaml_load,
    };
    #[pymodule_export]
    use crate::notice::{
        Alive, BadData, Call, Discovered, Incoming, Listeners, Rehydrated, Reply, RequestEnded,
        SchemaFetched,
    };
    #[pymodule_export]
    use crate::raw::{Message, Replies, Watch, scout};
}
