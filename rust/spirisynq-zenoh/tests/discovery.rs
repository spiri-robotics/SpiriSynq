mod common;

use common::*;
use spirisynq_core::{Notice, ObjectOptions, RequestEnd};

#[test]
fn discovery_finds_every_owner() {
    let nodes = network(2);
    owner(&nodes[0], "robot/a", ObjectOptions::default());
    owner(&nodes[0], "robot/b", ObjectOptions::default());
    reachable(&nodes[1], "robot/b");
    let type_id = battery().id().clone();
    for (prefix, type_id) in [(None, None), (Some("robot"), Some(&type_id))] {
        nodes[1].with(|s| s.discover(prefix, type_id)).unwrap();
        let mut found = Vec::new();
        let end = wait_for(&nodes[1], |n| match n {
            Notice::Discovered { metadata, .. } => {
                assert_eq!(metadata.owner, nodes[0].zid());
                assert_eq!(&metadata.type_id, battery().id());
                found.push(metadata.topic);
                None
            }
            Notice::RequestEnded { end, .. } => Some(end),
            _ => None,
        });
        found.sort();
        assert_eq!(
            (found, end),
            (vec!["robot/a".into(), "robot/b".into()], RequestEnd::Done)
        );
    }
}

#[test]
fn a_schema_can_be_fetched_from_its_owner() {
    let nodes = network(2);
    owner(&nodes[0], "robot/battery", ObjectOptions::default());
    reachable(&nodes[1], "robot/battery");
    nodes[1].with(|s| s.fetch_schema("robot/battery")).unwrap();
    let schema = wait_for(&nodes[1], |n| match n {
        Notice::Schema { result, .. } => Some(result.unwrap()),
        _ => None,
    });
    assert_eq!(schema, battery().raw().clone());
}

#[test]
fn a_node_knows_its_session_id() {
    let nodes = network(1);
    assert_eq!(nodes[0].zid().0, nodes[0].zenoh().zid().to_string());
}
