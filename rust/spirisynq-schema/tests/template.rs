//! Key templates: parsing, building keys, matching incoming keys and percent-encoding.

use proptest::prelude::*;
use spirisynq_schema::{Chunk, KeyError, KeyTemplate};

fn template(text: &str) -> KeyTemplate {
    KeyTemplate::parse(text).unwrap_or_else(|e| panic!("{text}: {e}"))
}

fn args(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(n, v)| (n.to_string(), v.to_string()))
        .collect()
}

#[test]
fn parses_chunks() {
    let t = template("read/data/{path:**}");
    assert_eq!(
        t.chunks(),
        [
            Chunk::Literal("read".into()),
            Chunk::Literal("data".into()),
            Chunk::Rest("path".into()),
        ]
    );
    assert_eq!(t.placeholders().collect::<Vec<_>>(), ["path"]);
    assert_eq!(t.key_expr(), "read/data/**");
    assert_eq!(t.to_string(), "read/data/{path:**}");

    let t = template("arm/{joint}/move/{_speed2}");
    assert_eq!(t.placeholders().collect::<Vec<_>>(), ["joint", "_speed2"]);
    assert_eq!(t.key_expr(), "arm/*/move/*");
    assert_eq!(t.to_string(), "arm/{joint}/move/{_speed2}");
    assert_eq!(t.clone(), t);
    assert!(format!("{t:?}").contains("joint"));

    let t = template("ping%");
    assert_eq!(t.placeholders().count(), 0);
    assert_eq!(t.key_expr(), "ping%");
}

#[test]
fn rejects_bad_templates() {
    for (text, reason) in [
        ("", "empty chunk"),
        ("a//b", "empty chunk"),
        ("a/", "empty chunk"),
        ("/a", "empty chunk"),
        ("{rest:**}/b", "`{rest:**}` must be the last chunk"),
        ("{a}/{a}", "placeholder `a` appears twice"),
        ("{a}/{a:**}", "placeholder `a` appears twice"),
        ("{a:*}", "bad placeholder `{a:*}`"),
        ("{a:}", "bad placeholder `{a:}`"),
        ("{1a}", "bad placeholder `{1a}`"),
        ("{}", "bad placeholder `{}`"),
        ("{a-b}", "bad placeholder `{a-b}`"),
        ("a{b}", "`{` in `a{b}`"),
        ("{a", "`{` in `{a`"),
        ("a}", "`}` in `a}`"),
        ("a*b", "`*` in `a*b`"),
        ("**", "`*` in `**`"),
        ("a$", "`$` in `a$`"),
        ("a?b", "`?` in `a?b`"),
        ("#", "`#` in `#`"),
    ] {
        assert_eq!(KeyTemplate::parse(text), Err(reason.to_owned()), "{text}");
    }
}

#[test]
fn fills_keys() {
    let t = template("read/data/{path:**}");
    assert_eq!(t.fill(&[("path", "a/b.txt")]).unwrap(), "read/data/a/b.txt");
    assert_eq!(t.fill(&[("path", "")]).unwrap(), "read/data");
    assert_eq!(
        t.fill(&[("path", "100%/*$?#")]).unwrap(),
        "read/data/100%25/%2A%24%3F%23"
    );
    let t = template("arm/{joint}");
    assert_eq!(
        t.fill(&[("other", "x"), ("joint", "a/b%")]).unwrap(),
        "arm/a%2Fb%25"
    );
    assert_eq!(t.fill(&[("joint", "élan")]).unwrap(), "arm/élan");
    assert_eq!(template("ping").fill(&[]).unwrap(), "ping");
}

#[test]
fn fill_errors() {
    let rest = template("read/{path:**}");
    for bad in ["/a", "a/", "a//b", "/"] {
        assert_eq!(
            rest.fill(&[("path", bad)]),
            Err(KeyError::EmptyChunk("path".into())),
            "{bad}"
        );
    }
    assert_eq!(
        rest.fill(&[]),
        Err(KeyError::MissingArgument("path".into()))
    );
    let one = template("arm/{joint}");
    assert_eq!(
        one.fill(&[("joint", "")]),
        Err(KeyError::EmptyChunk("joint".into()))
    );
    assert_eq!(
        one.fill(&[("arm", "x")]),
        Err(KeyError::MissingArgument("joint".into()))
    );
}

#[test]
fn matches_keys() {
    let t = template("read/data/{path:**}");
    assert_eq!(
        t.matches("read/data/a/b%25"),
        Some(Ok(args(&[("path", "a/b%")])))
    );
    assert_eq!(t.matches("read/data"), Some(Ok(args(&[("path", "")]))));
    assert_eq!(t.matches("read/other/a"), None);
    assert_eq!(t.matches("read"), None);
    assert_eq!(t.matches("read/data//a"), None);
    assert_eq!(t.matches(""), None);

    let t = template("arm/{joint}/move");
    assert_eq!(
        t.matches("arm/a%2Fb/move"),
        Some(Ok(args(&[("joint", "a/b")])))
    );
    assert_eq!(t.matches("arm/a/move/extra"), None);
    assert_eq!(t.matches("arm/a"), None);
    assert_eq!(t.matches("arm"), None);

    let t = template("{all:**}");
    assert_eq!(t.matches(""), Some(Ok(args(&[("all", "")]))));
    assert_eq!(t.matches("x/y"), Some(Ok(args(&[("all", "x/y")]))));
}

#[test]
fn decoding_accepts_only_what_encoding_writes() {
    let one = template("k/{v}");
    let rest = template("k/{v:**}");
    let bad = Some(Err(KeyError::BadEscape("v".into())));
    for chunk in ["%2a", "%41", "%", "%2", "%zz", "%é", "a*", "$", "?", "#"] {
        assert_eq!(one.matches(&format!("k/{chunk}")), bad, "{chunk}");
        assert_eq!(rest.matches(&format!("k/{chunk}")), bad, "{chunk}");
    }
    // A `/` inside one chunk only makes sense for `{name}`.
    assert_eq!(rest.matches("k/a%2Fb"), bad);
    assert_eq!(rest.matches("k/ok/%41"), bad);
    // The shape is checked before any value is decoded.
    assert_eq!(template("k/{v}/end").matches("k/%41/other"), None);
}

/// A value for a `{name}` placeholder: anything non-empty.
fn one_value() -> impl Strategy<Value = String> {
    ".+"
}

/// A value for a `{name:**}` placeholder: non-empty chunks joined by `/`, or nothing.
fn rest_value() -> impl Strategy<Value = String> {
    prop::collection::vec("[^/]+", 0..4).prop_map(|chunks| chunks.join("/"))
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn fill_then_match_round_trips(a in one_value(), b in rest_value()) {
        let t = template("x/{a}/y/{b:**}");
        let key = t.fill(&[("a", &a), ("b", &b)]).unwrap();
        prop_assert!(!key.contains("//"));
        prop_assert_eq!(t.matches(&key), Some(Ok(args(&[("a", &a), ("b", &b)]))));
        // The key never holds a raw wildcard or reserved character.
        prop_assert!(!key.contains(['*', '$', '?', '#']));
    }

    #[test]
    fn matching_never_panics(key in ".*") {
        let _ = template("x/{a}/y/{b:**}").matches(&key);
    }
}
