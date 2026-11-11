use std::fmt;

/// Why a document was rejected or a value couldn't be written.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ErrorKind {
    /// The input isn't UTF-8.
    NotUtf8,
    /// The input isn't valid YAML at all.
    Syntax(String),
    /// The input holds no document.
    Empty,
    /// More than one document (`---` or `...` followed by another).
    MultipleDocuments,
    /// An explicit `---` document start (and so any directive, which needs one).
    ExplicitDocumentStart,
    /// An anchor (`&name`).
    Anchor,
    /// An alias (`*name`).
    Alias,
    /// A tag outside the subset: any `!!` tag except `!!binary`, a verbatim `!<...>` tag, the
    /// bare `!` tag, a tag redefined by `%TAG`, or a tag name with characters outside
    /// `A-Z a-z 0-9 _ . : / -`. Holds the tag as written.
    Tag(String),
    /// `!!binary` content that isn't base64, or `!!binary` on a collection.
    Binary,
    /// A mapping key that's a collection, bytes or NaN.
    Key,
    /// The same key twice in one mapping.
    DuplicateKey(String),
    /// A plain scalar with no text (`key:` with nothing after it). Write `null` instead.
    EmptyValue,
    /// An integer outside `i64`, or a float too large to be finite.
    NumberOutOfRange(String),
    /// Collections nested deeper than [`crate::MAX_DEPTH`].
    TooDeep,
    /// Writing only: a tagged value whose content is neither a string nor a collection, or a
    /// tagged value inside another without anything between them.
    TaggedContent,
}

/// A rejected document or unwritable value, with where in the input it happened, if known.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    /// What went wrong.
    pub kind: ErrorKind,
    /// 1-based line and 0-based column in the input, when parsing.
    pub location: Option<(usize, usize)>,
}

impl Error {
    pub(crate) fn new(kind: ErrorKind) -> Self {
        Error {
            kind,
            location: None,
        }
    }

    pub(crate) fn at(kind: ErrorKind, line: usize, col: usize) -> Self {
        Error {
            kind,
            location: Some((line, col)),
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        use ErrorKind::*;
        match self {
            NotUtf8 => write!(f, "input is not UTF-8"),
            Syntax(msg) => write!(f, "invalid YAML: {msg}"),
            Empty => write!(f, "no document"),
            MultipleDocuments => write!(f, "more than one document"),
            ExplicitDocumentStart => write!(f, "explicit document start (---) is not allowed"),
            Anchor => write!(f, "anchors are not allowed"),
            Alias => write!(f, "aliases are not allowed"),
            Tag(tag) => write!(f, "tag {tag} is not allowed"),
            Binary => write!(f, "invalid !!binary value"),
            Key => write!(f, "mapping keys must be scalars (not bytes or NaN)"),
            DuplicateKey(key) => write!(f, "duplicate mapping key {key}"),
            EmptyValue => write!(f, "empty value; write null instead"),
            NumberOutOfRange(text) => write!(f, "number out of range: {text}"),
            TooDeep => write!(f, "nested deeper than {}", crate::MAX_DEPTH),
            TaggedContent => write!(f, "a tagged value must hold a string or a collection"),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.location {
            Some((line, col)) => write!(f, "line {line}, column {col}: {}", self.kind),
            None => write!(f, "{}", self.kind),
        }
    }
}

impl std::error::Error for Error {}
