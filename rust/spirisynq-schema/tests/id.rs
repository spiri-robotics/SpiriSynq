//! Type ids: the three forms, identity without versions, and what's rejected.

use spirisynq_schema::{ErrorKind, TypeId};

fn id(text: &str) -> TypeId {
    text.parse().unwrap_or_else(|e| panic!("{e}"))
}

const UUID: &str = "6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c42";

#[test]
fn pypi_form() {
    let camera = id("urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera");
    assert_eq!(
        camera.as_str(),
        "urn:pypi:spiricamera:0.3.4:spiricamera.camera.Camera"
    );
    assert_eq!(camera.to_string(), camera.as_str());
    assert_eq!(camera.version(), Some("0.3.4"));
    assert!(camera.same_type(&id(
        "urn:pypi:spiricamera:0.4.0.dev1+g1234:spiricamera.camera.Camera"
    )));
    assert!(!camera.same_type(&id("urn:pypi:spiricamera:0.3.4:spiricamera.camera.Other")));
    assert!(!camera.same_type(&id("urn:pypi:othercam:0.3.4:spiricamera.camera.Camera")));
}

#[test]
fn distribution_names_compare_normalized() {
    let a = id("urn:pypi:Spiri_Camera:1:m.C");
    for same in [
        "urn:pypi:spiri-camera:2:m.C",
        "urn:pypi:spiri.camera:1:m.C",
        "urn:pypi:SPIRI__-camera:1:m.C",
        "URN:PyPI:spiri-camera:1:m.C",
    ] {
        assert!(a.same_type(&id(same)), "{same}");
    }
    assert!(!a.same_type(&id("urn:pypi:spiricamera:1:m.C")));
}

#[test]
fn uuid_form() {
    let gripper = id(&format!("urn:uuid:{UUID}:1"));
    assert_eq!(gripper.version(), Some("1"));
    assert!(gripper.same_type(&id(&format!("urn:uuid:{}:2", UUID.to_uppercase()))));
    assert!(!gripper.same_type(&id("urn:uuid:00000000-0000-0000-0000-000000000000:1")));
}

#[test]
fn other_uris_are_opaque() {
    let a = id("https://example.com/types/Robot");
    assert_eq!(a.version(), None);
    assert!(a.same_type(&id("https://example.com/types/Robot")));
    assert!(!a.same_type(&id("https://example.com/types/robot")));
    assert!(!a.same_type(&id("urn:pypi:robot:1:robot.Robot")));
    id("x-custom+v.1:anything/goes?here");
}

#[test]
fn rejected() {
    for bad in [
        "",
        "urn",
        "no-colon-at-all",
        "1scheme:x",
        ":x",
        "bad_scheme:x",
        "scheme:",
        "has space:x",
        "scheme:x y",
        "scheme:bell\u{7}",
        // urn:pypi: needs exactly distribution, version and import path.
        "urn:pypi:robot:1",
        "urn:pypi:robot:1:robot.Robot:extra",
        "urn:pypi::1:robot.Robot",
        "urn:pypi:-robot:1:robot.Robot",
        "urn:pypi:robot-:1:robot.Robot",
        "urn:pypi:ro bot:1:robot.Robot",
        "urn:pypi:robot::robot.Robot",
        "urn:pypi:robot:1 0:robot.Robot",
        "urn:pypi:robot:1:Robot",
        "urn:pypi:robot:1:robot.1Robot",
        "urn:pypi:robot:1:robot..Robot",
        // urn:uuid: needs exactly a UUID and a version.
        &format!("urn:uuid:{UUID}"),
        &format!("urn:uuid:{UUID}:1:2"),
        &format!("urn:uuid:{UUID}:"),
        "urn:uuid:6f1c2a9e4b7d-4e2a-9c1f-3d8b5e0a7c42-:1",
        "urn:uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c4g:1",
        "urn:uuid:6f1c2a9e-4b7d-4e2a-9c1f-3d8b5e0a7c4:1",
    ] {
        let err = bad.parse::<TypeId>().expect_err(bad);
        assert_eq!(err.kind, ErrorKind::TypeId(bad.to_owned()));
        assert_eq!(err.pointer, "");
        assert_eq!(err.to_string(), format!("`{bad}` isn't a type id"));
    }
}

#[test]
fn ids_are_values() {
    let a = id("urn:pypi:robot:1:robot.Robot");
    let b = a.clone();
    assert_eq!(a, b);
    assert_ne!(a, id("urn:pypi:robot:2:robot.Robot"));
    assert!(format!("{a:?}").contains("robot.Robot"));
    let mut set = std::collections::HashSet::new();
    set.insert(a);
    assert!(set.contains(&b));
}

#[test]
fn key_chunks_leave_out_the_version() {
    assert_eq!(
        id("urn:pypi:Spiri_Camera:0.3.4:spiricamera.camera.Camera").key_chunk(),
        "pypi:spiri-camera:spiricamera.camera.Camera"
    );
    assert_eq!(
        id(&format!("urn:uuid:{}:7", UUID.to_uppercase())).key_chunk(),
        format!("uuid:{UUID}")
    );
    assert_eq!(
        id("https://example.com/types/a*b?c#d$e%f").key_chunk(),
        "uri:https:%2F%2Fexample.com%2Ftypes%2Fa%2Ab%3Fc%23d%24e%25f"
    );
}
