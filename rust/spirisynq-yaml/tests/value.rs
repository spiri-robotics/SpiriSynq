//! Value equality, conversions and error display.

use spirisynq_yaml::{Error, ErrorKind, Value, parse};

#[test]
fn nan_equals_nan_and_zero_signs_differ() {
    assert_eq!(Value::Float(f64::NAN), Value::Float(-f64::NAN));
    assert_ne!(Value::Float(0.0), Value::Float(-0.0));
    assert_eq!(Value::Float(1.5), Value::Float(1.5));
    assert_ne!(Value::Float(f64::NAN), Value::Float(1.0));
}

#[test]
fn equality_by_kind() {
    let all = [
        Value::Null,
        Value::Bool(true),
        Value::Int(1),
        Value::Float(1.0),
        Value::String("1".into()),
        Value::Binary(vec![1]),
        Value::Seq(vec![Value::Int(1)]),
        Value::Map(vec![(Value::Int(1), Value::Int(1))]),
        Value::tagged("T", Value::String("1".into())),
    ];
    for (i, a) in all.iter().enumerate() {
        for (j, b) in all.iter().enumerate() {
            assert_eq!(a == b, i == j, "{a:?} vs {b:?}");
        }
    }
    assert_ne!(Value::Bool(true), Value::Bool(false));
    assert_ne!(Value::Int(1), Value::Int(2));
    assert_ne!(Value::String("a".into()), Value::String("b".into()));
    assert_ne!(Value::Binary(vec![1]), Value::Binary(vec![2]));
    assert_ne!(Value::Seq(vec![]), Value::Seq(vec![Value::Null]));
    assert_ne!(
        Value::Map(vec![]),
        Value::Map(vec![(Value::Null, Value::Null)])
    );
    assert_ne!(
        Value::tagged("A", Value::Null),
        Value::tagged("B", Value::Null)
    );
}

#[test]
fn is_scalar() {
    assert!(Value::Null.is_scalar());
    assert!(Value::Binary(vec![]).is_scalar());
    assert!(Value::tagged("T", Value::String("x".into())).is_scalar());
    assert!(!Value::Seq(vec![]).is_scalar());
    assert!(!Value::Map(vec![]).is_scalar());
    assert!(!Value::tagged("T", Value::Map(vec![])).is_scalar());
}

#[test]
fn conversions() {
    assert_eq!(Value::from(true), Value::Bool(true));
    assert_eq!(Value::from(3i64), Value::Int(3));
    assert_eq!(Value::from(2.5), Value::Float(2.5));
    assert_eq!(Value::from("x"), Value::String("x".into()));
    assert_eq!(Value::from(String::from("y")), Value::String("y".into()));
}

#[test]
fn every_error_kind_displays() {
    let kinds = [
        ErrorKind::NotUtf8,
        ErrorKind::Syntax("bad".into()),
        ErrorKind::Empty,
        ErrorKind::MultipleDocuments,
        ErrorKind::ExplicitDocumentStart,
        ErrorKind::Anchor,
        ErrorKind::Alias,
        ErrorKind::Tag("!!str".into()),
        ErrorKind::Binary,
        ErrorKind::Key,
        ErrorKind::DuplicateKey("a".into()),
        ErrorKind::EmptyValue,
        ErrorKind::NumberOutOfRange("1e999".into()),
        ErrorKind::TooDeep,
        ErrorKind::TaggedContent,
    ];
    let mut seen = std::collections::HashSet::new();
    for kind in kinds {
        let text = kind.to_string();
        assert!(!text.is_empty());
        assert!(seen.insert(text), "{kind:?} shares its message");
    }
}

#[test]
fn errors_display_with_and_without_location() {
    let located = parse("a: on\nb: 1e999").unwrap_err();
    assert_eq!(
        located.to_string(),
        "line 2, column 3: number out of range: 1e999"
    );
    let unlocated = spirisynq_yaml::to_string(&Value::tagged("T", Value::Null)).unwrap_err();
    assert_eq!(unlocated.location, None);
    assert_eq!(
        unlocated.to_string(),
        "a tagged value must hold a string or a collection"
    );
    let as_std: &dyn std::error::Error = &located;
    assert!(as_std.source().is_none());
    let _: Error = located.clone();
}
