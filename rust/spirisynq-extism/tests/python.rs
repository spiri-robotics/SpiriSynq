//! The `python` loader: Python source run in the bundled MicroPython.

mod common;

use common::*;
use spirisynq_extism::Host;
use spirisynq_yaml::Value;

const CODE: &[Code] = &[
    (
        "per_cell",
        "property",
        "",
        "def per_cell(self):\n    return self.voltage / self.cells\n",
    ),
    (
        "total",
        "property",
        "",
        "def total(self):\n    return sum(self.readings)\n",
    ),
    (
        "info_name",
        "property",
        "",
        "def info_name(self):\n    return self.info['name']\n",
    ),
    (
        "charge",
        "method",
        "{properties: {amps: {}, hours: {}}}",
        "def charge(self, amps, hours):
    self.cells += 2
    self.name = 'spare'
    self.readings = [1, 2, 3]
    self.info = {'mode': 'fast'}
    self.voltage = self.voltage + 0.5
    return amps - hours
",
    ),
    ("echo", "method", "", "def echo(self, x):\n    return x\n"),
    (
        "bump",
        "method",
        "",
        "def bump(self, x):\n    return [type(x).__name__, x + 1]\n",
    ),
    (
        "values",
        "method",
        "",
        r#"def values(self):
    return {'int': 3, 'float': 3.0, 'tenth': 0.1, 'third': 0.1 + 0.2, 'big': 10**30,
            'huge': 1e300, 's': 'é"\\\n\x01', 'list': [1, 2], 'tuple': (1, 2), 'empty': {},
            'yes': True, 'no': False, 'none': None, 'raw': b'\x00\x01\x02',
            'buffer': bytearray(b'\x03'), 'keyed': {3: 'c'}}
"#,
    ),
    (
        "manufactured_year",
        "method",
        "",
        r#"def manufactured_year(self, serial):
    import re

    m = re.match(r"BAT-(\d+)-\d+$", serial)
    if m is None:
        raise ValueError("serial number not recognised: " + serial)
    return int(m.group(1))
"#,
    ),
    (
        "firmware_version",
        "method",
        "",
        r#"def firmware_version(self, firmware):
    import json

    try:
        info = json.loads(firmware)
    except ValueError:
        return None
    return tuple(int(p) for p in info["version"].split("."))
"#,
    ),
    (
        "weakest_cells",
        "method",
        "",
        "def weakest_cells(self, voltages, count):
    ranked = sorted(enumerate(voltages), key=lambda c: c[1])
    return {i: v for i, v in ranked[:count]}
",
    ),
    (
        "modules",
        "method",
        "",
        "def modules(self):
    import array, binascii, cmath, collections, hashlib, heapq, math, random, struct, sys
    h = [5, 1, 3]
    heapq.heapify(h)
    d = collections.deque((), 4)
    d.append(1)
    r = random.random()
    return [
        list(array.array('i', [1, 2])),
        binascii.hexlify(b'ab').decode(),
        cmath.sqrt(-4).imag,
        collections.OrderedDict(a=1)['a'],
        hashlib.sha256(b'').digest()[:2].hex(),
        heapq.heappop(h),
        d.popleft(),
        math.sqrt(16),
        0 <= r < 1,
        struct.pack('<H', 258).hex(),
        sys.implementation.name,
        sys.implementation.version >= (1, 29),
        f'{1 + 1}',
    ]
",
    ),
    (
        "imports",
        "method",
        "",
        "def imports(self):
    refused = []
    for name in ['os', 'time', 'gc', 'micropython', 'socket', 'select', 'machine', 'code']:
        try:
            __import__(name)
        except ImportError:
            refused.append(name)
    try:
        open('x')
    except OSError:
        refused.append('open')
    return refused
",
    ),
    (
        "boom",
        "method",
        "",
        "def boom(self):\n    raise ValueError('too high')\n",
    ),
    ("broken", "method", "", "def broken(self:\n"),
    ("missing", "method", "", "x = 1\n"),
    (
        "nope",
        "property",
        "",
        "def nope(self):\n    return self.nope\n",
    ),
    (
        "sets",
        "property",
        "",
        "def sets(self):\n    self.voltage = 1\n",
    ),
    (
        "thing",
        "method",
        "",
        "def thing(self):\n    return object()\n",
    ),
    (
        "nan",
        "method",
        "",
        "def nan(self):\n    return float('nan')\n",
    ),
    (
        "inf",
        "method",
        "",
        "def inf(self):\n    return float('inf')\n",
    ),
    (
        "tuple_key",
        "method",
        "",
        "def tuple_key(self):\n    return {(1, 2): 3}\n",
    ),
    (
        "deep",
        "method",
        "",
        "def deep(self):\n    t = []\n    for _ in range(200):\n        t = [t]\n    return t\n",
    ),
    (
        "set_fn",
        "method",
        "",
        "def set_fn(self):\n    self.name = len\n",
    ),
    (
        "recurse",
        "method",
        "",
        "def recurse(self, n=0):\n    return recurse(self, n + 1)\n",
    ),
    (
        "counter",
        "method",
        "",
        "n = 0\ndef counter(self):\n    global n\n    n += 1\n    return n\n",
    ),
    (
        "churn",
        "method",
        "",
        "def churn(self):\n    for i in range(100000):\n        s = str(i) * 10\n    return len(s)\n",
    ),
];

fn python() -> (
    spirisynq_core::memory::MemoryNetwork,
    spirisynq_core::ObjectId,
) {
    battery("python", CODE)
}

#[test]
fn self_reads_the_fields() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    let null = Value::Null;
    assert_eq!(run(s, &mut host, owner, "per_cell", &null), Ok(v("3.125")));
    assert_eq!(run(s, &mut host, owner, "total", &null), Ok(v("3.5")));
    assert_eq!(run(s, &mut host, owner, "info_name", &null), Ok(v("main")));
}

#[test]
fn a_method_sets_fields_and_takes_its_arguments_in_order() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
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
}

#[test]
fn values_cross_as_json() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "values", &Value::Null).unwrap();
    for (key, want) in [
        ("int", "3"),
        ("float", "3.0"),
        ("tenth", "0.1"),
        ("third", "0.30000000000000004"),
        // Too big for a 64-bit integer: it arrives as a float.
        ("big", "1.0e+30"),
        ("huge", "1.0e+300"),
        ("s", r#""é\"\\\n\x01""#),
        ("list", "[1, 2]"),
        ("tuple", "[1, 2]"),
        ("empty", "{}"),
        ("yes", "true"),
        ("no", "false"),
        ("none", "null"),
        // Bytes go out marked as bytes: still bytes where the schema doesn't say.
        ("raw", "!!binary AAEC"),
        ("buffer", "!!binary Aw=="),
        ("keyed", "{'3': c}"),
    ] {
        assert_eq!(get(&got, key), &v(want), "{key}");
    }
    let x = v(r#"{x: {text: "a\"b\\c/\b\f\n\r\t é 😀 \x01", list: [-1, 2.5e-3, true, null, {}]}}"#);
    let echoed = run(s, &mut host, owner, "echo", &x).unwrap();
    let want = get(&x, "x");
    for key in ["text", "list"] {
        assert_eq!(get(&echoed, key), get(want, key), "{key}");
    }
    let big = run(s, &mut host, owner, "bump", &v("{x: 9007199254740993}"));
    assert_eq!(big, Ok(v("[int, 9007199254740994]")));
}

#[test]
fn the_docs_examples_run() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    let year = run(
        s,
        &mut host,
        owner,
        "manufactured_year",
        &v("{serial: BAT-2024-00172}"),
    );
    assert_eq!(year, Ok(v("2024")));
    let bad = failure(run(
        s,
        &mut host,
        owner,
        "manufactured_year",
        &v("{serial: X}"),
    ));
    assert_eq!(bad, "ValueError: serial number not recognised: X (line 6)");
    let firmware = v(r#"{firmware: '{"version": "2.3.1"}'}"#);
    let version = run(s, &mut host, owner, "firmware_version", &firmware);
    assert_eq!(version, Ok(v("[2, 3, 1]")));
    let none = run(
        s,
        &mut host,
        owner,
        "firmware_version",
        &v("{firmware: '{oops'}"),
    );
    assert_eq!(none, Ok(Value::Null));
    let cells = v("{voltages: [3.8, 3.69, 3.9, 3.52], count: 2}");
    let weakest = run(s, &mut host, owner, "weakest_cells", &cells).unwrap();
    assert_eq!(
        (get(&weakest, "3"), get(&weakest, "1")),
        (&v("3.52"), &v("3.69"))
    );
}

#[test]
fn the_promised_modules_are_there_and_nothing_else() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    let got = run(s, &mut host, owner, "modules", &Value::Null);
    let want = "[[1, 2], '6162', 2.0, 1, e3b0, 1, 1, 4.0, true, '0201', micropython, true, '2']";
    assert_eq!(got, Ok(v(want)));
    let refused = run(s, &mut host, owner, "imports", &Value::Null);
    let want = "[os, time, gc, micropython, socket, select, machine, code, open]";
    assert_eq!(refused, Ok(v(want)));
}

#[test]
fn what_python_cant_do_is_an_error() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    let cases = [
        ("boom", "ValueError: too high (line 2)"),
        ("broken", "SyntaxError"),
        ("missing", "NameError: no function `missing`"),
        ("nope", "no field `nope`"),
        ("sets", "a property can't set fields (`voltage`)"),
        ("thing", "TypeError: a object has no JSON form"),
        ("nan", "ValueError: nan has no JSON form"),
        ("inf", "ValueError: inf has no JSON form"),
        (
            "tuple_key",
            "TypeError: a dict key must be a string or an integer",
        ),
        ("deep", "ValueError: nested more than 128 deep"),
        ("set_fn", "has no JSON form"),
        ("recurse", "RuntimeError: pystack exhausted (line 2)"),
    ];
    for (name, why) in cases {
        let got = failure(run(s, &mut host, owner, name, &Value::Null));
        assert!(got.contains(why), "{name}: {got}");
    }
}

#[test]
fn each_call_starts_afresh() {
    let (mut net, owner) = python();
    let mut host = Host::default();
    let s = net.session(0);
    for _ in 0..2 {
        assert_eq!(
            run(s, &mut host, owner, "counter", &Value::Null),
            Ok(v("1"))
        );
    }
    // Garbage isn't collected during a call, but a call can make plenty of it.
    assert_eq!(run(s, &mut host, owner, "churn", &Value::Null), Ok(v("50")));
}
