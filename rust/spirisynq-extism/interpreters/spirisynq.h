/* What every bundled interpreter's runner shares: Extism's kernel functions, SpiriSynq's host
 * functions, and helpers to move text between Extism memory and the runner's own.
 *
 * A runner is an Extism plug-in exporting `run`. Its input is JSON:
 *
 *     {"source": "<the code>", "entry": "<function name>", "args": [<arguments, in order>]}
 *
 * On its first call it runs `source` once; every call then calls `entry(self, *args)`, where
 * `self` reads and sets the object's fields through the host functions, and outputs the result
 * as JSON. On failure it sets Extism's error to a message and returns 1.
 *
 * A field hook's input also has `"sample": "in"` (an `after_receive`) or `"out"` (a
 * `before_send`): the runner then passes a sample object as the last argument. Coming in it has
 * `payload` (bytes), `encoding`, `attachment` (bytes, or none) and `decode()`; going out it has
 * `send(payload, encoding, attachment)` and `send_value(value)`.
 *
 * A remote method's caller hooks have `"call"`. A `before_call` (`"send"`) gets a call object
 * before its arguments: `send_args(args)` sends them the usual way, and `send(payload,
 * parameters, encoding, attachment)` as they are. An `after_call` (`"replies"`) gets the call's
 * replies as its one argument, to iterate over: each reply is read like an incoming sample, and
 * an error reply is raised as an `RpcException` (`kind`, `message`). The runner hands every
 * result the entry yields to `call_yield`; if an `RpcException` it raised gets out of the
 * entry, it calls `reply_escaped` before failing.
 *
 * Bytes arrive as base64 strings and integer keys as strings, and the runner makes them the
 * language's own. Marks say where they are: `{"bytes": [...], "int_keys": [...]}`, each a list
 * of paths of keys and indexes, an integer key's path being its entry's, and inner keys after
 * outer ones. The input's `"marks"` are from its root (`["args", 0]`); `marks` gives them for the
 * JSON a host function last returned. Going out, the runner writes bytes as base64 strings and
 * integer keys as strings, and marks where with `mark`, just before it hands the JSON over.
 */
#ifndef SPIRISYNQ_H
#define SPIRISYNQ_H

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define SYNQ_EXTISM(name) __attribute__((import_module("extism:host/env"), import_name(#name)))
#define SYNQ_HOST(name) __attribute__((import_module("extism:host/user"), import_name(#name)))
#define SYNQ_EXPORT(name) __attribute__((export_name(#name)))

typedef uint64_t synq_block_t;

SYNQ_EXTISM(input_length) uint64_t extism_input_length(void);
SYNQ_EXTISM(input_load_u8) uint8_t extism_input_load_u8(uint64_t offset);
SYNQ_EXTISM(input_load_u64) uint64_t extism_input_load_u64(uint64_t offset);
SYNQ_EXTISM(alloc) synq_block_t extism_alloc(uint64_t length);
SYNQ_EXTISM(free) void extism_free(synq_block_t block);
SYNQ_EXTISM(length) uint64_t extism_length(synq_block_t block);
SYNQ_EXTISM(load_u8) uint8_t extism_load_u8(uint64_t offset);
SYNQ_EXTISM(load_u64) uint64_t extism_load_u64(uint64_t offset);
SYNQ_EXTISM(store_u8) void extism_store_u8(uint64_t offset, uint8_t value);
SYNQ_EXTISM(store_u64) void extism_store_u64(uint64_t offset, uint64_t value);
SYNQ_EXTISM(output_set) void extism_output_set(synq_block_t block, uint64_t length);
SYNQ_EXTISM(error_set) void extism_error_set(synq_block_t block);

SYNQ_HOST(field_get) synq_block_t synq_host_field_get(synq_block_t name);
SYNQ_HOST(field_set) void synq_host_field_set(synq_block_t name, synq_block_t json);
SYNQ_HOST(arg_get) synq_block_t synq_host_arg_get(synq_block_t name);
SYNQ_HOST(sample_payload) synq_block_t synq_host_sample_payload(void);
SYNQ_HOST(sample_encoding) synq_block_t synq_host_sample_encoding(void);
SYNQ_HOST(sample_attachment) synq_block_t synq_host_sample_attachment(void);
SYNQ_HOST(sample_decode) synq_block_t synq_host_sample_decode(void);
SYNQ_HOST(sample_send)
void synq_host_sample_send(synq_block_t payload, synq_block_t encoding, synq_block_t attachment);
SYNQ_HOST(sample_send_value) void synq_host_sample_send_value(synq_block_t json);
SYNQ_HOST(call_send_args) void synq_host_call_send_args(synq_block_t json);
SYNQ_HOST(call_send)
void synq_host_call_send(synq_block_t payload, synq_block_t parameters, synq_block_t encoding,
                         synq_block_t attachment);
SYNQ_HOST(reply_next) synq_block_t synq_host_reply_next(void);
SYNQ_HOST(reply_payload) synq_block_t synq_host_reply_payload(int64_t index);
SYNQ_HOST(reply_encoding) synq_block_t synq_host_reply_encoding(int64_t index);
SYNQ_HOST(reply_attachment) synq_block_t synq_host_reply_attachment(int64_t index);
SYNQ_HOST(reply_decode) synq_block_t synq_host_reply_decode(int64_t index);
SYNQ_HOST(reply_escaped) void synq_host_reply_escaped(void);
SYNQ_HOST(call_yield) void synq_host_call_yield(synq_block_t json);
SYNQ_HOST(marks) synq_block_t synq_host_marks(void);
SYNQ_HOST(mark) void synq_host_mark(synq_block_t json);

/* A part of a sample, or of a reply, when the runner asks for it. */
typedef enum { SYNQ_PAYLOAD, SYNQ_ENCODING, SYNQ_ATTACHMENT, SYNQ_DECODED } synq_part_t;

/* A new Extism block holding `data`. */
static inline synq_block_t synq_block(const char *data, size_t len) {
    synq_block_t block = extism_alloc(len);
    size_t i = 0;
    for (; i + 8 <= len; i += 8) {
        uint64_t word;
        memcpy(&word, data + i, 8);
        extism_store_u64(block + i, word);
    }
    for (; i < len; i++) extism_store_u8(block + i, (uint8_t)data[i]);
    return block;
}

/* A block's bytes, NUL-terminated, in memory the caller frees. */
static inline char *synq_read(synq_block_t block, size_t *len) {
    size_t n = block ? (size_t)extism_length(block) : 0;
    char *text = malloc(n + 1);
    if (text == NULL) return NULL;
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        uint64_t word = extism_load_u64(block + i);
        memcpy(text + i, &word, 8);
    }
    for (; i < n; i++) text[i] = (char)extism_load_u8(block + i);
    text[n] = 0;
    *len = n;
    return text;
}

/* The plug-in's input, NUL-terminated, in memory the caller frees. */
static inline char *synq_input(size_t *len) {
    size_t n = (size_t)extism_input_length();
    char *text = malloc(n + 1);
    if (text == NULL) return NULL;
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        uint64_t word = extism_input_load_u64(i);
        memcpy(text + i, &word, 8);
    }
    for (; i < n; i++) text[i] = (char)extism_input_load_u8(i);
    text[n] = 0;
    *len = n;
    return text;
}

static inline void synq_output(const char *data, size_t len) {
    synq_block_t block = synq_block(data, len);
    extism_output_set(block, len);
}

/* Fail the call with `message`: returns what `run` returns on failure. */
static inline int synq_fail(const char *message, size_t len) {
    extism_error_set(synq_block(message, len));
    return 1;
}

/* A field's value as JSON text, NUL-terminated, in memory the caller frees. A field that
 * doesn't exist stops the code: the host says why. */
static inline char *synq_field_get(const char *name, size_t name_len, size_t *len) {
    synq_block_t key = synq_block(name, name_len);
    synq_block_t value = synq_host_field_get(key);
    extism_free(key);
    char *text = synq_read(value, len);
    extism_free(value);
    return text;
}

static inline void synq_field_set(const char *name, size_t name_len, const char *json, size_t len) {
    synq_block_t key = synq_block(name, name_len);
    synq_block_t value = synq_block(json, len);
    synq_host_field_set(key, value);
    extism_free(key);
    extism_free(value);
}

/* A part of reply `index`, or of the incoming sample for an index below 0, NUL-terminated, in
 * memory the caller frees. Block 0 is empty bytes; for the attachment, `*none` says there is
 * none. NULL if out of memory. */
static inline char *synq_part_get(int64_t index, synq_part_t part, size_t *len, int *none) {
    synq_block_t block;
    if (index < 0) {
        switch (part) {
            case SYNQ_PAYLOAD: block = synq_host_sample_payload(); break;
            case SYNQ_ENCODING: block = synq_host_sample_encoding(); break;
            case SYNQ_ATTACHMENT: block = synq_host_sample_attachment(); break;
            default: block = synq_host_sample_decode(); break;
        }
    } else {
        switch (part) {
            case SYNQ_PAYLOAD: block = synq_host_reply_payload(index); break;
            case SYNQ_ENCODING: block = synq_host_reply_encoding(index); break;
            case SYNQ_ATTACHMENT: block = synq_host_reply_attachment(index); break;
            default: block = synq_host_reply_decode(index); break;
        }
    }
    if (none) *none = block == 0;
    char *text = synq_read(block, len);
    if (block) extism_free(block);
    return text;
}

/* Send the field's value as these bytes; `attachment` NULL for none. */
static inline void synq_sample_send(const char *payload, size_t payload_len, const char *encoding,
                                    size_t encoding_len, const char *attachment,
                                    size_t attachment_len) {
    synq_block_t p = payload_len ? synq_block(payload, payload_len) : 0;
    synq_block_t e = encoding_len ? synq_block(encoding, encoding_len) : 0;
    synq_block_t a = attachment && attachment_len ? synq_block(attachment, attachment_len) : 0;
    synq_host_sample_send(p, e, a);
    if (p) extism_free(p);
    if (e) extism_free(e);
    if (a) extism_free(a);
}

/* Hand JSON text to a host function: `send_value`, `call_send_args` or `call_yield`. */
static inline void synq_send_json(void (*host)(synq_block_t), const char *json, size_t len) {
    synq_block_t value = synq_block(json, len);
    host(value);
    extism_free(value);
}

/* Send the call as these bytes, with these selector parameters (empty for the caller's). */
static inline void synq_call_send(const char *payload, size_t payload_len, const char *parameters,
                                  size_t parameters_len, const char *encoding, size_t encoding_len,
                                  const char *attachment, size_t attachment_len) {
    synq_block_t p = payload_len ? synq_block(payload, payload_len) : 0;
    synq_block_t q = parameters_len ? synq_block(parameters, parameters_len) : 0;
    synq_block_t e = encoding_len ? synq_block(encoding, encoding_len) : 0;
    synq_block_t a = attachment && attachment_len ? synq_block(attachment, attachment_len) : 0;
    synq_host_call_send(p, q, e, a);
    if (p) extism_free(p);
    if (q) extism_free(q);
    if (e) extism_free(e);
    if (a) extism_free(a);
}

/* Wait for the call's next reply: JSON text, NUL-terminated, in memory the caller frees. */
static inline char *synq_reply_next(size_t *len) {
    synq_block_t block = synq_host_reply_next();
    char *text = synq_read(block, len);
    extism_free(block);
    return text;
}

/* The marks in the JSON a host function last returned: JSON text, NUL-terminated, in memory the
 * caller frees, empty if there are none. NULL if out of memory. */
static inline char *synq_marks(size_t *len) {
    synq_block_t block = synq_host_marks();
    char *text = synq_read(block, len);
    if (block) extism_free(block);
    return text;
}

/* Mark where the bytes and integer keys are in the JSON handed over next. */
static inline void synq_mark(const char *json, size_t len) {
    synq_send_json(synq_host_mark, json, len);
}

static const char SYNQ_BASE64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* `n` bytes as base64, padded, into `out`, which holds at least 4 * ((n + 2) / 3) bytes. */
static inline size_t synq_base64_encode(const unsigned char *in, size_t n, char *out) {
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < n) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = SYNQ_BASE64[v >> 18 & 63];
        out[o++] = SYNQ_BASE64[v >> 12 & 63];
        out[o++] = i + 1 < n ? SYNQ_BASE64[v >> 6 & 63] : '=';
        out[o++] = i + 2 < n ? SYNQ_BASE64[v & 63] : '=';
    }
    return o;
}

/* Padded base64 back into bytes, into `out`, which holds at least 3 * (n / 4) bytes. Returns
 * how many, or -1 if `in` isn't base64. */
static inline long synq_base64_decode(const char *in, size_t n, unsigned char *out) {
    if (n % 4) return -1;
    size_t o = 0;
    for (size_t i = 0; i < n; i += 4) {
        uint32_t v = 0;
        int pad = 0;
        for (int k = 0; k < 4; k++) {
            char c = in[i + k];
            const char *at = c ? strchr(SYNQ_BASE64, c) : NULL;
            if (c == '=' && i + 4 == n && k >= 2) { pad++; v <<= 6; continue; }
            if (at == NULL || pad) return -1;
            v = v << 6 | (uint32_t)(at - SYNQ_BASE64);
        }
        out[o++] = (unsigned char)(v >> 16);
        if (pad < 2) out[o++] = (unsigned char)(v >> 8);
        if (pad < 1) out[o++] = (unsigned char)v;
    }
    return (long)o;
}

#endif
