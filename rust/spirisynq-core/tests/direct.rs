//! One session driven by hand, for events a well-behaved network rarely or never sends: late
//! events for closed objects, queries on the wrong entity, keys no SpiriSynq peer would write.

mod common;

use common::*;
use spirisynq_core::{
    Command, EntityId, Event, Notice, ObjectId, ObjectOptions, QueryId, Session, Source, Zid,
};

struct Owner {
    session: Session,
    object: ObjectId,
    /// Every key the owner declared something on, with the entity's id.
    declared: Vec<(String, EntityId)>,
}

impl Owner {
    fn new() -> Self {
        let mut session = Session::new(Zid("me".into()));
        let object = session
            .create_owner("robot/battery", battery(), ObjectOptions::default())
            .unwrap();
        let declared = commands(&mut session)
            .into_iter()
            .filter_map(|c| match c {
                Command::DeclarePublisher { id, key, .. } => Some((format!("pub {key}"), id)),
                Command::DeclareSubscriber { id, key } => Some((format!("sub {key}"), id)),
                Command::DeclareQueryable { id, key } => Some((format!("queryable {key}"), id)),
                Command::DeclareToken { id, key } => Some((format!("token {key}"), id)),
                _ => None,
            })
            .collect();
        Owner {
            session,
            object,
            declared,
        }
    }

    fn id(&self, what: &str) -> EntityId {
        self.declared
            .iter()
            .find(|(k, _)| k == what)
            .unwrap_or_else(|| panic!("nothing declared as {what}: {:?}", self.declared))
            .1
    }

    fn query(&mut self, queryable: &str, key: &str) -> Vec<Command> {
        let queryable = self.id(queryable);
        self.session.handle(Event::Query {
            queryable,
            query: QueryId(7),
            key: key.into(),
            parameters: String::new(),
            payload: None,
        });
        commands(&mut self.session)
    }
}

#[test]
fn what_an_owner_declares() {
    let o = Owner::new();
    let keys: Vec<&str> = o.declared.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(
        keys,
        [
            "pub robot/battery/voltage",
            "sub robot/battery/voltage",
            "pub robot/battery/charging",
            "sub robot/battery/charging",
            "pub robot/battery/image",
            "sub robot/battery/image",
            "pub robot/battery/gps",
            "sub robot/battery/gps",
            "pub robot/battery/gps/latitude",
            "sub robot/battery/gps/latitude",
            "pub robot/battery/gps/fix",
            "sub robot/battery/gps/fix",
            "pub robot/battery/gps/fix/quality",
            "sub robot/battery/gps/fix/quality",
            "pub robot/battery/gps/fix/raw",
            "sub robot/battery/gps/fix/raw",
            "pub robot/battery/camera",
            "sub robot/battery/camera",
            "queryable robot/battery/sr_rehydrate",
            "queryable robot/battery/sr_metadata",
            "queryable robot/battery/sr_metadata/pypi:spiri-robot:spirirobot.Battery",
            "queryable robot/battery/sr_metadata/uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42",
            "queryable robot/battery/sr_object_schema",
            "queryable robot/battery/sr_type_schema/pypi:spiri-robot:spirirobot.Battery",
            "queryable robot/battery/sr_type_schema/uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42",
            "queryable robot/battery/set_limit",
            "queryable robot/battery/count",
            "queryable robot/battery/read/*/**",
            "queryable robot/battery/snapshot",
            "token robot/battery/sr_alive/me",
        ]
    );
}

#[test]
fn type_mounts_answer_on_their_own_keys() {
    let mut o = Owner::new();
    let key = "robot/battery/sr_type_schema/uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42";
    let sent = o.query(&format!("queryable {key}"), "**/sr_type_schema/*");
    assert_eq!(
        sent[0],
        Command::Reply {
            query: QueryId(7),
            key: key.into(),
            sample: spirisynq_core::Sample::new(
                spirisynq_yaml::to_string(&v(BATTERY)).unwrap().into_bytes(),
                "application/yaml"
            ),
        }
    );
    let key = "robot/battery/sr_metadata/pypi:spiri-robot:spirirobot.Battery";
    let sent = o.query(&format!("queryable {key}"), key);
    assert!(matches!(&sent[0], Command::Reply { key: k, .. } if k == key));
    assert!(
        matches!(&sent[1], Command::Reply { key: k, sample, .. } if k == key && sample.encoding == "x-spirisynq/done")
    );
    assert_eq!(sent[2], Command::Finish { query: QueryId(7) });
}

#[test]
fn keys_with_escapes_spirisynq_doesnt_write() {
    let mut o = Owner::new();
    let sent = o.query(
        "queryable robot/battery/read/*/**",
        "robot/battery/read/al/%41",
    );
    assert_eq!(
        sent,
        [
            Command::ReplyErr {
                query: QueryId(7),
                sample: yaml(
                    "type: ValueError\nmessage: \"the value for `path` is badly escaped\"\n"
                ),
            },
            Command::Finish { query: QueryId(7) },
        ]
    );
}

#[test]
fn queries_on_entities_that_arent_queryables() {
    let mut o = Owner::new();
    let sent = o.query("sub robot/battery/voltage", "robot/battery/voltage");
    assert_eq!(sent, [Command::Finish { query: QueryId(7) }]);
}

#[test]
fn late_events_for_a_closed_object() {
    let mut o = Owner::new();
    o.session.close(o.object).unwrap();
    commands(&mut o.session);
    let sent = o.query(
        "queryable robot/battery/set_limit",
        "robot/battery/set_limit",
    );
    assert_eq!(sent, [Command::Finish { query: QueryId(7) }]);
    let s = &mut o.session;
    let sub = o
        .declared
        .iter()
        .find(|(k, _)| k == "sub robot/battery/voltage")
        .unwrap()
        .1;
    let publisher = o
        .declared
        .iter()
        .find(|(k, _)| k == "pub robot/battery/voltage")
        .unwrap()
        .1;
    s.handle(Event::Sample {
        subscriber: sub,
        key: "robot/battery/voltage".into(),
        sample: yaml("1"),
        source: Source::Unknown,
    });
    s.handle(Event::Matching {
        publisher,
        matched: true,
    });
    s.handle(Event::Liveliness {
        subscriber: sub,
        key: "robot/battery/sr_alive/x".into(),
        alive: true,
    });
    assert_eq!(notices(s), []);
    assert_eq!(commands(s), []);
}

#[test]
fn replies_to_requests_nobody_made() {
    let mut s = Session::new(Zid("me".into()));
    let mut other = Session::new(Zid("other".into()));
    // A request id from another session means nothing here.
    let request = other.fetch_schema("a").unwrap();
    s.handle(Event::Reply {
        request,
        key: "a/sr_object_schema".into(),
        result: Ok(yaml("1")),
        replier: Source::Unknown,
    });
    s.handle(Event::RequestDone { request });
    assert_eq!(notices(&mut s), []);
    assert_eq!(s.zid(), &Zid("me".into()));
}

#[test]
fn a_mirror_writes_on_its_own_publisher_once_declared() {
    let (mut net, _owner, mirror) = pair();
    for value in ["1.0", "2.0"] {
        net.session(1)
            .set_field(mirror, "voltage", v(value))
            .unwrap();
    }
    let sent = commands(net.session(1));
    let declared = sent
        .iter()
        .filter(|c| matches!(c, Command::DeclarePublisher { .. }))
        .count();
    assert_eq!(declared, 1);
    assert_eq!(
        sent.iter()
            .filter(|c| matches!(c, Command::Put { .. }))
            .count(),
        2
    );
    let _: Vec<Notice> = Vec::new();
}
