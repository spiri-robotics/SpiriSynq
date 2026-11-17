//! Bytes in every interpreter: they arrive as the language's bytes, wherever they are, and go
//! back out as bytes.

mod common;

use common::*;
use spirisynq_extism::Host;
use spirisynq_yaml::Value;

/// The same code in each language:
///
/// - `look(x)`: the lengths of the bytes in `blob_a`, `frames[0]` and `x` (3, 2 and 1, where
///   base64 text would be 4 long), and whether `x` is the language's bytes.
/// - `keep(x)`: bytes put back in fields, typed and not, nested, and in an `anyOf`; bytes put
///   where text goes; and `x` returned.
/// - `bad_text`: bytes that aren't UTF-8 where text goes.
/// - `plain`, `five`: text and a number in the `anyOf`.
const CODE: [(&str, [Code; 5]); 3] = [
    (
        "lua",
        [
            (
                "look",
                "method",
                "",
                "function look(self, x)
                   return {#self.blob_a, #self.frames[1], #x, type(x) == 'string'}
                 end",
            ),
            (
                "keep",
                "method",
                "",
                "function keep(self, x)
                   self.blob_b = self.blob_a
                   self.frames = {x, x}
                   self.either = x
                   self.anything = {raw = x, list = {x}}
                   self.name = 'hi'
                   return x
                 end",
            ),
            (
                "bad_text",
                "method",
                "",
                r#"function bad_text(self) self.name = "\255" end"#,
            ),
            (
                "plain",
                "method",
                "",
                "function plain(self) self.either = 'text' end",
            ),
            (
                "five",
                "method",
                "",
                "function five(self) self.either = 5 end",
            ),
        ],
    ),
    (
        "python",
        [
            (
                "look",
                "method",
                "",
                "def look(self, x):\n    return [len(self.blob_a), len(self.frames[0]), len(x), type(x) is bytes]\n",
            ),
            (
                "keep",
                "method",
                "",
                "def keep(self, x):\n    self.blob_b = self.blob_a\n    self.frames = [x, bytearray(x)]\n    self.either = x\n    self.anything = {'raw': x, 'list': (x,)}\n    self.name = b'hi'\n    return x\n",
            ),
            (
                "bad_text",
                "method",
                "",
                "def bad_text(self):\n    self.name = b'\\xff'\n",
            ),
            (
                "plain",
                "method",
                "",
                "def plain(self):\n    self.either = 'text'\n",
            ),
            (
                "five",
                "method",
                "",
                "def five(self):\n    self.either = 5\n",
            ),
        ],
    ),
    (
        "javascript",
        [
            (
                "look",
                "method",
                "",
                "function look(self, x) {
                   return [self.blob_a.length, self.frames[0].length, x.length, x instanceof Uint8Array];
                 }",
            ),
            (
                "keep",
                "method",
                "",
                "function keep(self, x) {
                   self.blob_b = self.blob_a;
                   self.frames = [x, x];
                   self.either = x;
                   self.anything = {raw: x, list: [x]};
                   self.name = Uint8Array.of(104, 105);
                   return x;
                 }",
            ),
            (
                "bad_text",
                "method",
                "",
                "function bad_text(self) { self.name = Uint8Array.of(255); }",
            ),
            (
                "plain",
                "method",
                "",
                "function plain(self) { self.either = 'text'; }",
            ),
            (
                "five",
                "method",
                "",
                "function five(self) { self.either = 5; }",
            ),
        ],
    ),
];

#[test]
fn bytes_arrive_as_bytes_and_go_out_as_bytes() {
    for (loader, code) in CODE {
        let (mut net, owner) = battery(loader, &code);
        let mut host = Host::default();
        let s = net.session(0);
        s.set_field(owner, "blob_a", v("!!binary AAEC")).unwrap();
        s.set_field(owner, "frames", v("[!!binary AAE=]")).unwrap();
        // One byte, 0xFF: not UTF-8, so even Lua marks it as bytes.
        let x = v("{x: !!binary /w==}");
        let looked = run(s, &mut host, owner, "look", &x);
        assert_eq!(looked, Ok(v("[3, 2, 1, true]")), "{loader}");
        let kept = run(s, &mut host, owner, "keep", &x);
        assert_eq!(kept, Ok(v("!!binary /w==")), "{loader}");
        for (path, value) in [
            ("blob_b", "!!binary AAEC"),
            ("frames", "[!!binary /w==, !!binary /w==]"),
            ("either", "!!binary /w=="),
            ("name", "hi"),
        ] {
            let got = s.field(owner, path).unwrap();
            assert_eq!(got, Some(&v(value)), "{loader} {path}");
        }
        // In no particular order: Lua's tables have none.
        let anything = s.field(owner, "anything").unwrap().unwrap();
        assert_eq!(get(anything, "raw"), &v("!!binary /w=="), "{loader}");
        assert_eq!(get(anything, "list"), &v("[!!binary /w==]"), "{loader}");
        let bad = failure(run(s, &mut host, owner, "bad_text", &Value::Null));
        assert_eq!(
            bad, "field `name`: bytes where text goes, and not UTF-8",
            "{loader}"
        );
        // Text that isn't marked takes the branch that isn't bytes, though it's valid base64.
        run(s, &mut host, owner, "plain", &Value::Null).unwrap();
        assert_eq!(s.field(owner, "either").unwrap(), Some(&text("text")));
        run(s, &mut host, owner, "five", &Value::Null).unwrap();
        assert_eq!(s.field(owner, "either").unwrap(), Some(&v("5")));
    }
}

/// Integer keys in each language: `keys(x)` puts `x`, a mapping with integer keys holding bytes,
/// in a field with no schema, and `{5: 1}` in `counts`, whose schema says integer keys; it
/// returns whether `x`'s key is an integer, and the length of the bytes in it. `bad_keys` puts a
/// key that isn't an integer in `counts`. Then what `anything` holds: JavaScript's keys are
/// strings, so they stay strings where the schema doesn't say.
const KEYS: [(&str, [Code; 2], &str); 3] = [
    (
        "lua",
        [
            (
                "keys",
                "method",
                "",
                "function keys(self, x)
                   self.anything = x
                   self.counts = {[5] = 1}
                   return {math.type(next(x)) == 'integer', #x[3][7]}
                 end",
            ),
            (
                "bad_keys",
                "method",
                "",
                "function bad_keys(self) self.counts = {x = 1} end",
            ),
        ],
        "{3: {7: !!binary /w==}}",
    ),
    (
        "python",
        [
            (
                "keys",
                "method",
                "",
                "def keys(self, x):\n    self.anything = x\n    self.counts = {5: 1}\n    return [type(list(x)[0]) is int, len(x[3][7])]\n",
            ),
            (
                "bad_keys",
                "method",
                "",
                "def bad_keys(self):\n    self.counts = {'x': 1}\n",
            ),
        ],
        "{3: {7: !!binary /w==}}",
    ),
    (
        "javascript",
        [
            (
                "keys",
                "method",
                "",
                "function keys(self, x) {
                   self.anything = x;
                   self.counts = {5: 1};
                   return [Object.keys(x)[0] === '3', x[3][7].length];
                 }",
            ),
            (
                "bad_keys",
                "method",
                "",
                "function bad_keys(self) { self.counts = {x: 1}; }",
            ),
        ],
        "{'3': {'7': !!binary /w==}}",
    ),
];

#[test]
fn integer_keys_arrive_and_go_out_as_integers() {
    for (loader, code, anything) in KEYS {
        let (mut net, owner) = battery(loader, &code);
        let mut host = Host::default();
        let s = net.session(0);
        let x = v("{x: {3: {7: !!binary /w==}}}");
        let got = run(s, &mut host, owner, "keys", &x);
        assert_eq!(got, Ok(v("[true, 1]")), "{loader}");
        let field = |s: &mut spirisynq_core::Session, path| s.field(owner, path).unwrap().cloned();
        assert_eq!(field(s, "anything"), Some(v(anything)), "{loader}");
        assert_eq!(field(s, "counts"), Some(v("{5: 1}")), "{loader}");
        let bad = failure(run(s, &mut host, owner, "bad_keys", &Value::Null));
        assert_eq!(bad, "field `counts`: key `x` isn't an integer", "{loader}");
    }
}
