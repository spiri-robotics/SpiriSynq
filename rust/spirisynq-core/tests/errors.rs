//! Refused calls: what each error says, and that nothing was sent.

mod common;

use common::*;
use spirisynq_core::{CallOptions, Error, FieldOptions, ObjectOptions, Session, Zid};
use spirisynq_schema::KeyError;

#[test]
fn messages() {
    let yaml_error = spirisynq_yaml::to_string(&unwritable()).unwrap_err();
    let cases = [
        (Error::UnknownObject, "no such object".to_owned()),
        (Error::UnknownQuery, "no such incoming call".to_owned()),
        (Error::UnknownField("x".into()), "no field `x`".to_owned()),
        (
            Error::UnknownMethod("m".into()),
            "no remote method `m`".to_owned(),
        ),
        (
            Error::Topic("empty chunk".into()),
            "bad topic: empty chunk".to_owned(),
        ),
        (Error::NotMirror, "only a mirror can do this".to_owned()),
        (
            Error::NotOwner,
            "only an authoritative object can do this".to_owned(),
        ),
        (
            Error::LazyNeedsIgnoreIncoming("image".into()),
            "lazy field `image` must also ignore incoming writes".to_owned(),
        ),
        (
            Error::ParentMissing("gps".into()),
            "`gps` isn't there to hold the field".to_owned(),
        ),
        (
            Error::Key(KeyError::MissingArgument("user".into())),
            KeyError::MissingArgument("user".into()).to_string(),
        ),
        (
            Error::NotAString("user".into()),
            "`user` is in the key, so it must be a string".to_owned(),
        ),
        (Error::Encode(yaml_error.clone()), yaml_error.to_string()),
    ];
    for (error, text) in cases {
        assert_eq!(error.to_string(), text);
    }
    let boxed: Box<dyn std::error::Error> = Box::new(Error::NotOwner);
    assert!(boxed.source().is_none());
    assert_eq!(Error::from(yaml_error.clone()), Error::Encode(yaml_error));
}

#[test]
fn opening_objects() {
    let mut s = Session::new(Zid("me".into()));
    let lazy = FieldOptions {
        lazy: true,
        ..FieldOptions::default()
    };
    let cases = [
        (
            "a//b",
            ObjectOptions::default(),
            Error::Topic("empty chunk".into()),
        ),
        (
            "a/sr_x",
            ObjectOptions::default(),
            Error::Topic("`sr_x` is reserved".into()),
        ),
        (
            "a",
            options(&[("nope", FieldOptions::default())]),
            Error::UnknownField("nope".into()),
        ),
        (
            "a",
            ObjectOptions {
                raw_methods: vec!["fly".into()],
                ..ObjectOptions::default()
            },
            Error::UnknownMethod("fly".into()),
        ),
    ];
    for (topic, o, error) in cases {
        assert_eq!(
            s.create_owner(topic, battery(), o.clone()),
            Err(error.clone())
        );
        assert_eq!(s.create_mirror(topic, battery(), o), Err(error));
    }
    assert_eq!(
        s.create_owner("a", battery(), options(&[("image", lazy)])),
        Err(Error::LazyNeedsIgnoreIncoming("image".into()))
    );
    // Mirrors ignore owner-only settings.
    assert!(
        s.create_mirror("a", battery(), options(&[("image", lazy)]))
            .is_ok()
    );
    commands(&mut s);
    notices(&mut s);

    // Nothing was declared for the refused objects.
    let mut s = Session::new(Zid("me".into()));
    let _ = s.create_owner("a//b", battery(), ObjectOptions::default());
    assert_eq!(commands(&mut s), []);
}

#[test]
fn closed_objects() {
    let mut s = Session::new(Zid("me".into()));
    let gone = s
        .create_owner("a", battery(), ObjectOptions::default())
        .unwrap();
    s.close(gone).unwrap();
    let unknown = Err(Error::UnknownObject);
    assert_eq!(s.close(gone), unknown);
    assert_eq!(s.set_field(gone, "voltage", v("1")).map(|_| ()), unknown);
    assert_eq!(
        s.set_field_local(gone, "voltage", v("1")).map(|_| ()),
        unknown
    );
    assert_eq!(s.send_raw(gone, "voltage", yaml("1")), unknown);
    assert_eq!(s.field(gone, "voltage").map(|_| ()), unknown);
    assert_eq!(s.alive(gone).map(|_| ()), unknown);
    assert_eq!(s.has_listeners(gone, None).map(|_| ()), unknown);
    assert_eq!(s.rehydrate(gone), unknown);
    assert_eq!(
        s.call(gone, "count", vec![], CallOptions::default())
            .map(|_| ()),
        unknown
    );
    assert_eq!(
        s.call_raw(gone, "count", &[], None, CallOptions::default())
            .map(|_| ()),
        unknown
    );
}
