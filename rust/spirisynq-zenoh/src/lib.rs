//! Runs the SpiriSynq engine ([`spirisynq_core::Session`]) over a zenoh session.
//!
//! A [`Node`] owns a zenoh session and an engine. The binding works the engine through
//! [`Node::with`] and takes its [`Notice`]s from [`Node::notices`]. zenoh's callbacks only queue
//! what arrived; one driver thread hands it to the engine and carries out the engine's commands,
//! so the engine is never called from inside zenoh.
//!
//! Shared memory: zenoh is built with `shared-memory`, so it moves large messages between
//! processes on one machine through shared memory by itself (`transport/shared_memory` in the
//! zenoh config). Payloads that arrive are [`ZenohBytes`] wrapped in a
//! [`Payload`](spirisynq_core::Payload), shared memory included, and go back out as the same
//! buffer. [`Node::alloc_shm`] gives a binding a shared-memory buffer to write a payload into.

#![forbid(unsafe_code)]

mod net;

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread::JoinHandle;
use std::time::Duration;

pub use net::ZenohBytes;
use spirisynq_core::{Notice, Session, Zid};
pub use zenoh;
use zenoh::Wait;
use zenoh::shm::{ShmProviderState, ZShmMut};

use crate::net::{Input, Net, Step};

struct State {
    core: Session,
    net: Net,
}

impl State {
    /// Carry out every command the engine has, then pass on its notices.
    fn flush(&mut self) {
        while let Some(command) = self.core.poll_command() {
            self.net.execute(command);
        }
        while let Some(notice) = self.core.poll_notice() {
            self.net.notify(notice);
        }
    }
}

type Shared = Arc<Mutex<State>>;

fn lock(state: &Shared) -> MutexGuard<'_, State> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

/// An engine running over a zenoh session.
pub struct Node {
    state: Shared,
    zenoh: zenoh::Session,
    inbox: flume::Sender<Input>,
    notices: flume::Receiver<Notice>,
    driver: Option<JoinHandle<()>>,
}

impl Node {
    /// Open a zenoh session with `config` and start an engine on it.
    pub fn open(config: zenoh::Config) -> zenoh::Result<Node> {
        let zenoh = zenoh::open(config).wait()?;
        let (inbox, inputs) = flume::unbounded();
        let (notify, notices) = flume::unbounded();
        let zid = Zid(zenoh.zid().to_string());
        let state = Arc::new(Mutex::new(State {
            core: Session::new(zid),
            net: Net::new(zenoh.clone(), inbox.clone(), notify),
        }));
        let driven = state.clone();
        let driver = std::thread::Builder::new()
            .name("spirisynq".into())
            .spawn(move || drive(&driven, &inputs))?;
        Ok(Node {
            state,
            zenoh,
            inbox,
            notices,
            driver: Some(driver),
        })
    }

    /// Work the engine, then carry out what it asked for. Don't call [`Node::with`] from inside
    /// `f`: the engine is locked while `f` runs.
    pub fn with<R>(&self, f: impl FnOnce(&mut Session) -> R) -> R {
        let mut state = lock(&self.state);
        let result = f(&mut state.core);
        state.flush();
        result
    }

    /// The engine's notices, in order.
    pub fn notices(&self) -> &flume::Receiver<Notice> {
        &self.notices
    }

    /// This node's zenoh session, for whatever the engine doesn't do.
    pub fn zenoh(&self) -> &zenoh::Session {
        &self.zenoh
    }

    /// This node's session id, as the engine writes it.
    pub fn zid(&self) -> Zid {
        lock(&self.state).core.zid().clone()
    }

    /// A shared-memory buffer of `len` bytes, to write a payload into and send as a
    /// [`ZenohBytes`] without copying it. `None` when shared memory is turned off in the zenoh
    /// config, or none is left.
    pub fn alloc_shm(&self, len: usize) -> Option<ZShmMut> {
        // zenoh starts its provider on first use. Waiting on one that is still starting would
        // take the one message saying it's ready (zenoh 1.10), so the session would never record
        // it, and would drop it, freeing every buffer from it, with the last handle. Each call
        // here checks without taking it, so poll for up to a second instead.
        let mut state = self.zenoh.get_shm_provider();
        for _ in 0..1000 {
            if !matches!(state, ShmProviderState::Initializing(_)) {
                break;
            }
            std::thread::sleep(Duration::from_millis(1));
            state = self.zenoh.get_shm_provider();
        }
        state.into_option()?.alloc(len).wait().ok()
    }

    /// Stop the engine and close the zenoh session. Dropping the node does the same.
    pub fn close(mut self) {
        self.stop();
    }

    fn stop(&mut self) {
        if let Some(driver) = self.driver.take() {
            self.inbox
                .send(Input::Stop)
                .expect("the driver runs until told to stop");
            driver.join().expect("the driver doesn't panic");
            lock(&self.state).net.clear();
            net::report("closing the zenoh session", self.zenoh.close().wait());
        }
    }
}

impl Drop for Node {
    fn drop(&mut self) {
        self.stop();
    }
}

/// The driver thread: hand each input to the engine, then carry out what it asked for.
fn drive(state: &Shared, inputs: &flume::Receiver<Input>) {
    for input in inputs.iter() {
        let mut state = lock(state);
        let State { core, net } = &mut *state;
        match net.event(input) {
            Step::Handle(event) => {
                core.handle(event);
                state.flush();
            }
            Step::Skip => {}
            Step::Stop => return,
        }
    }
}
