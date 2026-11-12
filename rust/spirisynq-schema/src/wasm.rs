//! `x-wasm` entries: code that travels with a type.

use sha2::{Digest, Sha256};
use spirisynq_yaml::Value;

use crate::read::{child, get, map, string};
use crate::{Error, ErrorKind};

/// What a piece of WASM code is for. Unknown roles are kept, so a newer peer's schema still
/// loads; the core can't run them.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Role {
    /// A read-only property, on any copy.
    Property,
    /// A plain method, on any copy.
    Method,
    /// A remote method's `before_call`, on the caller.
    BeforeCall,
    /// A remote method's `after_call`, on the caller.
    AfterCall,
    /// `@on_change.mirror`: runs on each mirror when a field changes.
    OnChangeMirror,
    /// `<field>_before_send`: encodes a field's value for the wire.
    BeforeSend,
    /// `<field>_after_receive`: decodes a field's value from the wire.
    AfterReceive,
    /// A role this version doesn't know.
    Other(String),
}

/// What runs a piece of code. Unknown loaders are kept, like unknown roles.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Loader {
    /// Python source, for the bundled MicroPython.
    Python,
    /// Lua source, for the bundled Lua.
    Lua,
    /// JavaScript source, for the bundled QuickJS.
    JavaScript,
    /// WebAssembly text.
    Wat,
    /// A compiled WebAssembly module.
    Wasm,
    /// A loader this version doesn't know.
    Other(String),
}

/// Inline code: text for the text loaders, bytes for `wasm`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Source {
    Text(String),
    Binary(Vec<u8>),
}

/// One `x-wasm` entry.
#[derive(Debug, Clone, PartialEq)]
pub struct WasmEntry {
    /// The entry's key in `x-wasm`: the attribute name on the class.
    pub name: String,
    pub role: Role,
    /// What a hook hooks: a remote method's name for `before_call` and `after_call`, a field's
    /// key for the field hooks. `None` for properties and methods.
    pub of: Option<String>,
    pub loader: Loader,
    /// The function to call inside the code.
    pub entry: String,
    /// The code itself, when small enough to inline; otherwise fetched by `hash`.
    pub source: Option<Source>,
    /// `sha256:` and the hex digest of the code (UTF-8 text, or the module's bytes).
    pub hash: String,
    /// The parameters' JSON Schema, if declared.
    pub params: Option<Value>,
    /// The result's JSON Schema, if declared.
    pub returns: Option<Value>,
}

/// What a role's `of` must name.
enum Target {
    Nothing,
    Method,
    Field,
    Anything,
}

impl Role {
    pub fn as_str(&self) -> &str {
        match self {
            Role::Property => "property",
            Role::Method => "method",
            Role::BeforeCall => "before_call",
            Role::AfterCall => "after_call",
            Role::OnChangeMirror => "on_change_mirror",
            Role::BeforeSend => "before_send",
            Role::AfterReceive => "after_receive",
            Role::Other(name) => name,
        }
    }

    fn parse(name: &str) -> Role {
        match name {
            "property" => Role::Property,
            "method" => Role::Method,
            "before_call" => Role::BeforeCall,
            "after_call" => Role::AfterCall,
            "on_change_mirror" => Role::OnChangeMirror,
            "before_send" => Role::BeforeSend,
            "after_receive" => Role::AfterReceive,
            other => Role::Other(other.to_owned()),
        }
    }

    fn target(&self) -> Target {
        match self {
            Role::Property | Role::Method => Target::Nothing,
            Role::BeforeCall | Role::AfterCall => Target::Method,
            Role::OnChangeMirror | Role::BeforeSend | Role::AfterReceive => Target::Field,
            Role::Other(_) => Target::Anything,
        }
    }
}

impl Loader {
    pub fn as_str(&self) -> &str {
        match self {
            Loader::Python => "python",
            Loader::Lua => "lua",
            Loader::JavaScript => "javascript",
            Loader::Wat => "wat",
            Loader::Wasm => "wasm",
            Loader::Other(name) => name,
        }
    }

    fn parse(name: &str) -> Loader {
        match name {
            "python" => Loader::Python,
            "lua" => Loader::Lua,
            "javascript" => Loader::JavaScript,
            "wat" => Loader::Wat,
            "wasm" => Loader::Wasm,
            other => Loader::Other(other.to_owned()),
        }
    }

    /// Whether `source` is the kind of code this loader takes.
    fn accepts(&self, source: &Source) -> bool {
        match (self, source) {
            (Loader::Wasm, Source::Binary(_)) | (Loader::Other(_), _) => true,
            (Loader::Wasm, Source::Text(_)) | (_, Source::Binary(_)) => false,
            (_, Source::Text(_)) => true,
        }
    }
}

impl Source {
    /// `sha256:` and the hex digest of the code, as an entry's `hash` holds it.
    pub fn hash(&self) -> String {
        let bytes = match self {
            Source::Text(text) => text.as_bytes(),
            Source::Binary(bytes) => bytes,
        };
        let digest = Sha256::digest(bytes);
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        format!("sha256:{hex}")
    }
}

impl WasmEntry {
    pub(crate) fn read(
        name: &str,
        value: &Value,
        at: &str,
        is_field: impl Fn(&str) -> bool,
        is_method: impl Fn(&str) -> bool,
    ) -> Result<WasmEntry, Error> {
        let m = map(value, at)?;
        let required = |key: &str| {
            let at = child(at, key);
            match get(m, key) {
                Some(v) => string(v, &at).map(str::to_owned),
                None => Err(Error::new(at, ErrorKind::Missing)),
            }
        };
        let role = Role::parse(&required("role")?);
        let loader = Loader::parse(&required("loader")?);
        let entry = required("entry")?;
        let hash = required("hash")?;
        let hash_at = child(at, "hash");
        if !valid_hash(&hash) {
            return Err(Error::new(hash_at, ErrorKind::Hash));
        }
        let source = match get(m, "source") {
            None => None,
            Some(Value::String(text)) => Some(Source::Text(text.clone())),
            Some(Value::Binary(bytes)) => Some(Source::Binary(bytes.clone())),
            Some(_) => return Err(Error::new(child(at, "source"), ErrorKind::Source)),
        };
        if let Some(source) = &source {
            if !loader.accepts(source) {
                return Err(Error::new(child(at, "source"), ErrorKind::Source));
            }
            if source.hash() != hash {
                return Err(Error::new(hash_at, ErrorKind::HashMismatch));
            }
        }
        let of_at = child(at, "of");
        let of = get(m, "of")
            .map(|v| string(v, &of_at).map(str::to_owned))
            .transpose()?;
        let hook = |reason: String| Err(Error::new(&of_at, ErrorKind::Hook(reason)));
        match (role.target(), &of) {
            (Target::Nothing, Some(_)) => {
                return hook(format!("a {} hooks nothing", role.as_str()));
            }
            (Target::Method | Target::Field, None) => {
                return Err(Error::new(&of_at, ErrorKind::Missing));
            }
            (Target::Method, Some(target)) if !is_method(target) => {
                return hook(format!("no remote method `{target}`"));
            }
            (Target::Field, Some(target)) if !is_field(target) => {
                return hook(format!("no field `{target}`"));
            }
            _ => {}
        }
        if matches!(role, Role::Property | Role::Method) && (is_field(name) || is_method(name)) {
            return Err(Error::new(at, ErrorKind::NameTaken(name.to_owned())));
        }
        Ok(WasmEntry {
            name: name.to_owned(),
            role,
            of,
            loader,
            entry,
            source,
            hash,
            params: get(m, "params").cloned(),
            returns: get(m, "returns").cloned(),
        })
    }
}

fn valid_hash(hash: &str) -> bool {
    hash.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}
