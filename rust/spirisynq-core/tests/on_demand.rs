//! On-demand fields: a key chunk starting with `@` makes a field a stream. Mirrors subscribe
//! only when asked, the state fetch leaves it out, and the owner publishes it only to those who
//! asked.

mod common;

use std::sync::Arc;

use common::*;
use spirisynq_core::{Command, Data, Error, FieldOptions, Notice, ObjectId, ObjectOptions, Origin};
use spirisynq_schema::TypeSchema;

const CAMERA: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-camera:1.0:spiricamera.Camera
properties:
  exposure: {type: number}
  '@frame': {type: string}
  '@interframe': {type: string, x-skip-rehydrate: true}
  jpeg: {$ref: "#/$defs/Jpeg"}
$defs:
  Jpeg:
    type: object
    x-sync: fields
    properties:
      quality: {type: integer}
      '@data': {type: string}
"##;

fn camera() -> Arc<TypeSchema> {
    schema_from(CAMERA)
}

/// A camera owned on session 0 with a frame and a JPEG already set, and a mirror of it on
/// session 1 that has fetched its state.
fn cameras(owner_options: ObjectOptions) -> (Net, ObjectId, ObjectId) {
    let mut net = Net::new();
    net.add_session();
    net.add_session();
    let s = net.session(0);
    let owner = s.create_owner("cam", camera(), owner_options).unwrap();
    s.set_field(owner, "exposure", v("0.5")).unwrap();
    s.set_field(owner, "jpeg", v("{quality: 80}")).unwrap();
    s.set_field(owner, "@frame", v("first")).unwrap();
    s.send_raw(owner, "jpeg/@data", raw(b"\xff\xd8", "image/jpeg"))
        .unwrap();
    // The mirror decodes JPEGs itself, as a binding with an `after_receive` would.
    let raw = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    let mirror = net
        .session(1)
        .create_mirror("cam", camera(), options(&[("jpeg/@data", raw)]))
        .unwrap();
    settle(&mut net);
    (net, owner, mirror)
}

fn paths(notices: &[Notice]) -> Vec<String> {
    let mut paths: Vec<String> = notices
        .iter()
        .filter_map(|n| match n {
            Notice::Incoming { path, .. } => Some(path.clone()),
            _ => None,
        })
        .collect();
    paths.sort();
    paths
}

#[test]
fn a_mirror_doesnt_subscribe_to_on_demand_fields_or_fetch_them() {
    let mut net = Net::new();
    net.add_session();
    let s = net.session(0);
    s.create_mirror("cam", camera(), ObjectOptions::default())
        .unwrap();
    let subscribed: Vec<String> = commands(s)
        .into_iter()
        .filter_map(|c| match c {
            Command::DeclareSubscriber { key, .. } => Some(key),
            _ => None,
        })
        .collect();
    assert_eq!(subscribed, ["cam/exposure", "cam/jpeg", "cam/jpeg/quality"]);

    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    let s = net.session(1);
    assert_eq!(s.field(mirror, "exposure").unwrap(), Some(&v("0.5")));
    assert_eq!(s.field(mirror, "@frame").unwrap(), None);
    assert_eq!(s.field(mirror, "jpeg").unwrap(), Some(&v("{quality: 80}")));
    // The owner keeps its streams out of the state.
    let s = net.session(0);
    assert_eq!(s.field(owner, "@frame").unwrap(), Some(&v("first")));
    assert_eq!(s.field(owner, "jpeg").unwrap(), Some(&v("{quality: 80}")));
}

#[test]
fn subscribing_fetches_the_last_value_then_follows() {
    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    assert!(net.session(1).subscribe(mirror, "@frame").unwrap());
    assert!(net.session(1).subscribe(mirror, "jpeg/@data").unwrap());
    let got = settle(&mut net);
    assert_eq!(paths(&got[1]), ["@frame", "jpeg/@data"]);
    assert!(got[1].contains(&incoming(mirror, "@frame", "first", Origin::Rehydrate)));
    assert!(got[1].iter().any(|n| matches!(
        n,
        Notice::Incoming { path, data: Data::Raw(s), origin: Origin::Rehydrate, .. }
            if path == "jpeg/@data" && s.encoding == "image/jpeg"
    )));
    assert_eq!(
        net.session(1).field(mirror, "@frame").unwrap(),
        Some(&v("first"))
    );
    // The owner publishes now that someone listens.
    assert!(net.session(0).has_listeners(owner, Some("@frame")).unwrap());
    net.session(0)
        .set_field(owner, "@frame", v("second"))
        .unwrap();
    let got = settle(&mut net);
    assert!(got[1].contains(&incoming(mirror, "@frame", "second", Origin::Owner)));
}

#[test]
fn a_field_that_skips_the_state_isnt_fetched() {
    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    net.session(0)
        .set_field(owner, "@interframe", v("delta"))
        .unwrap();
    net.session(1).subscribe(mirror, "@interframe").unwrap();
    assert!(paths(&settle(&mut net)[1]).is_empty());
}

#[test]
fn holds_are_counted() {
    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    let s = net.session(1);
    assert!(s.subscribe(mirror, "@frame").unwrap());
    assert!(!s.subscribe(mirror, "@frame").unwrap());
    commands(s);
    assert!(!s.unsubscribe(mirror, "@frame").unwrap());
    assert!(commands(s).is_empty());
    assert!(s.unsubscribe(mirror, "@frame").unwrap());
    assert!(matches!(commands(s)[..], [Command::Undeclare { .. }]));
    let not = Error::NotSubscribed("@frame".into());
    assert_eq!(s.unsubscribe(mirror, "@frame"), Err(not.clone()));
    assert_eq!(not.to_string(), "not subscribed to `@frame`");
    settle(&mut net);
    // Nobody listens again: the owner stops publishing.
    assert!(!net.session(0).has_listeners(owner, Some("@frame")).unwrap());
    net.session(0)
        .set_field(owner, "@frame", v("third"))
        .unwrap();
    assert!(paths(&settle(&mut net)[1]).is_empty());
}

#[test]
fn only_a_mirrors_on_demand_fields_can_be_subscribed() {
    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    let s = net.session(1);
    let not = Error::NotOnDemand("exposure".into());
    assert_eq!(s.subscribe(mirror, "exposure"), Err(not.clone()));
    assert_eq!(
        not.to_string(),
        "`exposure` isn't on demand: mirrors always subscribe to it"
    );
    assert_eq!(
        s.subscribe(mirror, "nope"),
        Err(Error::UnknownField("nope".into()))
    );
    assert_eq!(
        s.unsubscribe(mirror, "nope"),
        Err(Error::UnknownField("nope".into()))
    );
    let gone = s
        .create_mirror("cam3", camera(), ObjectOptions::default())
        .unwrap();
    s.close(gone).unwrap();
    assert_eq!(s.subscribe(gone, "@frame"), Err(Error::UnknownObject));
    assert_eq!(s.unsubscribe(gone, "@frame"), Err(Error::UnknownObject));
    let s = net.session(0);
    assert_eq!(s.subscribe(owner, "@frame"), Err(Error::NotMirror));
}

#[test]
fn an_owner_ignores_writes_to_on_demand_fields() {
    // Lazy without ignoring writes is refused for other fields, but an on-demand field always
    // ignores them.
    let lazy = FieldOptions {
        lazy: true,
        ..FieldOptions::default()
    };
    let (mut net, owner, mirror) = cameras(options(&[("@frame", lazy)]));
    net.session(1)
        .set_field(mirror, "@frame", v("forged"))
        .unwrap();
    let got = settle(&mut net);
    assert!(paths(&got[0]).is_empty());
    assert_eq!(
        net.session(0).field(owner, "@frame").unwrap(),
        Some(&v("first"))
    );
    // A mirror keeps what it's given locally, too.
    assert!(
        net.session(1)
            .set_field_local(mirror, "@frame", v("local"))
            .unwrap()
    );
    assert!(
        !net.session(1)
            .set_field_local(mirror, "@frame", v("local"))
            .unwrap()
    );
}

#[test]
fn a_fetched_value_for_a_field_let_go_meanwhile_is_dropped() {
    let (mut net, _, mirror) = cameras(ObjectOptions::default());
    let s = net.session(1);
    s.subscribe(mirror, "@frame").unwrap();
    s.unsubscribe(mirror, "@frame").unwrap();
    assert!(paths(&settle(&mut net)[1]).is_empty());
}

#[test]
fn a_one_field_fetch_answers_for_any_field() {
    // Asked for a field it holds no value for, or one that isn't in its type, the owner just
    // ends the fetch.
    let (mut net, owner, mirror) = cameras(ObjectOptions::default());
    let s = net.session(0);
    let fresh = s
        .create_owner("cam2", camera(), ObjectOptions::default())
        .unwrap();
    let mirror2 = net
        .session(1)
        .create_mirror("cam2", camera(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.session(1).subscribe(mirror2, "@frame").unwrap();
    assert!(paths(&settle(&mut net)[1]).is_empty());
    let _ = (owner, mirror, fresh);
}

#[test]
fn closing_a_mirror_drops_its_fetches() {
    let (mut net, _, mirror) = cameras(ObjectOptions::default());
    let s = net.session(1);
    s.subscribe(mirror, "@frame").unwrap();
    s.discover(None, None).unwrap(); // not the mirror's: carries on
    s.close(mirror).unwrap();
    assert!(paths(&settle(&mut net)[1]).is_empty());
}
