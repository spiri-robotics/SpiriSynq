//! Name rules shared by key templates, member names and type ids.

/// A placeholder or Python-style identifier: a letter or `_`, then letters, digits or `_`.
pub(crate) fn is_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_alphabetic() || c == '_')
        && chars.all(|c| c.is_alphanumeric() || c == '_')
}

/// Whether `chunk` can be one literal chunk of a key: not empty, and free of `/`, zenoh's
/// wildcard and reserved characters (`*`, `$`, `?`, `#`) and template braces.
pub(crate) fn check_chunk(chunk: &str) -> Result<(), String> {
    if chunk.is_empty() {
        return Err("empty chunk".into());
    }
    match chunk.chars().find(|c| "/*$?#{}".contains(*c)) {
        Some(c) => Err(format!("`{c}` in `{chunk}`")),
        None => Ok(()),
    }
}

/// Whether `topic` can be an object's topic: `/`-separated chunks, each one that
/// [`check_chunk`] accepts, none of them `.` or `..`, and none starting with `sr_`, which
/// SpiriSynq keeps for its own keys under every topic. The error says what's wrong.
pub fn check_topic(topic: &str) -> Result<(), String> {
    for chunk in topic.split('/') {
        check_chunk(chunk)?;
        if chunk == "." || chunk == ".." {
            return Err(format!("`{chunk}` chunk"));
        }
        if chunk.starts_with("sr_") {
            return Err(format!("`{chunk}` is reserved"));
        }
    }
    Ok(())
}
