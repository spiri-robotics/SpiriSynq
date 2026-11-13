/* The `lua` loader: Lua 5.4 as an Extism plug-in. See ../spirisynq.h for the protocol.
 *
 * Values cross as JSON: null is nil, numbers are integers or floats as written, arrays are
 * tables indexed from 1, objects are tables. A table goes out as an array when its keys are
 * exactly 1..n (an empty table is an empty array), else as an object, whose keys must be
 * strings or integers. Strings are bytes: bytes arrive as strings, and a string that isn't
 * UTF-8 goes out as base64, marked as bytes (see ../spirisynq.h). One that is UTF-8 goes out as
 * text, and the engine takes its bytes where the schema says bytes.
 *
 * Only Lua's base, coroutine, table, string, math and utf8 libraries are open: no files, OS or
 * packages, and `load` takes text only, since Lua's bytecode loader trusts its input.
 *
 * An `after_call` is run as a coroutine: each value it yields with `coroutine.yield` is a
 * result. Its replies are an iterator (`for reply in replies do`); an error reply is raised as
 * a table with `kind` and `message`.
 */

#include <errno.h>
#include <math.h>
#include <stdio.h>

#include "lauxlib.h"
#include "lua.h"
#include "lualib.h"
#include "spirisynq.h"

#define MAX_DEPTH 128

/* ---- JSON text to Lua values ---- */

typedef struct {
    lua_State *L;
    const char *p, *end;
} Reader;

static void bad(Reader *r, const char *why) { luaL_error(r->L, "bad JSON: %s", why); }

static void skip(Reader *r) {
    while (r->p < r->end && (*r->p == ' ' || *r->p == '\t' || *r->p == '\n' || *r->p == '\r'))
        r->p++;
}

static unsigned hex4(Reader *r) {
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        if (r->p >= r->end) bad(r, "short \\u escape");
        char c = *r->p++;
        v <<= 4;
        if (c >= '0' && c <= '9') v |= c - '0';
        else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
        else bad(r, "bad \\u escape");
    }
    return v;
}

static void add_utf8(luaL_Buffer *b, unsigned cp) {
    char out[4];
    int n;
    if (cp < 0x80) { out[0] = cp; n = 1; }
    else if (cp < 0x800) { out[0] = 0xC0 | (cp >> 6); out[1] = 0x80 | (cp & 0x3F); n = 2; }
    else if (cp < 0x10000) {
        out[0] = 0xE0 | (cp >> 12); out[1] = 0x80 | ((cp >> 6) & 0x3F);
        out[2] = 0x80 | (cp & 0x3F); n = 3;
    } else {
        out[0] = 0xF0 | (cp >> 18); out[1] = 0x80 | ((cp >> 12) & 0x3F);
        out[2] = 0x80 | ((cp >> 6) & 0x3F); out[3] = 0x80 | (cp & 0x3F); n = 4;
    }
    luaL_addlstring(b, out, n);
}

static void read_string(Reader *r) {
    luaL_Buffer b;
    luaL_buffinit(r->L, &b);
    r->p++; /* the opening quote */
    for (;;) {
        if (r->p >= r->end) bad(r, "unterminated string");
        unsigned char c = *r->p++;
        if (c == '"') break;
        if (c != '\\') { luaL_addchar(&b, c); continue; }
        if (r->p >= r->end) bad(r, "unterminated string");
        c = *r->p++;
        switch (c) {
        case '"': case '\\': case '/': luaL_addchar(&b, c); break;
        case 'b': luaL_addchar(&b, '\b'); break;
        case 'f': luaL_addchar(&b, '\f'); break;
        case 'n': luaL_addchar(&b, '\n'); break;
        case 'r': luaL_addchar(&b, '\r'); break;
        case 't': luaL_addchar(&b, '\t'); break;
        case 'u': {
            unsigned cp = hex4(r);
            if (cp >= 0xD800 && cp < 0xDC00) {
                if (r->end - r->p < 2 || r->p[0] != '\\' || r->p[1] != 'u') bad(r, "lone surrogate");
                r->p += 2;
                unsigned lo = hex4(r);
                if (lo < 0xDC00 || lo >= 0xE000) bad(r, "lone surrogate");
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            }
            add_utf8(&b, cp);
            break;
        }
        default: bad(r, "bad escape");
        }
    }
    luaL_pushresult(&b);
}

static void read_number(Reader *r) {
    const char *start = r->p;
    int is_float = 0;
    while (r->p < r->end && strchr("+-0123456789.eE", *r->p)) {
        if (*r->p == '.' || *r->p == 'e' || *r->p == 'E') is_float = 1;
        r->p++;
    }
    char text[64];
    size_t n = r->p - start;
    if (n == 0 || n >= sizeof text) bad(r, "bad number");
    memcpy(text, start, n);
    text[n] = 0;
    char *end;
    if (!is_float) {
        errno = 0;
        long long i = strtoll(text, &end, 10);
        if (*end == 0 && errno == 0) { lua_pushinteger(r->L, i); return; }
    }
    double d = strtod(text, &end);
    if (*end != 0) bad(r, "bad number");
    lua_pushnumber(r->L, d);
}

static void read_value(Reader *r, int depth);

static void expect(Reader *r, const char *word) {
    size_t n = strlen(word);
    if ((size_t)(r->end - r->p) < n || memcmp(r->p, word, n) != 0) bad(r, "unexpected text");
    r->p += n;
}

static void read_value(Reader *r, int depth) {
    if (depth > MAX_DEPTH) bad(r, "nested too deep");
    luaL_checkstack(r->L, 4, "nested too deep");
    skip(r);
    if (r->p >= r->end) bad(r, "unexpected end");
    switch (*r->p) {
    case 'n': expect(r, "null"); lua_pushnil(r->L); return;
    case 't': expect(r, "true"); lua_pushboolean(r->L, 1); return;
    case 'f': expect(r, "false"); lua_pushboolean(r->L, 0); return;
    case '"': read_string(r); return;
    case '[': {
        r->p++;
        lua_newtable(r->L);
        skip(r);
        if (r->p < r->end && *r->p == ']') { r->p++; return; }
        for (lua_Integer i = 1;; i++) {
            read_value(r, depth + 1);
            lua_rawseti(r->L, -2, i);
            skip(r);
            if (r->p < r->end && *r->p == ',') { r->p++; continue; }
            if (r->p < r->end && *r->p == ']') { r->p++; return; }
            bad(r, "expected , or ]");
        }
    }
    case '{': {
        r->p++;
        lua_newtable(r->L);
        skip(r);
        if (r->p < r->end && *r->p == '}') { r->p++; return; }
        for (;;) {
            skip(r);
            if (r->p >= r->end || *r->p != '"') bad(r, "expected a key");
            read_string(r);
            skip(r);
            if (r->p >= r->end || *r->p != ':') bad(r, "expected :");
            r->p++;
            read_value(r, depth + 1);
            lua_rawset(r->L, -3);
            skip(r);
            if (r->p < r->end && *r->p == ',') { r->p++; continue; }
            if (r->p < r->end && *r->p == '}') { r->p++; return; }
            bad(r, "expected , or }");
        }
    }
    default: read_number(r);
    }
}

/* Push the value `text` holds. */
static void decode(lua_State *L, const char *text, size_t len) {
    Reader r = {L, text, text + len};
    read_value(&r, 0);
    skip(&r);
    if (r.p != r.end) bad(&r, "text after the value");
}

/* Replace the base64 string at the top of the stack with the bytes it holds. */
static void unbase64(lua_State *L) {
    size_t n;
    const char *text = lua_tolstring(L, -1, &n);
    if (text == NULL) luaL_error(L, "bytes that aren't a base64 string");
    unsigned char *raw = malloc(n / 4 * 3 + 1);
    if (raw == NULL) luaL_error(L, "out of memory");
    long len = synq_base64_decode(text, n, raw);
    if (len < 0) {
        free(raw);
        luaL_error(L, "bytes that aren't base64");
    }
    lua_pushlstring(L, (const char *)raw, (size_t)len);
    free(raw);
    lua_replace(L, -2);
}

/* Push key `k` of the path at `path`, as a table key: a JSON index counts from 0, Lua's from 1. */
static void push_key(lua_State *L, int path, lua_Integer k) {
    if (lua_rawgeti(L, path, k) == LUA_TNUMBER) {
        lua_Integer i = lua_tointeger(L, -1);
        lua_pop(L, 1);
        lua_pushinteger(L, i + 1);
    }
}

/* Make the base64 strings in the value at `idx` bytes, where the paths at the top of the stack
 * say (popped). A path that's empty is the value itself. */
static void unpack(lua_State *L, int idx) {
    idx = lua_absindex(L, idx);
    int paths = lua_gettop(L);
    lua_Integer n = (lua_Integer)lua_rawlen(L, paths);
    luaL_checkstack(L, 6, "too many paths");
    for (lua_Integer i = 1; i <= n; i++) {
        lua_rawgeti(L, paths, i);
        int path = lua_gettop(L);
        lua_Integer len = (lua_Integer)lua_rawlen(L, path);
        lua_pushvalue(L, idx);
        if (len == 0) {
            unbase64(L);
            lua_replace(L, idx);
            lua_pop(L, 1);
            continue;
        }
        for (lua_Integer k = 1; k < len; k++) {
            push_key(L, path, k);
            lua_gettable(L, -2);
            lua_remove(L, -2);
        }
        push_key(L, path, len);
        lua_pushvalue(L, -1);
        lua_gettable(L, -3);
        unbase64(L);
        lua_settable(L, -3);
        lua_pop(L, 2);
    }
    lua_pop(L, 1);
}

/* Make the bytes in the value at the top of the stack strings, where the host function that
 * returned it says they are. */
static void unpack_given(lua_State *L) {
    size_t len;
    char *json = synq_binary_paths(&len);
    if (json == NULL) luaL_error(L, "out of memory");
    if (len == 0) {
        free(json);
        return;
    }
    lua_pushlstring(L, json, len); /* so the text is freed even if decoding fails */
    free(json);
    const char *text = lua_tolstring(L, -1, &len);
    decode(L, text, len);
    lua_remove(L, -2);
    unpack(L, -2);
}

/* ---- Lua values to JSON text ---- */

/* Buffers reused by every call; a failed encoding leaves nothing to free. `out` is the JSON,
 * `path` where in it the encoder is (keys and indexes, comma-separated), and `marks` the paths
 * of the bytes in it. */
typedef struct { char *data; size_t len, cap; } Buffer;
static Buffer out, path, marks;

/* Make room for `n` more bytes. */
static void reserve(lua_State *L, Buffer *b, size_t n) {
    if (b->len + n > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        while (cap < b->len + n) cap *= 2;
        char *data = realloc(b->data, cap);
        if (data == NULL) luaL_error(L, "out of memory");
        b->data = data;
        b->cap = cap;
    }
}

static void add(lua_State *L, Buffer *b, const char *s, size_t n) {
    reserve(L, b, n);
    memcpy(b->data + b->len, s, n);
    b->len += n;
}

static void put(lua_State *L, const char *s, size_t n) { add(L, &out, s, n); }

/* How many bytes the UTF-8 character at `s` takes; 0 if there isn't one. */
static size_t utf8_width(const unsigned char *s, size_t n) {
    unsigned char c = s[0];
    size_t width = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
    if (width == 0 || width > n) return 0;
    for (size_t k = 1; k < width; k++)
        if ((s[k] & 0xC0) != 0x80) return 0;
    return width;
}

static int is_utf8(const unsigned char *s, size_t n) {
    for (size_t i = 0, w; i < n; i += w)
        if ((w = utf8_width(s + i, n - i)) == 0) return 0;
    return 1;
}

static void put_string(lua_State *L, Buffer *b, const unsigned char *s, size_t n) {
    if (!is_utf8(s, n)) luaL_error(L, "a table key that isn't UTF-8 has no JSON form");
    add(L, b, "\"", 1);
    for (size_t i = 0, width; i < n; i += width) {
        unsigned char c = s[i];
        width = utf8_width(s + i, n - i);
        if (c == '"' || c == '\\') { add(L, b, "\\", 1); add(L, b, (const char *)&s[i], 1); }
        else if (c < 0x20) { char esc[7]; snprintf(esc, sizeof esc, "\\u%04x", c); add(L, b, esc, 6); }
        else add(L, b, (const char *)&s[i], width);
    }
    add(L, b, "\"", 1);
}

/* Bytes that aren't text: base64, marked where they are. */
static void put_bytes(lua_State *L, const unsigned char *s, size_t n) {
    size_t len = 4 * ((n + 2) / 3);
    reserve(L, &out, len + 2);
    out.data[out.len++] = '"';
    out.len += synq_base64_encode(s, n, out.data + out.len);
    out.data[out.len++] = '"';
    add(L, &marks, marks.len ? ",[" : "[[", 2);
    add(L, &marks, path.data, path.len);
    add(L, &marks, "]", 1);
}

/* Go into an index or a key, at the end of `path`; returns where to come back to. */
static size_t enter(lua_State *L) {
    size_t back = path.len;
    if (path.len) add(L, &path, ",", 1);
    return back;
}

static void put_float(lua_State *L, double d) {
    if (!isfinite(d)) luaL_error(L, "%f has no JSON form", d);
    char text[40];
    for (int digits = 15; digits <= 17; digits++) {
        snprintf(text, sizeof text, "%.*g", digits, d);
        if (strtod(text, NULL) == d) break;
    }
    put(L, text, strlen(text));
    if (strpbrk(text, ".e") == NULL) put(L, ".0", 2); /* keep it a float */
}

/* Whether the table at `idx` has exactly the keys 1..n, for some n >= 0. */
static int is_array(lua_State *L, int idx) {
    lua_Integer n = (lua_Integer)lua_rawlen(L, idx), count = 0;
    lua_pushnil(L);
    while (lua_next(L, idx)) { lua_pop(L, 1); count++; }
    if (count != n) return 0;
    for (lua_Integer i = 1; i <= n; i++) {
        int missing = lua_rawgeti(L, idx, i) == LUA_TNIL;
        lua_pop(L, 1);
        if (missing) return 0;
    }
    return 1;
}

static void encode(lua_State *L, int idx, int depth) {
    idx = lua_absindex(L, idx);
    switch (lua_type(L, idx)) {
    case LUA_TNIL: put(L, "null", 4); return;
    case LUA_TBOOLEAN: lua_toboolean(L, idx) ? put(L, "true", 4) : put(L, "false", 5); return;
    case LUA_TNUMBER:
        if (lua_isinteger(L, idx)) {
            char text[24];
            int n = snprintf(text, sizeof text, "%lld", (long long)lua_tointeger(L, idx));
            put(L, text, n);
        } else put_float(L, lua_tonumber(L, idx));
        return;
    case LUA_TSTRING: {
        size_t n;
        const unsigned char *s = (const unsigned char *)lua_tolstring(L, idx, &n);
        if (is_utf8(s, n)) put_string(L, &out, s, n);
        else put_bytes(L, s, n);
        return;
    }
    case LUA_TTABLE:
        if (depth > MAX_DEPTH) luaL_error(L, "nested more than %d deep", MAX_DEPTH);
        luaL_checkstack(L, 4, "nested too deep");
        if (is_array(L, idx)) {
            lua_Integer n = (lua_Integer)lua_rawlen(L, idx);
            put(L, "[", 1);
            for (lua_Integer i = 1; i <= n; i++) {
                if (i > 1) put(L, ",", 1);
                size_t back = enter(L);
                char text[24];
                add(L, &path, text, snprintf(text, sizeof text, "%lld", (long long)(i - 1)));
                lua_rawgeti(L, idx, i);
                encode(L, -1, depth + 1);
                lua_pop(L, 1);
                path.len = back;
            }
            put(L, "]", 1);
        } else {
            put(L, "{", 1);
            int first = 1;
            lua_pushnil(L);
            while (lua_next(L, idx)) {
                if (!first) put(L, ",", 1);
                first = 0;
                size_t back = enter(L);
                if (lua_type(L, -2) == LUA_TSTRING) {
                    size_t n;
                    const unsigned char *key = (const unsigned char *)lua_tolstring(L, -2, &n);
                    put_string(L, &out, key, n);
                    put_string(L, &path, key, n);
                } else if (lua_isinteger(L, -2)) {
                    char text[26];
                    int n = snprintf(text, sizeof text, "\"%lld\"", (long long)lua_tointeger(L, -2));
                    put(L, text, n);
                    add(L, &path, text, n);
                } else luaL_error(L, "a table key must be a string or an integer");
                put(L, ":", 1);
                encode(L, -1, depth + 1);
                lua_pop(L, 1);
                path.len = back;
            }
            put(L, "}", 1);
        }
        return;
    default: luaL_error(L, "a %s has no JSON form", luaL_typename(L, idx));
    }
}

/* Encode the value at `idx` into `out`, marking where it has bytes. */
static void encode_value(lua_State *L, int idx) {
    out.len = path.len = marks.len = 0;
    encode(L, idx, 0);
}

/* Mark where the bytes are in `out`, if anywhere: just before handing it over. */
static void send_marks(lua_State *L) {
    if (marks.len == 0) return;
    add(L, &marks, "]", 1);
    synq_binary_mark(marks.data, marks.len);
}

/* ---- self ---- */

static int self_index(lua_State *L) {
    size_t n, len;
    const char *name = luaL_checklstring(L, 2, &n);
    char *json = synq_field_get(name, n, &len);
    if (json == NULL) return luaL_error(L, "out of memory");
    lua_pushlstring(L, json, len); /* so the text is freed even if decoding fails */
    free(json);
    const char *text = lua_tolstring(L, -1, &len);
    decode(L, text, len);
    unpack_given(L);
    return 1;
}

static int self_newindex(lua_State *L) {
    size_t n;
    const char *name = luaL_checklstring(L, 2, &n);
    encode_value(L, 3);
    send_marks(L);
    synq_field_set(name, n, out.data, out.len);
    return 0;
}

/* ---- samples and replies ---- */

/* Push a part of reply `index`, or of the incoming sample for -1. */
static int push_part(lua_State *L, lua_Integer index, synq_part_t part) {
    size_t len;
    int none;
    char *text = synq_part_get(index, part, &len, &none);
    if (text == NULL) return luaL_error(L, "out of memory");
    if (part == SYNQ_ATTACHMENT && none) lua_pushnil(L);
    else lua_pushlstring(L, text, len);
    free(text);
    return 1;
}

/* received:decode(), for the reply or sample in its upvalue. */
static int received_decode(lua_State *L) {
    push_part(L, lua_tointeger(L, lua_upvalueindex(1)), SYNQ_DECODED);
    size_t len;
    const char *text = lua_tolstring(L, -1, &len);
    decode(L, text, len);
    unpack_given(L);
    return 1;
}

/* Push a reply, or the incoming sample for -1: payload, encoding, attachment and decode. */
static void push_received(lua_State *L, lua_Integer index) {
    lua_createtable(L, 0, 4);
    push_part(L, index, SYNQ_PAYLOAD);
    lua_setfield(L, -2, "payload");
    push_part(L, index, SYNQ_ENCODING);
    lua_setfield(L, -2, "encoding");
    push_part(L, index, SYNQ_ATTACHMENT);
    lua_setfield(L, -2, "attachment");
    lua_pushinteger(L, index);
    lua_pushcclosure(L, received_decode, 1);
    lua_setfield(L, -2, "decode");
}

/* An optional attachment, at `arg`: NULL for nil. */
static const char *attachment(lua_State *L, int arg, size_t *len) {
    *len = 0;
    return lua_isnoneornil(L, arg) ? NULL : luaL_checklstring(L, arg, len);
}

/* sample:send(payload, encoding, attachment) */
static int sample_send(lua_State *L) {
    size_t pn, en, an;
    const char *p = luaL_checklstring(L, 2, &pn);
    const char *e = luaL_optlstring(L, 3, "zenoh/bytes", &en);
    const char *a = attachment(L, 4, &an);
    synq_sample_send(p, pn, e, en, a, an);
    return 0;
}

/* Encode the value at 2 and hand it to the host function in the upvalue: sample:send_value,
 * call:send_args. */
static int hand(lua_State *L) {
    lua_settop(L, 2);
    encode_value(L, 2);
    send_marks(L);
    void (*host)(synq_block_t) = (void (*)(synq_block_t))lua_touserdata(L, lua_upvalueindex(1));
    synq_send_json(host, out.data, out.len);
    return 0;
}

static void push_hand(lua_State *L, void (*host)(synq_block_t)) {
    lua_pushlightuserdata(L, (void *)host);
    lua_pushcclosure(L, hand, 1);
}

/* call:send(payload, parameters, encoding, attachment) */
static int call_send(lua_State *L) {
    size_t pn, qn, en, an;
    const char *p = luaL_checklstring(L, 2, &pn);
    const char *q = luaL_optlstring(L, 3, "", &qn);
    const char *e = luaL_optlstring(L, 4, "zenoh/bytes", &en);
    const char *a = attachment(L, 5, &an);
    synq_call_send(p, pn, q, qn, e, en, a, an);
    return 0;
}

/* The replies iterator: the next reply, or nil at the end. An error reply is raised as a
 * table, kept in the registry to tell it apart if it gets out of the code. */
static int replies_next(lua_State *L) {
    size_t len;
    char *json = synq_reply_next(&len);
    if (json == NULL) return luaL_error(L, "out of memory");
    lua_pushlstring(L, json, len);
    free(json);
    const char *text = lua_tolstring(L, -1, &len);
    decode(L, text, len);
    if (lua_isnil(L, -1)) return 1;
    if (lua_getfield(L, -1, "reply") == LUA_TNUMBER) {
        push_received(L, lua_tointeger(L, -1));
        return 1;
    }
    lua_getfield(L, -2, "error");
    luaL_setmetatable(L, "spirisynq.error");
    lua_pushvalue(L, -1);
    lua_setfield(L, LUA_REGISTRYINDEX, "spirisynq.raised");
    return lua_error(L);
}

static int error_tostring(lua_State *L) {
    lua_getfield(L, 1, "kind");
    const char *kind = luaL_tolstring(L, -1, NULL);
    lua_getfield(L, 1, "message");
    const char *message = luaL_tolstring(L, -1, NULL);
    lua_pushfstring(L, "%s: %s", kind, message);
    return 1;
}

/* Push the object a hook gets: `kind` is "in" or "out" for a sample, "send" for a call. */
static void push_sample(lua_State *L, const char *kind) {
    if (strcmp(kind, "in") == 0) {
        push_received(L, -1);
        return;
    }
    lua_createtable(L, 0, 2);
    if (strcmp(kind, "out") == 0) {
        lua_pushcfunction(L, sample_send);
        lua_setfield(L, -2, "send");
        push_hand(L, synq_host_sample_send_value);
        lua_setfield(L, -2, "send_value");
    } else {
        lua_pushcfunction(L, call_send);
        lua_setfield(L, -2, "send");
        push_hand(L, synq_host_call_send_args);
        lua_setfield(L, -2, "send_args");
    }
}

/* ---- running ---- */

static lua_State *state;
static int loaded;

static const char SANDBOX[] =
    "local load = load\n"
    "_G.load = function(chunk, name, _, env) return load(chunk, name, 't', env) end\n"
    "dofile, loadfile = nil, nil\n";

static lua_State *open_state(void) {
    lua_State *L = luaL_newstate();
    if (L == NULL) return NULL;
    static const luaL_Reg libs[] = {
        {LUA_GNAME, luaopen_base},       {LUA_COLIBNAME, luaopen_coroutine},
        {LUA_TABLIBNAME, luaopen_table}, {LUA_STRLIBNAME, luaopen_string},
        {LUA_MATHLIBNAME, luaopen_math}, {LUA_UTF8LIBNAME, luaopen_utf8},
        {NULL, NULL},
    };
    for (const luaL_Reg *lib = libs; lib->func; lib++) {
        luaL_requiref(L, lib->name, lib->func, 1);
        lua_pop(L, 1);
    }
    luaL_dostring(L, SANDBOX);
    /* The proxy every call gets as `self`, kept in the registry. */
    lua_newtable(L);
    lua_newtable(L);
    lua_pushcfunction(L, self_index);
    lua_setfield(L, -2, "__index");
    lua_pushcfunction(L, self_newindex);
    lua_setfield(L, -2, "__newindex");
    lua_setmetatable(L, -2);
    lua_setfield(L, LUA_REGISTRYINDEX, "spirisynq.self");
    luaL_newmetatable(L, "spirisynq.error");
    lua_pushcfunction(L, error_tostring);
    lua_setfield(L, -2, "__tostring");
    lua_pop(L, 1);
    return L;
}

/* Run the after_call at the top of the stack as a coroutine, with `self` and the replies,
 * handing each value it yields to the host. */
static void run_after_call(lua_State *L) {
    lua_pushnil(L);
    lua_setfield(L, LUA_REGISTRYINDEX, "spirisynq.raised");
    lua_State *co = lua_newthread(L);
    lua_rotate(L, -2, 1); /* the thread under the entry, to keep it alive */
    lua_xmove(L, co, 1);
    lua_getfield(L, LUA_REGISTRYINDEX, "spirisynq.self");
    lua_pushcfunction(L, replies_next);
    lua_xmove(L, co, 2);
    int nargs = 2, nres;
    for (;;) {
        int status = lua_resume(co, L, nargs, &nres);
        nargs = 0;
        if (status == LUA_YIELD) {
            luaL_checkstack(L, nres + 4, "too many results");
            lua_xmove(co, L, nres);
            if (nres > 0) encode_value(L, -nres);
            else out.len = marks.len = 0, put(L, "null", 4);
            send_marks(L);
            lua_pop(L, nres);
            synq_send_json(synq_host_call_yield, out.data, out.len);
            continue;
        }
        if (status == LUA_OK) break;
        lua_xmove(co, L, 1);
        lua_getfield(L, LUA_REGISTRYINDEX, "spirisynq.raised");
        if (lua_rawequal(L, -1, -2)) synq_host_reply_escaped();
        lua_pop(L, 1);
        lua_error(L);
    }
    out.len = 0;
    put(L, "null", 4);
}

/* Run the input (a string at index 1) and leave the result's JSON in `out`. */
static int call(lua_State *L) {
    size_t len;
    const char *input = lua_tolstring(L, 1, &len);
    decode(L, input, len);
    int request = lua_gettop(L);
    if (lua_getfield(L, request, "binary") == LUA_TTABLE) unpack(L, request);
    else lua_pop(L, 1);
    if (!loaded) {
        lua_getfield(L, request, "source");
        size_t n;
        const char *source = luaL_checklstring(L, -1, &n);
        if (luaL_loadbufferx(L, source, n, "=code", "t") != LUA_OK) return lua_error(L);
        lua_call(L, 0, 0);
        lua_pop(L, 1);
        loaded = 1;
    }
    lua_getfield(L, request, "entry");
    const char *entry = luaL_checkstring(L, -1);
    lua_getfield(L, request, "sample");
    const char *sample = lua_tostring(L, -1);
    lua_getfield(L, request, "call");
    const char *call = lua_tostring(L, -1);
    lua_getfield(L, request, "args");
    int args = lua_gettop(L);
    lua_Integer n = luaL_len(L, args);
    luaL_checkstack(L, (int)n + 8, "too many arguments");
    if (lua_getglobal(L, entry) != LUA_TFUNCTION) return luaL_error(L, "no function `%s`", entry);
    if (call && strcmp(call, "replies") == 0) {
        run_after_call(L);
        return 0;
    }
    lua_getfield(L, LUA_REGISTRYINDEX, "spirisynq.self");
    if (call) push_sample(L, call);
    for (lua_Integer i = 1; i <= n; i++) lua_rawgeti(L, args, i);
    if (sample) push_sample(L, sample);
    lua_call(L, 1 + (int)n + (sample != NULL) + (call != NULL), 1);
    encode_value(L, -1);
    send_marks(L);
    return 0;
}

SYNQ_EXPORT(run) int run(void) {
    if (state == NULL) state = open_state();
    if (state == NULL) return synq_fail("out of memory", 13);
    lua_State *L = state;
    size_t len;
    char *input = synq_input(&len);
    if (input == NULL) return synq_fail("out of memory", 13);
    lua_pushcfunction(L, call);
    lua_pushlstring(L, input, len);
    free(input);
    if (lua_pcall(L, 1, 0, 0) != LUA_OK) {
        size_t n;
        const char *message = lua_tolstring(L, -1, &n);
        if (message == NULL) message = "the code raised a value that isn't a string", n = strlen(message);
        int status = synq_fail(message, n);
        lua_settop(L, 0);
        return status;
    }
    lua_settop(L, 0);
    synq_output(out.data, out.len);
    return 0;
}
