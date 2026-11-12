//! SpiriSynq type schemas.
//!
//! A binding describes each syncable type to the core with a JSON Schema (2020-12, plus
//! SpiriSynq's `x-` keywords), as a [`spirisynq_yaml::Value`] tree. [`TypeSchema::from_value`]
//! checks it and works out what the core needs:
//!
//! - the type's identity, its [`TypeId`], and those of its bases, for checking links;
//! - every key a field travels on, relative to the object's topic ([`Field`]). A field that holds a
//!   dataclass synced field by field (`x-sync: fields` on its `$defs` entry) has a key for itself
//!   and one for each of its fields;
//! - every remote method and the [`KeyTemplate`] it answers on ([`RemoteMethod`]);
//! - the WASM code that travels with the type ([`WasmEntry`]).
//!
//! It rejects a schema whose members' keys overlap, or that claims a key SpiriSynq reserves for
//! itself (any key whose first chunk starts with `sr_`).
//!
//! Field values and method arguments aren't checked here: that is each binding's job.

#![forbid(unsafe_code)]

mod error;
mod id;
mod names;
mod read;
mod schema;
mod template;
mod wasm;

pub use error::{Error, ErrorKind, KeyError};
pub use id::TypeId;
pub use names::check_topic;
pub use schema::{DIALECT, FORMAT_VERSION, Field, RemoteMethod, TypeSchema};
pub use template::{Args, Chunk, KeyTemplate};
pub use wasm::{Loader, Role, Source, WasmEntry};
