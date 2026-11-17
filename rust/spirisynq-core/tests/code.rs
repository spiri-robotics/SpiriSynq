//! WASM code fetched by its hash: an owner leaves long code out of the schema it serves and
//! answers for it by hash; a mirror fetches what its schema leaves out, and keeps only what its
//! hash names.

mod common;

use std::sync::Arc;

use common::*;
use spirisynq_core::{
    Command, Error, Event, Notice, ObjectId, ObjectOptions, QueryId, Session, Source, Zid, wire,
};
use spirisynq_schema::{Source as Code, TypeSchema};
use spirisynq_yaml::Value;

fn text(s: &str) -> Value {
    Value::String(s.into())
}

/// A WASM method entry.
fn entry(loader: &str, source: Value, hash: String) -> Value {
    Value::Map(vec![
        (text("role"), text("method")),
        (text("loader"), text(loader)),
        (text("entry"), text("run")),
        (text("hash"), text(&hash)),
        (text("source"), source),
        (text("params"), Value::Map(Vec::new())),
    ])
}

/// The battery with three methods: `big`, WAT text longer than an owner inlines, `blob`, a
/// module as long, and `small`. The code of each, too.
fn coded() -> (Arc<TypeSchema>, [Code; 3]) {
    let big = Code::Text(format!("(module) ;; {}", "x".repeat(wire::INLINE_LIMIT)));
    let blob = Code::Binary((0..=255).cycle().take(wire::INLINE_LIMIT + 1).collect());
    let small = Code::Text("(module)".into());
    let source = |c: &Code| match c {
        Code::Text(t) => text(t),
        Code::Binary(b) => Value::Binary(b.clone()),
    };
    let wasm = Value::Map(vec![
        (text("big"), entry("wat", source(&big), big.hash())),
        (text("blob"), entry("wasm", source(&blob), blob.hash())),
        (text("small"), entry("wat", source(&small), small.hash())),
    ]);
    let Value::Map(mut root) = v(BATTERY) else { unreachable!() };
    root.push((text("x-wasm"), wasm));
    let schema = TypeSchema::from_value(Value::Map(root)).unwrap();
    (Arc::new(schema), [big, blob, small])
}

/// The schema `session` fetches from the owner on `robot/battery`.
fn served(net: &mut Net, session: usize) -> Arc<TypeSchema> {
    net.session(session).fetch_schema("robot/battery").unwrap();
    let all = settle(net);
    let schema = all[session].iter().find_map(|n| match n {
        Notice::Schema { result, .. } => Some(result.clone().unwrap()),
        _ => None,
    });
    Arc::new(TypeSchema::from_value(schema.unwrap()).unwrap())
}

/// The code a job for `name` would run.
fn code_of(s: &Session, object: ObjectId, name: &str) -> Result<Code, Error> {
    let job = s.wasm_job(object, name, &Value::Null)?;
    Ok(job.code().source.clone().unwrap())
}

#[test]
fn a_mirror_fetches_the_code_its_schema_leaves_out() {
    let (schema, [big, blob, small]) = coded();
    let mut net = Net::new();
    net.add_session();
    net.add_session();
    net.session(0)
        .create_owner("robot/battery", schema, ObjectOptions::default())
        .unwrap();
    let served = served(&mut net, 1);
    let left_out: Vec<_> = served
        .wasm()
        .iter()
        .map(|c| (c.name.as_str(), c.source.is_none()))
        .collect();
    assert_eq!(left_out, [("big", true), ("blob", true), ("small", false)]);
    let s = net.session(1);
    let mirror = s
        .create_mirror("robot/battery", served, ObjectOptions::default())
        .unwrap();
    // Until it arrives, code left out can't run.
    let not_yet = Error::CodeNotFetched("big".into());
    assert_eq!(code_of(s, mirror, "big"), Err(not_yet.clone()));
    assert_eq!(
        not_yet.to_string(),
        "the code of `big` hasn't been fetched from the owner yet"
    );
    assert_eq!(code_of(s, mirror, "small"), Ok(small));
    settle(&mut net);
    let s = net.session(1);
    assert_eq!(code_of(s, mirror, "big"), Ok(big));
    assert_eq!(code_of(s, mirror, "blob"), Ok(blob));
    // Fetched once per session: another mirror of the type has it straight away.
    let served = s.schema(mirror).unwrap().clone();
    let again = s
        .create_mirror("robot/battery", served, ObjectOptions::default())
        .unwrap();
    assert!(
        commands(s)
            .iter()
            .all(|c| !matches!(c, Command::Get { key, .. } if key.contains("sr_code")))
    );
    assert!(code_of(s, again, "big").is_ok());
}

#[test]
fn a_mirror_keeps_only_the_code_its_hash_names() {
    let (schema, [big, blob, _]) = coded();
    let served = Arc::new(TypeSchema::from_value(served_by_hand(&schema)).unwrap());
    let mut s = Session::new(Zid("me".into()));
    let mirror = s
        .create_mirror("robot/battery", served, ObjectOptions::default())
        .unwrap();
    let gets = |s: &mut Session| -> Vec<_> {
        commands(s)
            .into_iter()
            .filter_map(|c| match c {
                Command::Get { id, key, .. } if key.contains("sr_code") => Some((id, key)),
                _ => None,
            })
            .collect()
    };
    let liveliness = |cs: &[Command]| {
        cs.iter().find_map(|c| match c {
            Command::DeclareLivelinessSubscriber { id, .. } => Some(*id),
            _ => None,
        })
    };
    let first = commands(&mut s);
    let alive = liveliness(&first).unwrap();
    let fetches: Vec<_> = first
        .iter()
        .filter_map(|c| match c {
            Command::Get { id, key, .. } if key.contains("sr_code") => Some((*id, key.clone())),
            _ => None,
        })
        .collect();
    let hex = |c: &Code| c.hash().trim_start_matches("sha256:").to_owned();
    assert_eq!(
        fetches.iter().map(|(_, k)| k.clone()).collect::<Vec<_>>(),
        [
            format!("robot/battery/sr_code/{}", hex(&big)),
            format!("robot/battery/sr_code/{}", hex(&blob)),
        ]
    );
    // Wrong bytes, text that isn't UTF-8, and an error reply: none of them is the code.
    let replies = [
        Ok(raw(b"wrong", "zenoh/bytes")),
        Ok(raw(&[0xff], "zenoh/bytes")),
    ];
    for ((request, key), result) in fetches.iter().zip(replies) {
        let reply = Event::Reply {
            request: *request,
            key: key.clone(),
            result,
            replier: Source::Unknown,
        };
        s.handle(reply);
    }
    let (request, key) = fetches[0].clone();
    let error = raw(b"no code with this hash", "text/plain");
    s.handle(Event::Reply {
        request,
        key,
        result: Err(error),
        replier: Source::Unknown,
    });
    // While the fetches are under way, an owner appearing doesn't start more.
    let token = Event::Liveliness {
        subscriber: alive,
        key: "robot/battery/sr_alive/owner".into(),
        alive: true,
    };
    s.handle(token.clone());
    assert!(gets(&mut s).is_empty());
    for (request, _) in &fetches {
        s.handle(Event::RequestDone { request: *request });
    }
    assert_eq!(
        code_of(&s, mirror, "big"),
        Err(Error::CodeNotFetched("big".into()))
    );
    // Once they've ended, the next owner to appear is asked again.
    s.handle(token);
    assert_eq!(gets(&mut s).len(), 2);
    assert!(
        notices(&mut s)
            .iter()
            .all(|n| !matches!(n, Notice::RequestEnded { .. }))
    );
}

/// The schema with its long code left out, as an owner serves it.
fn served_by_hand(schema: &TypeSchema) -> Value {
    let Value::Map(mut root) = schema.raw().clone() else { unreachable!() };
    for (key, wasm) in &mut root {
        let Value::Map(entries) = wasm else { continue };
        if *key != text("x-wasm") {
            continue;
        }
        for (name, entry) in entries {
            let Value::Map(fields) = entry else { unreachable!() };
            if *name != text("small") {
                fields.retain(|(k, _)| *k != text("source"));
            }
        }
    }
    Value::Map(root)
}

#[test]
fn an_owner_answers_only_for_its_own_code() {
    let (schema, [big, ..]) = coded();
    let mut s = Session::new(Zid("me".into()));
    s.create_owner("robot/battery", schema, ObjectOptions::default())
        .unwrap();
    // Another type's code, known to the session but not this object's.
    let other = Code::Text("(module $other)".into());
    let Value::Map(mut root) = v(&BATTERY.replace("spirirobot.Battery", "spirirobot.Other")) else {
        unreachable!()
    };
    let wasm = Value::Map(vec![(
        text("other"),
        entry("wat", text("(module $other)"), other.hash()),
    )]);
    root.push((text("x-wasm"), wasm));
    let other_schema = Arc::new(TypeSchema::from_value(Value::Map(root)).unwrap());
    s.create_owner("robot/other", other_schema, ObjectOptions::default())
        .unwrap();
    let queryable = commands(&mut s)
        .into_iter()
        .find_map(|c| match c {
            Command::DeclareQueryable { id, key } if key == "robot/battery/sr_code/*" => Some(id),
            _ => None,
        })
        .unwrap();
    let ask = |s: &mut Session, hex: &str| {
        s.handle(Event::Query {
            queryable,
            query: QueryId(1),
            key: format!("robot/battery/sr_code/{hex}"),
            parameters: String::new(),
            payload: None,
        });
        commands(s)
    };
    let hex = |c: &Code| c.hash().trim_start_matches("sha256:").to_owned();
    let answer = ask(&mut s, &hex(&big));
    let Command::Reply { sample, .. } = &answer[0] else { panic!("{answer:?}") };
    assert_eq!(sample.payload.bytes().into_owned(), big.as_bytes());
    for hex in [hex(&other), "00".into()] {
        let answer = ask(&mut s, &hex);
        assert!(matches!(&answer[0], Command::ReplyErr { .. }), "{answer:?}");
    }
}
