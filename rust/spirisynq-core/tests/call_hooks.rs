//! WASM hooks on a remote method's caller, run on a scripted host: `before_call` and
//! `after_call`.

mod common;

use std::collections::VecDeque;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

use common::*;
use serde_json::{Value as Json, json};
use spirisynq_core::{
    CallOptions, Data, Error, Frame, IncomingCall, NextReply, Notice, ObjectId, ObjectOptions,
    Replies, ReplyError, RequestEnd, Sample, WasmHost,
};
use spirisynq_schema::WasmEntry;
use spirisynq_yaml::Value;

const HASH: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

const LOG: &str = r##"
$schema: https://json-schema.org/draft/2020-12/schema
x-spirisynq-schema: 3
$id: urn:pypi:spiri-robot:1.0:spirirobot.Log
properties:
  limit: {type: integer}
x-remote-methods:
  history:
    parameters:
      type: object
      properties: {minutes: {type: integer}, salt: {type: string, x-yaml-tag: "!!binary"}}
    returns: {type: array, items: {type: number}}
  count:
    parameters: {type: object, properties: {n: {type: integer}}}
    returns: {type: number}
    x-generator: true
  write:
    x-key: write/data/{path:**}
    parameters: {type: object, properties: {path: {type: string}}}
  plain: {}
x-wasm:
  check: {role: before_call, of: history, loader: wat, entry: check, hash: HASH}
  round: {role: after_call, of: history, loader: wat, entry: round, hash: HASH}
  tally: {role: after_call, of: count, loader: wat, entry: tally, hash: HASH}
  pack: {role: before_call, of: write, loader: wat, entry: pack, hash: HASH}
  look: {role: method, loader: wat, entry: look, hash: HASH}
"##;

/// A host that runs a script instead of WASM.
type Body = Box<dyn FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String> + Send>;

struct Script(Body);

impl WasmHost for Script {
    fn run(&mut self, code: &WasmEntry, frame: &mut Frame) -> Result<Vec<u8>, String> {
        (self.0)(code, frame)
    }
}

fn host(
    f: impl FnMut(&WasmEntry, &mut Frame) -> Result<Vec<u8>, String> + Send + 'static,
) -> Script {
    Script(Box::new(f))
}

fn input(frame: &Frame) -> Json {
    serde_json::from_slice(&frame.input()).unwrap()
}

fn wasm_error(result: Result<impl std::fmt::Debug, Error>) -> String {
    match result {
        Err(Error::Wasm(why)) => why,
        other => panic!("{other:?}"),
    }
}

fn args(pairs: &[(&str, &str)]) -> Vec<(String, Value)> {
    pairs.iter().map(|(k, x)| (k.to_string(), v(x))).collect()
}

/// A log owned on session 0, with `write` a raw method, and a mirror of it on session 1.
fn logs() -> (Net, ObjectId, ObjectId) {
    let schema = schema_from(&LOG.replace("HASH", HASH));
    let mut net = Net::new();
    net.add_session();
    let options = ObjectOptions {
        raw_methods: vec!["write".into()],
        ..ObjectOptions::default()
    };
    let owner = net
        .session(0)
        .create_owner("robot/log", schema.clone(), options)
        .unwrap();
    net.session(0).set_field(owner, "limit", v("60")).unwrap();
    net.add_session();
    let mirror = net
        .session(1)
        .create_mirror("robot/log", schema, ObjectOptions::default())
        .unwrap();
    settle(&mut net);
    net.clear_trace();
    (net, owner, mirror)
}

fn the_call(net: &mut Net) -> IncomingCall {
    net.run();
    match <[_; 1]>::try_from(notices(net.session(0))) {
        Ok([Notice::Call(call)]) => call,
        other => panic!("not one call: {other:?}"),
    }
}

/// Replies given in advance, and what was yielded.
#[derive(Default)]
struct Given {
    replies: VecDeque<Result<Sample, ReplyError>>,
    yielded: Arc<Mutex<Vec<Value>>>,
}

impl Replies for Given {
    fn next(&mut self) -> Option<Result<Sample, ReplyError>> {
        self.replies.pop_front()
    }

    fn give(&mut self, value: Value) {
        self.yielded.lock().unwrap().push(value);
    }
}

fn given(replies: Vec<Result<Sample, ReplyError>>) -> Box<Given> {
    Box::new(Given {
        replies: replies.into(),
        ..Given::default()
    })
}

fn remote(kind: &str, message: &str) -> ReplyError {
    ReplyError::Remote {
        kind: kind.into(),
        message: message.into(),
    }
}

#[test]
fn before_call_checks_the_arguments_and_sends_them() {
    let (mut net, _, mirror) = logs();
    let mut check = host(|code, frame| {
        assert_eq!(code.name, "check");
        assert_eq!(input(frame)["args"], json!({"minutes": 5}));
        assert_eq!(frame.field("limit"), Ok(json!(60)));
        assert_eq!(
            frame.set_field("limit", json!(1)),
            Err("a before_call can't set fields (`limit`)".into())
        );
        frame.send_args(json!({"minutes": 5, "salt": "AAE="}))?;
        assert_eq!(
            frame.send_args(json!({})),
            Err("a before_call sends exactly once".into())
        );
        Ok(Vec::new())
    });
    let options = CallOptions {
        timeout_ms: Some(500),
        ..CallOptions::default()
    };
    let request = net
        .session(1)
        .run_call(
            &mut check,
            mirror,
            "history",
            args(&[("minutes", "5")]),
            options,
        )
        .unwrap();
    let call = the_call(&mut net);
    // Sent the usual way, read as the parameters' schema says.
    assert_eq!(call.args, v("{minutes: 5, salt: !!binary AAE=}"));
    net.session(0).reply(call.query, &v("[1.234]")).unwrap();
    net.session(0).finish(call.query).unwrap();
    net.run();
    // `history` has a WASM `after_call`, so its replies stay raw for it.
    let got = notices(net.session(1));
    assert!(
        matches!(&got[0], Notice::Reply { request: r, result: Ok(Data::Raw(_)) } if *r == request),
        "{got:?}"
    );
}

#[test]
fn a_method_without_a_before_call_is_called_as_usual() {
    let (mut net, _, mirror) = logs();
    let mut never = host(|_, _| panic!("`plain` has no before_call"));
    let s = net.session(1);
    assert!(
        s.before_call_job(mirror, "plain", &[], CallOptions::default())
            .unwrap()
            .is_none()
    );
    s.run_call(
        &mut never,
        mirror,
        "plain",
        Vec::new(),
        CallOptions::default(),
    )
    .unwrap();
    let call = the_call(&mut net);
    net.session(0).reply(call.query, &v("1")).unwrap();
    net.run();
    let got = notices(net.session(1));
    // No `after_call` either: replies are decoded as usual.
    assert!(
        matches!(
            &got[0],
            Notice::Reply {
                result: Ok(Data::Value(_)),
                ..
            }
        ),
        "{got:?}"
    );
    let s = net.session(1);
    let unknown = Err(Error::UnknownMethod("nope".into()));
    let job = s.before_call_job(mirror, "nope", &[], CallOptions::default());
    assert_eq!(job.map(|_| ()), unknown);
    let job = s.after_call_job(mirror, "nope", given(vec![]));
    assert_eq!(job.map(|_| ()), unknown);
    assert!(
        s.after_call_job(mirror, "plain", given(vec![]))
            .unwrap()
            .is_none()
    );
}

#[test]
fn a_before_call_that_fails_sends_nothing() {
    let (mut net, _, mirror) = logs();
    let history = |net: &mut Net, script: &mut Script, minutes: &str| {
        let args = args(&[("minutes", minutes)]);
        let options = CallOptions::default();
        net.session(1)
            .run_call(script, mirror, "history", args, options)
    };
    let mut refuse = host(|_, _| Err("minutes must be between 1 and 60".into()));
    assert_eq!(
        wasm_error(history(&mut net, &mut refuse, "600")),
        "minutes must be between 1 and 60"
    );
    let mut silent = host(|_, _| Ok(Vec::new()));
    assert_eq!(
        wasm_error(history(&mut net, &mut silent, "5")),
        "the before_call didn't send"
    );
    let mut not_a_map = host(|_, frame| {
        assert_eq!(
            frame.send_args(json!([5])),
            Err("the arguments sent must be a mapping".into())
        );
        Ok(Vec::new())
    });
    wasm_error(history(&mut net, &mut not_a_map, "5"));
    let mut bad = host(|_, frame| {
        frame.send_args(json!({"salt": "not base64!"}))?;
        Ok(Vec::new())
    });
    let why = wasm_error(history(&mut net, &mut bad, "5"));
    assert!(why.starts_with("the arguments sent: not base64"), "{why}");
    let job = net.session(1).before_call_job(
        mirror,
        "history",
        &[("x".into(), unwritable_json())],
        CallOptions::default(),
    );
    assert_eq!(
        wasm_error(job.map(|_| ())),
        "`x`: a mapping key that isn't a string or an integer has no JSON form"
    );
    net.run();
    assert!(net.trace().is_empty(), "{:?}", net.trace());
}

#[test]
fn a_before_call_can_send_the_call_as_it_is() {
    let (mut net, _, mirror) = logs();
    let mut pack = host(|code, frame| {
        assert_eq!(code.name, "pack");
        let offset = frame.arg("offset")?;
        let mut sample = raw(b"zz", "application/zlib");
        sample.attachment = Some(offset.to_string().into_bytes().into());
        let parameters = frame.arg("parameters")?.as_str().unwrap().to_owned();
        frame.send_call(sample, parameters)?;
        Ok(Vec::new())
    });
    let write = |net: &mut Net, pack: &mut Script, path: &str, parameters: &str| {
        let args = vec![
            ("path".into(), v(path)),
            ("offset".into(), v("7")),
            ("parameters".into(), Value::String(parameters.into())),
        ];
        let options = CallOptions {
            parameters: "caller=1".into(),
            ..CallOptions::default()
        };
        net.session(1)
            .run_call(pack, mirror, "write", args, options)
    };
    write(&mut net, &mut pack, "logs/today.txt", "v=2").unwrap();
    let call = the_call(&mut net);
    // The key still comes from the template, with the before_call's `path`.
    assert_eq!(call.key, "robot/log/write/data/logs/today.txt");
    assert_eq!(call.parameters, "v=2");
    let payload = call.payload.expect("a raw method gets the payload");
    assert_eq!(payload.encoding, "application/zlib");
    assert_eq!(&payload.payload.bytes()[..], b"zz");
    assert_eq!(&payload.attachment.unwrap().bytes()[..], b"7");
    // Without parameters of its own, the caller's are sent.
    write(&mut net, &mut pack, "a", "").unwrap();
    assert_eq!(the_call(&mut net).parameters, "caller=1");
    // A key argument must be a string, and must be there.
    assert_eq!(
        write(&mut net, &mut pack, "3", ""),
        Err(Error::NotAString("path".into()))
    );
    let mut no_path = host(|_, frame| {
        frame.send_call(raw(b"", "zenoh/bytes"), String::new())?;
        Ok(Vec::new())
    });
    let result = net.session(1).run_call(
        &mut no_path,
        mirror,
        "write",
        Vec::new(),
        CallOptions::default(),
    );
    assert!(matches!(result, Err(Error::Key(_))), "{result:?}");
}

#[test]
fn only_call_hooks_send_calls_and_read_replies() {
    let (mut net, owner, _) = logs();
    let mut method = host(|_, frame| {
        let refused = Err::<(), String>("only a before_call sends a call".into());
        assert_eq!(frame.send_args(json!({})), refused);
        assert_eq!(frame.send_call(raw(b"", "x"), String::new()), refused);
        let reads = "only an after_call reads replies".to_owned();
        assert_eq!(frame.next_reply(), Err(reads.clone()));
        assert_eq!(frame.reply(0).map(|_| ()), Err(reads.clone()));
        assert_eq!(frame.decode_reply(0), Err(reads.clone()));
        assert_eq!(frame.reply_escaped(), Err(reads));
        assert_eq!(
            frame.give(json!(1)),
            Err("only an after_call yields results".into())
        );
        Ok(Vec::new())
    });
    net.session(0)
        .run_wasm(&mut method, owner, "look", &Value::Null)
        .unwrap();
}

#[test]
fn after_call_reads_the_replies_and_yields_the_result() {
    let (mut net, _, mirror) = logs();
    let replies = given(vec![Ok(yaml("[1.234, 2.345]"))]);
    let mut job = net
        .session(1)
        .after_call_job(mirror, "history", replies)
        .unwrap()
        .expect("`history` has an after_call");
    let mut round = host(|code, frame| {
        assert_eq!(code.name, "round");
        assert_eq!(input(frame)["args"], json!({}));
        assert_eq!(frame.next_reply(), Ok(NextReply::Reply(0)));
        assert_eq!(frame.reply(0)?.encoding, "application/yaml");
        assert_eq!(frame.reply(1).map(|_| ()), Err("no reply 1".into()));
        let values = frame.decode_reply(0)?;
        let rounded: Vec<f64> = values
            .as_array()
            .unwrap()
            .iter()
            .map(|x| (x.as_f64().unwrap() * 100.0).round() / 100.0)
            .collect();
        frame.give(json!(rounded))?;
        // The end, however often it's asked for.
        assert_eq!(frame.next_reply(), Ok(NextReply::Ended));
        assert_eq!(frame.next_reply(), Ok(NextReply::Ended));
        Ok(b"null".to_vec())
    });
    let output = job.run(&mut round);
    let result = net.session(1).finish_wasm(job, output);
    assert_eq!(result, Ok(v("[1.23, 2.35]")));
}

#[test]
fn a_method_that_replies_once_gets_exactly_one_result() {
    let (mut net, _, mirror) = logs();
    for n in [0, 2] {
        let job = net
            .session(1)
            .after_call_job(mirror, "history", given(vec![]))
            .unwrap();
        let mut job = job.unwrap();
        let mut yields = host(move |_, frame| {
            for _ in 0..n {
                frame.give(json!([1]))?;
            }
            Ok(Vec::new())
        });
        let output = job.run(&mut yields);
        assert_eq!(
            wasm_error(net.session(1).finish_wasm(job, output)),
            format!("`history` replies with one value, and the after_call yielded {n}")
        );
    }
    let job = net
        .session(1)
        .after_call_job(mirror, "history", given(vec![]));
    let mut job = job.unwrap().unwrap();
    let mut bad = host(|_, frame| frame.give(json!("x")).map(|_| Vec::new()));
    // Not an array: read as it is, since `items` says nothing about a string.
    let output = job.run(&mut bad);
    assert_eq!(net.session(1).finish_wasm(job, output), Ok(v("x")));
}

#[test]
fn a_binding_can_read_the_schema_and_replies_as_samples() {
    let (mut net, owner, _) = logs();
    let schema = net.session(0).schema(owner).unwrap();
    assert!(schema.method("count").unwrap().generator);
    assert!(!schema.method("history").unwrap().generator);
    net.session(0).close(owner).unwrap();
    assert_eq!(
        net.session(0).schema(owner).err(),
        Some(&Error::UnknownObject).cloned()
    );
    let sample = Data::Value(v("[1, 2]")).into_sample().unwrap();
    assert_eq!(sample, yaml("- 1\n- 2\n"));
    assert_eq!(Data::Raw(yaml("x")).into_sample(), Ok(yaml("x")));
    let unkeyable = Value::Map(vec![(v("[1]"), v("2"))]);
    let Err(ReplyError::Undecodable(why)) = Data::Value(unkeyable).into_sample() else {
        panic!("a sequence can't be a key");
    };
    assert!(!why.is_empty());
}

#[test]
fn a_generator_methods_results_go_out_as_theyre_yielded() {
    let (mut net, _, mirror) = logs();
    let replies = given(vec![Ok(yaml("1")), Ok(yaml("2")), Ok(yaml("3"))]);
    let yielded = replies.yielded.clone();
    let mut job = net
        .session(1)
        .after_call_job(mirror, "count", replies)
        .unwrap()
        .unwrap();
    let mut tally = host(|_, frame| {
        let mut total = 0;
        while let NextReply::Reply(i) = frame.next_reply()? {
            total += frame.decode_reply(i)?.as_i64().unwrap();
            frame.give(json!(total))?;
        }
        Ok(Vec::new())
    });
    let output = job.run(&mut tally);
    assert_eq!(net.session(1).finish_wasm(job, output), Ok(Value::Null));
    // `returns` says `number`: read as floats.
    assert_eq!(*yielded.lock().unwrap(), [v("1.0"), v("3.0"), v("6.0")]);
}

#[test]
fn an_error_reply_reaches_the_code_and_maybe_the_caller() {
    let (mut net, _, mirror) = logs();
    let run = |net: &mut Net, script: &mut Script| {
        let replies = given(vec![
            Ok(yaml("1")),
            Err(remote("ValueError", "too high")),
            Err(ReplyError::Undecodable("not YAML".into())),
        ]);
        let mut job = net
            .session(1)
            .after_call_job(mirror, "count", replies)
            .unwrap()
            .unwrap();
        let output = job.run(script);
        net.session(1).finish_wasm(job, output)
    };
    // Let through, the error reaches the caller as it is.
    let mut through = host(|_, frame| {
        assert_eq!(frame.reply_escaped(), Err("no error reply came".into()));
        assert_eq!(frame.next_reply(), Ok(NextReply::Reply(0)));
        let error = NextReply::Error {
            kind: "ValueError".into(),
            message: "too high".into(),
        };
        assert_eq!(frame.next_reply(), Ok(error));
        frame.reply_escaped()?;
        Err("ValueError: too high".into())
    });
    let result = run(&mut net, &mut through);
    assert_eq!(result, Err(Error::Reply(remote("ValueError", "too high"))));
    assert_eq!(result.unwrap_err().to_string(), "ValueError: too high");
    // Caught, the code carries on: a later error reply is the one that counts.
    let mut caught = host(|_, frame| {
        frame.next_reply()?;
        frame.next_reply()?;
        frame.reply_escaped()?;
        let error = NextReply::Error {
            kind: "Undecodable".into(),
            message: "not YAML".into(),
        };
        assert_eq!(frame.next_reply(), Ok(error));
        frame.give(json!(1))?;
        Ok(Vec::new())
    });
    assert_eq!(run(&mut net, &mut caught), Ok(Value::Null));
    // Failing for a reason of its own, it fails as code does.
    let mut own = host(|_, frame| {
        frame.next_reply()?;
        frame.next_reply()?;
        Err("KeyError: x".into())
    });
    assert_eq!(
        run(&mut net, &mut own),
        Err(Error::Wasm("KeyError: x".into()))
    );
}

/// Replies fed from another thread, as a binding does.
struct Fed {
    replies: mpsc::Receiver<Result<Sample, ReplyError>>,
    yielded: mpsc::Sender<Value>,
}

impl Replies for Fed {
    fn next(&mut self) -> Option<Result<Sample, ReplyError>> {
        self.replies.recv().ok()
    }

    fn give(&mut self, value: Value) {
        self.yielded.send(value).unwrap();
    }
}

#[test]
fn after_call_runs_on_a_thread_while_replies_arrive() {
    let (mut net, owner, mirror) = logs();
    let request = net
        .session(1)
        .call(mirror, "count", args(&[("n", "3")]), CallOptions::default())
        .unwrap();
    let (feed, replies) = mpsc::channel();
    let (yielded, results) = mpsc::channel();
    let fed = Box::new(Fed { replies, yielded });
    let mut job = net
        .session(1)
        .after_call_job(mirror, "count", fed)
        .unwrap()
        .unwrap();
    let code = std::thread::spawn(move || {
        let mut tally = host(|_, frame| {
            while let NextReply::Reply(i) = frame.next_reply()? {
                let value = frame.decode_reply(i)?;
                frame.give(value)?;
            }
            Ok(Vec::new())
        });
        let output = job.run(&mut tally);
        (job, output)
    });
    let call = the_call(&mut net);
    assert_eq!(call.object, owner);
    let mut feed = Some(feed);
    for i in 1..=3 {
        net.session(0)
            .reply(call.query, &v(&i.to_string()))
            .unwrap();
        if i == 3 {
            net.session(0).finish(call.query).unwrap();
        }
        net.run();
        for notice in notices(net.session(1)) {
            match notice {
                Notice::Reply { request: r, result } if r == request => {
                    let sample = match result {
                        Ok(Data::Raw(sample)) => Ok(sample),
                        Ok(Data::Value(_)) => panic!("replies are raw for an after_call"),
                        Err(e) => Err(e),
                    };
                    feed.as_ref().unwrap().send(sample).unwrap();
                }
                Notice::RequestEnded { end, .. } => {
                    assert_eq!(end, RequestEnd::Done);
                    feed = None;
                }
                other => panic!("{other:?}"),
            }
        }
        // Each result arrives while the code still runs.
        assert_eq!(results.recv(), Ok(v(&format!("{i}.0"))));
    }
    assert!(feed.is_none());
    let (job, output) = code.join().unwrap();
    assert_eq!(net.session(1).finish_wasm(job, output), Ok(Value::Null));
}

/// A value with no JSON form.
fn unwritable_json() -> Value {
    Value::Map(vec![(Value::Bool(true), Value::Null)])
}

#[test]
fn jobs_show_what_they_run() {
    let (mut net, _, mirror) = logs();
    let s = net.session(1);
    let job = s
        .before_call_job(mirror, "history", &[], CallOptions::default())
        .unwrap()
        .unwrap();
    assert_eq!(job.job().code().name, "check");
    let job = s
        .after_call_job(mirror, "count", given(vec![]))
        .unwrap()
        .unwrap();
    let shown = format!("{job:?}");
    assert!(
        shown.contains("Stream { received: 0, error: None, ended: false, .. }"),
        "{shown}"
    );
}
