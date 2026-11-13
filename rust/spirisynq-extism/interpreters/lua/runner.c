/* The `lua` loader: Lua 5.4 as an Extism plug-in. See ../spirisynq.h for the protocol.
 *
 * Values cross as JSON: null is nil, numbers are integers or floats as written, arrays are
 * tables indexed from 1, objects are tables. A table goes out as an array when its keys are
 * exactly 1..n (an empty table is an empty array), else as an object, whose keys must be
 * strings or integers.
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

/* ---- Lua values to JSON text ---- */

/* One buffer, reused by every call; a failed encoding leaves nothing to free. */
static struct { char *data; size_t len, cap; } out;

static void put(lua_State *L, const char *s, size_t n) {
    if (out.len + n > out.cap) {
        size_t cap = out.cap ? out.cap : 256;
        while (cap < out.len + n) cap *= 2;
        char *data = realloc(out.data, cap);
        if (data == NULL) luaL_error(L, "out of memory");
        out.data = data;
        out.cap = cap;
    }
    memcpy(out.data + out.len, s, n);
    out.len += n;
}

static void put_string(lua_State *L, const unsigned char *s, size_t n) {
    put(L, "\"", 1);
    for (size_t i = 0; i < n;) {
        unsigned char c = s[i];
        size_t width = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
        if (width == 0 || i + width > n) luaL_error(L, "a string that isn't UTF-8 has no JSON form");
        for (size_t k = 1; k < width; k++)
            if ((s[i + k] & 0xC0) != 0x80) luaL_error(L, "a string that isn't UTF-8 has no JSON form");
        if (c == '"' || c == '\\') { put(L, "\\", 1); put(L, (const char *)&s[i], 1); }
        else if (c < 0x20) { char esc[7]; snprintf(esc, sizeof esc, "\\u%04x", c); put(L, esc, 6); }
        else put(L, (const char *)&s[i], width);
        i += width;
    }
    put(L, "\"", 1);
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
        const char *s = lua_tolstring(L, idx, &n);
        put_string(L, (const unsigned char *)s, n);
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
                lua_rawgeti(L, idx, i);
                encode(L, -1, depth + 1);
                lua_pop(L, 1);
            }
            put(L, "]", 1);
        } else {
            put(L, "{", 1);
            int first = 1;
            lua_pushnil(L);
            while (lua_next(L, idx)) {
                if (!first) put(L, ",", 1);
                first = 0;
                if (lua_type(L, -2) == LUA_TSTRING) encode(L, -2, depth + 1);
                else if (lua_isinteger(L, -2)) {
                    char text[26];
                    int n = snprintf(text, sizeof text, "\"%lld\"", (long long)lua_tointeger(L, -2));
                    put(L, text, n);
                } else luaL_error(L, "a table key must be a string or an integer");
                put(L, ":", 1);
                encode(L, -1, depth + 1);
                lua_pop(L, 1);
            }
            put(L, "}", 1);
        }
        return;
    default: luaL_error(L, "a %s has no JSON form", luaL_typename(L, idx));
    }
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
    return 1;
}

static int self_newindex(lua_State *L) {
    size_t n;
    const char *name = luaL_checklstring(L, 2, &n);
    out.len = 0;
    encode(L, 3, 0);
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
    out.len = 0;
    encode(L, 2, 0);
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
            out.len = 0;
            if (nres > 0) encode(L, -nres, 0);
            else put(L, "null", 4);
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
    out.len = 0;
    encode(L, -1, 0);
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
