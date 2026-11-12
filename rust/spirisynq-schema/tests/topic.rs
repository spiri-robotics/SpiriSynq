//! Topics: which keys can name an object.

use spirisynq_schema::check_topic;

#[test]
fn good_topics() {
    for topic in [
        "myhost",
        "myhost/robot/camera",
        "a.b/c-d/sr",
        "x/my_sr_thing",
    ] {
        assert_eq!(check_topic(topic), Ok(()), "{topic}");
    }
}

#[test]
fn bad_topics() {
    for (topic, reason) in [
        ("", "empty chunk"),
        ("a//b", "empty chunk"),
        ("/a", "empty chunk"),
        ("a/", "empty chunk"),
        ("a/*", "`*` in `*`"),
        ("a/**", "`*` in `**`"),
        ("a/b$c", "`$` in `b$c`"),
        ("a/{b}", "`{` in `{b}`"),
        ("./a", "`.` chunk"),
        ("a/../b", "`..` chunk"),
        ("a/sr_rehydrate", "`sr_rehydrate` is reserved"),
    ] {
        assert_eq!(check_topic(topic), Err(reason.into()), "{topic}");
    }
}
