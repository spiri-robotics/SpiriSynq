#![allow(dead_code)]

use std::sync::Arc;
use std::time::Duration;

use spirisynq_core::{Data, IncomingCall, Notice, ObjectId, ObjectOptions, Origin, Sample};
use spirisynq_schema::TypeSchema;
use spirisynq_yaml::Value;
use spirisynq_zenoh::Node;
use spirisynq_zenoh::zenoh::{Config, Wait};

/// A battery with a field of each kind and a method of each kind.
pub const BATTERY: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Battery
properties:
  voltage: {type: number}
  image: {type: string, x-skip-rehydrate: true}
x-remote-methods:
  set_limit:
    parameters: {type: object, properties: {percent: {type: integer}}}
    returns: {type: integer}
  read:
    x-key: read/{path:**}
    parameters: {type: object, properties: {path: {type: string}}}
    returns: {type: string}
  blob: {}
"##;

pub fn battery() -> Arc<TypeSchema> {
    let value = spirisynq_yaml::parse(BATTERY).expect("test YAML parses");
    Arc::new(TypeSchema::from_value(value).unwrap_or_else(|e| panic!("{e}")))
}

pub fn v(text: &str) -> Value {
    spirisynq_yaml::parse(text).expect("test YAML parses")
}

/// A zenoh config for tests: loopback TCP only, no multicast scouting, and connected to `seed`
/// if given.
pub fn config(seed: Option<&str>) -> Config {
    let mut config = Config::default();
    config
        .insert_json5("listen/endpoints", r#"["tcp/127.0.0.1:0"]"#)
        .unwrap();
    config
        .insert_json5("scouting/multicast/enabled", "false")
        .unwrap();
    if let Some(seed) = seed {
        config
            .insert_json5("connect/endpoints", &format!(r#"["{seed}"]"#))
            .unwrap();
    }
    config
}

/// `n` nodes on a network of their own, every one connected to every other.
pub fn network(n: usize) -> Vec<Node> {
    network_with(n, |_| {})
}

/// [`network`], with each node's config changed first.
pub fn network_with(n: usize, change: impl Fn(&mut Config)) -> Vec<Node> {
    let mut first = config(None);
    change(&mut first);
    let seed = Node::open(first).unwrap();
    let locator = seed.zenoh().info().locators().wait()[0].to_string();
    let mut nodes = vec![seed];
    for _ in 1..n {
        let mut config = config(Some(&locator));
        change(&mut config);
        nodes.push(Node::open(config).unwrap());
    }
    for node in &nodes {
        wait_until(|| node.zenoh().info().peers_zid().wait().count() == n - 1);
    }
    nodes
}

/// Poll until `done`, for up to ten seconds.
pub fn wait_until(mut done: impl FnMut() -> bool) {
    for _ in 0..1000 {
        if done() {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("timed out");
}

/// The node's next notice.
pub fn next(node: &Node) -> Notice {
    node.notices()
        .recv_timeout(Duration::from_secs(10))
        .expect("a notice in time")
}

/// Skip notices until `pick` takes one.
pub fn wait_for<T>(node: &Node, mut pick: impl FnMut(Notice) -> Option<T>) -> T {
    loop {
        if let Some(found) = pick(next(node)) {
            return found;
        }
    }
}

/// Whether no notice arrives in a moment.
pub fn quiet(node: &Node) -> Option<Notice> {
    node.notices().recv_timeout(Duration::from_millis(300)).ok()
}

/// Wait for an incoming value of `path` and return its data and origin.
pub fn incoming(node: &Node, want: &str) -> (Data, Origin) {
    wait_for(node, |n| match n {
        Notice::Incoming {
            path, data, origin, ..
        } if path == want => Some((data, origin)),
        _ => None,
    })
}

/// Wait for a call to arrive.
pub fn call(node: &Node) -> IncomingCall {
    wait_for(node, |n| match n {
        Notice::Call(call) => Some(call),
        _ => None,
    })
}

/// An owner of the battery at `topic`, with voltage 12.5.
pub fn owner(node: &Node, topic: &str, options: ObjectOptions) -> ObjectId {
    node.with(|s| {
        let owner = s.create_owner(topic, battery(), options)?;
        s.set_field(owner, "voltage", v("12.5"))?;
        Ok::<_, spirisynq_core::Error>(owner)
    })
    .unwrap()
}

/// Wait until `node` can reach the owner at `topic`. The answer comes over the same link as the
/// owner's declarations and first values, so those have arrived too: a mirror opened now gets
/// the owner's state, rather than a value still in flight.
pub fn reachable(node: &Node, topic: &str) {
    let key = format!("{topic}/sr_alive/*");
    wait_until(|| {
        let replies = node.zenoh().liveliness().get(&key).wait().unwrap();
        replies.recv().is_ok()
    });
}

/// A mirror of the battery at `topic`, once its owner is reachable and its state has arrived.
pub fn mirror(node: &Node, topic: &str, options: ObjectOptions) -> ObjectId {
    reachable(node, topic);
    let mirror = node
        .with(|s| s.create_mirror(topic, battery(), options))
        .unwrap();
    let complete = wait_for(node, |n| match n {
        Notice::Rehydrated { complete, .. } => Some(complete),
        _ => None,
    });
    assert!(complete);
    mirror
}

pub fn raw(bytes: &[u8], encoding: &str) -> Sample {
    Sample::new(bytes.to_vec(), encoding)
}
