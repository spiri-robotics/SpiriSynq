mod common;

use common::*;
use spirisynq_core::{
    Data, FieldOptions, Notice, ObjectId, ObjectOptions, Origin, Priority, Qos, Sample,
};
use spirisynq_zenoh::Node;
use spirisynq_zenoh::zenoh::Wait;

#[test]
fn a_mirror_fetches_the_owners_state() {
    let nodes = network(2);
    owner(&nodes[0], "robot/battery", ObjectOptions::default());
    reachable(&nodes[1], "robot/battery");
    nodes[1]
        .with(|s| s.create_mirror("robot/battery", battery(), ObjectOptions::default()))
        .unwrap();
    let (data, origin) = incoming(&nodes[1], "voltage");
    assert_eq!((data, origin), (Data::Value(v("12.5")), Origin::Rehydrate));
    let complete = wait_for(&nodes[1], |n| match n {
        Notice::Rehydrated { complete, .. } => Some(complete),
        _ => None,
    });
    assert!(complete);
}

#[test]
fn owner_values_reach_mirrors_and_not_the_owner() {
    let nodes = network(2);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    nodes[0]
        .with(|s| s.set_field(owner, "voltage", v("13.0")))
        .unwrap();
    let (data, origin) = incoming(&nodes[1], "voltage");
    assert_eq!((data, origin), (Data::Value(v("13.0")), Origin::Owner));
    // The owner's own put comes back to its own subscriber, and is dropped there.
    while let Some(notice) = quiet(&nodes[0]) {
        assert!(!matches!(notice, Notice::Incoming { .. }), "{notice:?}");
    }
}

#[test]
fn a_mirrors_write_goes_to_the_owner_which_confirms_it() {
    let nodes = network(2);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    let mirror = mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    nodes[1]
        .with(|s| s.set_field(mirror, "voltage", v("11.0")))
        .unwrap();
    let (data, origin) = incoming(&nodes[0], "voltage");
    assert_eq!(origin, Origin::Write);
    let Data::Value(value) = data else { panic!("{data:?}") };
    nodes[0]
        .with(|s| s.set_field(owner, "voltage", value))
        .unwrap();
    let (data, origin) = incoming(&nodes[1], "voltage");
    assert_eq!((data, origin), (Data::Value(v("11.0")), Origin::Owner));
}

#[test]
fn a_mirror_on_the_owners_own_node() {
    let nodes = network(1);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    nodes[0]
        .with(|s| s.create_mirror("robot/battery", battery(), ObjectOptions::default()))
        .unwrap();
    // The state comes from a local queryable, and the owner from the local token.
    let (data, origin) = incoming(&nodes[0], "voltage");
    assert_eq!((data, origin), (Data::Value(v("12.5")), Origin::Rehydrate));
    nodes[0]
        .with(|s| s.set_field(owner, "voltage", v("14.0")))
        .unwrap();
    let (data, origin) = incoming(&nodes[0], "voltage");
    assert_eq!((data, origin), (Data::Value(v("14.0")), Origin::Owner));
}

#[test]
fn a_put_without_source_info_is_a_write_but_not_the_owners() {
    let nodes = network(3);
    owner(&nodes[0], "robot/battery", ObjectOptions::default());
    mirror(&nodes[2], "robot/battery", ObjectOptions::default());
    nodes[1]
        .zenoh()
        .put("robot/battery/voltage", "9.0")
        .encoding("application/yaml")
        .wait()
        .unwrap();
    let (data, origin) = incoming(&nodes[0], "voltage");
    assert_eq!((data, origin), (Data::Value(v("9.0")), Origin::Write));
    while let Some(notice) = quiet(&nodes[2]) {
        assert!(!matches!(notice, Notice::Incoming { .. }), "{notice:?}");
    }
}

fn raw_image() -> ObjectOptions {
    let field = FieldOptions {
        raw: true,
        qos: Qos {
            priority: Priority::InteractiveHigh,
            reliable: true,
        },
        ..FieldOptions::default()
    };
    ObjectOptions {
        fields: vec![("image".into(), field)],
        ..ObjectOptions::default()
    }
}

#[test]
fn raw_fields_keep_their_encoding_and_attachment() {
    let nodes = network(2);
    let owner = owner(&nodes[0], "robot/battery", raw_image());
    mirror(&nodes[1], "robot/battery", raw_image());
    let sample = Sample {
        attachment: Some(b"frame 7".to_vec().into()),
        ..raw(b"\x89PNG", "image/png")
    };
    nodes[0]
        .with(|s| s.send_raw(owner, "image", sample.clone()))
        .unwrap();
    let (data, origin) = incoming(&nodes[1], "image");
    assert_eq!((data, origin), (Data::Raw(sample), Origin::Owner));
}

#[test]
fn owners_learn_when_someone_listens() {
    // The owner's own subscriber would count as a listener, so it has to ignore writes.
    let nodes = network(2);
    let field = FieldOptions {
        ignore_incoming: true,
        ..FieldOptions::default()
    };
    let options = ObjectOptions {
        fields: vec![("voltage".into(), field)],
        ..ObjectOptions::default()
    };
    let owner = owner(&nodes[0], "robot/battery", options);
    let listening = |want: bool| {
        wait_for(&nodes[0], |n| match n {
            Notice::Listeners {
                path, listening, ..
            } if path == "voltage" && listening == want => Some(()),
            _ => None,
        })
    };
    let mirror = mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    listening(true);
    assert!(
        nodes[0]
            .with(|s| s.has_listeners(owner, Some("voltage")))
            .unwrap()
    );
    nodes[1].with(|s| s.close(mirror)).unwrap();
    listening(false);
}

#[test]
fn mirrors_follow_their_owners_liveliness() {
    let mut nodes = network(3);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    common::owner(&nodes[2], "robot/other", ObjectOptions::default());
    let first = mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    let second = mirror(&nodes[1], "robot/other", ObjectOptions::default());
    let gone = |node: &Node, which: ObjectId| {
        let alive = wait_for(node, |n| match n {
            Notice::Alive { object, alive } if object == which => Some(alive),
            _ => None,
        });
        assert!(!alive);
    };
    assert!(nodes[1].with(|s| s.alive(first)).unwrap());
    assert!(nodes[1].with(|s| s.alive(second)).unwrap());
    // Closing the object takes its token away; so does closing its node.
    nodes[0].with(|s| s.close(owner)).unwrap();
    gone(&nodes[1], first);
    nodes.pop().unwrap().close();
    gone(&nodes[1], second);
}

#[test]
fn samples_from_this_node_are_known_by_publisher() {
    // A mirror on the owner's node is told the owner's local samples apart from anyone else's
    // with the same session id.
    let nodes = network(1);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    let mirror = mirror(&nodes[0], "robot/battery", ObjectOptions::default());
    nodes[0]
        .zenoh()
        .put("robot/battery/voltage", "1.0")
        .encoding("application/yaml")
        .wait()
        .unwrap();
    nodes[0]
        .with(|s| s.set_field(owner, "voltage", v("2.0")))
        .unwrap();
    let (data, _) = incoming(&nodes[0], "voltage");
    // The raw put is a write for the owner; the mirror only takes the owner's value.
    assert_eq!(data, Data::Value(v("1.0")));
    let got = wait_for(&nodes[0], |n| match n {
        Notice::Incoming {
            object,
            data,
            origin: Origin::Owner,
            ..
        } if object == mirror => Some(data),
        _ => None,
    });
    assert_eq!(got, Data::Value(v("2.0")));
}
