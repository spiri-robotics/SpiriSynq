//! Every error says something different, and says where.

mod common;

use spirisynq_schema::{Error, ErrorKind, KeyError};

#[test]
fn every_error_kind_displays_differently() {
    let kinds = [
        ErrorKind::Dialect,
        ErrorKind::FormatVersion,
        ErrorKind::Missing,
        ErrorKind::Expected("a list"),
        ErrorKind::TypeId("x".into()),
        ErrorKind::Name("empty chunk".into()),
        ErrorKind::Ref("#/$defs/A".into()),
        ErrorKind::Cycle("A".into()),
        ErrorKind::Union,
        ErrorKind::Template("empty chunk".into()),
        ErrorKind::Placeholder("path".into()),
        ErrorKind::Reserved("field `sr_x`".into()),
        ErrorKind::Overlap("field `a`".into(), "remote method `a`".into()),
        ErrorKind::Hook("no field `x`".into()),
        ErrorKind::Source,
        ErrorKind::Hash,
        ErrorKind::HashMismatch,
        ErrorKind::NameTaken("x".into()),
    ];
    let mut seen = std::collections::HashSet::new();
    for kind in kinds {
        let text = kind.to_string();
        assert!(!text.is_empty());
        assert!(seen.insert(text), "{kind:?} shares its message");
    }
}

#[test]
fn errors_say_where() {
    let err = common::reject("properties: {sr_x: {}}");
    assert_eq!(
        err.to_string(),
        "/properties/sr_x: field `sr_x` claims a reserved key (first chunk starting with `sr_`, \
         or a placeholder)"
    );
    let anywhere = "nope".parse::<spirisynq_schema::TypeId>().unwrap_err();
    assert_eq!(anywhere.to_string(), "`nope` isn't a type id");
    let as_std: &dyn std::error::Error = &err;
    assert!(as_std.source().is_none());
    let copy: Error = err.clone();
    assert_eq!(copy, err);
    assert!(format!("{err:?}").contains("Reserved"));
}

#[test]
fn key_errors_display() {
    let errors = [
        KeyError::MissingArgument("path".into()),
        KeyError::EmptyChunk("path".into()),
        KeyError::BadEscape("path".into()),
    ];
    let texts: Vec<String> = errors.iter().map(ToString::to_string).collect();
    assert_eq!(
        texts,
        [
            "no value for `path`",
            "the value for `path` makes an empty chunk",
            "the value for `path` is badly escaped",
        ]
    );
    let as_std: &dyn std::error::Error = &errors[0];
    assert!(as_std.source().is_none());
    assert_eq!(errors[0].clone(), errors[0]);
}
