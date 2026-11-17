//! WASM field hooks, run on a scripted host: `on_change_mirror`, `after_receive` and
//! `before_send`.

mod common;

use common::*;
use serde_json::{Value as Json, json};
use spirisynq_core::{Data, Error, Frame, Notice, ObjectId, ObjectOptions, Origin, WasmHost};
use spirisynq_schema::{Role, WasmEntry};
use spirisynq_yaml::Value;

const HASH: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

const CAMERA: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Camera
properties:
  current_a: {type: number}
  smoothed_a: {type: number}
  charging: {type: boolean}
  image: {type: string, x-yaml-tag: "!!binary"}
  plain: {type: string}
x-remote-methods:
  snap: {}
x-wasm:
  smooth: {role: on_change_mirror, of: current_a, loader: wat, entry: smooth, hash: HASH}
  watch: {role: on_change_mirror, of: [current_a, charging], loader: wat, entry: watch, hash: HASH}
  image_after_receive: {role: after_receive, of: image, loader: wat, entry: dec, hash: HASH}
  image_before_send: {role: before_send, of: image, loader: wat, entry: enc, hash: HASH}
  look: {role: method, loader: wat, entry: look, hash: HASH}
  check: {role: before_call, of: snap, loader: wat, entry: check, hash: HASH}
"##;

/// A host that runs a script instead of WASM.
type Body = Box<dyn FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String>>;

struct Script(Body);

impl WasmHost for Script {
    fn run(&mut self, code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String> {
        (self.0)(code, frame)
    }
}

fn host(f: impl FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String> + 'static) -> Script {
    Script(Box::new(f))
}

fn args(frame: &Frame) -> Json {
    let input: Json = serde_json::from_slice(&frame.input()).unwrap();
    input["args"].clone()
}

fn wasm_error(result: Result<impl std::fmt::Debug, Error>) -> String {
    match result {
        Err(Error::Wasm(why)) => why,
        other => panic!("{other:?}"),
    }
}

/// A camera owned on session 0, and a mirror of it on session 1.
fn cameras() -> (Net, ObjectId, ObjectId) {
    let schema = schema_from(&CAMERA.replace("HASH", HASH));
    let mut net = Net::new();
    net.add_session();
    let owner = net
        .session(0)
        .create_owner("robot/camera", schema.clone(), ObjectOptions::default())
        .unwrap();
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/camera", schema, ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    (net, owner, mirror)
}

#[test]
fn hooks_are_found_by_role_and_what_they_hook() {
    let (mut net, owner, _) = cameras();
    let s = net.session(0);
    let names = |role: Role, of: &str| s.hooks(owner, &role, of).unwrap();
    assert_eq!(
        names(Role::OnChangeMirror, "current_a"),
        ["smooth", "watch"]
    );
    assert_eq!(names(Role::OnChangeMirror, "charging"), ["watch"]);
    assert_eq!(names(Role::AfterReceive, "image"), ["image_after_receive"]);
    assert_eq!(names(Role::BeforeCall, "snap"), ["check"]);
    assert!(names(Role::BeforeSend, "plain").is_empty());
}

#[test]
fn on_change_mirror_sets_only_the_mirrors_copy() {
    let (mut net, owner, mirror) = cameras();
    let s = net.session(1);
    s.set_field_local(mirror, "smoothed_a", v("1.0")).unwrap();
    let mut code = host(|code, frame| {
        match code.name.as_str() {
            "smooth" => {
                assert_eq!(args(frame), json!({"value": 3.0}));
                let old = frame.field("smoothed_a")?.as_f64().unwrap();
                frame.set_field("smoothed_a", json!(0.5 * old + 0.5 * 3.0))?;
            }
            _ => {
                // It watches two fields, so it's told which changed; it sees what `smooth` set.
                assert_eq!(args(frame), json!({"value": 3.0, "field": "current_a"}));
                assert_eq!(frame.field("smoothed_a"), Ok(json!(2.0)));
                frame.set_field("charging", json!(true))?;
            }
        }
        Ok(Vec::new())
    });
    s.run_on_change(&mut code, mirror, "current_a", &v("3.0"))
        .unwrap();
    assert_eq!(s.field(mirror, "smoothed_a").unwrap(), Some(&v("2.0")));
    assert_eq!(s.field(mirror, "charging").unwrap(), Some(&v("true")));
    settle(&mut net);
    // Nothing was published or written to the owner.
    assert_eq!(net.session(0).field(owner, "smoothed_a").unwrap(), None);
    // A field nothing watches runs nothing.
    let mut never = host(|_, _| panic!("nothing watches `plain`"));
    net.session(1)
        .run_on_change(&mut never, mirror, "plain", &v("x"))
        .unwrap();
}

#[test]
fn on_change_mirror_runs_only_on_mirrors_and_for_what_it_watches() {
    let (mut net, owner, mirror) = cameras();
    let one = v("1");
    assert_eq!(
        net.session(0)
            .on_change_job(owner, "smooth", "current_a", &one)
            .map(|_| ()),
        Err(Error::NotMirror)
    );
    let s = net.session(1);
    let refused = |s: &mut spirisynq_core::Session, name: &str, field: &str, value: &Value| {
        s.on_change_job(mirror, name, field, value).map(|_| ())
    };
    assert_eq!(
        refused(s, "nope", "current_a", &one),
        Err(Error::UnknownCode("nope".into()))
    );
    for (name, field) in [("smooth", "charging"), ("look", "current_a")] {
        assert_eq!(
            wasm_error(refused(s, name, field, &one)),
            format!("`{name}` doesn't watch `{field}`")
        );
    }
    assert_eq!(
        wasm_error(refused(s, "smooth", "current_a", &v(".nan"))),
        "the value: NaN has no JSON form"
    );
    // The first that fails stops the rest.
    let mut failing = host(|code, _| {
        assert_eq!(code.name, "smooth");
        Err("trapped".into())
    });
    let result = s.run_on_change(&mut failing, mirror, "current_a", &one);
    assert_eq!(result, Err(Error::Wasm("trapped".into())));
    s.close(mirror).unwrap();
    assert_eq!(
        s.hooks(mirror, &Role::Method, "x"),
        Err(Error::UnknownObject)
    );
}

#[test]
fn a_field_with_wasm_hooks_has_its_own_encoding() {
    let (mut net, owner, mirror) = cameras();
    // The owner publishes the image as its `before_send` sends it, here as JPEG bytes.
    let mut enc = host(|code, frame| {
        assert_eq!(code.name, "image_before_send");
        assert_eq!(args(frame), json!({"value": "AAEC"}));
        assert_eq!(
            frame.field("image"),
            Err("field `image` has its own encoding".into())
        );
        frame.send(raw(b"\xff\xd8jpeg", "image/jpeg"))?;
        assert_eq!(
            frame.send_value(json!(1)),
            Err("a before_send sends exactly once".into())
        );
        Ok(Vec::new())
    });
    let sent = net
        .session(0)
        .run_before_send(&mut enc, owner, "image", &v("!!binary AAEC"));
    assert_eq!(sent, Ok(true));
    let got = settle(&mut net);
    let arrived = got[1].iter().find_map(|n| match n {
        Notice::Incoming {
            path,
            data: Data::Raw(sample),
            origin: Origin::Owner,
            ..
        } if path == "image" => Some(sample.clone()),
        _ => None,
    });
    let sample = arrived.expect("the mirror got the image raw");
    assert_eq!(sample, raw(b"\xff\xd8jpeg", "image/jpeg"));
    // The mirror decodes it with its `after_receive`.
    let mut dec = host(|code, frame| {
        assert_eq!(code.name, "image_after_receive");
        assert_eq!(args(frame), json!({}));
        let sample = frame.sample()?;
        assert_eq!(sample.encoding, "image/jpeg");
        assert_eq!(&sample.payload.bytes()[..2], b"\xff\xd8");
        assert_eq!(
            frame.set_field("plain", json!("x")),
            Err("an after_receive can't set fields (`plain`)".into())
        );
        assert!(frame.decode_sample().is_err());
        Ok(br#""AAEC""#.to_vec())
    });
    let value = net
        .session(1)
        .run_after_receive(&mut dec, mirror, "image", sample);
    // The result is read as the field's schema says: bytes.
    assert_eq!(value, Ok(Some(v("!!binary AAEC"))));
}

#[test]
fn after_receive_can_decode_the_usual_way() {
    let (mut net, _, mirror) = cameras();
    let s = net.session(1);
    let mut dec = host(|_, frame| {
        let decoded = frame.decode_sample()?;
        assert_eq!(decoded, json!({"a": "AAE="}));
        Ok(br#""AAE=""#.to_vec())
    });
    let value = s.run_after_receive(&mut dec, mirror, "image", yaml("{a: !!binary AAE=}"));
    assert_eq!(value, Ok(Some(v("!!binary AAE="))));
    let mut never = host(|_, _| panic!("`plain` has no after_receive"));
    let none = s.run_after_receive(&mut never, mirror, "plain", yaml("x"));
    assert_eq!(none, Ok(None));
    assert_eq!(
        s.run_after_receive(&mut never, mirror, "nope", yaml("x")),
        Err(Error::UnknownField("nope".into()))
    );
}

#[test]
fn before_send_can_send_the_usual_way() {
    let (mut net, owner, mirror) = cameras();
    let mut enc = host(|_, frame| {
        frame.send_value(json!("AAEC"))?;
        Ok(Vec::new())
    });
    // A mirror's `before_send` writes to the owner.
    let sent = net
        .session(1)
        .run_before_send(&mut enc, mirror, "image", &v("!!binary AAEC"));
    assert_eq!(sent, Ok(true));
    let got = settle(&mut net);
    let arrived = got[0].iter().find_map(|n| match n {
        Notice::Incoming {
            data: Data::Raw(sample),
            origin: Origin::Write,
            ..
        } => Some(sample.clone()),
        _ => None,
    });
    // Sent as YAML, read as the field's schema says: bytes.
    let arrived = arrived.expect("the owner got the write raw");
    assert_eq!(arrived.encoding, "application/yaml");
    let text = String::from_utf8(arrived.payload.bytes().into_owned()).unwrap();
    assert_eq!(v(&text), v("!!binary AAEC"));
    let s = net.session(0);
    let mut never = host(|_, _| panic!("`plain` has no before_send"));
    assert_eq!(
        s.run_before_send(&mut never, owner, "plain", &v("x")),
        Ok(false)
    );
    assert_eq!(
        s.run_before_send(&mut never, owner, "nope", &v("x")),
        Err(Error::UnknownField("nope".into()))
    );
}

#[test]
fn a_before_send_must_send_once_and_well() {
    let (mut net, owner, _) = cameras();
    let s = net.session(0);
    let mut silent = host(|_, _| Ok(Vec::new()));
    assert_eq!(
        wasm_error(s.run_before_send(&mut silent, owner, "image", &v("!!binary AA=="))),
        "the before_send didn't send"
    );
    let mut bad = host(|_, frame| {
        frame.send_value(json!("not base64!"))?;
        Ok(Vec::new())
    });
    let why = wasm_error(s.run_before_send(&mut bad, owner, "image", &v("!!binary AA==")));
    assert!(why.starts_with("the value sent: not base64"), "{why}");
    assert_eq!(
        wasm_error(
            s.before_send_job(owner, "image", &unwritable_json())
                .map(|_| ())
        ),
        "the value: a mapping key that isn't a string or an integer has no JSON form"
    );
    // Only a `before_send` sends, and only an `after_receive` has a sample.
    let mut method = host(|_, frame| {
        assert_eq!(
            frame.send(raw(b"x", "text/plain")),
            Err("only a before_send sends a field's value".into())
        );
        assert_eq!(
            frame.sample().map(|_| ()),
            Err("only an after_receive has a sample to read".into())
        );
        Ok(Vec::new())
    });
    s.run_wasm(&mut method, owner, "look", &Value::Null)
        .unwrap();
}

/// A value with no JSON form.
fn unwritable_json() -> Value {
    Value::Map(vec![(Value::Bool(true), Value::Null)])
}
