mod common;

use common::*;
use spirisynq_core::{CallOptions, Notice, ObjectOptions, RequestEnd};
use spirisynq_zenoh::zenoh::Wait;

#[test]
fn the_engine_carries_on_when_zenoh_fails() {
    let nodes = network(2);
    let owner = owner(&nodes[0], "robot/battery", ObjectOptions::default());
    let mirror = mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    nodes[1]
        .with(|s| s.call(mirror, "set_limit", vec![], CallOptions::default()))
        .unwrap();
    let call = call(&nodes[0]);
    // Close the session underneath the node: everything it does on zenoh fails from now on.
    nodes[0].zenoh().close().wait().unwrap();
    nodes[0]
        .with(|s| {
            s.reply(call.query, &v("1"))?;
            s.reply_error(call.query, "ValueError", "too late")?;
            s.set_field(owner, "voltage", v("1.0"))?;
            s.create_owner("robot/other", battery(), ObjectOptions::default())?;
            s.create_mirror("robot/third", battery(), ObjectOptions::default())
        })
        .unwrap();
    // A failed get still ends.
    let complete = wait_for(&nodes[0], |n| match n {
        Notice::Rehydrated { complete, .. } => Some(complete),
        _ => None,
    });
    assert!(!complete);
    // And the caller learns the owner went away.
    let end = wait_for(&nodes[1], |n| match n {
        Notice::RequestEnded { end, .. } => Some(end),
        _ => None,
    });
    assert_eq!(end, RequestEnd::CutShort);
}
