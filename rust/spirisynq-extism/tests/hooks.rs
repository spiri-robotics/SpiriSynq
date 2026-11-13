//! Field hooks in every interpreter: `on_change_mirror`, `after_receive` with its sample, and
//! `before_send` sending one.

mod common;

use common::*;
use spirisynq_extism::Host;
use spirisynq_yaml::Value;

/// The same hooks in one language.
struct Language {
    loader: &'static str,
    code: [Code; 10],
    /// What sending something that isn't bytes fails with.
    not_bytes: &'static str,
}

const LUA: Language = Language {
    loader: "lua",
    code: [
        (
            "smooth",
            "on_change_mirror:voltage",
            "",
            "function smooth(self, value) self.smoothed = value / 2 end",
        ),
        (
            "watch",
            "on_change_mirror:voltage,cells",
            "",
            "function watch(self, value, field) self.name = field end",
        ),
        (
            "dec",
            "after_receive:blob_a",
            "",
            "function dec(self, sample)
            if sample.encoding ~= 'text/x-base64' or sample.attachment ~= 'meta' then
              error('wrong sample')
            end
            return sample.payload
          end",
        ),
        (
            "dec_usual",
            "after_receive:blob_b",
            "",
            "function dec_usual(self, sample) return sample:decode().x end",
        ),
        (
            "dec_bare",
            "after_receive:blob_c",
            "",
            "function dec_bare(self, sample)
            assert(sample.attachment == nil and sample.payload == '' and sample.encoding == '')
          end",
        ),
        (
            "enc",
            "before_send:blob_a",
            "",
            "function enc(self, value, sample) sample:send(value, 'text/x-base64', 'meta') end",
        ),
        (
            "enc_plain",
            "before_send:blob_b",
            "",
            "function enc_plain(self, value, sample) sample:send(value) end",
        ),
        (
            "enc_usual",
            "before_send:blob_c",
            "",
            "function enc_usual(self, value, sample) sample:send_value(value) end",
        ),
        (
            "enc_twice",
            "before_send:blob_d",
            "",
            "function enc_twice(self, value, sample) sample:send_value(1) sample:send_value(2) end",
        ),
        (
            "enc_bad",
            "before_send:blob_e",
            "",
            "function enc_bad(self, value, sample) sample:send({}) end",
        ),
    ],
    not_bytes: "string expected",
};

const PYTHON: Language = Language {
    loader: "python",
    code: [
        (
            "smooth",
            "on_change_mirror:voltage",
            "",
            "def smooth(self, value):\n    self.smoothed = value / 2\n",
        ),
        (
            "watch",
            "on_change_mirror:voltage,cells",
            "",
            "def watch(self, value, field):\n    self.name = field\n",
        ),
        (
            "dec",
            "after_receive:blob_a",
            "",
            "def dec(self, sample):\n    if sample.encoding != 'text/x-base64' or sample.attachment != b'meta':\n        raise ValueError('wrong sample')\n    return sample.payload\n",
        ),
        (
            "dec_usual",
            "after_receive:blob_b",
            "",
            "def dec_usual(self, sample):\n    return sample.decode()['x']\n",
        ),
        (
            "dec_bare",
            "after_receive:blob_c",
            "",
            "def dec_bare(self, sample):\n    assert sample.attachment is None and sample.payload == b'' and sample.encoding == ''\n",
        ),
        (
            "enc",
            "before_send:blob_a",
            "",
            "def enc(self, value, sample):\n    sample.send(value, encoding='text/x-base64', attachment=bytearray(b'meta'))\n",
        ),
        (
            "enc_plain",
            "before_send:blob_b",
            "",
            "def enc_plain(self, value, sample):\n    sample.send(bytearray(value))\n",
        ),
        (
            "enc_usual",
            "before_send:blob_c",
            "",
            "def enc_usual(self, value, sample):\n    sample.send_value(value)\n",
        ),
        (
            "enc_twice",
            "before_send:blob_d",
            "",
            "def enc_twice(self, value, sample):\n    sample.send_value(1)\n    sample.send_value(2)\n",
        ),
        (
            "enc_bad",
            "before_send:blob_e",
            "",
            "def enc_bad(self, value, sample):\n    sample.send(value, attachment='meta')\n",
        ),
    ],
    not_bytes: "TypeError: the attachment must be bytes",
};

const JAVASCRIPT: Language = Language {
    loader: "javascript",
    code: [
        ("smooth", "on_change_mirror:voltage", "",
         "function smooth(self, value) { self.smoothed = value / 2; }"),
        ("watch", "on_change_mirror:voltage,cells", "",
         "function watch(self, value, field) { self.name = field; }"),
        ("dec", "after_receive:blob_a", "",
         "function dec(self, sample) {
            if (sample.encoding !== 'text/x-base64' || String.fromCharCode(...sample.attachment) !== 'meta')
              throw new Error('wrong sample');
            return sample.payload;
          }"),
        ("dec_usual", "after_receive:blob_b", "",
         "function dec_usual(self, sample) { return sample.decode().x; }"),
        ("dec_bare", "after_receive:blob_c", "",
         "function dec_bare(self, sample) {
            if (sample.attachment !== null || sample.payload.length !== 0 || sample.encoding !== '')
              throw new Error('wrong sample');
          }"),
        ("enc", "before_send:blob_a", "",
         "const bytes = (s) => Uint8Array.from(s, (c) => c.charCodeAt(0));
          function enc(self, value, sample) {
            sample.send(value, {encoding: 'text/x-base64', attachment: bytes('meta').buffer});
          }"),
        ("enc_plain", "before_send:blob_b", "",
         "function enc_plain(self, value, sample) {
            sample.send(new DataView(value.buffer));
          }"),
        ("enc_usual", "before_send:blob_c", "",
         "function enc_usual(self, value, sample) { sample.sendValue(value); }"),
        ("enc_twice", "before_send:blob_d", "",
         "function enc_twice(self, value, sample) { sample.sendValue(1); sample.sendValue(2); }"),
        ("enc_bad", "before_send:blob_e", "",
         "function enc_bad(self, value, sample) { sample.send('text'); }"),
    ],
    not_bytes: "TypeError: the payload must be bytes",
};

const LANGUAGES: [Language; 3] = [LUA, PYTHON, JAVASCRIPT];

#[test]
fn on_change_mirror_sets_the_mirrors_fields() {
    for lang in LANGUAGES {
        let (mut net, _, mirror) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let s = net.session(1);
        s.run_on_change(&mut host, mirror, "voltage", &v("3.0"))
            .unwrap();
        assert_eq!(s.field(mirror, "smoothed").unwrap(), Some(&v("1.5")));
        assert_eq!(s.field(mirror, "name").unwrap(), Some(&text("voltage")));
        s.run_on_change(&mut host, mirror, "cells", &v("4"))
            .unwrap();
        assert_eq!(s.field(mirror, "name").unwrap(), Some(&text("cells")));
    }
}

#[test]
fn after_receive_reads_the_sample() {
    for lang in LANGUAGES {
        let (mut net, owner, _) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let s = net.session(0);
        let loader = lang.loader;
        let mut dec = |field: &str, sample: spirisynq_core::Sample| {
            s.run_after_receive(&mut host, owner, field, sample)
        };
        // The payload, as bytes: Lua's string is its bytes too.
        let got = dec("blob_a", sample(b"AAEC", "text/x-base64", Some(b"meta")));
        assert_eq!(got, Ok(Some(v("!!binary QUFFQw=="))), "{loader}");
        let got = dec(
            "blob_b",
            sample(b"{x: !!binary AAEC}", "application/yaml", None),
        );
        assert_eq!(got, Ok(Some(v("!!binary AAEC"))), "{loader}");
        let got = dec("blob_c", sample(b"", "", None));
        assert_eq!(got, Ok(Some(Value::Null)), "{loader}");
        let got = dec("blob_a", sample(b"AAEC", "text/plain", None));
        assert!(
            failure(got.map(|_| Value::Null)).contains("wrong sample"),
            "{loader}"
        );
    }
}

#[test]
fn before_send_sends_exactly_once() {
    for lang in LANGUAGES {
        let (mut net, owner, _) = pair(lang.loader, &lang.code);
        let mut host = Host::default();
        let loader = lang.loader;
        // Bytes, which every hook gets as the language's bytes, and sends as they are.
        let value = v("!!binary AAEC");
        let mut send = |net: &mut spirisynq_core::memory::MemoryNetwork, field: &str| {
            net.session(0)
                .run_before_send(&mut host, owner, field, &value)
        };
        assert_eq!(send(&mut net, "blob_a"), Ok(true), "{loader}");
        assert_eq!(
            arrived(&mut net, "blob_a"),
            [sample(&[0, 1, 2], "text/x-base64", Some(b"meta"))],
            "{loader}"
        );
        assert_eq!(send(&mut net, "blob_b"), Ok(true), "{loader}");
        assert_eq!(
            arrived(&mut net, "blob_b"),
            [sample(&[0, 1, 2], "zenoh/bytes", None)],
            "{loader}"
        );
        assert_eq!(send(&mut net, "blob_c"), Ok(true), "{loader}");
        let usual = arrived(&mut net, "blob_c");
        let text = String::from_utf8(usual[0].payload.bytes().into_owned()).unwrap();
        assert_eq!(
            (usual[0].encoding.as_str(), v(&text)),
            ("application/yaml", value.clone())
        );
        for (field, why) in [
            ("blob_d", "a before_send sends exactly once"),
            ("blob_e", lang.not_bytes),
        ] {
            let got = failure(send(&mut net, field).map(|_| Value::Null));
            assert!(got.contains(why), "{loader} {field}: {got}");
            assert!(arrived(&mut net, field).is_empty(), "{loader} {field}");
        }
    }
}
