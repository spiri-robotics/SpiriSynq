/* The `javascript` loader: QuickJS-ng as an Extism plug-in. See ../spirisynq.h for the protocol.
 *
 * Values cross as JSON. An integer too large for a number to hold exactly arrives as a BigInt,
 * and a BigInt goes out as the integer it is. Bytes are Uint8Arrays, crossing as base64 strings
 * marked where they are (see ../spirisynq.h). A number that isn't finite, a function or a
 * symbol has no JSON form, and is an error rather than quietly becoming null.
 *
 * QuickJS without its libc has no I/O at all: the code sees the language and its built-in
 * objects only, and `RpcException`, the error an `after_call`'s replies raise for an error
 * reply. An `after_call` is a generator function (`function*`): each value it yields is a
 * result.
 */

#include <string.h>

#include "quickjs.h"
#include "spirisynq.h"

/* Builds `self` and the conversions, and gives back the function each call goes through. */
static const char PRELUDE[] =
    "(function (fieldGet, fieldSet, evaluate, part, send, hand, callSend, replyNext, escaped,\n"
    "           binaryPaths, mark) {\n"
    "  'use strict';\n"
    "  const { parse: jsonParse, stringify: jsonStringify, rawJSON } = JSON;\n"
    "  const parse = (text) => jsonParse(text, (key, value, context) =>\n"
    "    typeof value === 'number' && !Number.isSafeInteger(value) && Number.isInteger(value)\n"
    "      && /^-?[0-9]+$/.test(context.source) ? BigInt(context.source) : value);\n"
    "  const unpack = (value, paths) => {\n"
    "    for (const path of paths) {\n"
    "      if (path.length === 0) { value = Uint8Array.fromBase64(value); continue; }\n"
    "      let node = value;\n"
    "      for (const key of path.slice(0, -1)) node = node[key];\n"
    "      const last = path[path.length - 1];\n"
    "      node[last] = Uint8Array.fromBase64(node[last]);\n"
    "    }\n"
    "    return value;\n"
    "  };\n"
    "  const load = (text) => {\n"
    "    const value = parse(text);\n"
    "    const paths = binaryPaths();\n"
    "    return paths ? unpack(value, jsonParse(paths)) : value;\n"
    "  };\n"
    "  const replace = (key, value) => {\n"
    "    switch (typeof value) {\n"
    "      case 'number':\n"
    "        if (!Number.isFinite(value)) throw new TypeError(`${value} has no JSON form`);\n"
    "        return value;\n"
    "      case 'bigint': return rawJSON(value.toString());\n"
    "      case 'function': case 'symbol':\n"
    "        throw new TypeError(`a ${typeof value} has no JSON form`);\n"
    "      case 'undefined': return null;\n"
    "    }\n"
    "    return value;\n"
    "  };\n"
    "  const stringify = (value) => {\n"
    "    const at = new Map(), marks = [];\n"
    "    const text = jsonStringify(value, function (key, v) {\n"
    "      const base = at.get(this);\n"
    "      const path = base === undefined ? [] : [...base, Array.isArray(this) ? Number(key) : key];\n"
    "      if (v instanceof Uint8Array) { marks.push(path); return v.toBase64(); }\n"
    "      v = replace(key, v);\n"
    "      if (v !== null && typeof v === 'object') at.set(v, path);\n"
    "      return v;\n"
    "    }) ?? 'null';\n"
    "    if (marks.length) mark(jsonStringify(marks));\n"
    "    return text;\n"
    "  };\n"
    "  const self = new Proxy({}, {\n"
    "    get: (_, key) => typeof key === 'string' ? load(fieldGet(key)) : undefined,\n"
    "    set: (_, key, value) => { fieldSet(String(key), stringify(value)); return true; },\n"
    "  });\n"
    "  const bytes = (value, what) => {\n"
    "    if (value instanceof Uint8Array) return value;\n"
    "    if (value instanceof ArrayBuffer) return new Uint8Array(value);\n"
    "    if (ArrayBuffer.isView(value))\n"
    "      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);\n"
    "    throw new TypeError(`${what} must be bytes`);\n"
    "  };\n"
    "  const attached = (value) => value === null ? null : bytes(value, 'the attachment');\n"
    "  const received = (index) => ({\n"
    "    payload: part(index, 0), encoding: part(index, 1), attachment: part(index, 2),\n"
    "    decode: () => load(part(index, 3)),\n"
    "  });\n"
    "  class RpcException extends Error {\n"
    "    constructor(kind, message) {\n"
    "      super(message);\n"
    "      this.name = 'RpcException';\n"
    "      this.kind = kind;\n"
    "    }\n"
    "  }\n"
    "  globalThis.RpcException = RpcException;\n"
    "  const replies = () => {\n"
    "    const it = {\n"
    "      error: null,\n"
    "      [Symbol.iterator]: () => it,\n"
    "      next: () => {\n"
    "        const got = parse(replyNext());\n"
    "        if (got === null) return { done: true, value: undefined };\n"
    "        if (got.error) throw (it.error = new RpcException(got.error.kind, got.error.message));\n"
    "        return { done: false, value: received(got.reply) };\n"
    "      },\n"
    "    };\n"
    "    return it;\n"
    "  };\n"
    "  const objects = {\n"
    "    in: () => received(-1),\n"
    "    out: () => ({\n"
    "      send: (payload, { encoding = 'zenoh/bytes', attachment = null } = {}) =>\n"
    "        send(bytes(payload, 'the payload'), String(encoding), attached(attachment)),\n"
    "      sendValue: (value) => hand(0, stringify(value)),\n"
    "    }),\n"
    "    send: () => ({\n"
    "      sendArgs: (args) => hand(1, stringify(args)),\n"
    "      send: (payload, { parameters = '', encoding = 'zenoh/bytes', attachment = null } = {}) =>\n"
    "        callSend(bytes(payload, 'the payload'), String(parameters), String(encoding),\n"
    "          attached(attachment)),\n"
    "    }),\n"
    "  };\n"
    "  let loaded = false;\n"
    "  return (text) => {\n"
    "    const input = parse(text);\n"
    "    if (input.binary) unpack(input, input.binary);\n"
    "    if (!loaded) { evaluate(input.source); loaded = true; }\n"
    "    const entry = globalThis[input.entry];\n"
    "    if (typeof entry !== 'function') throw new Error(`no function \\`${input.entry}\\``);\n"
    "    const args = input.args;\n"
    "    if (input.sample) args.push(objects[input.sample]());\n"
    "    if (input.call === 'send') args.unshift(objects.send());\n"
    "    if (input.call === 'replies') {\n"
    "      const it = replies();\n"
    "      try {\n"
    "        for (const value of entry(self, it)) hand(2, stringify(value));\n"
    "      } catch (e) {\n"
    "        if (e === it.error) escaped();\n"
    "        throw e;\n"
    "      }\n"
    "      return 'null';\n"
    "    }\n"
    "    return stringify(entry(self, ...args));\n"
    "  };\n"
    "})\n";

static JSValue field_get(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t n, len;
    const char *name = JS_ToCStringLen(ctx, &n, argv[0]);
    if (name == NULL) return JS_EXCEPTION;
    char *json = synq_field_get(name, n, &len);
    JS_FreeCString(ctx, name);
    if (json == NULL) return JS_ThrowOutOfMemory(ctx);
    JSValue text = JS_NewStringLen(ctx, json, len);
    free(json);
    return text;
}

static JSValue field_set(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t n, len;
    const char *name = JS_ToCStringLen(ctx, &n, argv[0]);
    if (name == NULL) return JS_EXCEPTION;
    const char *json = JS_ToCStringLen(ctx, &len, argv[1]);
    if (json == NULL) {
        JS_FreeCString(ctx, name);
        return JS_EXCEPTION;
    }
    synq_field_set(name, n, json, len);
    JS_FreeCString(ctx, name);
    JS_FreeCString(ctx, json);
    return JS_UNDEFINED;
}

/* A part of a reply, or of the incoming sample for index -1: a Uint8Array for the payload and
 * attachment (null for none), a string for the encoding and the decoded JSON. */
static JSValue sample_part(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    int64_t index;
    int32_t part;
    if (JS_ToInt64(ctx, &index, argv[0]) || JS_ToInt32(ctx, &part, argv[1])) return JS_EXCEPTION;
    size_t len;
    int none;
    char *text = synq_part_get(index, (synq_part_t)part, &len, &none);
    if (text == NULL) return JS_ThrowOutOfMemory(ctx);
    JSValue value;
    if (part == SYNQ_ATTACHMENT && none) value = JS_NULL;
    else if (part == SYNQ_PAYLOAD || part == SYNQ_ATTACHMENT)
        value = JS_NewUint8ArrayCopy(ctx, (const uint8_t *)text, len);
    else value = JS_NewStringLen(ctx, text, len);
    free(text);
    return value;
}

static JSValue sample_send(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t pn, en, an = 0;
    uint8_t *p = JS_GetUint8Array(ctx, &pn, argv[0]);
    if (p == NULL) return JS_EXCEPTION;
    uint8_t *a = NULL;
    if (!JS_IsNull(argv[2])) {
        a = JS_GetUint8Array(ctx, &an, argv[2]);
        if (a == NULL) return JS_EXCEPTION;
    }
    const char *e = JS_ToCStringLen(ctx, &en, argv[1]);
    if (e == NULL) return JS_EXCEPTION;
    synq_sample_send((const char *)p, pn, e, en, (const char *)a, an);
    JS_FreeCString(ctx, e);
    return JS_UNDEFINED;
}

/* Hand JSON text to the host: 0 sends a field's value, 1 a call's arguments, 2 yields. */
static void (*const HANDS[])(synq_block_t) = {
    synq_host_sample_send_value,
    synq_host_call_send_args,
    synq_host_call_yield,
};

static JSValue hand(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    int32_t which;
    if (JS_ToInt32(ctx, &which, argv[0])) return JS_EXCEPTION;
    size_t len;
    const char *json = JS_ToCStringLen(ctx, &len, argv[1]);
    if (json == NULL) return JS_EXCEPTION;
    synq_send_json(HANDS[which], json, len);
    JS_FreeCString(ctx, json);
    return JS_UNDEFINED;
}

static JSValue call_send(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t pn, qn, en, an = 0;
    uint8_t *p = JS_GetUint8Array(ctx, &pn, argv[0]);
    if (p == NULL) return JS_EXCEPTION;
    uint8_t *a = NULL;
    if (!JS_IsNull(argv[3])) {
        a = JS_GetUint8Array(ctx, &an, argv[3]);
        if (a == NULL) return JS_EXCEPTION;
    }
    const char *q = JS_ToCStringLen(ctx, &qn, argv[1]);
    if (q == NULL) return JS_EXCEPTION;
    const char *e = JS_ToCStringLen(ctx, &en, argv[2]);
    if (e == NULL) {
        JS_FreeCString(ctx, q);
        return JS_EXCEPTION;
    }
    synq_call_send((const char *)p, pn, q, qn, e, en, (const char *)a, an);
    JS_FreeCString(ctx, q);
    JS_FreeCString(ctx, e);
    return JS_UNDEFINED;
}

static JSValue reply_next(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t len;
    char *json = synq_reply_next(&len);
    if (json == NULL) return JS_ThrowOutOfMemory(ctx);
    JSValue text = JS_NewStringLen(ctx, json, len);
    free(json);
    return text;
}

static JSValue reply_escaped(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    synq_host_reply_escaped();
    return JS_UNDEFINED;
}

/* Where the bytes are in the JSON a host function last returned: '' if nowhere. */
static JSValue binary_paths(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t len;
    char *json = synq_binary_paths(&len);
    if (json == NULL) return JS_ThrowOutOfMemory(ctx);
    JSValue text = JS_NewStringLen(ctx, json, len);
    free(json);
    return text;
}

static JSValue binary_mark(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t len;
    const char *json = JS_ToCStringLen(ctx, &len, argv[0]);
    if (json == NULL) return JS_EXCEPTION;
    synq_binary_mark(json, len);
    JS_FreeCString(ctx, json);
    return JS_UNDEFINED;
}

/* Run the code's source as a script, in the global scope. */
static JSValue evaluate(JSContext *ctx, JSValueConst this_val, int argc, JSValueConst *argv) {
    size_t n;
    const char *source = JS_ToCStringLen(ctx, &n, argv[0]);
    if (source == NULL) return JS_EXCEPTION;
    JSValue result = JS_Eval(ctx, source, n, "code", JS_EVAL_TYPE_GLOBAL);
    JS_FreeCString(ctx, source);
    return result;
}

static JSRuntime *runtime;
static JSContext *context;
static JSValue call;

static int start(void) {
    runtime = JS_NewRuntime();
    if (runtime == NULL) return 0;
    JS_SetMaxStackSize(runtime, 512 * 1024);
    context = JS_NewContext(runtime);
    if (context == NULL) return 0;
    JSValue make = JS_Eval(context, PRELUDE, strlen(PRELUDE), "prelude", JS_EVAL_TYPE_GLOBAL);
    JSValue args[11] = {
        JS_NewCFunction(context, field_get, "fieldGet", 1),
        JS_NewCFunction(context, field_set, "fieldSet", 2),
        JS_NewCFunction(context, evaluate, "evaluate", 1),
        JS_NewCFunction(context, sample_part, "part", 2),
        JS_NewCFunction(context, sample_send, "send", 3),
        JS_NewCFunction(context, hand, "hand", 2),
        JS_NewCFunction(context, call_send, "callSend", 4),
        JS_NewCFunction(context, reply_next, "replyNext", 0),
        JS_NewCFunction(context, reply_escaped, "escaped", 0),
        JS_NewCFunction(context, binary_paths, "binaryPaths", 0),
        JS_NewCFunction(context, binary_mark, "mark", 1),
    };
    call = JS_Call(context, make, JS_UNDEFINED, 11, args);
    for (int i = 0; i < 11; i++) JS_FreeValue(context, args[i]);
    JS_FreeValue(context, make);
    return !JS_IsException(call);
}

/* Fail with the pending exception: its type and message, and where in the code it was. */
static int fail_with_exception(void) {
    JSValue error = JS_GetException(context);
    const char *message = JS_ToCString(context, error);
    JSValue stack = JS_GetPropertyStr(context, error, "stack");
    const char *where = JS_IsString(stack) ? JS_ToCString(context, stack) : NULL;
    const char *at = where ? strstr(where, "code:") : NULL;
    size_t at_len = at ? strcspn(at, ")\n") : 0;
    const char *text = message ? message : "the code raised something that can't be shown";
    size_t len = strlen(text);
    char *full = malloc(len + at_len + 4);
    int status;
    if (full == NULL) {
        status = synq_fail(text, len);
    } else {
        memcpy(full, text, len);
        size_t n = len;
        if (at_len) {
            memcpy(full + n, " (", 2);
            memcpy(full + n + 2, at, at_len);
            n += at_len + 2;
            full[n++] = ')';
        }
        status = synq_fail(full, n);
        free(full);
    }
    if (where) JS_FreeCString(context, where);
    if (message) JS_FreeCString(context, message);
    JS_FreeValue(context, stack);
    JS_FreeValue(context, error);
    return status;
}

SYNQ_EXPORT(run) int run(void) {
    if (runtime == NULL && !start()) {
        if (context != NULL) return fail_with_exception();
        return synq_fail("out of memory", 13);
    }
    size_t len;
    char *input = synq_input(&len);
    if (input == NULL) return synq_fail("out of memory", 13);
    JSValue text = JS_NewStringLen(context, input, len);
    free(input);
    JSValue result = JS_Call(context, call, JS_UNDEFINED, 1, &text);
    JS_FreeValue(context, text);
    if (JS_IsException(result)) return fail_with_exception();
    size_t n;
    const char *json = JS_ToCStringLen(context, &n, result);
    JS_FreeValue(context, result);
    if (json == NULL) return fail_with_exception();
    synq_output(json, n);
    JS_FreeCString(context, json);
    return 0;
}
