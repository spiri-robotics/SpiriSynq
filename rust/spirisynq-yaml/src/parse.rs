//! Reading: saphyr-parser's event stream, with the subset's rules applied on top.

use std::borrow::Cow;
use std::collections::HashSet;

use base64::Engine as _;
use saphyr_parser::{Event, Parser, ScalarStyle, ScanError, Span, StrInput, Tag};

use crate::scalar::{resolve_plain, valid_tag_name};
use crate::{Error, ErrorKind, MAX_DEPTH, Value};

const CORE_HANDLE: &str = "tag:yaml.org,2002:";

/// Parse one document of the subset from bytes, which must be UTF-8.
pub fn parse_bytes(input: &[u8]) -> Result<Value, Error> {
    let text = std::str::from_utf8(input).map_err(|_| Error::new(ErrorKind::NotUtf8))?;
    parse(text)
}

/// Parse one document of the subset.
pub fn parse(input: &str) -> Result<Value, Error> {
    let mut reader = Reader {
        parser: Parser::new_from_str(input),
    };
    // The parser always opens with StreamStart, then a DocumentStart or StreamEnd.
    reader.next()?;
    let value = match reader.next()? {
        (Event::DocumentStart(false), _) => {
            let (event, span) = reader.next()?;
            reader.node(event, span, 0)?
        }
        (Event::DocumentStart(true), span) => {
            return Err(at(ErrorKind::ExplicitDocumentStart, span));
        }
        (_, span) => return Err(at(ErrorKind::Empty, span)),
    };
    loop {
        match reader.next()? {
            (Event::DocumentEnd, _) => continue,
            (Event::StreamEnd, _) => return Ok(value),
            (_, span) => return Err(at(ErrorKind::MultipleDocuments, span)),
        }
    }
}

struct Reader<'a> {
    parser: Parser<'a, StrInput<'a>>,
}

enum TagKind {
    Local(String),
    Binary,
}

impl<'a> Reader<'a> {
    fn next(&mut self) -> Result<(Event<'a>, Span), Error> {
        // Past the end the parser returns None; we never read that far, but treat it as the end.
        let next = self
            .parser
            .next_event()
            .unwrap_or(Ok((Event::StreamEnd, Span::default())));
        next.map_err(|e| scan_error(&e))
    }

    fn node(&mut self, event: Event<'a>, span: Span, depth: usize) -> Result<Value, Error> {
        match event {
            Event::SequenceStart(anchor, tag) => {
                no_anchor(anchor, span)?;
                let tag = self.collection_tag(tag, span, depth)?;
                let mut items = Vec::new();
                loop {
                    let (event, span) = self.next()?;
                    if matches!(event, Event::SequenceEnd) {
                        break;
                    }
                    items.push(self.node(event, span, depth + 1)?);
                }
                Ok(wrap(tag, Value::Seq(items)))
            }
            Event::MappingStart(anchor, tag) => {
                no_anchor(anchor, span)?;
                let tag = self.collection_tag(tag, span, depth)?;
                let mut entries = Vec::new();
                let mut seen = HashSet::new();
                loop {
                    let (event, key_span) = self.next()?;
                    if matches!(event, Event::MappingEnd) {
                        break;
                    }
                    let key = self.node(event, key_span, depth + 1)?;
                    let repr = key_repr(&key).ok_or_else(|| at(ErrorKind::Key, key_span))?;
                    if !seen.insert(repr) {
                        let shown = crate::emit::inline_key(&key).unwrap_or_default();
                        return Err(at(ErrorKind::DuplicateKey(shown), key_span));
                    }
                    let (event, span) = self.next()?;
                    let value = self.node(event, span, depth + 1)?;
                    entries.push((key, value));
                }
                Ok(wrap(tag, Value::Map(entries)))
            }
            other => leaf(other, span),
        }
    }

    fn collection_tag(
        &self,
        tag: Option<Cow<'_, Tag>>,
        span: Span,
        depth: usize,
    ) -> Result<Option<String>, Error> {
        if depth >= MAX_DEPTH {
            return Err(at(ErrorKind::TooDeep, span));
        }
        match tag
            .as_deref()
            .map(classify)
            .transpose()
            .map_err(|e| e.located(span))?
        {
            None => Ok(None),
            Some(TagKind::Local(name)) => Ok(Some(name)),
            Some(TagKind::Binary) => Err(at(ErrorKind::Binary, span)),
        }
    }
}

/// Where a node is expected, the grammar allows only a collection start, a scalar or an alias. An
/// alias needs its anchor first, and the anchor has already been rejected, so in practice this is
/// always a scalar.
fn leaf(event: Event<'_>, span: Span) -> Result<Value, Error> {
    use ErrorKind::Alias;
    let Event::Scalar(text, style, a, tag) = event else { return Err(at(Alias, span)) };
    no_anchor(a, span)?;
    scalar(&text, style, tag.as_deref(), span)
}

fn scalar(text: &str, style: ScalarStyle, tag: Option<&Tag>, span: Span) -> Result<Value, Error> {
    match tag.map(classify).transpose().map_err(|e| e.located(span))? {
        Some(TagKind::Local(name)) => Ok(Value::tagged(name, Value::String(text.to_owned()))),
        Some(TagKind::Binary) => {
            let compact: String = text.chars().filter(|c| !c.is_ascii_whitespace()).collect();
            base64::engine::general_purpose::STANDARD
                .decode(compact)
                .map(Value::Binary)
                .map_err(|_| at(ErrorKind::Binary, span))
        }
        None if style == ScalarStyle::Plain => resolve_plain(text).map_err(|k| at(k, span)),
        None => Ok(Value::String(text.to_owned())),
    }
}

fn classify(tag: &Tag) -> Result<TagKind, Error> {
    if tag.handle == "!" && valid_tag_name(&tag.suffix) {
        Ok(TagKind::Local(tag.suffix.clone()))
    } else if tag.handle == CORE_HANDLE && tag.suffix == "binary" {
        Ok(TagKind::Binary)
    } else {
        let shown = match tag.handle.as_str() {
            "!" => format!("!{}", tag.suffix),
            CORE_HANDLE => format!("!!{}", tag.suffix),
            handle => format!("!<{handle}{}>", tag.suffix),
        };
        Err(Error::new(ErrorKind::Tag(shown)))
    }
}

fn wrap(tag: Option<String>, value: Value) -> Value {
    match tag {
        Some(name) => Value::tagged(name, value),
        None => value,
    }
}

/// What makes two keys the same key. `None` for values that can't be keys.
#[derive(Hash, PartialEq, Eq)]
enum KeyRepr {
    Null,
    Bool(bool),
    Int(i64),
    Float(u64),
    Str(String),
    Tagged(String, String),
}

fn key_repr(key: &Value) -> Option<KeyRepr> {
    Some(match key {
        Value::Null => KeyRepr::Null,
        Value::Bool(b) => KeyRepr::Bool(*b),
        Value::Int(i) => KeyRepr::Int(*i),
        Value::Float(f) if !f.is_nan() => KeyRepr::Float(f.to_bits()),
        Value::String(s) => KeyRepr::Str(s.clone()),
        Value::Tagged(t) => match &t.value {
            Value::String(s) => KeyRepr::Tagged(t.tag.clone(), s.clone()),
            _ => return None,
        },
        _ => return None,
    })
}

fn no_anchor(anchor: usize, span: Span) -> Result<(), Error> {
    if anchor == 0 {
        Ok(())
    } else {
        Err(at(ErrorKind::Anchor, span))
    }
}

fn at(kind: ErrorKind, span: Span) -> Error {
    Error::at(kind, span.start.line(), span.start.col())
}

fn scan_error(e: &ScanError) -> Error {
    Error::at(
        ErrorKind::Syntax(e.info().to_owned()),
        e.marker().line(),
        e.marker().col(),
    )
}

impl Error {
    fn located(mut self, span: Span) -> Self {
        self.location
            .get_or_insert((span.start.line(), span.start.col()));
        self
    }
}
