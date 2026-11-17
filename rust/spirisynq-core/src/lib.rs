//! The SpiriSynq engine.
//!
//! A [`Session`] holds a process's authoritative objects and mirrors, and does everything the
//! protocol needs: field state and change detection, echo suppression, the owner's built-in
//! queries (full state, metadata, schema), remote-call routing, liveliness and listener reports.
//! Types are described only by their [`TypeSchema`](spirisynq_schema::TypeSchema); values are
//! [`spirisynq_yaml::Value`]s.
//!
//! The engine does no I/O. It talks to the network through [`Command`]s and [`Event`]s, and to
//! its binding (Python, C, the browser) through its methods and [`Notice`]s. A transport carries
//! the commands out over zenoh; [`memory::MemoryNetwork`] carries them between sessions in one
//! process, for tests.
//!
//! WASM code from a type's `x-wasm` runs through a [`WasmHost`]; see [`Session::wasm_job`].
//!
//! The wire protocol is in `rust/ARCHITECTURE.md`; [`wire`] has its constants.

#![forbid(unsafe_code)]

mod error;
mod keyexpr;
pub mod memory;
mod notice;
mod session;
mod store;
mod transport;
mod wasm;
pub mod wire;

pub use error::Error;
pub use notice::{Data, IncomingCall, Metadata, Notice, ObjectId, Origin, ReplyError, RequestEnd};
pub use session::{CallOptions, FieldOptions, ObjectOptions, Session};
pub use transport::{
    Bytes, Command, EntityId, Event, Payload, Priority, Qos, QosOverride, QueryId, RequestId,
    Sample, Source, Zid,
};
pub use wasm::{CallJob, Frame, NextReply, Replies, WasmHost, WasmJob};
