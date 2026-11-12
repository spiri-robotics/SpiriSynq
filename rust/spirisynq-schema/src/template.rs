//! Key templates: the part of the key tree a remote method answers on.

use std::collections::HashSet;
use std::fmt;

use crate::KeyError;
use crate::names::{check_chunk, is_identifier};

/// Placeholder names and their decoded values, in template order.
pub type Args = Vec<(String, String)>;

/// One chunk of a [`KeyTemplate`].
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Chunk {
    /// Matches itself.
    Literal(String),
    /// `{name}`: matches exactly one chunk.
    One(String),
    /// `{name:**}`: matches the rest of the key, zero or more chunks. Always last.
    Rest(String),
}

/// A remote method's key, relative to the object's topic: `read/data/{path:**}`.
///
/// Placeholder values are percent-encoded in keys, so that they can't act as zenoh wildcards:
/// `%` `*` `$` `?` `#` become `%25` `%2A` `%24` `%3F` `%23`, and in a `{name}` value `/`
/// becomes `%2F` (a `{name:**}` value's `/`s separate its chunks). Decoding accepts exactly
/// those escapes, so each value has one key: router ACLs written against keys can't be
/// sidestepped by escaping a character that didn't need it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyTemplate {
    chunks: Vec<Chunk>,
}

impl KeyTemplate {
    /// Parse a template. Chunks are `/`-separated and never empty. A placeholder is a whole
    /// chunk; its name is an identifier; names are unique; `{name:**}` comes last. Literal
    /// chunks can't contain `*`, `$`, `?`, `#`, `{` or `}`. The error says what's wrong.
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut chunks: Vec<Chunk> = Vec::new();
        let mut names = HashSet::new();
        for part in text.split('/') {
            if let Some(Chunk::Rest(name)) = chunks.last() {
                return Err(format!("`{{{name}:**}}` must be the last chunk"));
            }
            let chunk = parse_chunk(part)?;
            if let Chunk::One(name) | Chunk::Rest(name) = &chunk
                && !names.insert(name.clone())
            {
                return Err(format!("placeholder `{name}` appears twice"));
            }
            chunks.push(chunk);
        }
        Ok(KeyTemplate { chunks })
    }

    /// A template of one literal chunk, already checked.
    pub(crate) fn literal(name: &str) -> Self {
        KeyTemplate {
            chunks: vec![Chunk::Literal(name.to_owned())],
        }
    }

    pub fn chunks(&self) -> &[Chunk] {
        &self.chunks
    }

    /// The placeholders' names, in order.
    pub fn placeholders(&self) -> impl Iterator<Item = &str> {
        self.chunks.iter().filter_map(|c| match c {
            Chunk::Literal(_) => None,
            Chunk::One(name) | Chunk::Rest(name) => Some(name.as_str()),
        })
    }

    /// The zenoh key expression an owner's queryable declares, relative to the topic:
    /// placeholders become `*` and `**`.
    pub fn key_expr(&self) -> String {
        let parts: Vec<&str> = self
            .chunks
            .iter()
            .map(|c| match c {
                Chunk::Literal(text) => text.as_str(),
                Chunk::One(_) => "*",
                Chunk::Rest(_) => "**",
            })
            .collect();
        parts.join("/")
    }

    /// The key for a call, relative to the topic, with each placeholder's value from `args`.
    pub fn fill(&self, args: &[(&str, &str)]) -> Result<String, KeyError> {
        let mut parts = Vec::with_capacity(self.chunks.len());
        for chunk in &self.chunks {
            match chunk {
                Chunk::Literal(text) => parts.push(text.clone()),
                Chunk::One(name) => {
                    let value = argument(args, name)?;
                    if value.is_empty() {
                        return Err(KeyError::EmptyChunk(name.clone()));
                    }
                    parts.push(encode(value, true));
                }
                Chunk::Rest(name) => {
                    let value = argument(args, name)?;
                    if value.is_empty() {
                        continue;
                    }
                    for part in value.split('/') {
                        if part.is_empty() {
                            return Err(KeyError::EmptyChunk(name.clone()));
                        }
                        parts.push(encode(part, false));
                    }
                }
            }
        }
        Ok(parts.join("/"))
    }

    /// Match an incoming key, relative to the topic. `None` if the key doesn't have the
    /// template's shape; otherwise each placeholder's decoded value, or why a value couldn't be
    /// decoded.
    pub fn matches(&self, key: &str) -> Option<Result<Args, KeyError>> {
        let parts: Vec<&str> = if key.is_empty() {
            Vec::new()
        } else {
            key.split('/').collect()
        };
        if parts.contains(&"") {
            return None;
        }
        let mut captures: Vec<(&String, &[&str], bool)> = Vec::new();
        let mut pos = 0;
        for chunk in &self.chunks {
            match chunk {
                Chunk::Literal(text) => {
                    if parts.get(pos) != Some(&text.as_str()) {
                        return None;
                    }
                    pos += 1;
                }
                Chunk::One(name) => {
                    captures.push((name, parts.get(pos..=pos)?, true));
                    pos += 1;
                }
                Chunk::Rest(name) => {
                    captures.push((name, &parts[pos..], false));
                    pos = parts.len();
                }
            }
        }
        if pos != parts.len() {
            return None;
        }
        Some(
            captures
                .into_iter()
                .map(|(name, chunks, one)| {
                    let decoded: Option<Vec<String>> =
                        chunks.iter().map(|c| decode(c, one)).collect();
                    match decoded {
                        Some(values) => Ok((name.clone(), values.join("/"))),
                        None => Err(KeyError::BadEscape(name.clone())),
                    }
                })
                .collect(),
        )
    }
}

impl fmt::Display for KeyTemplate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (i, chunk) in self.chunks.iter().enumerate() {
            if i > 0 {
                f.write_str("/")?;
            }
            match chunk {
                Chunk::Literal(text) => f.write_str(text)?,
                Chunk::One(name) => write!(f, "{{{name}}}")?,
                Chunk::Rest(name) => write!(f, "{{{name}:**}}")?,
            }
        }
        Ok(())
    }
}

fn parse_chunk(part: &str) -> Result<Chunk, String> {
    let Some(inner) = part.strip_prefix('{').and_then(|p| p.strip_suffix('}')) else {
        check_chunk(part)?;
        return Ok(Chunk::Literal(part.to_owned()));
    };
    let (name, rest) = match inner.split_once(':') {
        None => (inner, false),
        Some((name, "**")) => (name, true),
        Some(_) => return Err(format!("bad placeholder `{part}`")),
    };
    if !is_identifier(name) {
        return Err(format!("bad placeholder `{part}`"));
    }
    let name = name.to_owned();
    Ok(if rest {
        Chunk::Rest(name)
    } else {
        Chunk::One(name)
    })
}

fn argument<'a>(args: &[(&str, &'a str)], name: &str) -> Result<&'a str, KeyError> {
    args.iter()
        .find_map(|(n, v)| (*n == name).then_some(*v))
        .ok_or_else(|| KeyError::MissingArgument(name.to_owned()))
}

const ESCAPES: [(char, &str); 6] = [
    ('%', "%25"),
    ('*', "%2A"),
    ('$', "%24"),
    ('?', "%3F"),
    ('#', "%23"),
    ('/', "%2F"),
];

/// Percent-encode one chunk's value. `/` is escaped only where it can't separate chunks.
pub(crate) fn encode(value: &str, escape_slash: bool) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        match ESCAPES.iter().find(|(raw, _)| *raw == c) {
            Some((raw, escaped)) if *raw != '/' || escape_slash => out.push_str(escaped),
            _ => out.push(c),
        }
    }
    out
}

/// Undo [`encode`], accepting only what it writes.
fn decode(chunk: &str, slash_escaped: bool) -> Option<String> {
    let mut out = String::with_capacity(chunk.len());
    let mut rest = chunk;
    while let Some(c) = rest.chars().next() {
        if c == '%' {
            let code = rest.get(..3)?;
            let (raw, _) = ESCAPES
                .iter()
                .find(|(raw, escaped)| *escaped == code && (*raw != '/' || slash_escaped))?;
            out.push(*raw);
            rest = &rest[3..];
        } else if "*$?#".contains(c) {
            return None;
        } else {
            out.push(c);
            rest = &rest[c.len_utf8()..];
        }
    }
    Some(out)
}
