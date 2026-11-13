//! The `javascript` loader: JavaScript source run in the bundled QuickJS-ng.

mod common;

use common::*;
use spirisynq_extism::Host;
use spirisynq_yaml::Value;

const CODE: &[Code] = &[
    (
        "per_cell",
        "property",
        "",
        "function per_cell(self) { return self.voltage / self.cells; }",
    ),
    (
        "total",
        "property",
        "",
        "function total(self) { return self.readings.reduce((a, b) => a + b, 0); }",
    ),
    (
        "info_name",
        "property",
        "",
        "function info_name(self) { return self.info.name; }",
    ),
    (
        "charge",
        "method",
        "{properties: {amps: {}, hours: {}}}",
        "function charge(self, amps, hours) {
           self.cells = self.cells + 2;
           self.name = 'spare';
           self.readings = [1, 2, 3];
           self.info = {mode: 'fast'};
           self.voltage = self.voltage + 0.5;
           return amps - hours;
         }",
    ),
    ("echo", "method", "", "function echo(self, x) { return x; }"),
    (
        "values",
        "method",
        "",
        r#"function values(self) {
             return {int: 3, tenth: 0.1, big: 1e300, s: "é\"\\\n\u0001", list: [1, 2],
                     empty: {}, emptyList: [], yes: true, none: null, missing: undefined,
                     huge: 2n ** 63n - 1n};
           }"#,
    ),
    (
        "big",
        "method",
        "",
        "function big(self, x, y) { return [typeof x, x + 1n, typeof y]; }",
    ),
    (
        "boom",
        "method",
        "",
        "function boom(self) {\n  throw new RangeError('too high');\n}",
    ),
    (
        "boom_object",
        "method",
        "",
        "function boom_object(self) { throw {}; }",
    ),
    ("broken", "method", "", "function ("),
    ("missing", "method", "", "var x = 1;"),
    (
        "nope",
        "property",
        "",
        "function nope(self) { return self.nope; }",
    ),
    (
        "sets",
        "property",
        "",
        "function sets(self) { self.voltage = 1; }",
    ),
    (
        "fn",
        "method",
        "",
        "function fn(self) { return [() => 1]; }",
    ),
    ("nan", "method", "", "function nan(self) { return NaN; }"),
    (
        "sym",
        "method",
        "",
        "function sym(self) { return Symbol('x'); }",
    ),
    (
        "set_fn",
        "method",
        "",
        "function set_fn(self) { self.name = () => 1; }",
    ),
    (
        "deep",
        "method",
        "",
        "function deep(self) { let t = []; for (let i = 0; i < 200; i++) t = [t]; return t; }",
    ),
    (
        "sandbox",
        "method",
        "",
        "function sandbox(self) {
           return ['std', 'os', 'require', 'process', 'print', 'fetch', 'console']
             .map((name) => typeof globalThis[name]);
         }",
    ),
    (
        "counter",
        "method",
        "",
        "var n = 0; function counter(self) { n += 1; return n; }",
    ),
    ("nothing", "method", "", "function nothing(self) {}"),
];

fn js() -> (
    spirisynq_core::memory::MemoryNetwork,
    spirisynq_core::ObjectId,
) {
    battery("javascript", CODE)
}

#[test]
fn self_reads_the_fields() {
    let (mut net, owner) = js();
    let mut host = Host::default();
    let s = net.session(0);
    let null = Value::Null;
    assert_eq!(run(s, &mut host, owner, "per_cell", &null), Ok(v("3.125")));
    assert_eq!(run(s, &mut host, owner, "total", &null), Ok(v("3.5")));
    assert_eq!(run(s, &mut host, owner, "info_name", &null), Ok(v("main")));
}

#[test]
fn a_method_sets_fields_and_takes_its_arguments_in_order() {
    let (mut net, owner) = js();
    let mut host = Host::default();
    let s = net.session(0);
    let result = run(s, &mut host, owner, "charge", &v("{hours: 2, amps: 3}"));
    assert_eq!(result, Ok(v("1")));
    for (path, value) in [
        // JavaScript writes 13, but the field is a number: it stays a float.
        ("voltage", "13.0"),
        ("cells", "6"),
        ("name", "spare"),
        ("readings", "[1.0, 2.0, 3.0]"),
        ("info", "{mode: fast}"),
    ] {
        assert_eq!(s.field(owner, path).unwrap(), Some(&v(value)), "{path}");
    }
    assert_eq!(
        run(s, &mut host, owner, "nothing", &Value::Null),
        Ok(Value::Null)
    );
}

#[test]
fn values_cross_as_json() {
    let (mut net, owner) = js();
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "values", &Value::Null).unwrap();
    let want = v(
        r#"{int: 3, tenth: 0.1, big: 1.0e+300, s: "é\"\\\n\x01", list: [1, 2],
        empty: {}, emptyList: [], yes: true, none: null, missing: null,
        huge: 9223372036854775807}"#,
    );
    assert_eq!(got, want);
    let x = v(r#"{x: {text: "a\"b\\c/\b\f\n\r\t é 😀 \x01", list: [-1, 2.5e-3, true, null, {}]}}"#);
    assert_eq!(
        run(s, &mut host, owner, "echo", &x).as_ref(),
        Ok(get(&x, "x"))
    );
    // An integer a number can't hold exactly arrives as a BigInt.
    let big = run(
        s,
        &mut host,
        owner,
        "big",
        &v("{x: 9007199254740993, y: 5}"),
    );
    assert_eq!(big, Ok(v("[bigint, 9007199254740994, number]")));
}

#[test]
fn what_javascript_cant_do_is_an_error() {
    let (mut net, owner) = js();
    let mut host = Host::default();
    let s = net.session(0);
    let cases = [
        ("boom", "RangeError: too high (code:2"),
        ("boom_object", "[object Object]"),
        ("broken", "SyntaxError"),
        ("missing", "Error: no function `missing`"),
        ("nope", "no field `nope`"),
        ("sets", "a property can't set fields (`voltage`)"),
        ("fn", "TypeError: a function has no JSON form"),
        ("nan", "TypeError: NaN has no JSON form"),
        ("sym", "TypeError: a symbol has no JSON form"),
        ("set_fn", "TypeError: a function has no JSON form"),
        ("deep", "the result isn't JSON"),
    ];
    for (name, why) in cases {
        let got = failure(run(s, &mut host, owner, name, &Value::Null));
        assert!(got.contains(why), "{name}: {got}");
    }
}

#[test]
fn javascript_is_sandboxed_and_starts_afresh() {
    let (mut net, owner) = js();
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "sandbox", &Value::Null);
    let undefined = "[undefined, undefined, undefined, undefined, undefined, undefined, undefined]";
    assert_eq!(got, Ok(v(undefined)));
    for _ in 0..2 {
        assert_eq!(
            run(s, &mut host, owner, "counter", &Value::Null),
            Ok(v("1"))
        );
    }
}
