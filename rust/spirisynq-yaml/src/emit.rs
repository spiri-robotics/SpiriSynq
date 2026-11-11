//! Writing: one canonical style that always reads back as the same value.
//!
//! Block style with two-space indents; `[]` and `{}` for empty collections; strings plain only
//! when they are made of `A-Z a-z 0-9 _ . / -` and inner spaces, start with a letter, `_` or `/`,
//! and wouldn't read as anything but a string; otherwise double-quoted.

use base64::Engine as _;

use crate::scalar::{plain_is_not_string, valid_tag_name};
use crate::{Error, ErrorKind, Value};

/// YAML allows implicit keys up to 1024 characters; longer ones are written as `? key`.
const MAX_IMPLICIT_KEY: usize = 1000;

/// Write `value` as one document of the subset, ending in a newline.
pub fn to_string(value: &Value) -> Result<String, Error> {
    let mut out = String::new();
    match render(value, 0)? {
        Out::Inline(s) => out.push_str(&s),
        Out::Block { tag, lines } => {
            if let Some(tag) = tag {
                out.push('!');
                out.push_str(&tag);
                out.push('\n');
            }
            out.push_str(&lines.join("\n"));
        }
    }
    out.push('\n');
    Ok(out)
}

enum Out {
    Inline(String),
    /// Lines at indent 0, and the tag that goes on the line introducing them.
    Block {
        tag: Option<String>,
        lines: Vec<String>,
    },
}

fn render(value: &Value, depth: usize) -> Result<Out, Error> {
    if depth > crate::MAX_DEPTH {
        return Err(Error::new(ErrorKind::TooDeep));
    }
    Ok(match value {
        Value::Seq(items) if items.is_empty() => Out::Inline("[]".into()),
        Value::Map(entries) if entries.is_empty() => Out::Inline("{}".into()),
        Value::Seq(items) => {
            let mut lines = Vec::new();
            for item in items {
                match render(item, depth + 1)? {
                    Out::Inline(s) => lines.push(format!("- {s}")),
                    Out::Block {
                        tag: None,
                        lines: inner,
                    } => {
                        let mut inner = inner.into_iter();
                        lines.push(format!("- {}", inner.next().unwrap_or_default()));
                        lines.extend(inner.map(indent));
                    }
                    Out::Block {
                        tag: Some(tag),
                        lines: inner,
                    } => {
                        lines.push(format!("- !{tag}"));
                        lines.extend(inner.into_iter().map(indent));
                    }
                }
            }
            Out::Block { tag: None, lines }
        }
        Value::Map(entries) => {
            let mut lines = Vec::new();
            for (key, val) in entries {
                let key = inline_key(key)?;
                let mut head = if key.chars().count() <= MAX_IMPLICIT_KEY {
                    vec![format!("{key}:")]
                } else {
                    vec![format!("? {key}"), ":".into()]
                };
                let last = head.len() - 1;
                match render(val, depth + 1)? {
                    Out::Inline(s) => {
                        head[last].push(' ');
                        head[last].push_str(&s);
                        lines.extend(head);
                    }
                    Out::Block { tag, lines: inner } => {
                        if let Some(tag) = tag {
                            head[last].push_str(" !");
                            head[last].push_str(&tag);
                        }
                        lines.extend(head);
                        lines.extend(inner.into_iter().map(indent));
                    }
                }
            }
            Out::Block { tag: None, lines }
        }
        Value::Tagged(t) => match &t.value {
            Value::String(s) => Out::Inline(tagged_string(&t.tag, s)?),
            Value::Seq(_) | Value::Map(_) if !valid_tag_name(&t.tag) => {
                return Err(Error::new(ErrorKind::Tag(format!("!{}", t.tag))));
            }
            Value::Seq(_) | Value::Map(_) => match render(&t.value, depth + 1)? {
                Out::Inline(s) => Out::Inline(format!("!{} {s}", t.tag)),
                Out::Block { lines, .. } => Out::Block {
                    tag: Some(t.tag.clone()),
                    lines,
                },
            },
            _ => return Err(Error::new(ErrorKind::TaggedContent)),
        },
        scalar => Out::Inline(inline_scalar(scalar)?),
    })
}

fn indent(line: String) -> String {
    format!("  {line}")
}

/// A key as written before its `:`.
pub(crate) fn inline_key(key: &Value) -> Result<String, Error> {
    match key {
        Value::Tagged(t) => match &t.value {
            Value::String(s) => tagged_string(&t.tag, s),
            _ => Err(Error::new(ErrorKind::Key)),
        },
        Value::Binary(_) => Err(Error::new(ErrorKind::Key)),
        Value::Float(f) if f.is_nan() => Err(Error::new(ErrorKind::Key)),
        scalar => inline_scalar(scalar),
    }
}

fn tagged_string(tag: &str, s: &str) -> Result<String, Error> {
    if valid_tag_name(tag) {
        Ok(format!("!{tag} {}", string(s)))
    } else {
        Err(Error::new(ErrorKind::Tag(format!("!{tag}"))))
    }
}

/// Untagged scalars. Collections and tagged values aren't inline scalars, so as keys they're
/// rejected.
fn inline_scalar(value: &Value) -> Result<String, Error> {
    Ok(match value {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Int(i) => i.to_string(),
        Value::Float(f) => float(*f),
        Value::String(s) => string(s),
        Value::Binary(bytes) => format!(
            "!!binary \"{}\"",
            base64::engine::general_purpose::STANDARD.encode(bytes)
        ),
        Value::Seq(_) | Value::Map(_) | Value::Tagged(_) => return Err(Error::new(ErrorKind::Key)),
    })
}

fn float(f: f64) -> String {
    if f.is_nan() {
        ".nan".into()
    } else if f == f64::INFINITY {
        ".inf".into()
    } else if f == f64::NEG_INFINITY {
        "-.inf".into()
    } else {
        // Debug is the shortest text that reads back as the same f64, and always has a `.` or an
        // exponent, so it never reads back as an int.
        format!("{f:?}")
    }
}

fn string(s: &str) -> String {
    if plain_safe(s) {
        s.to_owned()
    } else {
        double_quoted(s)
    }
}

fn plain_safe(s: &str) -> bool {
    let Some(first) = s.chars().next() else {
        return false;
    };
    (first.is_ascii_alphabetic() || first == '_' || first == '/')
        && !s.ends_with(' ')
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '/' | '-' | ' '))
        && !plain_is_not_string(s)
}

fn double_quoted(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            '\0' => out.push_str("\\0"),
            // Not printable in YAML, or read as a line break: C0, DEL, C1 (including NEL), the
            // Unicode line and paragraph separators, the BOM, and the two noncharacters.
            c if (c as u32) < 0x20 || c == '\u{7f}' => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c if ('\u{80}'..='\u{9f}').contains(&c)
                || matches!(
                    c,
                    '\u{2028}' | '\u{2029}' | '\u{feff}' | '\u{fffe}' | '\u{ffff}'
                ) =>
            {
                out.push_str(&format!("\\u{:04x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
