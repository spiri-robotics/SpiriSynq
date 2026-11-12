//! An object's field values.
//!
//! Each top-level field holds a [`Value`], or the raw sample its own encoding produced. The
//! fields of a dataclass synced field by field (`gps/latitude`) live inside their top-level
//! field's value, as mapping entries; a sub-field with its own encoding keeps its last raw sample
//! beside it.

use std::collections::{BTreeMap, HashMap};

use spirisynq_yaml::Value;

use crate::Error;
use crate::transport::Sample;

#[derive(Debug)]
pub(crate) enum Stored {
    Value(Value),
    Raw(Sample),
}

#[derive(Debug)]
pub(crate) struct Store {
    top: HashMap<String, Stored>,
    raw_sub: BTreeMap<String, Sample>,
}

impl Store {
    pub(crate) fn new() -> Self {
        Store {
            top: HashMap::new(),
            raw_sub: BTreeMap::new(),
        }
    }

    /// Set a field. Whether the value changed.
    pub(crate) fn set(&mut self, path: &str, value: Value) -> Result<bool, Error> {
        let Some((top, rest)) = path.split_once('/') else {
            self.clear_sub(path);
            let old = self
                .top
                .insert(path.to_owned(), Stored::Value(value.clone()));
            return Ok(!matches!(old, Some(Stored::Value(v)) if v == value));
        };
        let missing = |at: &str| Error::ParentMissing(at.to_owned());
        let mut node = match self.top.get_mut(top) {
            Some(Stored::Value(v)) => v,
            _ => return Err(missing(top)),
        };
        let mut at = top.len();
        let names: Vec<&str> = rest.split('/').collect();
        let (last, parents) = names.split_last().expect("split yields a chunk");
        for name in parents {
            let map = entries(node).ok_or_else(|| missing(&path[..at]))?;
            at += 1 + name.len();
            let slot = position(map, name).ok_or_else(|| missing(&path[..at]))?;
            node = &mut map[slot].1;
        }
        let map = entries(node).ok_or_else(|| missing(&path[..at]))?;
        self.raw_sub.remove(path);
        Ok(match position(map, last) {
            Some(i) if map[i].1 == value => false,
            Some(i) => {
                map[i].1 = value;
                true
            }
            None => {
                map.push(((*last).into(), value));
                true
            }
        })
    }

    /// Keep a raw sample for a field with its own encoding.
    pub(crate) fn set_raw(&mut self, path: &str, sample: Sample) {
        if path.contains('/') {
            self.raw_sub.insert(path.to_owned(), sample);
        } else {
            self.clear_sub(path);
            self.top.insert(path.to_owned(), Stored::Raw(sample));
        }
    }

    /// A field's value, if it holds one.
    pub(crate) fn get(&self, path: &str) -> Option<&Value> {
        let mut parts = path.split('/');
        let Some(Stored::Value(top)) = self.top.get(parts.next()?) else { return None };
        let mut node = top;
        for name in parts {
            let map = entries_ref(node)?;
            node = &map[position(map, name)?].1;
        }
        Some(node)
    }

    pub(crate) fn top(&self, path: &str) -> Option<&Stored> {
        self.top.get(path)
    }

    pub(crate) fn raw_subs(&self) -> impl Iterator<Item = (&String, &Sample)> {
        self.raw_sub.iter()
    }

    fn clear_sub(&mut self, top: &str) {
        let prefix = format!("{top}/");
        self.raw_sub.retain(|k, _| !k.starts_with(&prefix));
    }
}

fn position(map: &[(Value, Value)], name: &str) -> Option<usize> {
    map.iter()
        .position(|(k, _)| matches!(k, Value::String(s) if s == name))
}

/// A dataclass's mapping, tagged or not.
fn entries(value: &mut Value) -> Option<&mut Vec<(Value, Value)>> {
    match value {
        Value::Map(map) => Some(map),
        Value::Tagged(t) => match &mut t.value {
            Value::Map(map) => Some(map),
            _ => None,
        },
        _ => None,
    }
}

fn entries_ref(value: &Value) -> Option<&Vec<(Value, Value)>> {
    match value {
        Value::Map(map) => Some(map),
        Value::Tagged(t) => match &t.value {
            Value::Map(map) => Some(map),
            _ => None,
        },
        _ => None,
    }
}
