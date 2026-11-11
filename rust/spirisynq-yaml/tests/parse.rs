//! Reading: what the subset accepts, how scalars are typed, and everything it rejects.

use spirisynq_yaml::{ErrorKind, MAX_DEPTH, Value, parse, parse_bytes};

fn ok(text: &str) -> Value {
    parse(text).unwrap_or_else(|e| panic!("{text:?} should parse: {e}"))
}

fn err(text: &str) -> ErrorKind {
    match parse(text) {
        Ok(v) => panic!("{text:?} should be rejected, got {v:?}"),
        Err(e) => e.kind,
    }
}

fn s(text: &str) -> Value {
    Value::String(text.into())
}

fn map(entries: &[(&str, Value)]) -> Value {
    Value::Map(entries.iter().map(|(k, v)| (s(k), v.clone())).collect())
}

// --- Scalars: the JSON schema plus .inf/-.inf/.nan ---

#[test]
fn null_and_bools() {
    assert_eq!(ok("null"), Value::Null);
    assert_eq!(ok("true"), Value::Bool(true));
    assert_eq!(ok("false"), Value::Bool(false));
}

#[test]
fn yaml_1_1_and_core_schema_spellings_are_strings() {
    for text in [
        "on",
        "off",
        "yes",
        "no",
        "y",
        "n",
        "True",
        "FALSE",
        "Null",
        "NULL",
        "~",
        "0755",
        "0o17",
        "0x1F",
        "1_000",
        "+1",
        "+.inf",
        ".Inf",
        ".NaN",
        ".INF",
        "1:20",
        "2001-12-14",
        "01",
        "-01",
        "1.2.3",
        "1e",
        "1e+",
        "--1",
        ".5",
    ] {
        assert_eq!(ok(&format!("v: {text}")), map(&[("v", s(text))]), "{text}");
    }
}

#[test]
fn ints() {
    assert_eq!(ok("0"), Value::Int(0));
    assert_eq!(ok("-0"), Value::Int(0));
    assert_eq!(ok("42"), Value::Int(42));
    assert_eq!(ok("-17"), Value::Int(-17));
    assert_eq!(ok("9223372036854775807"), Value::Int(i64::MAX));
    assert_eq!(ok("-9223372036854775808"), Value::Int(i64::MIN));
}

#[test]
fn ints_outside_i64_are_rejected() {
    assert_eq!(
        err("9223372036854775808"),
        ErrorKind::NumberOutOfRange("9223372036854775808".into())
    );
    assert!(matches!(
        err("-9223372036854775809"),
        ErrorKind::NumberOutOfRange(_)
    ));
}

#[test]
fn floats() {
    assert_eq!(ok("1.5"), Value::Float(1.5));
    assert_eq!(ok("-0.25"), Value::Float(-0.25));
    assert_eq!(ok("1."), Value::Float(1.0));
    assert_eq!(ok("1e3"), Value::Float(1000.0));
    assert_eq!(ok("1E3"), Value::Float(1000.0));
    assert_eq!(ok("2.5e-3"), Value::Float(0.0025));
    assert_eq!(ok("2.5e+3"), Value::Float(2500.0));
    assert_eq!(ok("0.0"), Value::Float(0.0));
    assert_eq!(ok("-0.0"), Value::Float(-0.0));
}

#[test]
fn non_finite_floats() {
    assert_eq!(ok(".inf"), Value::Float(f64::INFINITY));
    assert_eq!(ok("-.inf"), Value::Float(f64::NEG_INFINITY));
    assert_eq!(ok(".nan"), Value::Float(f64::NAN));
}

#[test]
fn floats_too_large_to_be_finite_are_rejected() {
    assert_eq!(err("1e999"), ErrorKind::NumberOutOfRange("1e999".into()));
    assert!(matches!(err("-1e999"), ErrorKind::NumberOutOfRange(_)));
}

#[test]
fn quoted_and_block_scalars_are_always_strings() {
    assert_eq!(ok("'true'"), s("true"));
    assert_eq!(ok("\"42\""), s("42"));
    assert_eq!(ok("\"\""), s(""));
    assert_eq!(ok("''"), s(""));
    assert_eq!(ok("|\n  null\n"), s("null\n"));
    assert_eq!(ok(">\n  1.5\n"), s("1.5\n"));
}

#[test]
fn plain_strings() {
    assert_eq!(ok("hello world"), s("hello world"));
    assert_eq!(ok("robot/camera/0"), s("robot/camera/0"));
}

#[test]
fn empty_plain_values_are_rejected() {
    assert_eq!(err("a:"), ErrorKind::EmptyValue);
    assert_eq!(err("- "), ErrorKind::EmptyValue);
}

// --- Collections ---

#[test]
fn block_and_flow_collections() {
    let expected = map(&[
        ("a", Value::Int(1)),
        ("b", Value::Seq(vec![Value::Int(1), s("two")])),
        ("c", map(&[("d", Value::Null)])),
    ]);
    assert_eq!(ok("a: 1\nb:\n  - 1\n  - two\nc:\n  d: null\n"), expected);
    assert_eq!(ok("{a: 1, b: [1, two], c: {d: null}}"), expected);
    assert_eq!(ok("[]"), Value::Seq(vec![]));
    assert_eq!(ok("{}"), Value::Map(vec![]));
}

#[test]
fn json_is_accepted() {
    assert_eq!(
        ok(r#"{"a": [1, 2.5, true, null, "x"]}"#),
        map(&[(
            "a",
            Value::Seq(vec![
                Value::Int(1),
                Value::Float(2.5),
                Value::Bool(true),
                Value::Null,
                s("x")
            ])
        )])
    );
}

#[test]
fn mapping_order_is_kept() {
    let Value::Map(entries) = ok("z: 1\na: 2\nm: 3") else { panic!() };
    let keys: Vec<_> = entries.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(keys, vec![s("z"), s("a"), s("m")]);
}

#[test]
fn scalar_keys_of_every_kind() {
    assert_eq!(
        ok("null: a\ntrue: b\n1: c\n1.5: d\nx: e\n!Color red: f\n"),
        Value::Map(vec![
            (Value::Null, s("a")),
            (Value::Bool(true), s("b")),
            (Value::Int(1), s("c")),
            (Value::Float(1.5), s("d")),
            (s("x"), s("e")),
            (Value::tagged("Color", s("red")), s("f")),
        ])
    );
}

#[test]
fn keys_that_differ_only_in_kind_are_different_keys() {
    let v = ok("1: a\n\"1\": b\n1.0: c\n-0.0: d\n0.0: e\n");
    let Value::Map(entries) = v else { panic!() };
    assert_eq!(entries.len(), 5);
}

#[test]
fn collection_keys_are_rejected() {
    assert_eq!(err("? [a, b]\n: 1"), ErrorKind::Key);
    assert_eq!(err("? {a: 1}\n: 1"), ErrorKind::Key);
    assert_eq!(err("{[a]: 1}"), ErrorKind::Key);
    assert_eq!(err("? !T [a]\n: 1"), ErrorKind::Key);
}

#[test]
fn bytes_and_nan_keys_are_rejected() {
    assert_eq!(err("!!binary aGk=: 1"), ErrorKind::Key);
    assert_eq!(err(".nan: 1"), ErrorKind::Key);
}

#[test]
fn duplicate_keys_are_rejected() {
    assert_eq!(err("a: 1\na: 2"), ErrorKind::DuplicateKey("a".into()));
    assert_eq!(err("{1: a, 1: b}"), ErrorKind::DuplicateKey("1".into()));
    assert_eq!(
        err("!T x: 1\n!T x: 2"),
        ErrorKind::DuplicateKey("!T x".into())
    );
    assert_eq!(
        err("null: 1\nnull: 2"),
        ErrorKind::DuplicateKey("null".into())
    );
    // Same key in different mappings is fine.
    ok("a: {x: 1}\nb: {x: 2}");
}

#[test]
fn nesting_up_to_the_limit() {
    let deep = "[".repeat(MAX_DEPTH) + &"]".repeat(MAX_DEPTH);
    ok(&deep);
    let too_deep = "[".repeat(MAX_DEPTH + 1) + &"]".repeat(MAX_DEPTH + 1);
    assert_eq!(err(&too_deep), ErrorKind::TooDeep);
    let too_deep_maps = "{a: ".repeat(MAX_DEPTH + 1) + "1" + &"}".repeat(MAX_DEPTH + 1);
    assert_eq!(err(&too_deep_maps), ErrorKind::TooDeep);
}

// --- Tags ---

#[test]
fn local_tags() {
    assert_eq!(
        ok("!Link robot/camera"),
        Value::tagged("Link", s("robot/camera"))
    );
    assert_eq!(
        ok("!RootFrame 'world'"),
        Value::tagged("RootFrame", s("world"))
    );
    assert_eq!(
        ok("!a.b:c/d_e-f 1"),
        Value::tagged("a.b:c/d_e-f", s("1")),
        "tagged scalars are strings"
    );
    assert_eq!(
        ok("x: !Marker"),
        map(&[("x", Value::tagged("Marker", s("")))])
    );
    assert_eq!(
        ok("!Point\nx: 1\ny: 2\n"),
        Value::tagged("Point", map(&[("x", Value::Int(1)), ("y", Value::Int(2))]))
    );
    assert_eq!(
        ok("!Path [a, b]"),
        Value::tagged("Path", Value::Seq(vec![s("a"), s("b")]))
    );
}

#[test]
fn binary() {
    assert_eq!(ok("!!binary aGVsbG8="), Value::Binary(b"hello".to_vec()));
    assert_eq!(ok("!!binary \"\""), Value::Binary(vec![]));
    assert_eq!(
        ok("!!binary |\n  aGVs\n  bG8=\n"),
        Value::Binary(b"hello".to_vec())
    );
}

#[test]
fn bad_binary_is_rejected() {
    assert_eq!(err("!!binary not*base64"), ErrorKind::Binary);
    assert_eq!(err("!!binary [a]"), ErrorKind::Binary);
    assert_eq!(err("!!binary {a: 1}"), ErrorKind::Binary);
}

#[test]
fn other_tags_are_rejected() {
    for (text, tag) in [
        (
            "!!python/object/apply:os.system ['ls']",
            "!!python/object/apply:os.system",
        ),
        ("!!str 1", "!!str"),
        ("!!int 1", "!!int"),
        ("!!map {}", "!!map"),
        ("!!seq []", "!!seq"),
        ("!<tag:example.com,2000:x> 1", "!<tag:example.com,2000:x>"),
        ("! 1", "!<!>"),
        ("!a%2Cb 1", "!a,b"),
    ] {
        assert_eq!(err(text), ErrorKind::Tag(tag.into()), "{text}");
    }
}

// --- Document structure ---

#[test]
fn anchors_and_aliases_are_rejected() {
    assert_eq!(err("a: &x 1"), ErrorKind::Anchor);
    assert_eq!(err("a: &x [1]"), ErrorKind::Anchor);
    assert_eq!(err("a: &x {b: 1}"), ErrorKind::Anchor);
    assert_eq!(err("a: &x 1\nb: *x"), ErrorKind::Anchor);
}

#[test]
fn alias_is_rejected_even_without_its_anchor_in_view() {
    // An alias with no matching anchor is a syntax error to the scanner; one that refers to an
    // anchor needs the anchor first, which is already rejected. So every alias is unreachable
    // except through an anchor, and the Alias kind exists for completeness.
    assert!(matches!(err("*x"), ErrorKind::Syntax(_)));
}

#[test]
fn documents() {
    assert_eq!(err(""), ErrorKind::Empty);
    assert_eq!(err("# just a comment\n"), ErrorKind::Empty);
    assert_eq!(err("---\na: 1"), ErrorKind::ExplicitDocumentStart);
    assert!(matches!(
        err("%YAML 1.2\n---\na: 1"),
        ErrorKind::ExplicitDocumentStart | ErrorKind::Syntax(_)
    ));
    assert_eq!(err("a: 1\n...\nb: 2"), ErrorKind::MultipleDocuments);
    assert_eq!(err("a: 1\n---\nb: 2"), ErrorKind::MultipleDocuments);
    assert_eq!(ok("a: 1\n...\n"), map(&[("a", Value::Int(1))]));
    assert_eq!(
        ok("# comment\na: 1 # trailing\n"),
        map(&[("a", Value::Int(1))])
    );
}

#[test]
fn syntax_errors_carry_a_location() {
    let e = parse("a: 1\nb: [1, 2\n").unwrap_err();
    assert!(matches!(e.kind, ErrorKind::Syntax(_)));
    assert!(e.location.is_some());
    assert!(e.to_string().starts_with("line "));
}

#[test]
fn rule_errors_carry_a_location() {
    let e = parse("a: 1\nb: on\nc: 1e999\n").unwrap_err();
    assert_eq!(e.location, Some((3, 3)));
}

#[test]
fn bytes_must_be_utf8() {
    assert_eq!(parse_bytes(b"a: 1").unwrap(), map(&[("a", Value::Int(1))]));
    assert_eq!(
        parse_bytes(b"a: \xff").unwrap_err().kind,
        ErrorKind::NotUtf8
    );
}
