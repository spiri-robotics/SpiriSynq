//! Remote methods: their keys, placeholders, and the overlap and reserved-key checks.

mod common;

use common::{ok, reject};
use spirisynq_schema::{ErrorKind, KeyError, KeyTemplate};
use spirisynq_yaml::Value;

const FILES: &str = r##"
properties:
  root: {type: string}
x-remote-methods:
  move:
    description: Drive somewhere.
    parameters:
      type: object
      properties: {x: {type: number}, y: {type: number}}
      required: [x, y]
      additionalProperties: false
    returns: {type: boolean}
  read:
    x-key: read/data/{path:**}
    parameters:
      type: object
      properties: {path: {type: string}}
    returns: {type: string, contentEncoding: base64, x-yaml-tag: "!!binary"}
    x-generator: true
  stop: {}
"##;

#[test]
fn method_keys() {
    let s = ok(FILES);
    let names: Vec<&str> = s.methods().iter().map(|m| m.name.as_str()).collect();
    assert_eq!(names, ["move", "read", "stop"]);

    let mv = s.method("move").unwrap();
    assert_eq!(mv.key, KeyTemplate::parse("move").unwrap());
    assert!(!mv.generator);
    assert_eq!(
        mv.returns,
        spirisynq_yaml::parse("{type: boolean}").unwrap()
    );

    let read = s.method("read").unwrap();
    assert_eq!(read.key.key_expr(), "read/data/**");
    assert!(read.generator);

    let stop = s.method("stop").unwrap();
    assert_eq!(stop.parameters, Value::Map(vec![]));
    assert_eq!(stop.returns, Value::Map(vec![]));
    assert!(s.method("fly").is_none());
    assert_eq!(stop.clone(), *stop);
    assert!(format!("{stop:?}").contains("stop"));
}

#[test]
fn routing_incoming_keys() {
    let s = ok(FILES);
    let (m, args) = s.method_for_key("read/data/logs/today%3F").unwrap();
    assert_eq!(m.name, "read");
    assert_eq!(
        args,
        Ok(vec![("path".to_owned(), "logs/today?".to_owned())])
    );
    let (m, args) = s.method_for_key("move").unwrap();
    assert_eq!((m.name.as_str(), args), ("move", Ok(vec![])));
    let (m, args) = s.method_for_key("read/data/%41").unwrap();
    assert_eq!(
        (m.name.as_str(), args),
        ("read", Err(KeyError::BadEscape("path".into())))
    );
    assert!(s.method_for_key("root").is_none());
    assert!(s.method_for_key("read/other").is_none());
}

fn assert_rejects(body: &str, pointer: &str, kind: ErrorKind) {
    let err = reject(body);
    assert_eq!(
        (err.pointer.as_str(), &err.kind),
        (pointer, &kind),
        "{body}"
    );
}

#[test]
fn method_errors() {
    assert_rejects(
        "x-remote-methods: [move]",
        "/x-remote-methods",
        ErrorKind::Expected("a mapping"),
    );
    assert_rejects(
        "x-remote-methods: {move: []}",
        "/x-remote-methods/move",
        ErrorKind::Expected("a mapping"),
    );
    assert_rejects(
        "x-remote-methods: {\"mo?ve\": {}}",
        "/x-remote-methods/mo?ve",
        ErrorKind::Name("`?` in `mo?ve`".into()),
    );
    assert_rejects(
        "x-remote-methods: {move: {x-key: 1}}",
        "/x-remote-methods/move/x-key",
        ErrorKind::Expected("a string"),
    );
    assert_rejects(
        "x-remote-methods: {move: {x-key: \"a//b\"}}",
        "/x-remote-methods/move/x-key",
        ErrorKind::Template("empty chunk".into()),
    );
    assert_rejects(
        "x-remote-methods: {move: {parameters: []}}",
        "/x-remote-methods/move/parameters",
        ErrorKind::Expected("a mapping"),
    );
    assert_rejects(
        "x-remote-methods: {move: {x-generator: 1}}",
        "/x-remote-methods/move/x-generator",
        ErrorKind::Expected("true or false"),
    );
}

#[test]
fn placeholders_must_be_string_parameters() {
    for parameters in [
        "{}",
        "{properties: []}",
        "{properties: {}}",
        "{properties: {path: true}}",
        "{properties: {path: {}}}",
        "{properties: {path: {type: integer}}}",
        "{properties: {other: {type: string}}}",
    ] {
        assert_rejects(
            &format!(
                "x-remote-methods: {{read: {{x-key: \"read/{{path}}\", parameters: {parameters}}}}}"
            ),
            "/x-remote-methods/read/x-key",
            ErrorKind::Placeholder("path".into()),
        );
    }
}

#[test]
fn keys_must_not_overlap() {
    for (body, pointer, first, second) in [
        (
            r##"
properties:
  gps: {$ref: "#/$defs/Gps"}
x-remote-methods:
  aim: {x-key: "gps/{what}", parameters: {properties: {what: {type: string}}}}
$defs:
  Gps: {x-sync: fields, properties: {latitude: {}}}
"##,
            "/x-remote-methods/aim",
            "field `gps/latitude`",
            "remote method `aim`",
        ),
        (
            r##"
properties: {logs: {}}
x-remote-methods:
  tail: {x-key: "logs/{rest:**}", parameters: {properties: {rest: {type: string}}}}
"##,
            "/x-remote-methods/tail",
            "field `logs`",
            "remote method `tail`",
        ),
        (
            r##"
x-remote-methods:
  any: {x-key: "files/{rest:**}", parameters: {properties: {rest: {type: string}}}}
  one: {x-key: "files/x/{name}", parameters: {properties: {name: {type: string}}}}
"##,
            "/x-remote-methods/one",
            "remote method `any`",
            "remote method `one`",
        ),
        (
            r##"
x-remote-methods:
  any: {x-key: "files/{rest:**}", parameters: {properties: {rest: {type: string}}}}
  base: {x-key: "files"}
"##,
            "/x-remote-methods/base",
            "remote method `any`",
            "remote method `base`",
        ),
        (
            r##"
x-remote-methods:
  a: {x-key: "f/{x:**}", parameters: {properties: {x: {type: string}}}}
  b: {x-key: "f/{y:**}", parameters: {properties: {y: {type: string}}}}
"##,
            "/x-remote-methods/b",
            "remote method `a`",
            "remote method `b`",
        ),
        (
            "properties: {move: {}}\nx-remote-methods: {move: {}}",
            "/x-remote-methods/move",
            "field `move`",
            "remote method `move`",
        ),
    ] {
        assert_rejects(
            body,
            pointer,
            ErrorKind::Overlap(first.into(), second.into()),
        );
    }
}

#[test]
fn keys_that_dont_overlap() {
    ok(r##"
properties:
  gps: {$ref: "#/$defs/Gps"}
  logs: {}
x-remote-methods:
  aim: {x-key: "gps/aim/{what}", parameters: {properties: {what: {type: string}}}}
  one: {x-key: "files/{name}", parameters: {properties: {name: {type: string}}}}
  two: {x-key: "files/{dir}/{name}", parameters: {properties: {dir: {type: string}, name: {type: string}}}}
  tail: {x-key: "logs/tail/{rest:**}", parameters: {properties: {rest: {type: string}}}}
  read: {x-key: "read/{path:**}", parameters: {properties: {path: {type: string}}}}
  write: {x-key: "write/{path:**}", parameters: {properties: {path: {type: string}}}}
$defs:
  Gps: {x-sync: fields, properties: {latitude: {}}}
"##);
}

#[test]
fn reserved_keys() {
    for (body, pointer, member) in [
        (
            "properties: {sr_rehydrate: {}}",
            "/properties/sr_rehydrate",
            "field `sr_rehydrate`",
        ),
        (
            "x-remote-methods: {sr_metadata: {}}",
            "/x-remote-methods/sr_metadata",
            "remote method `sr_metadata`",
        ),
        (
            "x-remote-methods: {get: {x-key: \"sr_x/{a}\", parameters: {properties: {a: {type: string}}}}}",
            "/x-remote-methods/get",
            "remote method `get`",
        ),
        (
            "x-remote-methods: {get: {x-key: \"{a}/x\", parameters: {properties: {a: {type: string}}}}}",
            "/x-remote-methods/get",
            "remote method `get`",
        ),
        (
            "x-remote-methods: {get: {x-key: \"{a:**}\", parameters: {properties: {a: {type: string}}}}}",
            "/x-remote-methods/get",
            "remote method `get`",
        ),
    ] {
        assert_rejects(body, pointer, ErrorKind::Reserved(member.into()));
    }
    // Only the first chunk is reserved.
    ok(
        "properties: {gps: {$ref: \"#/$defs/G\"}}\n$defs: {G: {x-sync: fields, properties: {sr_x: {}}}}",
    );
}
