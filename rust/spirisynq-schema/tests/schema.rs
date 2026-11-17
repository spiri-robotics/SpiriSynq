//! Whole schemas: the header, fields and their keys, nested dataclasses and links.

mod common;

use common::{HEADER, ok, raw, reject};
use spirisynq_schema::{DIALECT, ErrorKind, FORMAT_VERSION, TypeId, TypeSchema};
use spirisynq_yaml::Value;

fn id(text: &str) -> TypeId {
    text.parse().unwrap()
}

const CAMERA: &str = "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera";

/// A robot with every kind of field.
const ROBOT: &str = r##"
x-bases:
  - urn:pypi:spiri-robot:1.0:spirirobot.Vehicle
  - urn:uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42:7
properties:
  voltage: {type: number}
  image: {type: string, contentEncoding: base64, x-yaml-tag: "!!binary", x-skip-rehydrate: true}
  gps:
    anyOf:
      - {$ref: "#/$defs/Gps"}
      - {type: "null"}
  camera:
    anyOf:
      - {type: string, x-yaml-tag: "!Link", x-link: "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"}
      - {type: "null"}
  cameras:
    type: object
    additionalProperties: {$ref: "#/$defs/CameraLink"}
  mounts:
    type: array
    items: {$ref: "#/$defs/Mount"}
  pose: {$ref: "#/$defs/Pose"}
$defs:
  Gps:
    type: object
    x-sync: fields
    properties:
      latitude: {type: number}
      fix: {$ref: "#/$defs/Fix"}
  Fix:
    type: object
    x-sync: fields
    properties:
      quality: {type: integer, x-skip-rehydrate: false}
  CameraLink: {type: string, x-yaml-tag: "!Link", x-link: "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"}
  Mount:
    type: object
    x-yaml-tag: "!Mount"
    properties:
      slot: {type: integer}
      device:
        oneOf:
          - {$ref: "#/$defs/CameraLink"}
          - {type: string, x-yaml-tag: "!Link", x-link: "https://example.com/types/Lidar"}
  Pose:
    type: object
    properties:
      frame: {type: string}
"##;

#[test]
fn header_only() {
    let s = ok("");
    assert_eq!(s.id(), &id("urn:pypi:spiri-robot:1.0:spirirobot.Robot"));
    assert!(s.bases().is_empty());
    assert!(s.fields().is_empty());
    assert!(s.methods().is_empty());
    assert!(s.wasm().is_empty());
    assert_eq!(s.raw(), &spirisynq_yaml::parse(HEADER).unwrap());
    assert_eq!(DIALECT, "https://json-schema.org/draft/2020-12/schema");
    assert_eq!(FORMAT_VERSION, 3);
}

#[test]
fn field_keys() {
    let s = ok(ROBOT);
    let paths: Vec<&str> = s.fields().iter().map(|f| f.path.as_str()).collect();
    assert_eq!(
        paths,
        [
            "voltage",
            "image",
            "gps",
            "gps/latitude",
            "gps/fix",
            "gps/fix/quality",
            "camera",
            "cameras",
            "mounts",
            "pose",
        ]
    );
    let field = |p| s.field(p).unwrap();
    assert!(field("voltage").rehydrate);
    assert!(!field("image").rehydrate);
    assert!(field("gps/fix/quality").rehydrate);
    assert!(field("gps").by_fields);
    assert!(field("gps/fix").by_fields);
    assert!(!field("gps/latitude").by_fields);
    assert!(!field("pose").by_fields, "a plain dataclass is sent whole");
    assert_eq!(
        field("voltage").schema,
        spirisynq_yaml::parse("{type: number}").unwrap()
    );
    assert!(s.field("pose/frame").is_none());
    assert!(s.field("nope").is_none());
}

#[test]
fn links() {
    let s = ok(ROBOT);
    let links = |p| {
        s.field(p)
            .unwrap()
            .links
            .iter()
            .map(TypeId::as_str)
            .collect::<Vec<_>>()
    };
    assert_eq!(links("camera"), [CAMERA]);
    assert_eq!(links("cameras"), [CAMERA]);
    assert_eq!(links("mounts"), [CAMERA, "https://example.com/types/Lidar"]);
    assert!(links("voltage").is_empty());
    assert!(links("gps").is_empty());
}

#[test]
fn bases() {
    let s = ok(ROBOT);
    assert_eq!(s.bases().len(), 2);
    assert!(s.is_a(&id("urn:pypi:spiri-robot:9.9:spirirobot.Robot")));
    assert!(s.is_a(&id("urn:pypi:spiri_robot:0.1:spirirobot.Vehicle")));
    assert!(s.is_a(&id("urn:uuid:6F1C2A9E-4B7D-4E2A-9C1F-3D8B5E0A7C42:1")));
    assert!(!s.is_a(&id(CAMERA)));
}

#[test]
fn links_anywhere_in_a_value() {
    let s = ok(r##"
properties:
  pair:
    type: array
    prefixItems:
      - {type: string, x-link: "urn:pypi:a:1:a.A"}
      - allOf: [{type: string, x-link: "urn:pypi:b:1:b.B"}]
  table:
    type: object
    properties:
      cell: {type: string, x-link: "urn:pypi:a:2:a.A"}
  tree: {$ref: "#/$defs/Node"}
  slash: {$ref: "#/$defs/a~1b~0c"}
  external: {$ref: "https://json-schema.org/draft/2020-12/schema"}
  myself: {$ref: "#"}
  anything: true
  odd:
    type: object
    "x-link-not": 1
    1: not a keyword
    properties: {}
$defs:
  Node:
    type: object
    properties:
      children: {type: array, items: {$ref: "#/$defs/Node"}}
      owner: {x-link: "urn:pypi:c:1:c.C"}
  a/b~c: {x-link: "urn:pypi:d:1:d.D"}
"##);
    let links = |p| {
        s.field(p)
            .unwrap()
            .links
            .iter()
            .map(TypeId::as_str)
            .collect::<Vec<_>>()
    };
    // Two versions of the same type are two entries: the value says which it got.
    assert_eq!(links("pair"), ["urn:pypi:a:1:a.A", "urn:pypi:b:1:b.B"]);
    assert_eq!(links("table"), ["urn:pypi:a:2:a.A"]);
    assert_eq!(links("tree"), ["urn:pypi:c:1:c.C"]);
    assert_eq!(links("slash"), ["urn:pypi:d:1:d.D"]);
    assert!(links("external").is_empty());
    assert!(links("myself").is_empty());
    assert!(links("anything").is_empty());
    assert!(s.field("anything").unwrap().rehydrate);
    assert!(links("odd").is_empty());
}

#[test]
fn nested_dataclass_shapes() {
    let s = ok(r##"
properties:
  bare: {$ref: "#/$defs/Empty"}
  either: {anyOf: [true, {$ref: "#/$defs/Pose"}, {$ref: "#/$defs/Empty"}]}
$defs:
  Empty: {type: object, x-sync: fields}
  Pose: {type: object, properties: {x: {type: number}}}
"##);
    let paths: Vec<&str> = s.fields().iter().map(|f| f.path.as_str()).collect();
    assert_eq!(paths, ["bare", "either"]);
    assert!(s.field("bare").unwrap().by_fields);
    assert!(s.field("either").unwrap().by_fields);
}

#[test]
fn schemas_are_values() {
    let s = ok(ROBOT);
    assert_eq!(s.clone(), s);
    assert!(format!("{s:?}").contains("gps/latitude"));
    let f = s.fields()[0].clone();
    assert_eq!(f, s.fields()[0]);
    assert!(format!("{f:?}").contains("voltage"));
}

#[test]
fn header_errors() {
    let not_a_map = TypeSchema::from_value(Value::Seq(vec![])).unwrap_err();
    assert_eq!(not_a_map.pointer, "");
    assert_eq!(not_a_map.kind, ErrorKind::Expected("a mapping"));

    for (text, pointer, kind) in [
        (
            "x-spirisynq-schema: 3\n$id: urn:x:y",
            "/$schema",
            ErrorKind::Dialect,
        ),
        (
            "$schema: https://json-schema.org/draft-07/schema\nx-spirisynq-schema: 3\n$id: urn:x:y",
            "/$schema",
            ErrorKind::Dialect,
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\n$id: urn:x:y",
            "/x-spirisynq-schema",
            ErrorKind::FormatVersion,
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\nx-spirisynq-schema: 2\n$id: urn:x:y",
            "/x-spirisynq-schema",
            ErrorKind::FormatVersion,
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\nx-spirisynq-schema: \"3\"\n$id: urn:x:y",
            "/x-spirisynq-schema",
            ErrorKind::FormatVersion,
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\nx-spirisynq-schema: 3",
            "/$id",
            ErrorKind::Missing,
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\nx-spirisynq-schema: 3\n$id: Robot",
            "/$id",
            ErrorKind::TypeId("Robot".into()),
        ),
        (
            "$schema: https://json-schema.org/draft/2020-12/schema\nx-spirisynq-schema: 3\n$id: 7",
            "/$id",
            ErrorKind::Expected("a string"),
        ),
    ] {
        let err = raw(text).unwrap_err();
        assert_eq!(
            (err.pointer.as_str(), &err.kind),
            (pointer, &kind),
            "{text}"
        );
    }
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
fn base_errors() {
    assert_rejects(
        "x-bases: urn:x:y",
        "/x-bases",
        ErrorKind::Expected("a list"),
    );
    assert_rejects(
        "x-bases: [urn:x:y, nope]",
        "/x-bases/1",
        ErrorKind::TypeId("nope".into()),
    );
}

#[test]
fn def_errors() {
    assert_rejects("$defs: []", "/$defs", ErrorKind::Expected("a mapping"));
    assert_rejects(
        "$defs: {1: {}}",
        "/$defs",
        ErrorKind::Expected("string keys"),
    );
    assert_rejects(
        "$defs: {A: true}",
        "/$defs/A",
        ErrorKind::Expected("a mapping"),
    );
    assert_rejects(
        "$defs: {A: {x-sync: whole}}",
        "/$defs/A/x-sync",
        ErrorKind::Expected("`fields`"),
    );
}

#[test]
fn field_errors() {
    assert_rejects(
        "properties: [a]",
        "/properties",
        ErrorKind::Expected("a mapping"),
    );
    assert_rejects(
        "properties: {\"a*b\": {}}",
        "/properties/a*b",
        ErrorKind::Name("`*` in `a*b`".into()),
    );
    assert_rejects(
        "properties: {\"\": {}}",
        "/properties/",
        ErrorKind::Name("empty chunk".into()),
    );
    assert_rejects(
        "properties: {\"a/b\": {}}",
        "/properties/a~1b",
        ErrorKind::Name("`/` in `a/b`".into()),
    );
    assert_rejects(
        "properties: {a: {x-skip-rehydrate: yes}}",
        "/properties/a/x-skip-rehydrate",
        ErrorKind::Expected("true or false"),
    );
    assert_rejects(
        "properties: {a: {x-link: Robot}}",
        "/properties/a/x-link",
        ErrorKind::TypeId("Robot".into()),
    );
    assert_rejects(
        "properties: {a: {items: {$ref: \"#/$defs/Missing\"}}}",
        "/properties/a/items/$ref",
        ErrorKind::Ref("#/$defs/Missing".into()),
    );
    assert_rejects(
        "properties: {a: {anyOf: {type: string}}}",
        "/properties/a/anyOf",
        ErrorKind::Expected("a list"),
    );
    assert_rejects(
        "properties: {a: {prefixItems: {type: string}}}",
        "/properties/a/prefixItems",
        ErrorKind::Expected("a list"),
    );
    assert_rejects(
        "properties: {a: {properties: [b]}}",
        "/properties/a/properties",
        ErrorKind::Expected("a mapping"),
    );
}

#[test]
fn nested_errors() {
    // A missing definition is reported where the field refers to it, even without links.
    assert_rejects(
        "properties: {a: {anyOf: [{type: \"null\"}, {$ref: \"#/$defs/Gone\"}]}}",
        "/properties/a/anyOf/1/$ref",
        ErrorKind::Ref("#/$defs/Gone".into()),
    );
    assert_rejects(
        r##"
properties: {a: {$ref: "#/$defs/A"}}
$defs:
  A: {x-sync: fields, properties: {b: {$ref: "#/$defs/B"}}}
  B: {x-sync: fields, properties: {a: {anyOf: [{$ref: "#/$defs/A"}, {type: "null"}]}}}
"##,
        "/$defs/B/properties/a",
        ErrorKind::Cycle("A".into()),
    );
    assert_rejects(
        r##"
properties: {a: {anyOf: [{$ref: "#/$defs/A"}, {$ref: "#/$defs/B"}]}}
$defs:
  A: {x-sync: fields}
  B: {x-sync: fields}
"##,
        "/properties/a",
        ErrorKind::Union,
    );
    assert_rejects(
        r##"
properties: {a: {$ref: "#/$defs/A"}}
$defs:
  A: {x-sync: fields, properties: {"b c#": {}}}
"##,
        "/$defs/A/properties/b c#",
        ErrorKind::Name("`#` in `b c#`".into()),
    );
}

#[test]
fn on_demand_fields() {
    let s = ok("
properties:
  '@frames': {type: string}
  transport: {$ref: '#/$defs/Transport'}
$defs:
  Transport:
    x-sync: fields
    properties:
      quality: {type: integer}
      '@data': {type: string}
");
    let on_demand = |p| s.field(p).unwrap().on_demand;
    assert!(on_demand("@frames"));
    assert!(on_demand("transport/@data"));
    assert!(!on_demand("transport"));
    assert!(!on_demand("transport/quality"));
}
