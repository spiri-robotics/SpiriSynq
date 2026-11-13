//! `x-wasm` entries: roles, loaders, inline source and hashes, and what hooks attach to.

mod common;

use common::{ok, reject};
use spirisynq_schema::{ErrorKind, Loader, Role, Source};

const WASM_MODULE: [u8; 8] = [0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00];

fn text_hash(text: &str) -> String {
    Source::Text(text.into()).hash()
}

fn wasm_hash() -> String {
    Source::Binary(WASM_MODULE.to_vec()).hash()
}

/// A type with a field, a nested field and a remote method for hooks to attach to.
fn with_members(wasm: &str) -> String {
    format!(
        r##"
properties:
  voltage: {{type: number}}
  gps: {{$ref: "#/$defs/Gps"}}
x-remote-methods:
  move: {{}}
$defs:
  Gps: {{x-sync: fields, properties: {{latitude: {{type: number}}}}}}
x-wasm:
{wasm}"##
    )
}

#[test]
fn hash_is_sha256_hex() {
    assert_eq!(
        text_hash("abc"),
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        Source::Binary(b"abc".to_vec()).hash(),
        text_hash("abc"),
        "text is hashed as its UTF-8 bytes"
    );
}

#[test]
fn every_role_and_loader() {
    let py = "def f(self): return self.voltage";
    let body = with_members(&format!(
        r#"  cell_voltage:
    role: property
    loader: python
    entry: f
    source: "{py}"
    hash: {py_hash}
    returns: {{type: number}}
  scaled:
    role: method
    loader: lua
    entry: scaled
    hash: {h}
    params: {{type: object, properties: {{k: {{type: number}}}}}}
  check_move:
    role: before_call
    of: move
    loader: javascript
    entry: check
    hash: {h}
  move_result:
    role: after_call
    of: move
    loader: wat
    entry: result
    hash: {h}
  on_voltage:
    role: on_change_mirror
    of: voltage
    loader: wasm
    entry: on_voltage
    source: !!binary AGFzbQEAAAA=
    hash: {wasm_hash}
  latitude_before_send:
    role: before_send
    of: gps/latitude
    loader: future-loader
    entry: enc
    source: !!binary AGFzbQEAAAA=
    hash: {wasm_hash}
  latitude_after_receive:
    role: after_receive
    of: gps/latitude
    loader: future-loader
    entry: dec
    source: abc
    hash: {abc_hash}
  later:
    role: future-role
    of: anything at all
    loader: python
    entry: later
    hash: {h}
  later2:
    role: future-role
    loader: python
    entry: later
    hash: {h}
"#,
        py_hash = text_hash(py),
        h = text_hash("elsewhere"),
        wasm_hash = wasm_hash(),
        abc_hash = text_hash("abc"),
    ));
    let s = ok(&body);
    let summary: Vec<(&str, &str, Option<&str>, &str)> = s
        .wasm()
        .iter()
        .map(|w| {
            (
                w.name.as_str(),
                w.role.as_str(),
                w.of.first().map(String::as_str),
                w.loader.as_str(),
            )
        })
        .collect();
    assert_eq!(
        summary,
        [
            ("cell_voltage", "property", None, "python"),
            ("scaled", "method", None, "lua"),
            ("check_move", "before_call", Some("move"), "javascript"),
            ("move_result", "after_call", Some("move"), "wat"),
            ("on_voltage", "on_change_mirror", Some("voltage"), "wasm"),
            (
                "latitude_before_send",
                "before_send",
                Some("gps/latitude"),
                "future-loader"
            ),
            (
                "latitude_after_receive",
                "after_receive",
                Some("gps/latitude"),
                "future-loader"
            ),
            ("later", "future-role", Some("anything at all"), "python"),
            ("later2", "future-role", None, "python"),
        ]
    );
    let w = &s.wasm()[0];
    assert_eq!(w.role, Role::Property);
    assert_eq!(w.loader, Loader::Python);
    assert_eq!(w.entry, "f");
    assert_eq!(w.source, Some(Source::Text(py.into())));
    assert!(w.params.is_none());
    assert!(w.returns.is_some());
    assert!(s.wasm()[1].params.is_some());
    assert_eq!(s.wasm()[1].source, None);
    assert_eq!(
        s.wasm()[4].source,
        Some(Source::Binary(WASM_MODULE.to_vec()))
    );
    assert_eq!(s.wasm()[7].role, Role::Other("future-role".into()));
    assert_eq!(s.wasm()[5].loader, Loader::Other("future-loader".into()));
    assert_eq!(w.clone(), *w);
    assert!(format!("{w:?}").contains("cell_voltage"));
    for role in [
        Role::Method,
        Role::BeforeCall,
        Role::AfterCall,
        Role::OnChangeMirror,
        Role::BeforeSend,
        Role::AfterReceive,
    ] {
        assert!(s.wasm().iter().any(|w| w.role == role), "{role:?}");
    }
    for loader in [Loader::Lua, Loader::JavaScript, Loader::Wat, Loader::Wasm] {
        assert!(s.wasm().iter().any(|w| w.loader == loader), "{loader:?}");
    }
}

fn entry(lines: &str) -> String {
    let indented: String = lines.lines().map(|l| format!("    {l}\n")).collect();
    with_members(&format!("  w:\n{indented}"))
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
fn entry_errors() {
    let h = text_hash("x");
    assert_rejects("x-wasm: [w]", "/x-wasm", ErrorKind::Expected("a mapping"));
    assert_rejects(
        "x-wasm: {w: []}",
        "/x-wasm/w",
        ErrorKind::Expected("a mapping"),
    );
    for missing in ["role", "loader", "entry", "hash"] {
        let lines: String = [
            "role: property",
            "loader: python",
            "entry: f",
            &format!("hash: {h}"),
        ]
        .iter()
        .filter(|l| !l.starts_with(missing))
        .map(|l| format!("{l}\n"))
        .collect();
        assert_rejects(
            &entry(&lines),
            &format!("/x-wasm/w/{missing}"),
            ErrorKind::Missing,
        );
    }
    assert_rejects(
        &entry(&format!("role: 1\nloader: python\nentry: f\nhash: {h}")),
        "/x-wasm/w/role",
        ErrorKind::Expected("a string"),
    );
}

#[test]
fn hash_errors() {
    for bad in [
        "md5:abc",
        "sha256:abc",
        "sha256:BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ] {
        assert_rejects(
            &entry(&format!(
                "role: property\nloader: python\nentry: f\nhash: {bad}"
            )),
            "/x-wasm/w/hash",
            ErrorKind::Hash,
        );
    }
    assert_rejects(
        &entry(&format!(
            "role: property\nloader: python\nentry: f\nsource: abd\nhash: {}",
            text_hash("abc")
        )),
        "/x-wasm/w/hash",
        ErrorKind::HashMismatch,
    );
}

#[test]
fn source_errors() {
    let h = text_hash("x");
    for (loader, source) in [
        ("python", "7"),
        ("wasm", "AGFzbQEAAAA="),
        ("python", "!!binary AGFzbQEAAAA="),
        ("lua", "!!binary AGFzbQEAAAA="),
        ("future", "[a]"),
    ] {
        assert_rejects(
            &entry(&format!(
                "role: property\nloader: {loader}\nentry: f\nsource: {source}\nhash: {h}"
            )),
            "/x-wasm/w/source",
            ErrorKind::Source,
        );
    }
}

#[test]
fn hook_errors() {
    let h = text_hash("x");
    let hook = |role: &str, of: Option<&str>| {
        let of = of.map(|o| format!("of: {o}\n")).unwrap_or_default();
        entry(&format!(
            "role: {role}\n{of}loader: python\nentry: f\nhash: {h}"
        ))
    };
    assert_rejects(
        &hook("property", Some("voltage")),
        "/x-wasm/w/of",
        ErrorKind::Hook("a property hooks nothing".into()),
    );
    assert_rejects(
        &hook("method", Some("move")),
        "/x-wasm/w/of",
        ErrorKind::Hook("a method hooks nothing".into()),
    );
    for role in ["before_call", "after_receive"] {
        assert_rejects(&hook(role, None), "/x-wasm/w/of", ErrorKind::Missing);
    }
    assert_rejects(
        &hook("after_call", Some("fly")),
        "/x-wasm/w/of",
        ErrorKind::Hook("no remote method `fly`".into()),
    );
    assert_rejects(
        &hook("before_call", Some("voltage")),
        "/x-wasm/w/of",
        ErrorKind::Hook("no remote method `voltage`".into()),
    );
    assert_rejects(
        &hook("before_send", Some("gps/longitude")),
        "/x-wasm/w/of",
        ErrorKind::Hook("no field `gps/longitude`".into()),
    );
    // Only `on_change_mirror` watches several.
    assert_rejects(
        &hook("before_send", Some("[voltage]")),
        "/x-wasm/w/of",
        ErrorKind::Expected("a string"),
    );
    assert_rejects(
        &hook("on_change_mirror", Some("[voltage, 3]")),
        "/x-wasm/w/of",
        ErrorKind::Expected("a string"),
    );
    assert_rejects(
        &hook("on_change_mirror", Some("[voltage, gps/longitude]")),
        "/x-wasm/w/of",
        ErrorKind::Hook("no field `gps/longitude`".into()),
    );
    assert_rejects(
        &hook("on_change_mirror", Some("[]")),
        "/x-wasm/w/of",
        ErrorKind::Missing,
    );
}

#[test]
fn a_field_or_method_has_one_hook_of_each_kind() {
    let h = text_hash("x");
    let two = |role: &str, of: &str| {
        with_members(&format!(
            "  a:\n    role: {role}\n    of: {of}\n    loader: python\n    entry: f\n    hash: {h}\n  b:\n    role: {role}\n    of: {of}\n    loader: python\n    entry: f\n    hash: {h}\n"
        ))
    };
    for (role, of) in [
        ("before_call", "move"),
        ("after_call", "move"),
        ("before_send", "voltage"),
        ("after_receive", "voltage"),
    ] {
        assert_rejects(
            &two(role, of),
            "/x-wasm/b/of",
            ErrorKind::Hook(format!("`{of}` already has a {role}: `a`")),
        );
    }
    // Several callbacks may watch one field; one may watch several.
    let s = ok(&two("on_change_mirror", "voltage"));
    assert_eq!(s.wasm().len(), 2);
    let s = ok(&with_members(&format!(
        "  c:\n    role: on_change_mirror\n    of: [voltage, gps/latitude]\n    loader: python\n    entry: f\n    hash: {h}\n"
    )));
    assert_eq!(s.wasm()[0].of, ["voltage", "gps/latitude"]);
    // Different hooks on one field don't clash.
    ok(&with_members(&format!(
        "  a:\n    role: before_send\n    of: voltage\n    loader: python\n    entry: f\n    hash: {h}\n  b:\n    role: after_receive\n    of: voltage\n    loader: python\n    entry: f\n    hash: {h}\n"
    )));
}

#[test]
fn properties_and_methods_need_their_own_names() {
    let h = text_hash("x");
    for (name, role) in [("voltage", "property"), ("move", "method")] {
        let body = with_members(&format!(
            "  {name}:\n    role: {role}\n    loader: python\n    entry: f\n    hash: {h}\n"
        ));
        assert_rejects(
            &body,
            &format!("/x-wasm/{name}"),
            ErrorKind::NameTaken(name.into()),
        );
    }
    // A hook may share a name with what it hooks.
    ok(&with_members(&format!(
        "  move:\n    role: before_call\n    of: move\n    loader: python\n    entry: f\n    hash: {h}\n"
    )));
}
