//! The `lua` loader: Lua source run in the bundled Lua 5.4.

mod common;

use common::*;
use spirisynq_extism::Host;
use spirisynq_yaml::Value;

/// Every piece of Lua the tests run: (entry, role, params, source).
const CODE: &[Code] = &[
    (
        "per_cell",
        "property",
        "",
        "function per_cell(self) return self.voltage / self.cells end",
    ),
    (
        "total",
        "property",
        "",
        "function total(self)
           local t = 0
           for _, r in ipairs(self.readings) do t = t + r end
           return t
         end",
    ),
    (
        "info_name",
        "property",
        "",
        "function info_name(self) return self.info.name end",
    ),
    (
        "charge",
        "method",
        "{properties: {amps: {}, hours: {}}}",
        "function charge(self, amps, hours)
           self.cells = self.cells + 2
           self.name = 'spare'
           self.readings = {1, 2, 3}
           self.info = {mode = 'fast'}
           self.voltage = self.voltage + 0.5
           return amps - hours
         end",
    ),
    (
        "pair",
        "method",
        "",
        "function pair(self, a, b) return {a, b} end",
    ),
    ("echo", "method", "", "function echo(self, x) return x end"),
    (
        "values",
        "method",
        "",
        r#"function values(self)
             return {int = 3, float = 3.0, tenth = 0.1, big = 1e300, s = "é\"\\\n\1",
                     list = {1, 2}, empty = {}, nested = {{}}, yes = true, no = false,
                     keyed = {[1] = "a", [3] = "c"}}
           end"#,
    ),
    (
        "boom",
        "method",
        "",
        "function boom(self) error('too high') end",
    ),
    (
        "boom_table",
        "method",
        "",
        "function boom_table(self) error({}) end",
    ),
    ("broken", "method", "", "function ("),
    ("missing", "method", "", "x = 1"),
    (
        "nope",
        "property",
        "",
        "function nope(self) return self.nope end",
    ),
    (
        "sets",
        "property",
        "",
        "function sets(self) self.voltage = 1 end",
    ),
    ("fn", "method", "", "function fn(self) return print end"),
    ("nan", "method", "", "function nan(self) return 0/0 end"),
    (
        "bad_utf8",
        "method",
        "",
        r#"function bad_utf8(self) return {["\255"] = 1} end"#,
    ),
    (
        "short_utf8",
        "method",
        "",
        r#"function short_utf8(self) return {["\xc3"] = 1} end"#,
    ),
    (
        "cut_utf8",
        "method",
        "",
        r#"function cut_utf8(self) return {["\xc3("] = 1} end"#,
    ),
    (
        "bool_key",
        "method",
        "",
        "function bool_key(self) return {[true] = 1} end",
    ),
    (
        "deep",
        "method",
        "",
        "function deep(self)
           local t = {}
           for _ = 1, 200 do t = {t} end
           return t
         end",
    ),
    (
        "set_fn",
        "method",
        "",
        "function set_fn(self) self.name = print end",
    ),
    (
        "sandbox",
        "method",
        "",
        "function sandbox(self)
           local _, err = load(string.dump(function() end))
           return {io == nil, os == nil, require == nil, dofile == nil, loadfile == nil,
                   debug == nil, package == nil, load('return 1')(), err}
         end",
    ),
    (
        "counter",
        "method",
        "",
        "function counter(self) n = (n or 0) + 1 return n end",
    ),
    (
        "second_true",
        "method",
        "true",
        "function second_true(self, x) return x end",
    ),
    (
        "second_five",
        "method",
        "{properties: 5}",
        "function second_five(self, x) return x end",
    ),
    (
        "second_empty",
        "method",
        "{}",
        "function second_empty(self, x) return x end",
    ),
    (
        "second_int_key",
        "method",
        "{properties: {1: {}, x: {}}}",
        "function second_int_key(self, x) return x end",
    ),
    (
        "needs_y",
        "method",
        "{properties: {y: {}}}",
        "function needs_y(self, y) return y end",
    ),
];

#[test]
fn self_reads_the_fields() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    let null = Value::Null;
    assert_eq!(run(s, &mut host, owner, "per_cell", &null), Ok(v("3.125")));
    assert_eq!(run(s, &mut host, owner, "total", &null), Ok(v("3.5")));
    assert_eq!(run(s, &mut host, owner, "info_name", &null), Ok(v("main")));
}

#[test]
fn a_method_sets_fields_and_takes_its_arguments_in_order() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    // Given out of order: `params` says which comes first.
    let result = run(s, &mut host, owner, "charge", &v("{hours: 2, amps: 3}"));
    assert_eq!(result, Ok(v("1")));
    for (path, value) in [
        ("voltage", "13.0"),
        ("cells", "6"),
        ("name", "spare"),
        ("readings", "[1.0, 2.0, 3.0]"),
        ("info", "{mode: fast}"),
    ] {
        assert_eq!(s.field(owner, path).unwrap(), Some(&v(value)), "{path}");
    }
    // Without `params`, the arguments go in the order given.
    let result = run(s, &mut host, owner, "pair", &v("{b: 1, a: 2}"));
    assert_eq!(result, Ok(v("[1, 2]")));
}

#[test]
fn values_cross_as_json() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "values", &Value::Null).unwrap();
    for (key, want) in [
        ("int", "3"),
        ("float", "3.0"),
        ("tenth", "0.1"),
        ("big", "1.0e+300"),
        ("s", r#""é\"\\\n\x01""#),
        ("list", "[1, 2]"),
        ("empty", "[]"),
        ("nested", "[[]]"),
        ("yes", "true"),
        ("no", "false"),
    ] {
        assert_eq!(get(&got, key), &v(want), "{key}");
    }
    let keyed = get(&got, "keyed");
    assert_eq!((get(keyed, "1"), get(keyed, "3")), (&text("a"), &text("c")));
    // What goes in comes back out.
    let x = v(
        r#"{x: {text: "a\"b\\c/\b\f\n\r\t é 😀 \x01", list: [-1, 2.5e-3, true, false, []], none: null}}"#,
    );
    let echoed = run(s, &mut host, owner, "echo", &x).unwrap();
    let want = get(&x, "x");
    for key in ["text", "list"] {
        assert_eq!(get(&echoed, key), get(want, key), "{key}");
    }
    // A null in a table is no entry at all.
    assert!(matches!(&echoed, Value::Map(entries) if entries.len() == 2));
    assert_eq!(
        run(s, &mut host, owner, "echo", &v("{x: null}")),
        Ok(Value::Null)
    );
    // An empty table can't say which it was: it goes out as a list.
    assert_eq!(run(s, &mut host, owner, "echo", &v("{x: {}}")), Ok(v("[]")));
}

#[test]
fn what_lua_cant_do_is_an_error() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    let cases = [
        ("boom", "code:1: too high"),
        ("boom_table", "the code raised a value that isn't a string"),
        ("broken", "code:1:"),
        ("missing", "no function `missing`"),
        ("nope", "no field `nope`"),
        ("sets", "a property can't set fields (`voltage`)"),
        ("fn", "a function has no JSON form"),
        ("nan", "has no JSON form"),
        // A string that isn't UTF-8 is bytes, but a key must be text.
        ("bad_utf8", "a table key that isn't UTF-8 has no JSON form"),
        (
            "short_utf8",
            "a table key that isn't UTF-8 has no JSON form",
        ),
        ("cut_utf8", "a table key that isn't UTF-8 has no JSON form"),
        ("bool_key", "a table key must be a string or an integer"),
        ("deep", "nested more than 128 deep"),
        ("set_fn", "a function has no JSON form"),
    ];
    for (name, why) in cases {
        let got = failure(run(s, &mut host, owner, name, &Value::Null));
        assert!(got.contains(why), "{name}: {got}");
    }
    let mut deep = Value::Null;
    for _ in 0..200 {
        deep = Value::Seq(vec![deep]);
    }
    let args = Value::Map(vec![(text("x"), deep)]);
    let got = failure(run(s, &mut host, owner, "echo", &args));
    assert!(got.contains("nested too deep"), "{got}");
}

#[test]
fn lua_is_sandboxed_and_starts_afresh() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "sandbox", &Value::Null).unwrap();
    let Value::Seq(items) = got else { panic!("{got:?}") };
    assert_eq!(
        items[..8],
        seq(&v("[true, true, true, true, true, true, true, 1]"))[..]
    );
    assert!(
        matches!(&items[8], Value::String(e) if e.contains("binary chunk")),
        "{:?}",
        items[8]
    );
    for _ in 0..2 {
        assert_eq!(
            run(s, &mut host, owner, "counter", &Value::Null),
            Ok(v("1"))
        );
    }
}

#[test]
fn params_without_names_fall_back_to_the_order_given() {
    let (mut net, owner) = battery("lua", CODE);
    let mut host = Host::default();
    let s = net.session(0);
    for name in [
        "second_true",
        "second_five",
        "second_empty",
        "second_int_key",
    ] {
        assert_eq!(
            run(s, &mut host, owner, name, &v("{x: 7}")),
            Ok(v("7")),
            "{name}"
        );
    }
    let got = failure(run(s, &mut host, owner, "needs_y", &Value::Null));
    assert_eq!(got, "no argument `y`");
}
