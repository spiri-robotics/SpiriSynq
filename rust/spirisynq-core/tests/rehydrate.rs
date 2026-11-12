//! A mirror's full-state fetch: one reply per field, the done marker, and live values that
//! arrive during the fetch.

mod common;

use common::*;
use spirisynq_core::{
    Command, Data, Error, Event, FieldOptions, Notice, ObjectOptions, Origin, Sample, Session,
    Source, Zid,
};

fn without_done(trace: &[String]) -> Vec<&str> {
    trace
        .iter()
        .map(String::as_str)
        .filter(|l| !l.contains(" done "))
        .collect()
}

#[test]
fn full_state_is_one_reply_per_field() {
    let raw_field = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    let (mut net, owner) = with_owner(options(&[("gps/latitude", raw_field)]));
    let s = net.session(0);
    s.set_field(owner, "voltage", v("12.5")).unwrap();
    s.set_field(owner, "image", v("big")).unwrap();
    s.set_field(owner, "gps", v("!Gps {fix: {quality: 1}}"))
        .unwrap();
    s.send_raw(owner, "gps/latitude", raw(b"\x01\x02", "x-packed"))
        .unwrap();
    s.send_raw(owner, "gps/fix/raw", raw(b"\x03", "x-packed"))
        .unwrap();
    s.send_raw(owner, "camera", raw(b"\x04", "x-link")).unwrap();
    net.run();
    net.clear_trace();

    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror(
            "robot/battery",
            battery(),
            options(&[("gps/latitude", raw_field)]),
        )
        .unwrap();
    net.run();
    // `image` and `gps/fix/raw` skip rehydrate; `charging` was never set.
    assert_eq!(
        without_done(net.trace()),
        [
            "1 get robot/battery/sr_rehydrate?",
            "0 reply robot/battery/voltage application/yaml 12.5",
            "0 reply robot/battery/gps application/yaml !Gps\\nfix:\\n  quality: 1",
            "0 reply robot/battery/camera x-link <1 bytes>",
            "0 reply robot/battery/gps/latitude x-packed <2 bytes>",
            "0 reply robot/battery/sr_rehydrate x-spirisynq/done <0 bytes>",
        ]
    );
    let got = notices(net.session(1));
    assert_eq!(
        got,
        [
            Notice::Alive {
                object: mirror,
                alive: true
            },
            incoming(mirror, "voltage", "12.5", Origin::Rehydrate),
            incoming(mirror, "gps", "!Gps {fix: {quality: 1}}", Origin::Rehydrate),
            Notice::BadData {
                object: mirror,
                path: "camera".into(),
                reason: "encoding `x-link` isn't YAML".into(),
            },
            Notice::Incoming {
                object: mirror,
                path: "gps/latitude".into(),
                data: Data::Raw(raw(b"\x01\x02", "x-packed")),
                origin: Origin::Rehydrate,
            },
            Notice::Rehydrated {
                object: mirror,
                complete: true
            },
        ]
    );

    // Replacing the dataclass drops its raw fields' samples.
    net.session(0).set_field(owner, "gps", v("null")).unwrap();
    net.session(1).rehydrate(mirror).unwrap();
    net.clear_trace();
    net.run();
    assert!(!net.trace().iter().any(|l| l.contains("gps/latitude")));
}

#[test]
fn nobody_answers() {
    let mut net = Net::new();
    net.add_session();
    let mirror = net
        .session(0)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.run();
    assert_eq!(
        notices(net.session(0)),
        [Notice::Rehydrated {
            object: mirror,
            complete: false
        }]
    );
    assert_eq!(net.session(0).alive(mirror), Ok(false));
}

#[test]
fn a_live_value_beats_the_fetched_one() {
    // The owner is on session 1, so that its put goes out after the mirror subscribed and before
    // it answers the fetch.
    let mut net = Net::new();
    net.add_session();
    net.add_session();
    let owner = net
        .session(1)
        .create_owner("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.session(1)
        .set_field(owner, "charging", v("true"))
        .unwrap();
    net.run();
    let mirror = net
        .session(0)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.session(1)
        .set_field(owner, "voltage", v("13.0"))
        .unwrap();
    net.run();
    let got = notices(net.session(0));
    assert_eq!(
        got[1..],
        [
            incoming(mirror, "voltage", "13.0", Origin::Owner),
            incoming(mirror, "charging", "true", Origin::Rehydrate),
            Notice::Rehydrated {
                object: mirror,
                complete: true
            },
        ]
    );
}

#[test]
fn fetching_again() {
    let (mut net, owner, mirror) = pair();
    let s = net.session(1);
    s.rehydrate(mirror).unwrap();
    s.rehydrate(mirror).unwrap();
    net.run();
    // The first fetch was dropped: one set of values.
    assert_eq!(
        notices(net.session(1)),
        [
            incoming(mirror, "voltage", "12.5", Origin::Rehydrate),
            Notice::Rehydrated {
                object: mirror,
                complete: true
            },
        ]
    );
    assert_eq!(net.session(0).rehydrate(owner), Err(Error::NotMirror));
    net.session(1).close(mirror).unwrap();
    assert_eq!(net.session(1).rehydrate(mirror), Err(Error::UnknownObject));
}

#[test]
fn closing_drops_the_fetch() {
    let (mut net, _owner, mirror) = pair();
    net.session(1).rehydrate(mirror).unwrap();
    net.session(1).close(mirror).unwrap();
    net.run();
    assert_eq!(notices(net.session(1)), []);
}

#[test]
fn without_a_fetch() {
    let (mut net, _owner) = with_owner(ObjectOptions::default());
    net.add_session();
    net.session(1)
        .create_mirror(
            "robot/battery",
            battery(),
            ObjectOptions {
                rehydrate: false,
                ..ObjectOptions::default()
            },
        )
        .unwrap();
    net.run();
    assert!(!net.trace().iter().any(|l| l.contains("sr_rehydrate")));
}

type Subscribers = Vec<(String, spirisynq_core::EntityId)>;

/// A mirror on its own, the id of its first fetch, and its subscribers by key.
fn lone_mirror() -> (
    Session,
    spirisynq_core::ObjectId,
    spirisynq_core::RequestId,
    Subscribers,
) {
    let mut session = Session::new(Zid("me".into()));
    let mirror = session
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    let mut request = None;
    let mut subscribers = Vec::new();
    for command in commands(&mut session) {
        match command {
            Command::Get { id, .. } => request = Some(id),
            Command::DeclareSubscriber { id, key }
            | Command::DeclareLivelinessSubscriber { id, key } => subscribers.push((key, id)),
            _ => {}
        }
    }
    (session, mirror, request.unwrap(), subscribers)
}

fn id_of(
    subscribers: &[(String, spirisynq_core::EntityId)],
    key: &str,
) -> spirisynq_core::EntityId {
    subscribers.iter().find(|(k, _)| k == key).unwrap().1
}

fn reply(
    request: spirisynq_core::RequestId,
    key: &str,
    result: Result<Sample, Sample>,
    replier: Source,
) -> Event {
    Event::Reply {
        request,
        key: key.into(),
        result,
        replier,
    }
}

#[test]
fn odd_replies() {
    let (mut s, mirror, request, _) = lone_mirror();
    let unknown = Source::Unknown;
    // Keys that aren't the object's fields are skipped.
    s.handle(reply(
        request,
        "elsewhere/voltage",
        Ok(yaml("1")),
        unknown.clone(),
    ));
    s.handle(reply(
        request,
        "robot/battery/nope",
        Ok(yaml("1")),
        unknown.clone(),
    ));
    s.handle(reply(
        request,
        "robot/battery/voltage",
        Ok(yaml("1.0")),
        unknown.clone(),
    ));
    s.handle(reply(
        request,
        "robot/battery/sr_rehydrate",
        Err(yaml("oops")),
        unknown.clone(),
    ));
    s.handle(reply(
        request,
        "robot/battery/charging",
        Ok(yaml("true")),
        unknown,
    ));
    s.handle(Event::RequestDone { request });
    s.handle(Event::RequestDone { request });
    assert_eq!(
        notices(&mut s),
        [
            incoming(mirror, "voltage", "1.0", Origin::Rehydrate),
            Notice::Rehydrated {
                object: mirror,
                complete: false
            },
        ]
    );
}

#[test]
fn the_fetch_names_the_owner() {
    // Before any token arrives, the first reply's sender is taken as the owner.
    let (mut s, mirror, request, subs) = lone_mirror();
    let z5 = Source::Remote(Zid("z5".into()));
    s.handle(reply(
        request,
        "robot/battery/voltage",
        Ok(yaml("1.0")),
        z5.clone(),
    ));
    // A later reply from someone else doesn't change it.
    s.handle(reply(
        request,
        "robot/battery/charging",
        Ok(yaml("true")),
        Source::Remote(Zid("z6".into())),
    ));
    for zid in ["z6", "z5"] {
        s.handle(Event::Sample {
            subscriber: id_of(&subs, "robot/battery/voltage"),
            key: "robot/battery/voltage".into(),
            sample: yaml(&format!("2.{}", &zid[1..])),
            source: Source::Remote(Zid(zid.into())),
        });
    }
    assert_eq!(
        notices(&mut s),
        [
            incoming(mirror, "voltage", "1.0", Origin::Rehydrate),
            incoming(mirror, "charging", "true", Origin::Rehydrate),
            incoming(mirror, "voltage", "2.5", Origin::Owner),
        ]
    );
}

#[test]
fn a_local_owner_named_by_the_fetch() {
    let (mut s, mirror, request, subs) = lone_mirror();
    let owner = s
        .create_owner("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    let publisher = commands(&mut s)
        .into_iter()
        .find_map(|c| match c {
            Command::DeclarePublisher { id, key, .. } if key == "robot/battery/voltage" => Some(id),
            _ => None,
        })
        .unwrap();
    s.handle(reply(
        request,
        "robot/battery/charging",
        Ok(yaml("true")),
        Source::Local(publisher),
    ));
    s.handle(Event::Sample {
        subscriber: id_of(&subs, "robot/battery/voltage"),
        key: "robot/battery/voltage".into(),
        sample: yaml("3.0"),
        source: Source::Local(publisher),
    });
    assert_eq!(
        notices(&mut s),
        [
            incoming(mirror, "charging", "true", Origin::Rehydrate),
            incoming(mirror, "voltage", "3.0", Origin::Owner),
        ]
    );
    let _ = owner;
}
