//! A whole type schema, and the key plan worked out from it.

use std::collections::{BTreeMap, HashSet};

use spirisynq_yaml::Value;

use crate::names::check_chunk;
use crate::read::{Map, child, entries, flag, get, list, map, string, type_id};
use crate::template::Args;
use crate::{Chunk, Error, ErrorKind, KeyError, KeyTemplate, TypeId, WasmEntry};

/// The JSON Schema dialect every type schema declares in `$schema`.
pub const DIALECT: &str = "https://json-schema.org/draft/2020-12/schema";

/// The value of `x-spirisynq-schema`: the version of SpiriSynq's schema format.
/// v1 wrote 2; v2 starts at 3.
pub const FORMAT_VERSION: i64 = 3;

const DEFS: &str = "#/$defs/";

/// One key a field's value travels on.
#[derive(Debug, Clone, PartialEq)]
pub struct Field {
    /// The key, relative to the object's topic: `voltage`, or `gps/latitude` for a field of a
    /// dataclass synced field by field.
    pub path: String,
    /// The field's JSON Schema, as written.
    pub schema: Value,
    /// Whether the field is part of the full state a new mirror fetches (`x-skip-rehydrate`
    /// turns it off).
    pub rehydrate: bool,
    /// Whether the field holds a dataclass synced field by field, whose fields have keys of
    /// their own under this one. The field's own key still carries the whole value, when the
    /// field is assigned as a whole.
    pub by_fields: bool,
    /// The types a link in this field's value may point to (`x-link`), anywhere in it. Empty if
    /// it holds no links.
    pub links: Vec<TypeId>,
}

/// A remote method.
#[derive(Debug, Clone, PartialEq)]
pub struct RemoteMethod {
    pub name: String,
    /// The key it answers on: `x-key`, or its name.
    pub key: KeyTemplate,
    /// The JSON Schema of its parameters, an object schema; `{}` if not given.
    pub parameters: Value,
    /// The JSON Schema of each value it replies with; `{}` if not given.
    pub returns: Value,
    /// Whether it may reply with any number of values (`x-generator`), rather than exactly one.
    pub generator: bool,
}

/// A checked type schema. See the crate docs.
#[derive(Debug, Clone, PartialEq)]
pub struct TypeSchema {
    id: TypeId,
    bases: Vec<TypeId>,
    fields: Vec<Field>,
    methods: Vec<RemoteMethod>,
    wasm: Vec<WasmEntry>,
    raw: Value,
}

impl TypeSchema {
    /// Check a schema and work out its key plan.
    pub fn from_value(raw: Value) -> Result<TypeSchema, Error> {
        let root = map(&raw, "")?;
        if !matches!(get(root, "$schema"), Some(Value::String(s)) if s == DIALECT) {
            return Err(Error::new("/$schema", ErrorKind::Dialect));
        }
        if get(root, "x-spirisynq-schema") != Some(&Value::Int(FORMAT_VERSION)) {
            return Err(Error::new("/x-spirisynq-schema", ErrorKind::FormatVersion));
        }
        let id = match get(root, "$id") {
            Some(v) => type_id(v, "/$id")?,
            None => return Err(Error::new("/$id", ErrorKind::Missing)),
        };
        let mut bases = Vec::new();
        if let Some(v) = get(root, "x-bases") {
            for (i, base) in list(v, "/x-bases")?.iter().enumerate() {
                bases.push(type_id(base, &child("/x-bases", &i.to_string()))?);
            }
        }

        let defs = Defs::read(root)?;
        let mut fields = Vec::new();
        let mut members = Vec::new();
        if let Some(props) = get(root, "properties") {
            defs.fields(
                props,
                "/properties",
                "",
                &mut Vec::new(),
                &mut fields,
                &mut members,
            )?;
        }

        let mut methods = Vec::new();
        if let Some(v) = get(root, "x-remote-methods") {
            for (name, entry) in entries(v, "/x-remote-methods")? {
                let at = child("/x-remote-methods", name);
                let method = read_method(name, entry, &at)?;
                members.push(Member {
                    what: format!("remote method `{name}`"),
                    at,
                    chunks: method.key.chunks().to_vec(),
                });
                methods.push(method);
            }
        }
        check_overlaps(&members)?;

        let mut wasm = Vec::new();
        if let Some(v) = get(root, "x-wasm") {
            let is_field = |path: &str| fields.iter().any(|f| f.path == path);
            let is_method = |name: &str| methods.iter().any(|m| m.name == name);
            for (name, entry) in entries(v, "/x-wasm")? {
                let at = child("/x-wasm", name);
                wasm.push(WasmEntry::read(name, entry, &at, is_field, is_method)?);
            }
        }

        Ok(TypeSchema {
            id,
            bases,
            fields,
            methods,
            wasm,
            raw,
        })
    }

    /// The type's `$id`.
    pub fn id(&self) -> &TypeId {
        &self.id
    }

    /// The `$id`s of the syncable types it derives from (`x-bases`).
    pub fn bases(&self) -> &[TypeId] {
        &self.bases
    }

    /// Whether this type is `id` or derives from it, whatever the versions: what a link to a
    /// field of type `id` requires of its target.
    pub fn is_a(&self, id: &TypeId) -> bool {
        self.id.same_type(id) || self.bases.iter().any(|b| b.same_type(id))
    }

    /// Every field key, parents before the fields of their dataclasses.
    pub fn fields(&self) -> &[Field] {
        &self.fields
    }

    /// The field on `path`, relative to the topic.
    pub fn field(&self, path: &str) -> Option<&Field> {
        self.fields.iter().find(|f| f.path == path)
    }

    pub fn methods(&self) -> &[RemoteMethod] {
        &self.methods
    }

    pub fn method(&self, name: &str) -> Option<&RemoteMethod> {
        self.methods.iter().find(|m| m.name == name)
    }

    /// The remote method that answers `key` (relative to the topic), with its placeholders'
    /// values. Keys never overlap, so at most one does.
    pub fn method_for_key(&self, key: &str) -> Option<(&RemoteMethod, Result<Args, KeyError>)> {
        self.methods
            .iter()
            .find_map(|m| m.key.matches(key).map(|args| (m, args)))
    }

    pub fn wasm(&self) -> &[WasmEntry] {
        &self.wasm
    }

    /// The schema as it was given, for serving to other processes.
    pub fn raw(&self) -> &Value {
        &self.raw
    }
}

/// A key some member answers, for the overlap check.
struct Member {
    what: String,
    at: String,
    chunks: Vec<Chunk>,
}

/// One `$defs` entry.
struct Def<'a> {
    value: &'a Value,
    map: &'a Map,
    at: String,
    by_fields: bool,
}

struct Defs<'a> {
    defs: BTreeMap<&'a str, Def<'a>>,
}

impl<'a> Defs<'a> {
    fn read(root: &'a Map) -> Result<Self, Error> {
        let mut defs = BTreeMap::new();
        if let Some(v) = get(root, "$defs") {
            for (name, def) in entries(v, "/$defs")? {
                let at = child("/$defs", name);
                let def_map = map(def, &at)?;
                let by_fields = match get(def_map, "x-sync") {
                    None => false,
                    Some(Value::String(s)) if s == "fields" => true,
                    Some(_) => {
                        return Err(Error::new(
                            child(&at, "x-sync"),
                            ErrorKind::Expected("`fields`"),
                        ));
                    }
                };
                let def = Def {
                    value: def,
                    map: def_map,
                    at,
                    by_fields,
                };
                defs.insert(name, def);
            }
        }
        Ok(Defs { defs })
    }

    /// Walk a `properties` mapping, adding a field (and a member) per key; recurse into
    /// dataclasses synced field by field. `stack` holds the definitions being walked.
    fn fields(
        &self,
        props: &'a Value,
        at: &str,
        prefix: &str,
        stack: &mut Vec<&'a str>,
        fields: &mut Vec<Field>,
        members: &mut Vec<Member>,
    ) -> Result<(), Error> {
        for (name, schema) in entries(props, at)? {
            let at = child(at, name);
            check_chunk(name).map_err(|reason| Error::new(&at, ErrorKind::Name(reason)))?;
            let rehydrate = match schema {
                Value::Map(m) => !flag(m, "x-skip-rehydrate", &at, false)?,
                _ => true,
            };
            let mut links = Vec::new();
            self.links(schema, &at, &mut HashSet::new(), &mut links)?;
            let nested = self.by_fields_def(schema, &at)?;
            let path = format!("{prefix}{name}");
            members.push(Member {
                what: format!("field `{path}`"),
                at: at.clone(),
                chunks: path.split('/').map(|c| Chunk::Literal(c.into())).collect(),
            });
            fields.push(Field {
                path: path.clone(),
                schema: schema.clone(),
                rehydrate,
                by_fields: nested.is_some(),
                links,
            });
            if let Some(def_name) = nested {
                if stack.contains(&def_name) {
                    return Err(Error::new(&at, ErrorKind::Cycle(def_name.into())));
                }
                let def = &self.defs[def_name];
                if let Some(props) = get(def.map, "properties") {
                    stack.push(def_name);
                    let props_at = child(&def.at, "properties");
                    self.fields(
                        props,
                        &props_at,
                        &format!("{path}/"),
                        stack,
                        fields,
                        members,
                    )?;
                    stack.pop();
                }
            }
        }
        Ok(())
    }

    /// The definition a `$ref` names, if it's local to `$defs`; an error if it names one that
    /// doesn't exist. Other references (the root, other documents) are left alone.
    fn local(&self, schema: &'a Map, at: &str) -> Result<Option<&'a str>, Error> {
        let Some(Value::String(target)) = get(schema, "$ref") else { return Ok(None) };
        let Some(escaped) = target.strip_prefix(DEFS) else { return Ok(None) };
        let name = escaped.replace("~1", "/").replace("~0", "~");
        match self.defs.get_key_value(name.as_str()) {
            Some((name, _)) => Ok(Some(name)),
            None => Err(Error::new(
                child(at, "$ref"),
                ErrorKind::Ref(target.clone()),
            )),
        }
    }

    /// The dataclass synced field by field that a field holds: its schema is a `$ref` to one,
    /// or an `anyOf` with one among its branches (`Gps | None`).
    fn by_fields_def(&self, schema: &'a Value, at: &str) -> Result<Option<&'a str>, Error> {
        let Value::Map(m) = schema else { return Ok(None) };
        let mut candidates = vec![(m.as_slice(), at.to_owned())];
        if let Some(any_of) = get(m, "anyOf") {
            let any_at = child(at, "anyOf");
            for (i, branch) in list(any_of, &any_at)?.iter().enumerate() {
                if let Value::Map(b) = branch {
                    candidates.push((b, child(&any_at, &i.to_string())));
                }
            }
        }
        let mut found = None;
        for (candidate, candidate_at) in candidates {
            if let Some(name) = self.local(candidate, &candidate_at)?
                && self.defs[name].by_fields
                && found.replace(name).is_some()
            {
                return Err(Error::new(at, ErrorKind::Union));
            }
        }
        Ok(found)
    }

    /// Collect every `x-link` target reachable from `schema` through its subschemas and local
    /// references.
    fn links(
        &self,
        schema: &'a Value,
        at: &str,
        visiting: &mut HashSet<&'a str>,
        out: &mut Vec<TypeId>,
    ) -> Result<(), Error> {
        let Value::Map(m) = schema else { return Ok(()) };
        if let Some(target) = get(m, "x-link") {
            let id = type_id(target, &child(at, "x-link"))?;
            if !out.contains(&id) {
                out.push(id);
            }
        }
        if let Some(name) = self.local(m, at)?
            && visiting.insert(name)
        {
            let def = &self.defs[name];
            self.links(def.value, &def.at, visiting, out)?;
        }
        for (key, sub) in m {
            if let Value::String(key) = key {
                self.subschemas(key, sub, at, visiting, out)?;
            }
        }
        Ok(())
    }

    /// Follow the subschemas under one keyword of a schema.
    fn subschemas(
        &self,
        key: &str,
        sub: &'a Value,
        at: &str,
        visiting: &mut HashSet<&'a str>,
        out: &mut Vec<TypeId>,
    ) -> Result<(), Error> {
        let at = child(at, key);
        match key {
            "items" | "additionalProperties" => self.links(sub, &at, visiting, out),
            "prefixItems" | "anyOf" | "oneOf" | "allOf" => {
                for (i, branch) in list(sub, &at)?.iter().enumerate() {
                    self.links(branch, &child(&at, &i.to_string()), visiting, out)?;
                }
                Ok(())
            }
            "properties" => {
                for (name, prop) in entries(sub, &at)? {
                    self.links(prop, &child(&at, name), visiting, out)?;
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }
}

fn read_method(name: &str, entry: &Value, at: &str) -> Result<RemoteMethod, Error> {
    check_chunk(name).map_err(|reason| Error::new(at, ErrorKind::Name(reason)))?;
    let m = map(entry, at)?;
    let key_at = child(at, "x-key");
    let key = match get(m, "x-key") {
        None => KeyTemplate::literal(name),
        Some(v) => KeyTemplate::parse(string(v, &key_at)?)
            .map_err(|reason| Error::new(&key_at, ErrorKind::Template(reason)))?,
    };
    let empty = || Value::Map(Vec::new());
    let parameters = get(m, "parameters").cloned().unwrap_or_else(empty);
    let param_props = get(map(&parameters, &child(at, "parameters"))?, "properties");
    for placeholder in key.placeholders() {
        let declared = param_props
            .and_then(|props| match props {
                Value::Map(props) => get(props, placeholder),
                _ => None,
            })
            .and_then(|schema| match schema {
                Value::Map(schema) => get(schema, "type"),
                _ => None,
            });
        if !matches!(declared, Some(Value::String(t)) if t == "string") {
            return Err(Error::new(
                &key_at,
                ErrorKind::Placeholder(placeholder.to_owned()),
            ));
        }
    }
    Ok(RemoteMethod {
        name: name.to_owned(),
        key,
        returns: get(m, "returns").cloned().unwrap_or_else(empty),
        parameters,
        generator: flag(m, "x-generator", at, false)?,
    })
}

/// No member may claim a reserved key, and no two members may answer overlapping keys.
fn check_overlaps(members: &[Member]) -> Result<(), Error> {
    for (i, member) in members.iter().enumerate() {
        let reserved = match member.chunks.first() {
            Some(Chunk::Literal(first)) => first.starts_with("sr_"),
            _ => true,
        };
        if reserved {
            return Err(Error::new(
                &member.at,
                ErrorKind::Reserved(member.what.clone()),
            ));
        }
        if let Some(other) = members[..i]
            .iter()
            .find(|o| overlap(&o.chunks, &member.chunks))
        {
            return Err(Error::new(
                &member.at,
                ErrorKind::Overlap(other.what.clone(), member.what.clone()),
            ));
        }
    }
    Ok(())
}

/// Whether some key matches both patterns. `Rest` matches zero or more chunks.
fn overlap(a: &[Chunk], b: &[Chunk]) -> bool {
    match (a.split_first(), b.split_first()) {
        (Some((Chunk::Rest(_), a_tail)), _) => {
            overlap(a_tail, b) || (!b.is_empty() && overlap(a, &b[1..]))
        }
        (_, Some((Chunk::Rest(_), b_tail))) => {
            overlap(a, b_tail) || (!a.is_empty() && overlap(&a[1..], b))
        }
        (Some((x, a_tail)), Some((y, b_tail))) => {
            let same = match (x, y) {
                (Chunk::Literal(x), Chunk::Literal(y)) => x == y,
                _ => true,
            };
            same && overlap(a_tail, b_tail)
        }
        (None, None) => true,
        _ => false,
    }
}
