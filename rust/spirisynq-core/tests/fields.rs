//! Fields: publishing, change detection, writes from mirrors, echo suppression, nested
//! dataclasses, raw encodings, bad data and lazy fields.

mod common;

use common::*;
use spirisynq_core::{
    Data, Error, FieldOptions, Notice, ObjectOptions, Origin, Priority, Qos, QosOverride, Source,
};

#[test]
fn owner_changes_reach_mirrors() {
    let (mut net, owner, mirror) = pair();
    assert_eq!(
        net.session(1).field(mirror, "voltage"),
        Ok(Some(&v("12.5")))
    );

    assert_eq!(
        net.session(0).set_field(owner, "voltage", v("12.1")),
        Ok(true)
    );
    let got = settle(&mut net);
    assert_eq!(
        net.trace(),
        ["0 put robot/battery/voltage application/yaml 12.1"]
    );
    assert_eq!(got[0], []);
    assert_eq!(got[1], [incoming(mirror, "voltage", "12.1", Origin::Owner)]);
    assert_eq!(
        net.session(1).field(mirror, "voltage"),
        Ok(Some(&v("12.1")))
    );
}

#[test]
fn unchanged_values_arent_published() {
    let (mut net, owner, _) = pair();
    assert_eq!(
        net.session(0).set_field(owner, "voltage", v("12.5")),
        Ok(false)
    );
    assert_eq!(
        net.session(0).set_field(owner, "charging", v(".nan")),
        Ok(true)
    );
    assert_eq!(
        net.session(0).set_field(owner, "charging", v(".nan")),
        Ok(false)
    );
    settle(&mut net);
    assert_eq!(
        net.trace(),
        ["0 put robot/battery/charging application/yaml .nan"]
    );
}

#[test]
fn mirror_writes_go_through_the_owner() {
    let (mut net, owner, mirror) = pair();
    net.add_session();
    let other = net
        .session(2)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.clear_trace();

    assert_eq!(
        net.session(1).set_field(mirror, "voltage", v("11.0")),
        Ok(true)
    );
    assert_eq!(
        net.session(1).field(mirror, "voltage"),
        Ok(Some(&v("11.0")))
    );
    let got = settle(&mut net);
    assert_eq!(
        net.trace(),
        [
            "1 put robot/battery/voltage application/yaml 11.0",
            "0 put robot/battery/voltage application/yaml 11.0",
        ]
    );
    // The owner hears the write; the other mirror only hears the owner.
    assert_eq!(got[0], [incoming(owner, "voltage", "11.0", Origin::Write)]);
    assert_eq!(got[1], [incoming(mirror, "voltage", "11.0", Origin::Owner)]);
    assert_eq!(got[2], [incoming(other, "voltage", "11.0", Origin::Owner)]);
}

#[test]
fn an_owner_can_correct_a_write() {
    let (mut net, owner, mirror) = pair();
    net.session(1)
        .set_field(mirror, "voltage", v("99.0"))
        .unwrap();
    net.run();
    assert_eq!(
        notices(net.session(0)),
        [incoming(owner, "voltage", "99.0", Origin::Write)]
    );
    // The binding clamps it instead of taking it as it is.
    net.session(0)
        .set_field(owner, "voltage", v("14.0"))
        .unwrap();
    settle(&mut net);
    assert_eq!(
        net.session(1).field(mirror, "voltage"),
        Ok(Some(&v("14.0")))
    );
}

#[test]
fn ignored_fields_have_no_subscriber() {
    let ignore = FieldOptions {
        ignore_incoming: true,
        ..FieldOptions::default()
    };
    let (mut net, _owner) = with_owner(options(&[("voltage", ignore)]));
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.session(1)
        .set_field(mirror, "voltage", v("1.0"))
        .unwrap();
    let got = settle(&mut net);
    assert_eq!(got[0], []);
    assert_eq!(
        net.trace().last().unwrap(),
        "1 put robot/battery/voltage application/yaml 1.0"
    );
}

#[test]
fn mirrors_only_listen_to_the_owner() {
    let (mut net, _owner, mirror) = pair();
    // A process that isn't the owner puts on the field's key: here, another owner on the same
    // topic, which SpiriSynq doesn't stop.
    net.add_session();
    let rogue = net
        .session(2)
        .create_owner("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.run();
    notices(net.session(1));
    net.session(2)
        .set_field(rogue, "voltage", v("1.0"))
        .unwrap();
    let got = settle(&mut net);
    // The mirror follows the token that appeared last, which is the rogue's.
    assert_eq!(got[1], [incoming(mirror, "voltage", "1.0", Origin::Owner)]);
}

#[test]
fn samples_without_a_source_are_ignored_by_mirrors() {
    let mut session = spirisynq_core::Session::new(spirisynq_core::Zid("me".into()));
    let mirror = session
        .create_mirror(
            "robot/battery",
            battery(),
            ObjectOptions {
                rehydrate: false,
                ..ObjectOptions::default()
            },
        )
        .unwrap();
    let ids = subscribers(&mut session);
    session.handle(spirisynq_core::Event::Liveliness {
        subscriber: ids[0],
        key: "robot/battery/sr_alive/z9".into(),
        alive: true,
    });
    notices(&mut session);
    for source in [
        Source::Unknown,
        Source::Remote(spirisynq_core::Zid("z8".into())),
    ] {
        session.handle(spirisynq_core::Event::Sample {
            subscriber: ids[1],
            key: "robot/battery/voltage".into(),
            sample: yaml("1.0"),
            source,
        });
    }
    assert_eq!(notices(&mut session), []);
    session.handle(spirisynq_core::Event::Sample {
        subscriber: ids[1],
        key: "robot/battery/voltage".into(),
        sample: yaml("1.0"),
        source: Source::Remote(spirisynq_core::Zid("z9".into())),
    });
    assert_eq!(
        notices(&mut session),
        [incoming(mirror, "voltage", "1.0", Origin::Owner)]
    );
}

/// The ids of a session's liveliness subscriber and field subscribers, in declaration order.
fn subscribers(session: &mut spirisynq_core::Session) -> Vec<spirisynq_core::EntityId> {
    commands(session)
        .into_iter()
        .filter_map(|c| match c {
            spirisynq_core::Command::DeclareLivelinessSubscriber { id, .. }
            | spirisynq_core::Command::DeclareSubscriber { id, .. } => Some(id),
            _ => None,
        })
        .collect()
}

#[test]
fn a_mirror_in_the_owners_session() {
    let (mut net, owner) = with_owner(ObjectOptions::default());
    let mirror = net
        .session(0)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    // A second mirror, of another topic, whose writes the first mirror must not take as the
    // owner's.
    settle(&mut net);
    net.session(0)
        .set_field(owner, "voltage", v("3.0"))
        .unwrap();
    let got = settle(&mut net);
    assert_eq!(got[0], [incoming(mirror, "voltage", "3.0", Origin::Owner)]);

    let twin = net
        .session(0)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.session(0).set_field(twin, "voltage", v("4.0")).unwrap();
    let got = settle(&mut net);
    // The owner takes the twin's write and publishes it; each mirror hears only the owner.
    assert_eq!(
        got[0],
        [
            incoming(owner, "voltage", "4.0", Origin::Write),
            incoming(mirror, "voltage", "4.0", Origin::Owner),
            incoming(twin, "voltage", "4.0", Origin::Owner),
        ]
    );
}

#[test]
fn nested_dataclasses_sync_field_by_field() {
    let (mut net, owner, mirror) = pair();
    let s = net.session(0);
    assert_eq!(
        s.set_field(owner, "gps/latitude", v("1.0")),
        Err(Error::ParentMissing("gps".into()))
    );
    s.set_field(owner, "gps", v("null")).unwrap();
    assert_eq!(
        s.set_field(owner, "gps/latitude", v("1.0")),
        Err(Error::ParentMissing("gps".into()))
    );
    s.set_field(owner, "gps", v("!Gps {latitude: 45.0}"))
        .unwrap();
    assert_eq!(s.set_field(owner, "gps/latitude", v("45.0")), Ok(false));
    assert_eq!(s.set_field(owner, "gps/latitude", v("46.0")), Ok(true));
    assert_eq!(
        s.set_field(owner, "gps/fix/quality", v("2")),
        Err(Error::ParentMissing("gps/fix".into()))
    );
    s.set_field(owner, "gps/fix", v("{quality: 1}")).unwrap();
    s.set_field(owner, "gps/fix/quality", v("2")).unwrap();
    assert_eq!(
        s.field(owner, "gps"),
        Ok(Some(&v("!Gps {latitude: 46.0, fix: {quality: 2}}")))
    );
    assert_eq!(s.field(owner, "gps/fix/quality"), Ok(Some(&v("2"))));
    s.set_field(owner, "gps/fix", v("3")).unwrap();
    assert_eq!(
        s.set_field(owner, "gps/fix/quality", v("2")),
        Err(Error::ParentMissing("gps/fix".into()))
    );
    assert_eq!(s.field(owner, "gps/fix/quality"), Ok(None));
    s.set_field(owner, "gps", v("!Gps 7")).unwrap();
    assert_eq!(
        s.set_field(owner, "gps/fix/quality", v("2")),
        Err(Error::ParentMissing("gps".into()))
    );
    assert_eq!(
        s.set_field(owner, "gps/latitude", v("1.0")),
        Err(Error::ParentMissing("gps".into()))
    );
    assert_eq!(s.field(owner, "gps/latitude"), Ok(None));
    s.set_field(owner, "gps", v("{latitude: 1.0}")).unwrap();
    assert_eq!(s.field(owner, "gps/fix"), Ok(None));
    assert_eq!(s.field(owner, "charging"), Ok(None));

    settle(&mut net);
    assert_eq!(
        net.session(1).field(mirror, "gps"),
        Ok(Some(&v("{latitude: 1.0}")))
    );
    assert_eq!(
        net.trace(),
        [
            "0 put robot/battery/gps application/yaml null",
            "0 put robot/battery/gps application/yaml !Gps\\nlatitude: 45.0",
            "0 put robot/battery/gps/latitude application/yaml 46.0",
            "0 put robot/battery/gps/fix application/yaml quality: 1",
            "0 put robot/battery/gps/fix/quality application/yaml 2",
            "0 put robot/battery/gps/fix application/yaml 3",
            "0 put robot/battery/gps application/yaml !Gps \"7\"",
            "0 put robot/battery/gps application/yaml latitude: 1.0",
        ]
    );
}

#[test]
fn raw_fields() {
    let raw_field = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    let (mut net, owner) = with_owner(options(&[("image", raw_field)]));
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", battery(), options(&[("image", raw_field)]))
        .unwrap();
    settle(&mut net);
    net.clear_trace();
    let jpeg = raw(b"\xff\xd8", "image/jpeg");
    net.session(0)
        .send_raw(owner, "image", jpeg.clone())
        .unwrap();
    assert_eq!(net.session(0).field(owner, "image"), Ok(None));
    net.run();
    assert_eq!(
        net.trace(),
        ["0 put robot/battery/image image/jpeg <2 bytes>"]
    );
    assert_eq!(
        notices(net.session(1)),
        [Notice::Incoming {
            object: mirror,
            path: "image".into(),
            data: Data::Raw(jpeg.clone()),
            origin: Origin::Owner,
        }]
    );
    net.session(1)
        .send_raw(mirror, "image", jpeg.clone())
        .unwrap();
    net.run();
    assert_eq!(
        notices(net.session(0)),
        [Notice::Incoming {
            object: owner,
            path: "image".into(),
            data: Data::Raw(jpeg),
            origin: Origin::Write,
        }]
    );
}

#[test]
fn a_sample_can_have_its_own_qos() {
    let (mut net, owner, mirror) = pair();
    net.clear_trace();
    let keyframe = QosOverride {
        priority: None,
        reliable: Some(true),
    };
    let s = net.session(0);
    s.send_raw_with(owner, "voltage", yaml("1"), keyframe)
        .unwrap();
    // Only what differs from the field's QoS counts: this one is the field's own.
    let same = QosOverride {
        priority: Some(Priority::Data),
        reliable: Some(false),
    };
    s.send_raw_with(owner, "voltage", yaml("2"), same).unwrap();
    net.run();
    let background = QosOverride {
        priority: Some(Priority::Background),
        reliable: None,
    };
    net.session(1)
        .send_raw_with(mirror, "voltage", yaml("3"), background)
        .unwrap();
    net.run();
    assert_eq!(
        net.trace(),
        [
            "0 put robot/battery/voltage application/yaml 1 (Qos { priority: Data, reliable: true })",
            "0 put robot/battery/voltage application/yaml 2",
            "1 put robot/battery/voltage application/yaml 3 (Qos { priority: Background, reliable: false })",
        ]
    );
}

#[test]
fn bad_data_is_reported() {
    let (mut net, owner, mirror) = pair();
    let session = net.session(0);
    // A field without its own encoding, sent raw, is bad data to its readers.
    session
        .send_raw(owner, "voltage", raw(b"\x01", "zenoh/bytes"))
        .unwrap();
    session
        .send_raw(owner, "charging", yaml("[unclosed"))
        .unwrap();
    net.run();
    let got = notices(net.session(1));
    assert_eq!(
        got[0],
        Notice::BadData {
            object: mirror,
            path: "voltage".into(),
            reason: "encoding `zenoh/bytes` isn't YAML".into(),
        }
    );
    assert!(
        matches!(&got[1], Notice::BadData { path, reason, .. } if path == "charging" && !reason.is_empty())
    );
}

#[test]
fn local_writes_on_mirrors() {
    let (mut net, owner, mirror) = pair();
    let s = net.session(1);
    assert_eq!(s.set_field_local(mirror, "voltage", v("1.0")), Ok(true));
    assert_eq!(s.set_field_local(mirror, "voltage", v("1.0")), Ok(false));
    assert_eq!(s.field(mirror, "voltage"), Ok(Some(&v("1.0"))));
    net.run();
    assert_eq!(net.trace(), [] as [&str; 0]);
    assert_eq!(
        net.session(0).set_field_local(owner, "voltage", v("1.0")),
        Err(Error::NotMirror)
    );
}

#[test]
fn lazy_fields_publish_only_to_listeners() {
    let lazy = FieldOptions {
        lazy: true,
        ignore_incoming: true,
        qos: Qos {
            priority: Priority::Background,
            reliable: true,
        },
        raw: false,
    };
    let (mut net, owner) = with_owner(options(&[("image", lazy)]));
    let s = net.session(0);
    assert_eq!(s.has_listeners(owner, Some("image")), Ok(false));
    // The owner's own subscriptions count as listeners for the fields it takes writes on.
    assert_eq!(s.has_listeners(owner, Some("voltage")), Ok(true));
    assert_eq!(s.has_listeners(owner, None), Ok(true));
    s.set_field(owner, "image", v("a")).unwrap();
    net.run();
    assert_eq!(net.trace(), [] as [&str; 0]);

    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.run();
    assert!(notices(net.session(0)).contains(&Notice::Listeners {
        object: owner,
        path: "image".into(),
        listening: true,
    }));
    assert_eq!(net.session(0).has_listeners(owner, Some("image")), Ok(true));
    net.session(0).set_field(owner, "image", v("b")).unwrap();
    net.run();
    assert_eq!(
        net.trace().last().unwrap(),
        "0 put robot/battery/image application/yaml b"
    );
    net.session(1).close(mirror).unwrap();
    net.run();
    assert!(notices(net.session(0)).contains(&Notice::Listeners {
        object: owner,
        path: "image".into(),
        listening: false,
    }));
}

#[test]
fn field_errors() {
    let (mut net, owner, mirror) = pair();
    let s = net.session(0);
    for path in ["nope", "gps/longitude", "voltage/x"] {
        let unknown = Err(Error::UnknownField(path.into()));
        assert_eq!(s.set_field(owner, path, v("1")).map(|_| ()), unknown);
        assert_eq!(s.field(owner, path).map(|_| ()), unknown);
        assert_eq!(s.send_raw(owner, path, yaml("1")), unknown);
        assert_eq!(
            s.has_listeners(owner, Some(path)),
            Err(Error::UnknownField(path.into()))
        );
    }
    assert!(matches!(
        s.set_field(owner, "voltage", unwritable()),
        Err(Error::Encode(_))
    ));
    assert_eq!(
        net.session(1).has_listeners(mirror, None),
        Err(Error::NotOwner)
    );
    assert_eq!(
        net.session(1).set_field_local(mirror, "nope", v("1")),
        Err(Error::UnknownField("nope".into()))
    );
}
