//! Property tests: whatever the writer writes, the parser reads back as an equal value; and the
//! parser never panics on arbitrary input.

use proptest::prelude::*;
use spirisynq_yaml::{Value, parse, parse_bytes, to_string};

fn tag_name() -> impl Strategy<Value = String> {
    "[A-Za-z0-9_.:/-]{1,12}"
}

fn key() -> impl Strategy<Value = Value> {
    prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Bool),
        any::<i64>().prop_map(Value::Int),
        any::<f64>()
            .prop_filter("NaN keys are rejected", |f| !f.is_nan())
            .prop_map(Value::Float),
        any::<String>().prop_map(Value::String),
        (tag_name(), any::<String>()).prop_map(|(t, s)| Value::tagged(t, Value::String(s))),
    ]
}

fn scalar() -> impl Strategy<Value = Value> {
    prop_oneof![
        key(),
        Just(Value::Float(f64::NAN)),
        prop::collection::vec(any::<u8>(), 0..40).prop_map(Value::Binary),
        // Strings that look like other kinds, to exercise quoting.
        prop::sample::select(vec![
            "true", "null", "1", "-1.5e3", ".inf", "on", "", " ", "a: b", "#", "- x", "\u{85}"
        ])
        .prop_map(|s| Value::String(s.into())),
    ]
}

fn value() -> impl Strategy<Value = Value> {
    scalar().prop_recursive(6, 64, 8, |inner| {
        let seq = prop::collection::vec(inner.clone(), 0..6)
            .prop_map(Value::Seq)
            .boxed();
        let map = prop::collection::vec((key(), inner), 0..6)
            .prop_map(|entries| {
                // Drop later duplicates: the writer emits what it's given, and duplicates are
                // rejected on read.
                let mut out: Vec<(Value, Value)> = Vec::new();
                for (k, v) in entries {
                    if !out.iter().any(|(seen, _)| *seen == k) {
                        out.push((k, v));
                    }
                }
                Value::Map(out)
            })
            .boxed();
        prop_oneof![
            seq.clone(),
            map.clone(),
            (tag_name(), seq).prop_map(|(t, v)| Value::tagged(t, v)),
            (tag_name(), map).prop_map(|(t, v)| Value::tagged(t, v)),
        ]
    })
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn written_values_read_back_equal(v in value()) {
        let text = to_string(&v).unwrap();
        let back = parse(&text).map_err(|e| TestCaseError::fail(format!("{e}\n{text}")))?;
        prop_assert_eq!(back, v, "{}", text);
    }

    #[test]
    fn writing_is_stable(v in value()) {
        let text = to_string(&v).unwrap();
        prop_assert_eq!(to_string(&parse(&text).unwrap()).unwrap(), text);
    }

    #[test]
    fn parser_never_panics_on_text(text in any::<String>()) {
        let _ = parse(&text);
    }

    #[test]
    fn parser_never_panics_on_bytes(bytes in prop::collection::vec(any::<u8>(), 0..200)) {
        let _ = parse_bytes(&bytes);
    }

    #[test]
    fn parser_never_panics_on_yaml_like_text(text in "[-:{}\\[\\], !&*?|>'\"#%@`a-z0-9\n .]{0,80}") {
        let _ = parse(&text);
    }
}
