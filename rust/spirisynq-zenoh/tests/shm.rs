mod common;

use common::*;
use spirisynq_core::{Data, FieldOptions, ObjectId, ObjectOptions, Payload, Sample};
use spirisynq_zenoh::zenoh::Wait;
use spirisynq_zenoh::{Node, ZenohBytes};

/// Well above zenoh's 3 KiB threshold for sending through shared memory.
const BIG: usize = 64 * 1024;

fn raw_image() -> ObjectOptions {
    let field = FieldOptions {
        raw: true,
        ..FieldOptions::default()
    };
    ObjectOptions {
        fields: vec![("image".into(), field)],
        ..ObjectOptions::default()
    }
}

/// An owner with a raw image field on the first node, and a mirror on the second.
fn pair() -> (Vec<Node>, ObjectId) {
    let nodes = network(2);
    let owner = owner(&nodes[0], "robot/battery", raw_image());
    mirror(&nodes[1], "robot/battery", raw_image());
    (nodes, owner)
}

/// The next image the mirror gets, and whether it is in shared memory.
fn image(node: &Node) -> (Vec<u8>, bool) {
    let (data, _) = incoming(node, "image");
    let Data::Raw(sample) = data else { panic!("{data:?}") };
    let bytes = sample
        .payload
        .downcast_ref::<ZenohBytes>()
        .expect("zenoh's own bytes");
    (
        sample.payload.bytes().into_owned(),
        bytes.0.as_shm().is_some(),
    )
}

#[test]
fn large_values_travel_through_shared_memory() {
    let (nodes, owner) = pair();
    // zenoh starts shared memory on first use, and copies large values the usual way until it
    // has (`transport/shared_memory/mode: init` starts it with the session instead).
    nodes[0].alloc_shm(1).expect("shared memory");
    let transports = nodes[1].zenoh().info().transports().wait();
    assert!(transports.into_iter().all(|t| t.is_shm()));
    let sent = vec![7u8; BIG];
    nodes[0]
        .with(|s| s.send_raw(owner, "image", raw(&sent, "image/png")))
        .unwrap();
    assert_eq!(image(&nodes[1]), (sent, true));
    // Small ones are copied, as before.
    nodes[0]
        .with(|s| s.send_raw(owner, "image", raw(b"tiny", "image/png")))
        .unwrap();
    assert_eq!(image(&nodes[1]), (b"tiny".to_vec(), false));
}

#[test]
fn a_shared_memory_buffer_is_sent_as_it_is() {
    let (nodes, owner) = pair();
    let mut buffer = nodes[0].alloc_shm(BIG).expect("shared memory");
    buffer.fill(3);
    let payload = Payload::from(ZenohBytes(buffer.into()));
    nodes[0]
        .with(|s| s.send_raw(owner, "image", Sample::new(payload, "image/png")))
        .unwrap();
    assert_eq!(image(&nodes[1]), (vec![3u8; BIG], true));
}

#[test]
fn no_buffer_when_shared_memory_is_off_or_full() {
    let nodes = network(1);
    assert!(nodes[0].alloc_shm(1 << 30).is_none());
    let nodes = network_with(1, |config| {
        config
            .insert_json5("transport/shared_memory/enabled", "false")
            .unwrap();
    });
    assert!(nodes[0].alloc_shm(16).is_none());
}
