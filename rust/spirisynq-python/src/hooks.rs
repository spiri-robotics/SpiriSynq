//! Running a type's WASM hooks when their moment comes, so Python never sees them.
//!
//! - `after_receive` decodes a field's samples as they come out of `Node.next_notice`.
//! - `before_send` sends a field's new value in `Node.set_field`.
//! - `on_change_mirror` runs in `Node.set_field_local`, when the mirror's value changed.
//! - `before_call` makes the call in `Node.call`.
//! - `after_call` reads the call's replies on a thread of its own: `Node.next_notice` feeds them
//!   to it rather than handing them out, and hands out what it yields instead, then the call's
//!   end.
//!
//! The code runs on [`Inner::run`]'s hosts, one per piece of code running at once.

use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use spirisynq_core::{
    self as core, Data, Notice, Replies, ReplyError, RequestEnd, RequestId, Sample, Session,
    WasmHost, WasmJob,
};
use spirisynq_extism::{Host, Limits};
use spirisynq_yaml::Value;

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Why the engine didn't do something.
pub(crate) enum Refused {
    Closed,
    Engine(core::Error),
}

impl fmt::Display for Refused {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Refused::Closed => f.write_str("the node is closed"),
            Refused::Engine(e) => e.fmt(f),
        }
    }
}

/// What a node's Python object and its `after_call` threads share.
pub(crate) struct Inner {
    pub node: Mutex<Option<spirisynq_zenoh::Node>>,
    limits: Limits,
    /// Hosts no code is running on.
    hosts: Mutex<Vec<Host>>,
    /// The calls whose replies go to an `after_call`, by request.
    pending: Mutex<HashMap<RequestId, flume::Sender<Feed>>>,
    /// Notices the hooks make: what an `after_call` yields, and its call's end.
    pub made: flume::Sender<Notice>,
}

impl Inner {
    pub fn new(node: spirisynq_zenoh::Node, limits: Limits, made: flume::Sender<Notice>) -> Self {
        Inner {
            node: Mutex::new(Some(node)),
            limits,
            hosts: Mutex::new(Vec::new()),
            pending: Mutex::new(HashMap::new()),
            made,
        }
    }

    /// Run `f` on the engine. Its commands go out when it returns.
    pub fn with<R>(
        &self,
        f: impl FnOnce(&mut Session) -> Result<R, core::Error>,
    ) -> Result<R, Refused> {
        let node = lock(&self.node);
        let node = node.as_ref().ok_or(Refused::Closed)?;
        node.with(f).map_err(Refused::Engine)
    }

    /// Run `f` on a host of its own.
    pub fn run<R>(&self, f: impl FnOnce(&mut dyn WasmHost) -> R) -> R {
        let idle = lock(&self.hosts).pop();
        let mut host = idle.unwrap_or_else(|| Host::new(self.limits));
        let result = f(&mut host);
        lock(&self.hosts).push(host);
        result
    }

    /// Run a job and finish it.
    pub fn run_job(&self, mut job: WasmJob) -> Result<Value, Refused> {
        let output = self.run(|host| job.run(host));
        self.with(|s| s.finish_wasm(job, output))
    }

    /// Make a call with `call`, inside the engine, and start its `after_call` if it has one.
    pub fn calling(
        self: &Arc<Self>,
        object: core::ObjectId,
        method: &str,
        call: impl FnOnce(&mut Session) -> Result<RequestId, core::Error>,
    ) -> Result<RequestId, Refused> {
        self.with(|s| {
            let request = call(s)?;
            let generator = s
                .schema(object)?
                .method(method)
                .is_some_and(|m| m.generator);
            let (feed, fed) = flume::unbounded();
            let end = Arc::new(Mutex::new(RequestEnd::Done));
            let replies = Fed {
                request,
                fed,
                made: self.made.clone(),
                end: end.clone(),
            };
            if let Some(job) = s.after_call_job(object, method, Box::new(replies))? {
                // Before the call goes out, when this returns, so no reply can miss it.
                lock(&self.pending).insert(request, feed);
                let inner = self.clone();
                std::thread::spawn(move || inner.after_call(request, job, generator, &end));
            }
            Ok(request)
        })
    }

    fn after_call(
        &self,
        request: RequestId,
        job: WasmJob,
        generator: bool,
        end: &Mutex<RequestEnd>,
    ) {
        let (result, end) = match self.run_job(job) {
            Ok(value) => ((!generator).then_some(Ok(Data::Value(value))), *lock(end)),
            Err(Refused::Engine(core::Error::Reply(e))) => (Some(Err(e)), RequestEnd::Failed),
            Err(e) => {
                let kind = "SynqError".to_owned();
                let message = e.to_string();
                let failed = ReplyError::Remote { kind, message };
                (Some(Err(failed)), RequestEnd::Failed)
            }
        };
        if let Some(result) = result {
            let _ = self.made.send(Notice::Reply { request, result });
        }
        let _ = self.made.send(Notice::RequestEnded { request, end });
    }

    /// Give a call's reply or end to its `after_call`, if it has one; else hand it back.
    pub fn feed(&self, notice: Notice) -> Option<Notice> {
        let mut pending = lock(&self.pending);
        match notice {
            Notice::Reply { request, result } if pending.contains_key(&request) => {
                let _ = pending[&request].send(Feed::Reply(result));
                None
            }
            Notice::RequestEnded { request, end } if pending.contains_key(&request) => {
                let _ = pending.remove(&request)?.send(Feed::End(end));
                None
            }
            other => Some(other),
        }
    }

    /// Decode an incoming sample with its field's `after_receive`, if it has one.
    pub fn receive(&self, notice: Notice) -> Notice {
        let Notice::Incoming {
            object,
            path,
            data: Data::Raw(sample),
            origin,
        } = notice
        else {
            return notice;
        };
        let job = self.with(|s| s.after_receive_job(object, &path, sample.clone()));
        let Some(job) = job.ok().flatten() else {
            let data = Data::Raw(sample);
            return Notice::Incoming {
                object,
                path,
                data,
                origin,
            };
        };
        match self.run_job(job) {
            Ok(value) => {
                let data = Data::Value(value);
                Notice::Incoming {
                    object,
                    path,
                    data,
                    origin,
                }
            }
            Err(e) => {
                let reason = e.to_string();
                Notice::BadData {
                    object,
                    path,
                    reason,
                }
            }
        }
    }

    /// Close: end every `after_call` that waits for replies, let the idle hosts go, and give
    /// the node to drop, if it wasn't closed yet.
    pub fn close(&self) -> Option<spirisynq_zenoh::Node> {
        lock(&self.pending).clear();
        lock(&self.hosts).clear();
        lock(&self.node).take()
    }
}

/// A call's reply or end, for its `after_call`.
enum Feed {
    Reply(Result<Data, ReplyError>),
    End(RequestEnd),
}

/// The replies an `after_call` reads.
struct Fed {
    request: RequestId,
    fed: flume::Receiver<Feed>,
    made: flume::Sender<Notice>,
    /// How the call ended, once it has.
    end: Arc<Mutex<RequestEnd>>,
}

impl Replies for Fed {
    fn next(&mut self) -> Option<Result<Sample, ReplyError>> {
        match self.fed.recv().ok()? {
            Feed::Reply(result) => Some(result.and_then(Data::into_sample)),
            Feed::End(end) => {
                *lock(&self.end) = end;
                None
            }
        }
    }

    fn give(&mut self, value: Value) {
        let request = self.request;
        let _ = self.made.send(Notice::Reply {
            request,
            result: Ok(Data::Value(value)),
        });
    }
}
