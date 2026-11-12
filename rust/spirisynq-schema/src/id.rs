//! Type identity: a schema's `$id`.

use std::fmt;
use std::str::FromStr;

use crate::names::is_identifier;
use crate::template::encode;
use crate::{Error, ErrorKind};

/// A type's identity, from its schema's `$id`.
///
/// Three forms are understood:
///
/// - `urn:pypi:<distribution>:<version>:<import path>`, for types defined in a Python package,
///   e.g. `urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera`;
/// - `urn:uuid:<uuid>:<version>`, for types with no package to name them;
/// - any other absolute URI, taken as it is, with no version.
///
/// [`TypeId::same_type`] compares identities without their versions: version mismatches are
/// reported, never refused.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TypeId {
    text: String,
    identity: String,
    version: Option<String>,
}

impl TypeId {
    /// The id as written.
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The version, for the `urn:pypi:` and `urn:uuid:` forms.
    pub fn version(&self) -> Option<&str> {
        self.version.as_deref()
    }

    /// Whether both name the same type, whatever their versions. Distribution names compare
    /// as PyPI normalizes them (`Spiri_Camera` is `spiri-camera`) and UUIDs ignore case.
    pub fn same_type(&self, other: &TypeId) -> bool {
        self.identity == other.identity
    }

    /// The identity without its version, as one key chunk: `pypi:<normalized
    /// distribution>:<import path>`, `uuid:<lowercase uuid>`, or `uri:` and the URI. Characters a
    /// key chunk can't hold are percent-encoded as in a [`KeyTemplate`](crate::KeyTemplate)'s
    /// `{name}` values. Per-type discovery keys use it, so that one wildcard query finds every
    /// version of a type.
    pub fn key_chunk(&self) -> String {
        encode(&self.identity, true)
    }
}

impl FromStr for TypeId {
    type Err = Error;

    fn from_str(text: &str) -> Result<Self, Error> {
        let bad = || Error::new("", ErrorKind::TypeId(text.to_owned()));
        let (identity, version) = if let Some(rest) = strip_prefix_ci(text, "urn:pypi:") {
            let parts: Vec<&str> = rest.split(':').collect();
            let [dist, version, path] = parts[..] else { return Err(bad()) };
            if !(valid_distribution(dist) && valid_version(version) && valid_import_path(path)) {
                return Err(bad());
            }
            (format!("pypi:{}:{path}", normalize(dist)), Some(version))
        } else if let Some(rest) = strip_prefix_ci(text, "urn:uuid:") {
            let parts: Vec<&str> = rest.split(':').collect();
            let [uuid, version] = parts[..] else { return Err(bad()) };
            if !(valid_uuid(uuid) && valid_version(version)) {
                return Err(bad());
            }
            (format!("uuid:{}", uuid.to_ascii_lowercase()), Some(version))
        } else if valid_uri(text) {
            (format!("uri:{text}"), None)
        } else {
            return Err(bad());
        };
        Ok(TypeId {
            text: text.to_owned(),
            identity,
            version: version.map(str::to_owned),
        })
    }
}

impl fmt::Display for TypeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text)
    }
}

/// URN namespace identifiers are case-insensitive.
fn strip_prefix_ci<'a>(text: &'a str, prefix: &str) -> Option<&'a str> {
    let head = text.get(..prefix.len())?;
    head.eq_ignore_ascii_case(prefix)
        .then(|| &text[prefix.len()..])
}

/// PEP 508 names: ASCII letters and digits, with `.`, `_` and `-` between them.
fn valid_distribution(name: &str) -> bool {
    let alnum = |c: Option<char>| c.is_some_and(|c| c.is_ascii_alphanumeric());
    alnum(name.chars().next())
        && alnum(name.chars().last())
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
}

/// PEP 503 normalization: lowercase, and each run of `.`, `_` and `-` becomes one `-`.
fn normalize(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for c in name.chars() {
        if !"._-".contains(c) {
            out.push(c.to_ascii_lowercase());
        } else if !out.ends_with('-') {
            out.push('-');
        }
    }
    out
}

/// Loose on purpose: any PEP 440 version, or a hand-picked one, fits.
fn valid_version(version: &str) -> bool {
    !version.is_empty()
        && version
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || ".!+_-".contains(c))
}

/// A module path and a class name: at least two dotted identifiers.
fn valid_import_path(path: &str) -> bool {
    path.contains('.') && path.split('.').all(is_identifier)
}

fn valid_uuid(uuid: &str) -> bool {
    uuid.len() == 36
        && uuid.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

/// An absolute URI: a scheme, a colon and something after it, with no whitespace or controls.
fn valid_uri(text: &str) -> bool {
    let Some((scheme, rest)) = text.split_once(':') else { return false };
    let mut scheme_chars = scheme.chars();
    scheme_chars.next().is_some_and(|c| c.is_ascii_alphabetic())
        && scheme_chars.all(|c| c.is_ascii_alphanumeric() || "+.-".contains(c))
        && !rest.is_empty()
        && !text.chars().any(|c| c.is_whitespace() || c.is_control())
}
