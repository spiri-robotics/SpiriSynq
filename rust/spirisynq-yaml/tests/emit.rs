//! Writing: the canonical text for each kind of value, and what can't be written.

use spirisynq_yaml::{ErrorKind, MAX_DEPTH, Value, parse, to_string};

fn s(text: &str) -> Value {
    Value::String(text.into())
}

fn write(v: &Value) -> String {
    let text = to_string(v).unwrap_or_else(|e| panic!("{v:?} should write: {e}"));
    assert_eq!(&parse(&text).unwrap(), v, "{text:?} should read back");
    text
}

fn map(entries: &[(&str, Value)]) -> Value {
    Value::Map(entries.iter().map(|(k, v)| (s(k), v.clone())).collect())
}

#[test]
fn scalars() {
    assert_eq!(write(&Value::Null), "null\n");
    assert_eq!(write(&Value::Bool(true)), "true\n");
    assert_eq!(write(&Value::Bool(false)), "false\n");
    assert_eq!(write(&Value::Int(-3)), "-3\n");
    assert_eq!(write(&Value::Int(i64::MIN)), "-9223372036854775808\n");
}

#[test]
fn floats_always_read_back_as_floats() {
    assert_eq!(write(&Value::Float(1.0)), "1.0\n");
    assert_eq!(write(&Value::Float(-0.0)), "-0.0\n");
    assert_eq!(write(&Value::Float(0.1)), "0.1\n");
    assert_eq!(write(&Value::Float(1e300)), "1e300\n");
    assert_eq!(write(&Value::Float(1.5e-7)), "1.5e-7\n");
    assert_eq!(write(&Value::Float(f64::INFINITY)), ".inf\n");
    assert_eq!(write(&Value::Float(f64::NEG_INFINITY)), "-.inf\n");
    assert_eq!(write(&Value::Float(f64::NAN)), ".nan\n");
    write(&Value::Float(f64::MAX));
    write(&Value::Float(f64::MIN_POSITIVE));
    write(&Value::Float(5e-324));
}

#[test]
fn plain_strings() {
    for text in [
        "hello",
        "hello world",
        "robot/camera/0",
        "_x",
        "/abs",
        "a-b.c_d",
    ] {
        assert_eq!(write(&s(text)), format!("{text}\n"));
    }
}

#[test]
fn strings_that_would_read_as_something_else_are_quoted() {
    for text in ["true", "null", "false", ".inf", ".nan", "-.inf"] {
        assert_eq!(write(&s(text)), format!("\"{text}\"\n"));
    }
    for text in [
        "1", "1.5", "-2", "1e3", "", " a", "a ", "-a", ".a", "a: b", "a #b", "é",
    ] {
        assert_eq!(write(&s(text)), format!("\"{text}\"\n"), "{text:?}");
    }
}

#[test]
fn yaml_1_1_lookalikes_stay_plain_strings() {
    // Not typed in our subset, so they can be written plain.
    for text in ["on", "yes", "True", "Null"] {
        assert_eq!(write(&s(text)), format!("{text}\n"));
    }
}

#[test]
fn escapes() {
    assert_eq!(write(&s("a\"b")), "\"a\\\"b\"\n");
    assert_eq!(write(&s("a\\b")), "\"a\\\\b\"\n");
    assert_eq!(write(&s("a\nb\tc\rd\0e")), "\"a\\nb\\tc\\rd\\0e\"\n");
    assert_eq!(write(&s("\u{1}\u{1f}\u{7f}")), "\"\\x01\\x1f\\x7f\"\n");
    assert_eq!(
        write(&s(
            "\u{80}\u{85}\u{9f}\u{2028}\u{2029}\u{feff}\u{fffe}\u{ffff}"
        )),
        "\"\\u0080\\u0085\\u009f\\u2028\\u2029\\ufeff\\ufffe\\uffff\"\n"
    );
    assert_eq!(write(&s("emoji 🤖 ok")), "\"emoji 🤖 ok\"\n");
}

#[test]
fn binary() {
    assert_eq!(
        write(&Value::Binary(b"hello".to_vec())),
        "!!binary \"aGVsbG8=\"\n"
    );
    assert_eq!(write(&Value::Binary(vec![])), "!!binary \"\"\n");
}

#[test]
fn collections() {
    assert_eq!(write(&Value::Seq(vec![])), "[]\n");
    assert_eq!(write(&Value::Map(vec![])), "{}\n");
    assert_eq!(
        write(&Value::Seq(vec![Value::Int(1), s("a")])),
        "- 1\n- a\n"
    );
    assert_eq!(
        write(&map(&[("a", Value::Int(1)), ("b", Value::Null)])),
        "a: 1\nb: null\n"
    );
}

#[test]
fn nested_collections() {
    let v = map(&[
        (
            "list",
            Value::Seq(vec![
                Value::Int(1),
                Value::Seq(vec![Value::Int(2), Value::Int(3)]),
            ]),
        ),
        (
            "obj",
            map(&[("x", Value::Int(1)), ("y", map(&[("z", Value::Int(2))]))]),
        ),
        (
            "items",
            Value::Seq(vec![map(&[("a", Value::Int(1)), ("b", Value::Int(2))])]),
        ),
        (
            "empty",
            Value::Seq(vec![Value::Map(vec![]), Value::Seq(vec![])]),
        ),
    ]);
    assert_eq!(
        write(&v),
        "list:\n  - 1\n  - - 2\n    - 3\n\
         obj:\n  x: 1\n  y:\n    z: 2\n\
         items:\n  - a: 1\n    b: 2\n\
         empty:\n  - {}\n  - []\n"
    );
}

#[test]
fn tagged_values() {
    assert_eq!(
        write(&Value::tagged("Link", s("robot/cam"))),
        "!Link robot/cam\n"
    );
    assert_eq!(write(&Value::tagged("Link", s("1"))), "!Link \"1\"\n");
    assert_eq!(write(&Value::tagged("Marker", s(""))), "!Marker \"\"\n");
    assert_eq!(write(&Value::tagged("E", Value::Map(vec![]))), "!E {}\n");
    assert_eq!(write(&Value::tagged("E", Value::Seq(vec![]))), "!E []\n");
    let point = Value::tagged("Point", map(&[("x", Value::Int(1))]));
    assert_eq!(write(&point), "!Point\nx: 1\n");
    assert_eq!(write(&map(&[("p", point.clone())])), "p: !Point\n  x: 1\n");
    assert_eq!(
        write(&Value::Seq(vec![point.clone()])),
        "- !Point\n  x: 1\n"
    );
    assert_eq!(
        write(&Value::Seq(vec![Value::tagged(
            "P",
            Value::Seq(vec![Value::Int(1)])
        )])),
        "- !P\n  - 1\n"
    );
}

#[test]
fn keys() {
    let v = Value::Map(vec![
        (Value::Null, Value::Int(1)),
        (Value::Bool(false), Value::Int(2)),
        (Value::Int(3), Value::Int(3)),
        (Value::Float(1.5), Value::Int(4)),
        (s("true"), Value::Int(5)),
        (Value::tagged("Color", s("red")), Value::Int(6)),
    ]);
    assert_eq!(
        write(&v),
        "null: 1\nfalse: 2\n3: 3\n1.5: 4\n\"true\": 5\n!Color red: 6\n"
    );
}

#[test]
fn long_keys_use_explicit_form() {
    let long = "k".repeat(1001);
    let v = Value::Map(vec![
        (s(&long), Value::Int(1)),
        (s("short"), map(&[("x", Value::Int(1))])),
        (s(&format!("{long}2")), map(&[("x", Value::Int(1))])),
        (
            s(&format!("{long}3")),
            Value::tagged("T", map(&[("x", Value::Int(1))])),
        ),
    ]);
    let text = write(&v);
    assert!(text.starts_with(&format!("? {long}\n: 1\n")));
    assert!(text.contains(&format!("? {long}2\n:\n  x: 1\n")));
    assert!(text.contains(&format!("? {long}3\n: !T\n  x: 1\n")));
    // 1000 characters is still an implicit key.
    let edge = "k".repeat(1000);
    assert_eq!(
        write(&map(&[(&edge, Value::Int(1))])),
        format!("{edge}: 1\n")
    );
}

#[test]
fn unwritable_keys() {
    for key in [
        Value::Seq(vec![]),
        Value::Map(vec![]),
        Value::Binary(vec![]),
        Value::Float(f64::NAN),
        Value::tagged("T", Value::Seq(vec![])),
    ] {
        let v = Value::Map(vec![(key.clone(), Value::Int(1))]);
        assert_eq!(to_string(&v).unwrap_err().kind, ErrorKind::Key, "{key:?}");
    }
    let bad_tag_key = Value::Map(vec![(Value::tagged("bad tag", s("x")), Value::Int(1))]);
    assert_eq!(
        to_string(&bad_tag_key).unwrap_err().kind,
        ErrorKind::Tag("!bad tag".into())
    );
}

#[test]
fn unwritable_tags() {
    for name in ["", "a b", "a!b", "a,b", "!x"] {
        let v = Value::tagged(name, s("x"));
        assert_eq!(
            to_string(&v).unwrap_err().kind,
            ErrorKind::Tag(format!("!{name}")),
            "{name:?}"
        );
    }
}

#[test]
fn tagged_content_must_be_a_string_or_collection() {
    for inner in [
        Value::Null,
        Value::Int(1),
        Value::Bool(true),
        Value::Float(1.0),
        Value::Binary(vec![]),
        Value::tagged("Inner", s("x")),
    ] {
        let v = Value::tagged("T", inner.clone());
        assert_eq!(
            to_string(&v).unwrap_err().kind,
            ErrorKind::TaggedContent,
            "{inner:?}"
        );
    }
}

#[test]
fn nesting_limit() {
    let mut v = Value::Int(1);
    for _ in 0..MAX_DEPTH {
        v = Value::Seq(vec![v]);
    }
    write(&v);
    let too_deep = Value::Seq(vec![v]);
    assert_eq!(to_string(&too_deep).unwrap_err().kind, ErrorKind::TooDeep);
}

#[test]
fn unwritable_tags_on_collections() {
    for inner in [Value::Map(vec![]), Value::Seq(vec![Value::Int(1)])] {
        let v = Value::tagged("bad tag", inner);
        assert_eq!(
            to_string(&v).unwrap_err().kind,
            ErrorKind::Tag("!bad tag".into())
        );
    }
}

#[test]
fn errors_inside_collections_propagate() {
    let bad = Value::tagged("bad tag", s("x"));
    let in_map = map(&[("ok", Value::Int(1)), ("bad", bad.clone())]);
    let in_seq = Value::Seq(vec![Value::Int(1), bad.clone()]);
    let in_tagged_map = Value::tagged("T", map(&[("bad", bad.clone())]));
    let in_tagged_seq = Value::tagged("T", Value::Seq(vec![bad.clone()]));
    for v in [in_map, in_seq, in_tagged_map, in_tagged_seq] {
        assert_eq!(
            to_string(&v).unwrap_err().kind,
            ErrorKind::Tag("!bad tag".into()),
            "{v:?}"
        );
    }
}
