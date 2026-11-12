mod common;

use common::*;
use spirisynq_core::{
    CallOptions, Data, Notice, ObjectId, ObjectOptions, ReplyError, RequestEnd, Sample,
};
use spirisynq_zenoh::Node;

/// An owner on the first node and a mirror of it on the second.
fn pair(options: ObjectOptions) -> (Vec<Node>, ObjectId) {
    let nodes = network(2);
    owner(&nodes[0], "robot/battery", options);
    let mirror = mirror(&nodes[1], "robot/battery", ObjectOptions::default());
    (nodes, mirror)
}

fn replies(node: &Node) -> (Vec<Result<Data, ReplyError>>, RequestEnd) {
    let mut replies = Vec::new();
    loop {
        match next(node) {
            Notice::Reply { result, .. } => replies.push(result),
            Notice::RequestEnded { end, .. } => return (replies, end),
            _ => {}
        }
    }
}

#[test]
fn a_call_gets_every_reply_then_ends() {
    let (nodes, mirror) = pair(ObjectOptions::default());
    let args = vec![("percent".to_string(), v("40"))];
    nodes[1]
        .with(|s| s.call(mirror, "set_limit", args, CallOptions::default()))
        .unwrap();
    let call = call(&nodes[0]);
    assert_eq!(
        (call.method.as_str(), &call.args),
        ("set_limit", &v("{percent: 40}"))
    );
    nodes[0]
        .with(|s| {
            s.reply(call.query, &v("40"))?;
            s.reply(call.query, &v("41"))?;
            s.finish(call.query)
        })
        .unwrap();
    let values = vec![Ok(Data::Value(v("40"))), Ok(Data::Value(v("41")))];
    assert_eq!(replies(&nodes[1]), (values, RequestEnd::Done));
}

#[test]
fn key_arguments_travel_in_the_key() {
    let (nodes, mirror) = pair(ObjectOptions::default());
    let args = vec![("path".to_string(), v("logs/today"))];
    nodes[1]
        .with(|s| s.call(mirror, "read", args, CallOptions::default()))
        .unwrap();
    let call = call(&nodes[0]);
    assert_eq!(call.key, "robot/battery/read/logs/today");
    assert_eq!(
        call.key_args,
        vec![("path".to_string(), "logs/today".to_string())]
    );
    nodes[0].with(|s| s.finish(call.query)).unwrap();
    assert_eq!(replies(&nodes[1]), (vec![], RequestEnd::Done));
}

#[test]
fn an_error_reply_ends_the_call() {
    let (nodes, mirror) = pair(ObjectOptions::default());
    nodes[1]
        .with(|s| s.call(mirror, "set_limit", vec![], CallOptions::default()))
        .unwrap();
    let call = call(&nodes[0]);
    nodes[0]
        .with(|s| s.reply_error(call.query, "ValueError", "too high"))
        .unwrap();
    let error = ReplyError::Remote {
        kind: "ValueError".into(),
        message: "too high".into(),
    };
    assert_eq!(replies(&nodes[1]), (vec![Err(error)], RequestEnd::Failed));
}

#[test]
fn a_call_nobody_answers_is_cut_short_by_its_timeout() {
    let (nodes, mirror) = pair(ObjectOptions::default());
    let options = CallOptions {
        timeout_ms: Some(200),
        ..CallOptions::default()
    };
    nodes[1]
        .with(|s| s.call(mirror, "set_limit", vec![], options))
        .unwrap();
    call(&nodes[0]);
    assert_eq!(replies(&nodes[1]), (vec![], RequestEnd::CutShort));
}

#[test]
fn raw_calls_and_replies_pass_through_untouched() {
    let options = ObjectOptions {
        raw_methods: vec!["blob".into()],
        ..ObjectOptions::default()
    };
    let (nodes, mirror) = pair(options);
    let sent = Sample {
        attachment: Some(b"meta".to_vec().into()),
        ..raw(b"\x00\x01", "application/octet-stream")
    };
    let options = CallOptions {
        raw_replies: true,
        parameters: "token=abc".into(),
        ..CallOptions::default()
    };
    nodes[1]
        .with(|s| s.call_raw(mirror, "blob", &[], Some(sent.clone()), options))
        .unwrap();
    let call = call(&nodes[0]);
    assert_eq!(call.payload, Some(sent));
    assert_eq!(call.parameters, "token=abc");
    let answer = Sample {
        attachment: Some(b"more".to_vec().into()),
        ..raw(b"\x02", "image/png")
    };
    nodes[0]
        .with(|s| {
            s.reply_raw(call.query, answer.clone())?;
            s.finish(call.query)
        })
        .unwrap();
    assert_eq!(
        replies(&nodes[1]),
        (vec![Ok(Data::Raw(answer))], RequestEnd::Done)
    );
}
