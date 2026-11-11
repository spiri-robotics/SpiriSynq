//! The strict YAML 1.2 subset SpiriSynq puts on the wire.
//!
//! One parser and one writer for every language, so every binding reads the wire the same way.
//!
//! - Scalars are typed by YAML 1.2's JSON schema: only `null`, `true`, `false` and JSON-style
//!   numbers have a type, plus `.inf`, `-.inf` and `.nan`. Everything else is a string: `on`,
//!   `yes`, `True`, `0755` and `1_000` are strings. A plain scalar with no text (`key:`) is
//!   rejected; write `null`.
//! - Rejected: anchors and aliases, `---` (and so directives), more than one document,
//!   collection, bytes or NaN mapping keys, duplicate keys, input that isn't UTF-8, nesting deeper
//!   than [`MAX_DEPTH`].
//! - Tags: local tags such as `!Link` (names made of `A-Z a-z 0-9 _ . : / -`) and `!!binary` for
//!   bytes. Every other tag is rejected, including `!!python/object/apply` and the rest of the
//!   `!!` family.
//! - [`to_string`] writes one canonical style; whatever it writes, [`parse`] reads back as an equal
//!   value.

#![forbid(unsafe_code)]

mod emit;
mod error;
mod parse;
mod scalar;
mod value;

pub use emit::to_string;
pub use error::{Error, ErrorKind};
pub use parse::{parse, parse_bytes};
pub use value::{Tagged, Value};

/// How deeply collections may nest. Bounds the parser's recursion on hostile input.
pub const MAX_DEPTH: usize = 128;
