//! Owners' liveliness tokens, as their mirrors see them.

mod common;

use common::*;
use spirisynq_core::{Command, Error, Event, Notice, ObjectOptions, Origin, Session, Zid};

#[test]
fn tokens_say_where_the_owner_is() {
    let (mut net, owner, mirror) = pair();
    assert_eq!(net.session(1).alive(mirror), Ok(true));
    assert_eq!(net.session(0).alive(owner), Err(Error::NotMirror));

    net.session(0).close(owner).unwrap();
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [Notice::Alive {
            object: mirror,
            alive: false
        }]
    );
    assert_eq!(net.session(1).alive(mirror), Ok(false));
    // The mirror keeps its state.
    assert_eq!(
        net.session(1).field(mirror, "voltage"),
        Ok(Some(&v("12.5")))
    );

    // A new owner, in another process, is followed.
    net.add_session();
    let again = net
        .session(2)
        .create_owner("robot/battery", battery(), ObjectOptions::default())
        .unwrap();
    net.session(2)
        .set_field(again, "voltage", v("9.0"))
        .unwrap();
    let got = settle(&mut net);
    assert_eq!(
        got[1],
        [
            Notice::Alive {
                object: mirror,
                alive: true
            },
            incoming(mirror, "voltage", "9.0", Origin::Owner),
        ]
    );
}

#[test]
fn a_dead_process() {
    let (mut net, _owner, mirror) = pair();
    net.disconnect(0);
    net.run();
    assert_eq!(
        notices(net.session(1)),
        [Notice::Alive {
            object: mirror,
            alive: false
        }]
    );
}

#[test]
fn several_tokens() {
    let mut s = Session::new(Zid("me".into()));
    let mirror = s
        .create_mirror(
            "robot/battery",
            battery(),
            ObjectOptions {
                rehydrate: false,
                ..ObjectOptions::default()
            },
        )
        .unwrap();
    let mut ids = commands(&mut s).into_iter().filter_map(|c| match c {
        Command::DeclareLivelinessSubscriber { id, .. } => Some(id),
        _ => None,
    });
    let subscriber = ids.next().unwrap();
    let token = |zid: &str, alive| Event::Liveliness {
        subscriber,
        key: format!("robot/battery/sr_alive/{zid}"),
        alive,
    };
    s.handle(token("a", true));
    s.handle(token("b", true));
    s.handle(token("b", false));
    s.handle(token("a", false));
    s.handle(token("a", false));
    assert_eq!(
        notices(&mut s),
        [
            Notice::Alive {
                object: mirror,
                alive: true
            },
            Notice::Alive {
                object: mirror,
                alive: false
            },
        ]
    );
}
