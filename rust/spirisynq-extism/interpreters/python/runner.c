/* The `python` loader: MicroPython as an Extism plug-in. See ../spirisynq.h for the protocol.
 *
 * The source is a function definition, run as a module named `code`; the entry is called with
 * `self` first. Values cross as JSON: integers of any size, floats, and bytes and integer keys
 * as strings, marked where they are (see ../spirisynq.h). A float that isn't finite, or anything
 * else that has no JSON form, is an error.
 */

#include <stdio.h>
#include <string.h>
#include <wasi/api.h>

#include "py/builtin.h"
#include "py/compile.h"
#include "py/gc.h"
#include "py/lexer.h"
#include "py/mperrno.h"
#include "py/mphal.h"
#include "py/objexcept.h"
#include "py/runtime.h"
#include "py/stackctrl.h"
#include "spirisynq.h"

/* Runs each call: reads the request, runs the source, calls the entry, writes the result. */
static const char PRELUDE[] =
    "import binascii, json\n"
    "def _run(text, natives):\n"
    "    field_get, field_set, _part, _send, _call_send, _hand, _next, _escaped, _marks, _mark = natives\n"
    "    def unpack(value, marks):\n"
    "        def parent(path):\n"
    "            node = value\n"
    "            for key in path[:-1]:\n"
    "                node = node[key]\n"
    "            return node\n"
    "        for path in marks.get('bytes', ()):\n"
    "            if not path:\n"
    "                value = binascii.a2b_base64(value)\n"
    "                continue\n"
    "            node = parent(path)\n"
    "            node[path[-1]] = binascii.a2b_base64(node[path[-1]])\n"
    "        for path in reversed(marks.get('int_keys', ())):\n"
    "            node = parent(path)\n"
    "            node[int(path[-1])] = node.pop(path[-1])\n"
    "        return value\n"
    "    def loads(text):\n"
    "        value = json.loads(text)\n"
    "        marks = _marks()\n"
    "        return unpack(value, json.loads(marks)) if marks else value\n"
    "    def out(value):\n"
    "        marks = {'bytes': [], 'int_keys': []}\n"
    "        text = dumps(value, marks, [])\n"
    "        marks = {kind: paths for kind, paths in marks.items() if paths}\n"
    "        if marks:\n"
    "            _mark(json.dumps(marks))\n"
    "        return text\n"
    "    def dumps(value, marks, path, depth=0):\n"
    "        if depth > 128:\n"
    "            raise ValueError('nested more than 128 deep')\n"
    "        kind = type(value)\n"
    "        if value is None:\n"
    "            return 'null'\n"
    "        if kind is bool:\n"
    "            return 'true' if value else 'false'\n"
    "        if kind is int:\n"
    "            return str(value)\n"
    "        if kind is float:\n"
    "            if value != value or value - value != 0:\n"
    "                raise ValueError('%r has no JSON form' % value)\n"
    "            text = repr(value)\n"
    "            if float(text) != value:\n"
    "                text = '%.17g' % value\n"
    "            if '.' not in text and 'e' not in text:\n"
    "                text += '.0'\n"
    "            return text\n"
    "        if kind is str:\n"
    "            return json.dumps(value)\n"
    "        if kind is bytes or kind is bytearray:\n"
    "            marks['bytes'].append(path)\n"
    "            return json.dumps(binascii.b2a_base64(value)[:-1].decode())\n"
    "        if kind is list or kind is tuple:\n"
    "            items = [dumps(v, marks, path + [i], depth + 1) for i, v in enumerate(value)]\n"
    "            return '[' + ','.join(items) + ']'\n"
    "        if kind is dict:\n"
    "            items = []\n"
    "            for k, v in value.items():\n"
    "                if type(k) is int:\n"
    "                    k = str(k)\n"
    "                    marks['int_keys'].append(path + [k])\n"
    "                elif type(k) is not str:\n"
    "                    raise TypeError('a dict key must be a string or an integer')\n"
    "                items.append(json.dumps(k) + ':' + dumps(v, marks, path + [k], depth + 1))\n"
    "            return '{' + ','.join(items) + '}'\n"
    "        raise TypeError('a %s has no JSON form' % kind.__name__)\n"
    "    class Self:\n"
    "        def __getattr__(self, name):\n"
    "            return loads(field_get(name))\n"
    "        def __setattr__(self, name, value):\n"
    "            field_set(name, out(value))\n"
    "    def raw(value, what):\n"
    "        if type(value) is not bytes and type(value) is not bytearray:\n"
    "            raise TypeError('%s must be bytes' % what)\n"
    "        return value\n"
    "    def attached(value):\n"
    "        return None if value is None else raw(value, 'the attachment')\n"
    "    class Received:\n"
    "        def __init__(self, index):\n"
    "            self._index = index\n"
    "            self.payload = _part(index, 0)\n"
    "            self.encoding = _part(index, 1)\n"
    "            self.attachment = _part(index, 2)\n"
    "        def decode(self):\n"
    "            return loads(_part(self._index, 3))\n"
    "    class Outgoing:\n"
    "        def send(self, payload, encoding='zenoh/bytes', attachment=None):\n"
    "            _send(raw(payload, 'the payload'), str(encoding), attached(attachment))\n"
    "        def send_value(self, value):\n"
    "            _hand(0, out(value))\n"
    "    class RpcException(Exception):\n"
    "        def __init__(self, kind, message):\n"
    "            super().__init__('%s: %s' % (kind, message))\n"
    "            self.kind = kind\n"
    "            self.message = message\n"
    "    class Call:\n"
    "        def send_args(self, args):\n"
    "            _hand(1, out(args))\n"
    "        def send(self, payload, parameters='', encoding='zenoh/bytes', attachment=None):\n"
    "            payload = raw(payload, 'the payload')\n"
    "            _call_send(payload, str(parameters), str(encoding), attached(attachment))\n"
    "    class Replies:\n"
    "        error = None\n"
    "        def __iter__(self):\n"
    "            return self\n"
    "        def __next__(self):\n"
    "            got = json.loads(_next())\n"
    "            if got is None:\n"
    "                raise StopIteration\n"
    "            if 'error' in got:\n"
    "                self.error = RpcException(got['error']['kind'], got['error']['message'])\n"
    "                raise self.error\n"
    "            return Received(got['reply'])\n"
    "    request = json.loads(text)\n"
    "    request = unpack(request, request.get('marks', {}))\n"
    "    scope = {'__name__': 'code', 'RpcException': RpcException}\n"
    "    exec(compile(request['source'], 'code', 'exec'), scope)\n"
    "    entry = scope.get(request['entry'])\n"
    "    if entry is None:\n"
    "        raise NameError('no function `%s`' % request['entry'])\n"
    "    args = request['args']\n"
    "    sample = request.get('sample')\n"
    "    if sample == 'in':\n"
    "        args.append(Received(-1))\n"
    "    elif sample == 'out':\n"
    "        args.append(Outgoing())\n"
    "    call = request.get('call')\n"
    "    if call == 'send':\n"
    "        args.insert(0, Call())\n"
    "    elif call == 'replies':\n"
    "        replies = Replies()\n"
    "        try:\n"
    "            for value in entry(Self(), replies):\n"
    "                _hand(2, out(value))\n"
    "        except RpcException as e:\n"
    "            if e is replies.error:\n"
    "                _escaped()\n"
    "            raise\n"
    "        return 'null'\n"
    "    return out(entry(Self(), *args))\n";

static mp_obj_t field_get(mp_obj_t name) {
    size_t n, len;
    const char *key = mp_obj_str_get_data(name, &n);
    char *json = synq_field_get(key, n, &len);
    if (json == NULL) mp_raise_msg(&mp_type_MemoryError, NULL);
    mp_obj_t text = mp_obj_new_str(json, len);
    free(json);
    return text;
}
static MP_DEFINE_CONST_FUN_OBJ_1(field_get_obj, field_get);

static mp_obj_t field_set(mp_obj_t name, mp_obj_t json) {
    size_t n, len;
    const char *key = mp_obj_str_get_data(name, &n);
    const char *text = mp_obj_str_get_data(json, &len);
    synq_field_set(key, n, text, len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(field_set_obj, field_set);

/* A part of a reply, or of the incoming sample for index -1: bytes for the payload and
 * attachment (None for none), text for the encoding and the decoded JSON. */
static mp_obj_t sample_part(mp_obj_t index, mp_obj_t which) {
    synq_part_t part = (synq_part_t)mp_obj_get_int(which);
    size_t len;
    int none;
    char *text = synq_part_get(mp_obj_get_int(index), part, &len, &none);
    if (text == NULL) mp_raise_msg(&mp_type_MemoryError, NULL);
    mp_obj_t value;
    if (part == SYNQ_ATTACHMENT && none) value = mp_const_none;
    else if (part == SYNQ_PAYLOAD || part == SYNQ_ATTACHMENT) value = mp_obj_new_bytes((const byte *)text, len);
    else value = mp_obj_new_str(text, len);
    free(text);
    return value;
}
static MP_DEFINE_CONST_FUN_OBJ_2(sample_part_obj, sample_part);

static mp_obj_t sample_send(mp_obj_t payload, mp_obj_t encoding, mp_obj_t attachment) {
    mp_buffer_info_t p, a = {0};
    mp_get_buffer_raise(payload, &p, MP_BUFFER_READ);
    if (attachment != mp_const_none) mp_get_buffer_raise(attachment, &a, MP_BUFFER_READ);
    size_t n;
    const char *e = mp_obj_str_get_data(encoding, &n);
    synq_sample_send(p.buf, p.len, e, n, a.buf, a.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(sample_send_obj, sample_send);

static mp_obj_t call_send(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t p, a = {0};
    mp_get_buffer_raise(args[0], &p, MP_BUFFER_READ);
    if (args[3] != mp_const_none) mp_get_buffer_raise(args[3], &a, MP_BUFFER_READ);
    size_t qn, en;
    const char *q = mp_obj_str_get_data(args[1], &qn);
    const char *e = mp_obj_str_get_data(args[2], &en);
    synq_call_send(p.buf, p.len, q, qn, e, en, a.buf, a.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(call_send_obj, 4, 4, call_send);

/* Hand JSON text to the host: 0 sends a field's value, 1 a call's arguments, 2 yields. */
static void (*const HANDS[])(synq_block_t) = {
    synq_host_sample_send_value,
    synq_host_call_send_args,
    synq_host_call_yield,
};

static mp_obj_t hand(mp_obj_t which, mp_obj_t json) {
    size_t len;
    const char *text = mp_obj_str_get_data(json, &len);
    synq_send_json(HANDS[mp_obj_get_int(which)], text, len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(hand_obj, hand);

static mp_obj_t reply_next(void) {
    size_t len;
    char *json = synq_reply_next(&len);
    if (json == NULL) mp_raise_msg(&mp_type_MemoryError, NULL);
    mp_obj_t text = mp_obj_new_str(json, len);
    free(json);
    return text;
}
static MP_DEFINE_CONST_FUN_OBJ_0(reply_next_obj, reply_next);

static mp_obj_t reply_escaped(void) {
    synq_host_reply_escaped();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(reply_escaped_obj, reply_escaped);

/* The marks in the JSON a host function last returned: '' if there are none. */
static mp_obj_t marks(void) {
    size_t len;
    char *json = synq_marks(&len);
    if (json == NULL) mp_raise_msg(&mp_type_MemoryError, NULL);
    mp_obj_t text = mp_obj_new_str(json, len);
    free(json);
    return text;
}
static MP_DEFINE_CONST_FUN_OBJ_0(marks_obj, marks);

static mp_obj_t mark(mp_obj_t json) {
    size_t len;
    const char *text = mp_obj_str_get_data(json, &len);
    synq_mark(text, len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mark_obj, mark);

/* ---- What the port provides MicroPython ---- */

/* Never collect during a call; see mpconfigport.h. The heap grows instead. */
void gc_collect(void) {
}

size_t gc_get_max_new_split(void) {
    return 64 * 1024 * 1024;
}

void nlr_jump_fail(void *val) {
    (void)val;
    abort();
}

/* Output goes nowhere, as for every loader. */
mp_uint_t mp_hal_stdout_tx_strn(const char *str, size_t len) {
    (void)str;
    return len;
}

void mp_hal_stdout_tx_strn_cooked(const char *str, size_t len) {
    (void)str;
    (void)len;
}

uint32_t spirisynq_random_seed(void) {
    uint32_t seed = 0;
    __wasi_random_get((uint8_t *)&seed, sizeof seed);
    return seed;
}

mp_lexer_t *mp_lexer_new_from_file(qstr filename) {
    mp_raise_OSError(MP_ENOENT);
}

/* There are no files. */
mp_obj_t mp_builtin_open(size_t n_args, const mp_obj_t *args, mp_map_t *kwargs) {
    (void)n_args;
    (void)args;
    (void)kwargs;
    mp_raise_OSError(MP_ENOENT);
}
MP_DEFINE_CONST_FUN_OBJ_KW(mp_builtin_open_obj, 1, mp_builtin_open);

mp_import_stat_t mp_import_stat(const char *path) {
    (void)path;
    return MP_IMPORT_STAT_NO_EXIST;
}

/* ---- Running ---- */

static char heap[256 * 1024];
static mp_obj_t pystack[16 * 1024];

/* Fail with an exception: its type and message, and its line in the code. */
static int fail_with(mp_obj_t error) {
    vstr_t text;
    mp_print_t print;
    vstr_init_print(&text, 64, &print);
    mp_obj_print_helper(&print, error, PRINT_EXC);
    size_t n;
    size_t *values;
    mp_obj_exception_get_traceback(error, &n, &values);
    qstr code = qstr_from_str("code");
    for (size_t i = n; i >= 3; i -= 3) {
        if (values[i - 3] == code) {
            mp_printf(&print, " (line %u)", (unsigned)values[i - 2]);
            break;
        }
    }
    return synq_fail(text.buf, text.len);
}

SYNQ_EXPORT(run) int run(void) {
    int top;
    mp_stack_set_top(&top);
    mp_stack_set_limit(768 * 1024);
    gc_init(heap, heap + sizeof heap);
    mp_pystack_init(pystack, pystack + MP_ARRAY_SIZE(pystack));
    mp_init();
    size_t len;
    char *input = synq_input(&len);
    if (input == NULL) return synq_fail("out of memory", 13);
    nlr_buf_t nlr;
    if (nlr_push(&nlr) != 0) {
        /* `input` may be gone: the instance is thrown away after the call anyway. */
        return fail_with(MP_OBJ_FROM_PTR(nlr.ret_val));
    }
    qstr name = qstr_from_str("prelude");
    mp_lexer_t *lex = mp_lexer_new_from_str_len(name, PRELUDE, strlen(PRELUDE), 0);
    mp_parse_tree_t tree = mp_parse(lex, MP_PARSE_FILE_INPUT);
    mp_call_function_0(mp_compile(&tree, name, false));
    mp_obj_t natives[10] = {
        MP_OBJ_FROM_PTR(&field_get_obj),     MP_OBJ_FROM_PTR(&field_set_obj),
        MP_OBJ_FROM_PTR(&sample_part_obj),   MP_OBJ_FROM_PTR(&sample_send_obj),
        MP_OBJ_FROM_PTR(&call_send_obj),     MP_OBJ_FROM_PTR(&hand_obj),
        MP_OBJ_FROM_PTR(&reply_next_obj),    MP_OBJ_FROM_PTR(&reply_escaped_obj),
        MP_OBJ_FROM_PTR(&marks_obj),         MP_OBJ_FROM_PTR(&mark_obj),
    };
    mp_obj_t args[2] = {mp_obj_new_str(input, len), mp_obj_new_tuple(10, natives)};
    free(input);
    mp_obj_t result = mp_call_function_n_kw(mp_load_global(qstr_from_str("_run")), 2, 0, args);
    size_t n;
    const char *json = mp_obj_str_get_data(result, &n);
    synq_output(json, n);
    nlr_pop();
    return 0;
}
