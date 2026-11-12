//! The in-memory network itself.

mod common;

use common::*;
use spirisynq_core::memory::{MemoryNetwork, intersects};
use std::borrow::Cow;

use spirisynq_core::{Bytes, CallOptions, Notice, ObjectOptions, Payload, RequestEnd};

/// A transport's own buffer type.
#[derive(Debug)]
struct Shared(Vec<u8>);

impl Bytes for Shared {
    fn bytes(&self) -> Cow<'_, [u8]> {
        Cow::Owned(self.0.clone())
    }
}

#[test]
fn key_expressions() {
    for (a, b, meet) in [
        ("a/b", "a/b", true),
        ("a/b", "a/c", false),
        ("a/*", "a/b", true),
        ("a/*", "a", false),
        ("a/*", "a/b/c", false),
        ("a/**", "a", true),
        ("a/**", "a/b/c", true),
        ("**", "a/b", true),
        ("**/x", "a/b/x", true),
        ("**/x", "a/b/y", false),
        ("a/b/x", "**/x", true),
        ("a", "a/**", true),
        ("a/*/c", "**/c", true),
        ("a/**/c", "a/b/**", true),
        ("a/b", "a", false),
        ("a", "a/b", false),
    ] {
        assert_eq!(intersects(a, b), meet, "{a} {b}");
        assert_eq!(intersects(b, a), meet, "{b} {a}");
    }
}

#[test]
fn defaults_and_payloads() {
    let mut net: MemoryNetwork = MemoryNetwork::default();
    assert_eq!(net.sessions(), 0);
    net.add_session();
    assert_eq!(net.sessions(), 1);
    let payload = Payload::from(vec![1]);
    assert_eq!(payload.bytes().as_ref(), [1]);
    assert_eq!(payload.downcast_ref::<Vec<u8>>(), Some(&vec![1]));
    assert_eq!(format!("{payload:?}"), "[1]");
    assert_eq!(Payload::new(Shared(vec![1])), payload);
    assert_eq!(
        Payload::new(Shared(vec![2])).downcast_ref::<Vec<u8>>(),
        None
    );
}

#[test]
fn late_replies_are_dropped() {
    let (mut net, _owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    let call = match &notices(net.session(0))[..] {
        [Notice::Call(call)] => call.clone(),
        other => panic!("{other:?}"),
    };
    net.expire_requests();
    net.session(0).reply(call.query, &v("1")).unwrap();
    net.session(0).reply_error(call.query, "E", "late").unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [Notice::RequestEnded {
            request,
            end: RequestEnd::CutShort
        }]
    );
}

#[test]
fn a_getter_that_dies() {
    let (mut net, _owner, mirror) = pair();
    net.session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    let call = match &notices(net.session(0))[..] {
        [Notice::Call(call)] => call.clone(),
        other => panic!("{other:?}"),
    };
    net.disconnect(1);
    // Commands from a dead session go nowhere.
    net.session(1)
        .create_owner("ghost", battery(), ObjectOptions::default())
        .unwrap();
    net.session(0).finish(call.query).unwrap();
    net.clear_trace();
    net.run();
    assert_eq!(net.trace(), [] as [&str; 0]);
    assert_eq!(notices(net.session(1)), []);
}
