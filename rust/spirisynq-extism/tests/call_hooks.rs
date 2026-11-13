//! Caller hooks in every interpreter: `before_call` sending the call, the usual way or as it
//! is, and `after_call` reading replies and yielding results.

mod common;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use spirisynq_core::memory::MemoryNetwork;
use spirisynq_core::{
    CallOptions, Error, IncomingCall, Notice, ObjectId, Replies, ReplyError, Sample,
};
use spirisynq_extism::{Host, Limits};
use spirisynq_yaml::Value;

/// The same hooks in one language.
struct Language {
    loader: &'static str,
    code: [Code; 6],
    /// What `catcher` yields for the error reply `ValueError: too high`.
    caught: &'static str,
}

const PYTHON: Language = Language {
    loader: "python",
    code: [
        (
            "check",
            "before_call:history",
            "{properties: {minutes: {}}}",
            "def check(self, call, minutes):
    if not 1 <= minutes <= 60:
        raise ValueError('minutes must be between 1 and 60')
    call.send_args({'minutes': minutes})
",
        ),
        (
            "pack",
            "before_call:write",
            "{properties: {path: {}, data: {}}}",
            "def pack(self, call, path, data):
    call.send(data.encode(), parameters='v=2', encoding='text/plain', attachment=b'meta')
",
        ),
        (
            "twice",
            "before_call:summary",
            "",
            "def twice(self, call):
    call.send_args({})
    call.send_args({})
",
        ),
        (
            "rounded",
            "after_call:history",
            "",
            "def rounded(self, replies):
    for reply in replies:
        yield [round(x, 2) for x in reply.decode()]
",
        ),
        (
            "tally",
            "after_call:count",
            "",
            "def tally(self, replies):
    total = 0
    for reply in replies:
        assert reply.encoding == 'application/yaml' and reply.attachment is None
        assert reply.payload == str(reply.decode()).encode()
        total += reply.decode()
        yield total
",
        ),
        (
            "catcher",
            "after_call:summary",
            "",
            "def catcher(self, replies):
    try:
        for reply in replies:
            yield reply.decode()
    except RpcException as e:
        yield '%s/%s/%s' % (e.kind, e.message, e)
",
        ),
    ],
    caught: "ValueError/too high/ValueError: too high",
};

const LUA: Language = Language {
    loader: "lua",
    code: [
        (
            "check",
            "before_call:history",
            "{properties: {minutes: {}}}",
            "function check(self, call, minutes)
               if minutes < 1 or minutes > 60 then error('minutes must be between 1 and 60') end
               call:send_args({minutes = minutes})
             end",
        ),
        (
            "pack",
            "before_call:write",
            "{properties: {path: {}, data: {}}}",
            "function pack(self, call, path, data) call:send(data, 'v=2', 'text/plain', 'meta') end",
        ),
        (
            "twice",
            "before_call:summary",
            "",
            "function twice(self, call) call:send_args({x = 1}) call:send_args({x = 1}) end",
        ),
        (
            "rounded",
            "after_call:history",
            "",
            "function rounded(self, replies)
               for reply in replies do
                 local out = {}
                 for i, x in ipairs(reply:decode()) do out[i] = math.floor(x * 100 + 0.5) / 100 end
                 coroutine.yield(out)
               end
             end",
        ),
        (
            "tally",
            "after_call:count",
            "",
            "function tally(self, replies)
               local total = 0
               for reply in replies do
                 assert(reply.encoding == 'application/yaml' and reply.attachment == nil)
                 assert(reply.payload == tostring(reply:decode()))
                 total = total + reply:decode()
                 coroutine.yield(total)
               end
             end",
        ),
        (
            "catcher",
            "after_call:summary",
            "",
            "function catcher(self, replies)
               local ok, e = pcall(function()
                 for reply in replies do coroutine.yield(reply:decode()) end
               end)
               if not ok then coroutine.yield(e.kind .. '/' .. e.message .. '/' .. tostring(e)) end
             end",
        ),
    ],
    caught: "ValueError/too high/ValueError: too high",
};

const JAVASCRIPT: Language = Language {
    loader: "javascript",
    code: [
        (
            "check",
            "before_call:history",
            "{properties: {minutes: {}}}",
            "function check(self, call, minutes) {
               if (minutes < 1 || minutes > 60) throw new RangeError('minutes must be between 1 and 60');
               call.sendArgs({minutes});
             }",
        ),
        (
            "pack",
            "before_call:write",
            "{properties: {path: {}, data: {}}}",
            "const bytes = (s) => Uint8Array.from(s, (c) => c.charCodeAt(0));
             function pack(self, call, path, data) {
               call.send(bytes(data), {parameters: 'v=2', encoding: 'text/plain', attachment: bytes('meta')});
             }",
        ),
        (
            "twice",
            "before_call:summary",
            "",
            "function twice(self, call) { call.sendArgs({}); call.sendArgs({}); }",
        ),
        (
            "rounded",
            "after_call:history",
            "",
            "function* rounded(self, replies) {
               for (const reply of replies) yield reply.decode().map((x) => Math.round(x * 100) / 100);
             }",
        ),
        (
            "tally",
            "after_call:count",
            "",
            "function* tally(self, replies) {
               let total = 0;
               for (const reply of replies) {
                 if (reply.encoding !== 'application/yaml' || reply.attachment !== null)
                   throw new Error('wrong reply');
                 if (String.fromCharCode(...reply.payload) !== String(reply.decode()))
                   throw new Error('wrong payload');
                 total += reply.decode();
                 yield total;
               }
             }",
        ),
        (
            "catcher",
            "after_call:summary",
            "",
            "function* catcher(self, replies) {
               try {
                 for (const reply of replies) yield reply.decode();
               } catch (e) {
                 yield `${e.kind}/${e.message}/${e instanceof RpcException}`;
               }
             }",
        ),
    ],
    caught: "ValueError/too high/true",
};

const LANGUAGES: [Language; 3] = [PYTHON, LUA, JAVASCRIPT];

/// The call that reached the owner.
fn the_call(net: &mut MemoryNetwork) -> IncomingCall {
    net.run();
    let notices: Vec<_> = std::iter::from_fn(|| net.session(0).poll_notice()).collect();
    match <[_; 1]>::try_from(notices) {
        Ok([Notice::Call(call)]) => call,
        other => panic!("not one call: {other:?}"),
    }
}

fn args(pairs: &[(&str, &str)]) -> Vec<(String, Value)> {
    pairs.iter().map(|(k, x)| (k.to_string(), v(x))).collect()
}

#[test]
fn before_call_checks_and_sends_the_arguments() {
    for lang in LANGUAGES {
        let (mut net, _, mirror) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let loader = lang.loader;
        let history = |net: &mut MemoryNetwork, host: &mut Host, minutes: &str| {
            let args = args(&[("minutes", minutes)]);
            net.session(1)
                .run_call(host, mirror, "history", args, CallOptions::default())
        };
        history(&mut net, &mut host, "5").unwrap();
        assert_eq!(the_call(&mut net).args, v("{minutes: 5}"), "{loader}");
        let got = failure(history(&mut net, &mut host, "600").map(|_| Value::Null));
        assert!(
            got.contains("minutes must be between 1 and 60"),
            "{loader}: {got}"
        );
        net.run();
        assert!(
            net.session(0).poll_notice().is_none(),
            "{loader}: nothing sent"
        );
        let twice = net.session(1).run_call(
            &mut host,
            mirror,
            "summary",
            Vec::new(),
            CallOptions::default(),
        );
        let got = failure(twice.map(|_| Value::Null));
        assert!(
            got.contains("a before_call sends exactly once"),
            "{loader}: {got}"
        );
    }
}

#[test]
fn before_call_can_send_the_call_as_it_is() {
    for lang in LANGUAGES {
        let (mut net, _, mirror) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let loader = lang.loader;
        let args = args(&[("path", "logs/today.txt"), ("data", "zz")]);
        net.session(1)
            .run_call(&mut host, mirror, "write", args, CallOptions::default())
            .unwrap();
        let call = the_call(&mut net);
        assert_eq!(call.key, "robot/battery/write/logs/today.txt", "{loader}");
        assert_eq!(call.parameters, "v=2", "{loader}");
        assert_eq!(
            call.payload,
            Some(sample(b"zz", "text/plain", Some(b"meta"))),
            "{loader}"
        );
    }
}

/// Replies given in advance, each after a wait, and what was yielded.
#[derive(Default)]
struct Given {
    replies: VecDeque<Result<Sample, ReplyError>>,
    wait: Duration,
    yielded: Arc<Mutex<Vec<Value>>>,
}

impl Replies for Given {
    fn next(&mut self) -> Option<Result<Sample, ReplyError>> {
        std::thread::sleep(self.wait);
        self.replies.pop_front()
    }

    fn give(&mut self, value: Value) {
        self.yielded.lock().unwrap().push(value);
    }
}

fn yaml(text: &str) -> Sample {
    sample(text.as_bytes(), "application/yaml", None)
}

fn too_high() -> ReplyError {
    ReplyError::Remote {
        kind: "ValueError".into(),
        message: "too high".into(),
    }
}

/// Run `method`'s after_call on `replies`: its result, and what it yielded.
fn after_call(
    net: &mut MemoryNetwork,
    host: &mut Host,
    mirror: ObjectId,
    method: &str,
    replies: Given,
) -> (Result<Value, Error>, Vec<Value>) {
    let yielded = replies.yielded.clone();
    let mut job = net
        .session(1)
        .after_call_job(mirror, method, Box::new(replies))
        .unwrap()
        .expect("it has an after_call");
    let output = job.run(host);
    let result = net.session(1).finish_wasm(job, output);
    let yielded = yielded.lock().unwrap().clone();
    (result, yielded)
}

fn given(replies: Vec<Result<Sample, ReplyError>>) -> Given {
    Given {
        replies: replies.into(),
        ..Given::default()
    }
}

#[test]
fn after_call_yields_what_the_caller_gets() {
    for lang in LANGUAGES {
        let (mut net, _, mirror) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let loader = lang.loader;
        let replies = given(vec![Ok(yaml("[1.234, 2.345]"))]);
        let (result, _) = after_call(&mut net, &mut host, mirror, "history", replies);
        assert_eq!(result, Ok(v("[1.23, 2.35]")), "{loader}");
        // A generator method's results go out one by one.
        let replies = given(vec![Ok(yaml("1")), Ok(yaml("2")), Ok(yaml("3"))]);
        let (result, yielded) = after_call(&mut net, &mut host, mirror, "count", replies);
        assert_eq!(result, Ok(Value::Null), "{loader}");
        assert_eq!(yielded, [v("1.0"), v("3.0"), v("6.0")], "{loader}");
    }
}

#[test]
fn an_error_reply_is_raised_in_the_code() {
    for lang in LANGUAGES {
        let (mut net, _, mirror) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let loader = lang.loader;
        // Let through, it reaches the caller as it is.
        let replies = given(vec![Ok(yaml("1")), Err(too_high())]);
        let (result, yielded) = after_call(&mut net, &mut host, mirror, "count", replies);
        assert_eq!(result, Err(Error::Reply(too_high())), "{loader}");
        assert_eq!(yielded, [v("1.0")], "{loader}");
        // Caught, the code carries on.
        let replies = given(vec![Err(too_high())]);
        let (result, _) = after_call(&mut net, &mut host, mirror, "summary", replies);
        assert_eq!(result, Ok(text(lang.caught)), "{loader}");
    }
}

#[test]
fn waiting_for_replies_isnt_running_time() {
    let (mut net, _, mirror) = pair(PYTHON.loader, &PYTHON.code);
    let mut host = Host::new(Limits {
        timeout: Duration::from_millis(250),
        ..Limits::default()
    });
    let replies = Given {
        replies: vec![Ok(yaml("1")), Ok(yaml("2"))].into(),
        wait: Duration::from_millis(200),
        ..Given::default()
    };
    // 600 ms waiting, well over the limit; the code itself runs for a few.
    let (result, yielded) = after_call(&mut net, &mut host, mirror, "count", replies);
    assert_eq!(result, Ok(Value::Null));
    assert_eq!(yielded, [v("1.0"), v("3.0")]);
}

/// Asks for a reply with an index that can't be one.
const BAD_INDEX: &str = r#"
(module
  (import "extism:host/user" "reply_payload" (func $reply_payload (param i64) (result i64)))
  (func (export "bad_index") (result i32)
    (drop (call $reply_payload (i64.const -1)))
    (i32.const 0)))
"#;

#[test]
fn a_reply_index_below_zero_is_refused() {
    let code = [("bad_index", "after_call:summary", "", BAD_INDEX)];
    let (mut net, _, mirror) = pair("wat", &code);
    let mut host = Host::default();
    let (result, _) = after_call(&mut net, &mut host, mirror, "summary", given(vec![]));
    assert!(failure(result).contains("no reply -1"));
}
