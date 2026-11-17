//! zenoh without the engine: puts, subscribers and gets with no object behind them, and
//! scouting. The `synq` command line uses them to watch the network and poke at it.

use std::sync::Mutex;
use std::time::{Duration, Instant, UNIX_EPOCH};

use pyo3::exceptions::{PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;
use spirisynq_zenoh::zenoh::config::WhatAmIMatcher;
use spirisynq_zenoh::zenoh::handlers::FifoChannelHandler;
use spirisynq_zenoh::zenoh::pubsub::Subscriber;
use spirisynq_zenoh::zenoh::query::{ConsolidationMode, QueryTarget, Reply, ReplyKeyExpr};
use spirisynq_zenoh::zenoh::sample::Sample as ZSample;
use spirisynq_zenoh::zenoh::scouting::Hello;
use spirisynq_zenoh::zenoh::{self, Wait};

use crate::hooks::lock;
use crate::{err, seconds, zerr};

/// A sample or a reply, as zenoh carries it.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Message {
    /// Its key.
    #[pyo3(get)]
    key: String,
    payload: Vec<u8>,
    /// Its zenoh encoding, such as `application/yaml`.
    #[pyo3(get)]
    encoding: String,
    attachment: Option<Vec<u8>>,
    /// When it was sent, in seconds since the Unix epoch, if it carries a timestamp.
    #[pyo3(get)]
    timestamp: Option<f64>,
    /// False for an error reply.
    #[pyo3(get)]
    ok: bool,
}

impl Message {
    fn of(sample: &ZSample, ok: bool) -> Self {
        let time = sample.timestamp().map(|t| t.get_time().to_system_time());
        let since = time.and_then(|t| t.duration_since(UNIX_EPOCH).ok());
        Message {
            key: sample.key_expr().to_string(),
            payload: sample.payload().to_bytes().into_owned(),
            encoding: sample.encoding().to_string(),
            attachment: sample.attachment().map(|a| a.to_bytes().into_owned()),
            timestamp: since.map(|d| d.as_secs_f64()),
            ok,
        }
    }

    /// An error reply, which has no key.
    fn error(e: &zenoh::query::ReplyError) -> Self {
        Message {
            key: String::new(),
            payload: e.payload().to_bytes().into_owned(),
            encoding: e.encoding().to_string(),
            attachment: None,
            timestamp: None,
            ok: false,
        }
    }
}

#[pymethods]
impl Message {
    #[getter]
    fn payload<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.payload)
    }

    #[getter]
    fn attachment<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.attachment.as_ref().map(|a| PyBytes::new(py, a))
    }
}

/// How long to wait at a time, so Python can handle signals, such as Ctrl-C, meanwhile.
const SLICE: Duration = Duration::from_millis(100);

/// Wait for the next item, until `deadline` if given: `Ok(None)` if none came in time, and
/// `Err(())` once the channel is closed. Raises if a signal handler does.
fn wait<T: Send>(
    py: Python<'_>,
    channel: &FifoChannelHandler<T>,
    deadline: Option<Instant>,
) -> PyResult<Result<Option<T>, ()>> {
    loop {
        let now = Instant::now();
        let slice = deadline.map_or(SLICE, |d| SLICE.min(d.saturating_duration_since(now)));
        match py.detach(|| channel.recv_timeout(slice)) {
            Ok(Some(item)) => return Ok(Ok(Some(item))),
            Ok(None) if deadline.is_some_and(|d| Instant::now() >= d) => return Ok(Ok(None)),
            Ok(None) => py.check_signals()?,
            Err(_) => return Ok(Err(())),
        }
    }
}

/// A subscriber: what's put on the keys its key expression matches.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Watch(Mutex<Option<Subscriber<FifoChannelHandler<ZSample>>>>);

impl Watch {
    pub(crate) fn declare(zenoh: &zenoh::Session, key: &str) -> PyResult<Self> {
        let subscriber = zenoh.declare_subscriber(key).wait();
        Ok(Watch(Mutex::new(Some(subscriber.map_err(zerr)?))))
    }
}

#[pymethods]
impl Watch {
    /// The next sample, or `None` if none came within `timeout` seconds. Waits for ever without
    /// a timeout. Raises `SynqError` once the watch or its node is closed.
    #[pyo3(signature = (timeout=None))]
    fn recv(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<Message>> {
        let deadline = seconds(timeout)?.map(|t| Instant::now() + t);
        // A clone of the channel, so `close` doesn't wait for a `recv` to finish.
        let channel = {
            let subscriber = lock(&self.0);
            let subscriber = subscriber.as_ref().ok_or_else(|| err("closed".into()))?;
            subscriber.handler().clone()
        };
        match wait(py, &channel, deadline)? {
            Ok(sample) => Ok(sample.map(|s| Message::of(&s, true))),
            Err(()) => Err(err("closed".into())),
        }
    }

    /// Stop watching. Does nothing if already stopped.
    fn close(&self) {
        drop(lock(&self.0).take());
    }
}

/// The replies to a get, as they come: iterate over it. Error replies come as messages that
/// aren't `ok`. It ends when every queryable has answered, or at the get's timeout.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Replies(FifoChannelHandler<Reply>);

impl Replies {
    pub(crate) fn get(
        zenoh: &zenoh::Session,
        key: &str,
        parameters: &str,
        payload: Option<(&[u8], &str)>,
        timeout: Duration,
    ) -> PyResult<Self> {
        let mut get = zenoh
            .get(format!("{key}?{parameters}"))
            .target(QueryTarget::All)
            .consolidation(ConsolidationMode::None)
            // An owner answers a state fetch on each field's own key.
            .accept_replies(ReplyKeyExpr::Any)
            .timeout(timeout);
        if let Some((payload, encoding)) = payload {
            get = get.payload(payload.to_vec()).encoding(encoding.to_owned());
        }
        Ok(Replies(get.wait().map_err(zerr)?))
    }
}

#[pymethods]
impl Replies {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Message> {
        loop {
            let Ok(Some(reply)) = wait(py, &self.0, None)? else {
                return Err(PyStopIteration::new_err(()));
            };
            // zenoh's own word that the get timed out ends it, as the channel closing does.
            if spirisynq_zenoh::is_timeout(&reply) {
                continue;
            }
            return Ok(match reply.result() {
                Ok(sample) => Message::of(sample, true),
                Err(e) => Message::error(e),
            });
        }
    }
}

pub(crate) fn put(
    zenoh: &zenoh::Session,
    key: &str,
    payload: &PyBackedBytes,
    encoding: &str,
    attachment: Option<&PyBackedBytes>,
) -> PyResult<()> {
    let mut put = zenoh
        .put(key, payload.to_vec())
        .encoding(encoding.to_owned());
    if let Some(attachment) = attachment {
        put = put.attachment(attachment.to_vec());
    }
    put.wait().map_err(zerr)
}

fn hello(hello: &Hello) -> (String, String, Vec<String>) {
    let locators = hello.locators().iter().map(ToString::to_string).collect();
    (
        hello.zid().to_string(),
        hello.whatami().to_string(),
        locators,
    )
}

/// Scout for zenoh nodes for `timeout` seconds, by multicast as `config` says (zenoh's JSON5
/// configuration). `what` is the kinds of node to look for, such as `router|peer`. Each node
/// found is `(zid, kind, locators)`.
#[pyfunction]
#[pyo3(signature = (what, timeout, config="{}"))]
pub(crate) fn scout(
    py: Python<'_>,
    what: &str,
    timeout: f64,
    config: &str,
) -> PyResult<Vec<(String, String, Vec<String>)>> {
    let matcher: WhatAmIMatcher = what
        .parse()
        .map_err(|()| PyValueError::new_err(format!("can't scout for `{what}`")))?;
    let config = zenoh::Config::from_json5(config).map_err(zerr)?;
    let deadline = Instant::now() + seconds(Some(timeout))?.unwrap_or_default();
    let scout = zenoh::scout(matcher, config).wait();
    let scout = scout.map_err(zerr)?;
    let mut found = Vec::new();
    while let Ok(Some(h)) = wait(py, &*scout, Some(deadline))? {
        found.push(hello(&h));
    }
    scout.stop();
    Ok(found)
}
