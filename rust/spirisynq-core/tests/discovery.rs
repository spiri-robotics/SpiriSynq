//! Finding objects (`sr_metadata`) and fetching their schemas (`sr_object_schema`).

mod common;

use common::*;
use spirisynq_core::{Error, Metadata, Notice, ObjectOptions, ReplyError, RequestEnd, Zid};

const GRIPPER: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:uuid:3c8e1f2a-7d4b-4a9e-b1c6-5f0d2e8a9b47:2
properties:
  closed: {type: boolean}
"##;

fn id(text: &str) -> spirisynq_schema::TypeId {
    text.parse().unwrap()
}

fn two_objects() -> Net {
    let (mut net, _) = with_owner(ObjectOptions::default());
    net.session(0)
        .create_owner(
            "arm/gripper",
            schema_from(GRIPPER),
            ObjectOptions::default(),
        )
        .unwrap();
    net.add_session();
    net.run();
    net.clear_trace();
    net
}

fn found(got: &[Notice]) -> Vec<String> {
    let mut topics: Vec<String> = got
        .iter()
        .filter_map(|n| match n {
            Notice::Discovered { metadata, .. } => Some(metadata.topic.clone()),
            _ => None,
        })
        .collect();
    topics.sort();
    topics
}

#[test]
fn everything() {
    let mut net = two_objects();
    let request = net.session(1).discover(None, None).unwrap();
    net.run();
    let got = notices(net.session(1));
    assert_eq!(found(&got), ["arm/gripper", "robot/battery"]);
    assert!(got.contains(&Notice::Discovered {
        request,
        metadata: Metadata {
            topic: "robot/battery".into(),
            owner: Zid("z0".into()),
            type_id: id("urn:pypi:spiri-robot:1.0:spirirobot.Battery"),
            bases: vec![id("urn:uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42:7")],
        },
    }));
    assert_eq!(
        got.last(),
        Some(&Notice::RequestEnded {
            request,
            end: RequestEnd::Done
        })
    );
    assert_eq!(net.trace()[0], "1 get **/sr_metadata?");
}

#[test]
fn by_type_whatever_the_version() {
    let mut net = two_objects();
    // The battery's base, at another version.
    let base = id("urn:uuid:6F1C2A9E-4B7D-4E2A-9C1F-3D8B5E0A7C42:1");
    net.session(1).discover(None, Some(&base)).unwrap();
    net.run();
    assert_eq!(found(&notices(net.session(1))), ["robot/battery"]);
    assert_eq!(
        net.trace()[0],
        "1 get **/sr_metadata/uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42?"
    );
    net.session(1)
        .discover(
            None,
            Some(&id("urn:uuid:3c8e1f2a-7d4b-4a9e-b1c6-5f0d2e8a9b47:2")),
        )
        .unwrap();
    net.run();
    assert_eq!(found(&notices(net.session(1))), ["arm/gripper"]);
}

#[test]
fn under_a_prefix() {
    let mut net = two_objects();
    net.session(1).discover(Some("arm"), None).unwrap();
    net.run();
    assert_eq!(found(&notices(net.session(1))), ["arm/gripper"]);
    assert_eq!(
        net.session(1).discover(Some("arm/*"), None),
        Err(Error::Topic("`*` in `*`".into()))
    );
}

#[test]
fn schemas() {
    let mut net = two_objects();
    let request = net.session(1).fetch_schema("arm/gripper").unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [
            Notice::Schema {
                request,
                result: Ok(v(GRIPPER))
            },
            Notice::RequestEnded {
                request,
                end: RequestEnd::Done
            },
        ]
    );
    let request = net.session(1).fetch_schema("arm/nothing").unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [Notice::RequestEnded {
            request,
            end: RequestEnd::CutShort
        }]
    );
    assert_eq!(
        net.session(1).fetch_schema("arm//x"),
        Err(Error::Topic("empty chunk".into()))
    );
}

#[test]
fn a_schema_that_cant_be_written() {
    // A keyword the core doesn't read can hold anything, even what YAML can't write.
    let mut value = v(GRIPPER);
    if let spirisynq_yaml::Value::Map(map) = &mut value {
        map.push(("x-odd".into(), unwritable()));
    }
    let schema = std::sync::Arc::new(spirisynq_schema::TypeSchema::from_value(value).unwrap());
    let mut net = Net::new();
    net.add_session();
    net.session(0)
        .create_owner("arm/gripper", schema, ObjectOptions::default())
        .unwrap();
    let request = net.session(0).fetch_schema("arm/gripper").unwrap();
    net.run();
    let got: Vec<_> = notices(net.session(0))
        .into_iter()
        .filter(|n| !matches!(n, Notice::Listeners { .. }))
        .collect();
    assert!(matches!(
        &got[0],
        Notice::Schema { result: Err(ReplyError::Remote { kind, message }), .. }
            if kind == "RuntimeError" && message.starts_with("schema: ")
    ));
    assert_eq!(
        got[1],
        Notice::RequestEnded {
            request,
            end: RequestEnd::Failed
        }
    );
}

#[test]
fn schema_replies_that_cant_be_read() {
    let mut s = spirisynq_core::Session::new(Zid("me".into()));
    let request = s.fetch_schema("arm/gripper").unwrap();
    s.handle(spirisynq_core::Event::Reply {
        request,
        key: "arm/gripper/sr_object_schema".into(),
        result: Ok(raw(b"\x00", "zenoh/bytes")),
        replier: spirisynq_core::Source::Unknown,
    });
    assert_eq!(
        notices(&mut s),
        [Notice::Schema {
            request,
            result: Err(ReplyError::Undecodable(
                "encoding `zenoh/bytes` isn't YAML".into()
            ))
        }]
    );
}

#[test]
fn odd_metadata_is_skipped() {
    let mut s = spirisynq_core::Session::new(Zid("me".into()));
    let request = s.discover(None, None).unwrap();
    let good = "{topic: a, owner: z, type: 'urn:x:y', bases: ['urn:x:z']}";
    let replies = [
        Ok(yaml("[not, a, mapping]")),
        Ok(yaml("{topic: a, owner: z, bases: []}")),
        Ok(yaml("{topic: a, owner: z, type: 'not a uri', bases: []}")),
        Ok(yaml("{topic: a, owner: z, type: 'urn:x:y'}")),
        Ok(yaml("{topic: a, owner: z, type: 'urn:x:y', bases: [1]}")),
        Ok(yaml("{topic: a, owner: z, type: 'urn:x:y', bases: ['no']}")),
        Ok(yaml("{owner: z, type: 'urn:x:y', bases: []}")),
        Ok(yaml("{topic: a, type: 'urn:x:y', bases: []}")),
        Ok(raw(b"\x00", "zenoh/bytes")),
        Err(yaml("{type: E, message: m}")),
        Ok(yaml(good)),
    ];
    for result in replies {
        s.handle(spirisynq_core::Event::Reply {
            request,
            key: "a/sr_metadata".into(),
            result,
            replier: spirisynq_core::Source::Unknown,
        });
    }
    assert_eq!(
        notices(&mut s),
        [Notice::Discovered {
            request,
            metadata: Metadata {
                topic: "a".into(),
                owner: Zid("z".into()),
                type_id: id("urn:x:y"),
                bases: vec![id("urn:x:z")],
            }
        }]
    );
}
