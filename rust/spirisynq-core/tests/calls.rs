//! Remote calls: keys and arguments, replies and the done marker, errors, raw methods and
//! raw replies, and calls cut short.

mod common;

use common::*;
use spirisynq_core::{
    CallOptions, Data, Error, IncomingCall, Notice, ObjectOptions, ReplyError, RequestEnd,
};
use spirisynq_schema::KeyError;
use spirisynq_yaml::Value;

fn args(pairs: &[(&str, &str)]) -> Vec<(String, Value)> {
    pairs.iter().map(|(k, x)| (k.to_string(), v(x))).collect()
}

fn the_call(notices: Vec<Notice>) -> IncomingCall {
    match <[_; 1]>::try_from(notices) {
        Ok([Notice::Call(call)]) => call,
        other => panic!("not one call: {other:?}"),
    }
}

#[test]
fn a_call_and_its_reply() {
    let (mut net, owner, mirror) = pair();
    let request = net
        .session(1)
        .call(
            mirror,
            "set_limit",
            args(&[("percent", "80")]),
            CallOptions::default(),
        )
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    assert_eq!(call.object, owner);
    assert_eq!(call.method, "set_limit");
    assert_eq!(call.key, "robot/battery/set_limit");
    assert_eq!(call.args, v("{percent: 80}"));
    assert_eq!(call.payload, None);
    net.session(0).reply(call.query, &v("80")).unwrap();
    net.session(0).finish(call.query).unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [
            Notice::Reply {
                request,
                result: Ok(Data::Value(v("80")))
            },
            Notice::RequestEnded {
                request,
                end: RequestEnd::Done
            },
        ]
    );
    assert_eq!(
        net.trace()[..3],
        [
            "1 get robot/battery/set_limit? application/yaml percent: 80",
            "0 reply robot/battery/set_limit application/yaml 80",
            "0 reply robot/battery/set_limit x-spirisynq/done <0 bytes>",
        ]
    );
    // The call is over.
    assert_eq!(net.session(0).finish(call.query), Err(Error::UnknownQuery));
    assert_eq!(
        net.session(0).reply(call.query, &v("1")),
        Err(Error::UnknownQuery)
    );
    assert_eq!(
        net.session(0).reply_raw(call.query, yaml("1")),
        Err(Error::UnknownQuery)
    );
    assert_eq!(
        net.session(0).reply_error(call.query, "E", "e"),
        Err(Error::UnknownQuery)
    );
}

#[test]
fn generators_reply_many_times() {
    let (mut net, _owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", args(&[("n", "2")]), CallOptions::default())
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    for i in ["0", "1"] {
        net.session(0).reply(call.query, &v(i)).unwrap();
    }
    net.session(0).finish(call.query).unwrap();
    net.run();
    let got = notices(net.session(1));
    assert_eq!(got.len(), 3);
    assert_eq!(
        got[2],
        Notice::RequestEnded {
            request,
            end: RequestEnd::Done
        }
    );
}

#[test]
fn errors_end_a_call() {
    let (mut net, _owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    assert_eq!(call.args, v("{}"));
    net.session(0).reply(call.query, &v("0")).unwrap();
    net.session(0)
        .reply_error(call.query, "ValueError", "n is required")
        .unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [
            Notice::Reply {
                request,
                result: Ok(Data::Value(v("0")))
            },
            Notice::Reply {
                request,
                result: Err(ReplyError::Remote {
                    kind: "ValueError".into(),
                    message: "n is required".into()
                })
            },
            Notice::RequestEnded {
                request,
                end: RequestEnd::Failed
            },
        ]
    );
}

#[test]
fn key_templates() {
    let (mut net, _owner, mirror) = pair();
    let s = net.session(1);
    s.call(
        mirror,
        "read",
        args(&[("user", "al/ice"), ("path", "logs/a*b"), ("start", "5")]),
        CallOptions {
            parameters: "token=abc".into(),
            timeout_ms: Some(500),
            ..CallOptions::default()
        },
    )
    .unwrap();
    net.run();
    assert_eq!(
        net.trace()[0],
        "1 get robot/battery/read/al%2Fice/logs/a%2Ab?token=abc application/yaml start: 5"
    );
    let call = the_call(notices(net.session(0)));
    assert_eq!(
        call.key_args,
        [
            ("user".to_owned(), "al/ice".to_owned()),
            ("path".to_owned(), "logs/a*b".to_owned())
        ]
    );
    assert_eq!(call.args, v("{start: 5}"));
    assert_eq!(call.parameters, "token=abc");

    let s = net.session(1);
    assert_eq!(
        s.call(
            mirror,
            "read",
            args(&[("user", "1"), ("path", "2")]),
            CallOptions::default()
        )
        .map(|_| ()),
        Err(Error::NotAString("user".into()))
    );
    assert_eq!(
        s.call(
            mirror,
            "read",
            args(&[("path", "x")]),
            CallOptions::default()
        )
        .map(|_| ()),
        Err(Error::Key(KeyError::MissingArgument("user".into())))
    );
    assert_eq!(
        s.call(mirror, "fly", vec![], CallOptions::default())
            .map(|_| ()),
        Err(Error::UnknownMethod("fly".into()))
    );
    assert!(matches!(
        s.call(
            mirror,
            "count",
            vec![("n".into(), unwritable())],
            CallOptions::default()
        ),
        Err(Error::Encode(_))
    ));
}

#[test]
fn raw_calls_and_replies() {
    let (mut net, owner) = with_owner(ObjectOptions {
        raw_methods: vec!["snapshot".into()],
        ..ObjectOptions::default()
    });
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    let payload = raw(b"\x00\x01", "application/zlib");
    let request = net
        .session(1)
        .call_raw(
            mirror,
            "snapshot",
            &[],
            Some(payload.clone()),
            CallOptions {
                raw_replies: true,
                ..CallOptions::default()
            },
        )
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    assert_eq!(call.object, owner);
    assert_eq!(call.args, v("{}"));
    assert_eq!(call.payload, Some(payload));
    let jpeg = raw(b"\xff\xd8", "image/jpeg");
    net.session(0).reply_raw(call.query, jpeg.clone()).unwrap();
    net.session(0).finish(call.query).unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1))[0],
        Notice::Reply {
            request,
            result: Ok(Data::Raw(jpeg))
        }
    );
    let s = net.session(1);
    assert_eq!(
        s.call_raw(
            mirror,
            "read",
            &[("path", "x")],
            None,
            CallOptions::default()
        )
        .map(|_| ()),
        Err(Error::Key(KeyError::MissingArgument("user".into())))
    );
    assert_eq!(
        s.call_raw(mirror, "fly", &[], None, CallOptions::default())
            .map(|_| ()),
        Err(Error::UnknownMethod("fly".into()))
    );
}

#[test]
fn bad_arguments_get_error_replies() {
    let (mut net, _owner, mirror) = pair();
    let cases = [
        (yaml("[1, 2]"), "the arguments must be a mapping"),
        (yaml("{1: 2}"), "argument names must be strings"),
        (
            raw(b"\x01", "zenoh/bytes"),
            "encoding `zenoh/bytes` isn't YAML",
        ),
        (
            yaml("{user: bob}"),
            "`user` is in the key, so it can't be in the payload too",
        ),
    ];
    for (payload, why) in cases {
        let request = net
            .session(1)
            .call_raw(
                mirror,
                "read",
                &[("user", "al"), ("path", "")],
                Some(payload),
                CallOptions::default(),
            )
            .unwrap();
        net.run();
        assert_eq!(notices(net.session(0)), []);
        assert_eq!(
            notices(net.session(1)),
            [
                Notice::Reply {
                    request,
                    result: Err(ReplyError::Remote {
                        kind: "ValueError".into(),
                        message: why.into()
                    })
                },
                Notice::RequestEnded {
                    request,
                    end: RequestEnd::Failed
                },
            ]
        );
    }
    // An empty payload is no arguments.
    net.session(1)
        .call_raw(
            mirror,
            "count",
            &[],
            Some(raw(b"", "application/yaml")),
            CallOptions::default(),
        )
        .unwrap();
    net.run();
    assert_eq!(the_call(notices(net.session(0))).args, v("{}"));
}

#[test]
fn replies_that_cant_be_read() {
    let (mut net, _owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    net.session(0)
        .reply_raw(call.query, raw(b"\x01", "zenoh/bytes"))
        .unwrap();
    net.session(0)
        .reply_raw(call.query, raw(b"bare text", "text/plain"))
        .unwrap();
    // An error reply that isn't in the usual form.
    net.session(0)
        .reply_raw(call.query, yaml("{type: E}"))
        .unwrap();
    net.run();
    let got = notices(net.session(1));
    assert_eq!(
        got[0],
        Notice::Reply {
            request,
            result: Err(ReplyError::Undecodable(
                "encoding `zenoh/bytes` isn't YAML".into()
            ))
        }
    );
    assert!(matches!(
        &got[1],
        Notice::Reply {
            result: Err(ReplyError::Undecodable(_)),
            ..
        }
    ));
    assert_eq!(
        got[2],
        Notice::Reply {
            request,
            result: Ok(Data::Value(v("{type: E}")))
        }
    );
}

#[test]
fn calls_cut_short() {
    let (mut net, owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    notices(net.session(0));
    net.expire_requests();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [Notice::RequestEnded {
            request,
            end: RequestEnd::CutShort
        }]
    );

    // An owner that closes with calls unanswered fails them.
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    notices(net.session(0));
    net.session(0).close(owner).unwrap();
    net.run();
    let got = notices(net.session(1));
    assert_eq!(
        got[1..],
        [
            Notice::Reply {
                request,
                result: Err(ReplyError::Remote {
                    kind: "RuntimeError".into(),
                    message: "object closed".into()
                })
            },
            Notice::RequestEnded {
                request,
                end: RequestEnd::Failed
            },
        ]
    );
    assert_eq!(net.session(0).close(owner), Err(Error::UnknownObject));
}

#[test]
fn an_owner_that_dies_mid_call() {
    let (mut net, _owner, mirror) = pair();
    let request = net
        .session(1)
        .call(mirror, "count", vec![], CallOptions::default())
        .unwrap();
    net.run();
    let call = the_call(notices(net.session(0)));
    net.session(0).reply(call.query, &v("0")).unwrap();
    net.run();
    net.disconnect(0);
    net.run();
    let got = notices(net.session(1));
    assert_eq!(
        got[1..],
        [
            Notice::Alive {
                object: mirror,
                alive: false
            },
            Notice::RequestEnded {
                request,
                end: RequestEnd::CutShort
            },
        ]
    );
}

#[test]
fn wildcard_queries_arent_calls() {
    // A wildcard query that reaches a method's queryable (`read/*/**` here) isn't a call: the
    // owner ends it quietly, whether or not the query key is under the object's topic.
    let (mut net, _owner, mirror) = pair();
    for prefix in ["robot", "robot/battery"] {
        let request = net.session(1).discover(Some(prefix), None).unwrap();
        net.run();
        assert_eq!(notices(net.session(0)), []);
        let got = notices(net.session(1));
        assert_eq!(
            got.last(),
            Some(&Notice::RequestEnded {
                request,
                end: RequestEnd::Done
            })
        );
    }
    let _ = mirror;
}
