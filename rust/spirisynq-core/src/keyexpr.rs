//! Matching a key against a zenoh key expression.

/// Whether the key expression `pattern` matches the key `key`, which has no wildcards. `*`
/// matches one chunk, `**` any number of chunks, and `$*` any part of a chunk.
pub(crate) fn includes(pattern: &str, key: &str) -> bool {
    let pattern: Vec<&str> = pattern.split('/').collect();
    let key: Vec<&str> = key.split('/').collect();
    chunks(&pattern, &key)
}

fn chunks(pattern: &[&str], key: &[&str]) -> bool {
    match pattern.split_first() {
        None => key.is_empty(),
        Some((&"**", rest)) => chunks(rest, key) || (!key.is_empty() && chunks(pattern, &key[1..])),
        Some((first, rest)) => key
            .split_first()
            .is_some_and(|(k, key)| chunk(first, k) && chunks(rest, key)),
    }
}

/// Whether one chunk of a pattern matches one chunk of a key.
fn chunk(pattern: &str, key: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    let mut parts = pattern.split("$*");
    let first = parts.next().unwrap_or_default();
    let Some(mut rest) = key.strip_prefix(first) else { return false };
    let parts: Vec<&str> = parts.collect();
    let Some((last, middle)) = parts.split_last() else { return rest.is_empty() };
    for part in middle {
        let Some(at) = rest.find(part) else { return false };
        rest = &rest[at + part.len()..];
    }
    rest.ends_with(last)
}
