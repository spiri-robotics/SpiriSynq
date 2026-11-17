"""SpiriSynq._core over a real zenoh network: fields, calls, discovery, and the node itself."""

import pytest

from SpiriSynq import _core

from . import conftest
from .conftest import config, mirror, wait_for

TOPIC = "robot/battery"


def incoming(node, path):
    return wait_for(node, _core.Incoming, lambda n: n.path == path)


def test_a_mirror_fetches_the_state_then_follows_the_owner(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    a.set_field(owner, "voltage", 12.5)
    copy = b.create_mirror(TOPIC, battery)
    got = incoming(b, "voltage")
    assert (got.object, got.data, got.origin) == (copy, 12.5, "rehydrate")
    wait_for(b, _core.Rehydrated, lambda n: n.object == copy and n.complete)
    assert b.alive(copy)
    a.set_field(owner, "voltage", 13.0)
    got = incoming(b, "voltage")
    assert (got.data, got.origin) == (13.0, "owner")
    # The binding applies what arrives on a mirror itself.
    assert b.set_field_local(copy, "voltage", 13.0)
    assert b.field(copy, "voltage") == 13.0
    b.rehydrate(copy)
    assert wait_for(b, _core.Rehydrated).complete


def test_a_mirrors_write_reaches_the_owner(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    b.set_field(copy, "voltage", 11.0)
    got = incoming(a, "voltage")
    assert (got.object, got.data, got.origin) == (owner, 11.0, "write")
    # Ids are usable as dict keys.
    assert {owner: 1}[got.object] == 1


def test_mirrors_hear_when_the_owner_goes(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    a.close_object(owner)
    gone = wait_for(b, _core.Alive)
    assert (gone.object, gone.alive) == (copy, False)
    assert not b.alive(copy)


def test_owners_hear_when_someone_listens(network, battery):
    a, b = network(2)
    quiet = {"voltage": _core.FieldOptions(ignore_incoming=True, lazy=True)}
    owner = a.create_owner(TOPIC, battery, fields=quiet)
    mirror(b, TOPIC, battery)
    heard = wait_for(a, _core.Listeners, lambda n: n.path == "voltage" and n.listening)
    assert heard.object == owner
    assert a.has_listeners(owner, "voltage")
    assert a.has_listeners(owner)


def test_raw_fields_arrive_as_samples(network, battery):
    a, b = network(2)
    raw = {"image": _core.FieldOptions(priority="interactive_high", reliable=True, raw=True)}
    owner = a.create_owner(TOPIC, battery, fields=raw)
    mirror(b, TOPIC, battery, fields=raw)
    sample = _core.Sample(b"\x89PNG", "image/png", b"frame 7")
    a.send_raw(owner, "image", sample)
    assert incoming(b, "image").data == sample
    keyframe = _core.Sample(b"\x89PNG key", "image/png")
    a.send_raw(owner, "image", keyframe, priority="real_time", reliable=False)
    assert incoming(b, "image").data == keyframe
    with pytest.raises(ValueError, match="no priority `urgent`"):
        a.send_raw(owner, "image", sample, priority="urgent")


def test_values_that_dont_decode_are_reported_and_dropped(network, battery):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery)
    copy = mirror(b, TOPIC, battery)
    a.send_raw(owner, "voltage", _core.Sample(b"[unclosed", "application/yaml"))
    bad = wait_for(b, _core.BadData)
    assert (bad.object, bad.path) == (copy, "voltage")
    assert bad.reason


def test_field_options_name_known_priorities(battery):
    with pytest.raises(ValueError, match="no priority `urgent`"):
        _core.FieldOptions(priority="urgent")
    node = _core.Node(config())
    with pytest.raises(TypeError):
        node.create_owner(TOPIC, battery, fields={"voltage": "fast"})
    node.close()


def call_pair(network, battery, **owner_options):
    a, b = network(2)
    owner = a.create_owner(TOPIC, battery, **owner_options)
    return a, owner, b, mirror(b, TOPIC, battery)


def collect(node, request):
    """The replies to `request`, as (values, errors, how it ended)."""
    values, errors = [], []
    while True:
        notice = node.next_notice(10.0)
        assert notice is not None
        if isinstance(notice, _core.Reply):
            assert notice.request == request
            if notice.error is None:
                values.append(notice.value)
            else:
                errors.append(notice.error)
        elif isinstance(notice, _core.RequestEnded):
            assert notice.request == request
            return values, errors, notice.end


def test_a_call_gets_every_reply_then_ends(network, battery):
    a, owner, b, copy = call_pair(network, battery)
    request = b.call(copy, "set_limit", {"percent": 40}, parameters="x=1")
    call = wait_for(a, _core.Call)
    assert (call.method, call.key, call.args, call.parameters) == (
        "set_limit",
        "robot/battery/set_limit",
        {"percent": 40},
        "x=1",
    )
    assert (call.key_args, call.payload) == ({}, None)
    assert call.object == owner
    a.reply(call.query, 40)
    a.reply(call.query, 41)
    a.finish(call.query)
    assert collect(b, request) == ([40, 41], [], "done")


def test_an_error_reply_ends_the_call(network, battery):
    a, owner, b, copy = call_pair(network, battery)
    request = b.call(copy, "set_limit")
    call = wait_for(a, _core.Call)
    a.reply_error(call.query, "ValueError", "too high")
    assert collect(b, request) == ([], [("ValueError", "too high")], "failed")


def test_a_reply_that_doesnt_decode_is_an_error(network, battery):
    a, owner, b, copy = call_pair(network, battery)
    request = b.call(copy, "set_limit")
    call = wait_for(a, _core.Call)
    a.reply_raw(call.query, _core.Sample(b"[unclosed", "application/yaml"))
    a.finish(call.query)
    values, errors, end = collect(b, request)
    assert (values, [kind for kind, _ in errors], end) == ([], ["Undecodable"], "done")


def test_a_call_nobody_answers_is_cut_short(network, battery):
    a, owner, b, copy = call_pair(network, battery)
    request = b.call(copy, "set_limit", timeout_ms=200)
    wait_for(a, _core.Call)
    assert collect(b, request) == ([], [], "cut_short")


def test_raw_calls_and_replies_pass_through(network, battery):
    a, owner, b, copy = call_pair(network, battery, raw_methods=["blob", "read"])
    sent = _core.Sample(b"\x00\x01", "application/octet-stream", b"meta")
    request = b.call_raw(copy, "read", {"path": "logs/today"}, sent, raw_replies=True)
    call = wait_for(a, _core.Call)
    assert (call.key, call.key_args, call.payload) == (
        "robot/battery/read/logs/today",
        {"path": "logs/today"},
        sent,
    )
    answer = _core.Sample(b"\x02", "image/png")
    a.reply_raw(call.query, answer)
    a.finish(call.query)
    assert collect(b, request) == ([answer], [], "done")
    request = b.call_raw(copy, "blob")
    call = wait_for(a, _core.Call)
    assert call.payload is None
    a.finish(call.query)
    assert collect(b, request) == ([], [], "done")


def test_discovery_finds_objects_by_prefix_and_type(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    # Once a mirror has the state, the owner's queryables have reached the other node.
    mirror(b, TOPIC, battery)
    for request in (
        b.discover(),
        b.discover("robot"),
        b.discover(type_id="urn:pypi:spiri-robot:2.0:spirirobot.Part"),
    ):
        found = wait_for(b, _core.Discovered, lambda n, r=request: n.request == r)
        assert (found.topic, found.owner, found.type_id, found.bases) == (
            TOPIC,
            a.zid,
            battery.id,
            battery.bases,
        )
    with pytest.raises(_core.SynqError):
        b.discover(type_id="not a type")


def test_a_schema_fetch_gets_the_objects_schema(network, battery):
    a, b = network(2)
    a.create_owner(TOPIC, battery)
    mirror(b, TOPIC, battery)
    request = b.fetch_schema(TOPIC)
    got = wait_for(b, _core.SchemaFetched)
    assert (got.request, got.error, got.value["$id"]) == (request, None, battery.id)
    assert {request: 1}[got.request] == 1


def test_the_engine_refuses_calls_that_make_no_sense(network, battery):
    (a,) = network(1)
    owner = a.create_owner(TOPIC, battery)
    with pytest.raises(_core.SynqError, match="no field `nope`"):
        a.set_field(owner, "nope", 1)
    with pytest.raises(_core.SynqError, match="only a mirror"):
        a.rehydrate(owner)
    with pytest.raises(_core.SynqError, match="no such incoming call"):
        a.finish(12345)


def test_a_node_knows_its_network(network):
    a, b = network(2)
    assert a.peers() == [b.zid]
    assert a.locators()[0].startswith("tcp/127.0.0.1:")
    assert a.zid != b.zid


def test_a_node_that_cant_open_says_why():
    with pytest.raises(_core.SynqError):
        _core.Node("{not json")
    with pytest.raises(_core.SynqError):
        _core.Node(
            '{"listen": {"endpoints": ["tcp/127.0.0.1:1"]}, '
            '"scouting": {"multicast": {"enabled": false}}}'
        )


def test_waiting_for_notices(network, battery):
    (a,) = network(1)
    assert a.next_notice(0.05) is None
    with pytest.raises(ValueError):
        a.next_notice(-1.0)
    owner = a.create_owner(TOPIC, battery)
    a.create_mirror(TOPIC, battery)
    # Without a timeout it waits as long as it takes.
    assert a.next_notice() is not None
    a.close_object(owner)


def test_a_closed_node_refuses_everything(battery):
    node = _core.Node(config())
    node.close()
    node.close()
    assert node.next_notice() is None
    with pytest.raises(_core.SynqError, match="the node is closed"):
        node.create_owner(TOPIC, battery)
    with pytest.raises(_core.SynqError, match="the node is closed"):
        node.peers()


def test_on_demand_fields_are_subscribed_when_asked(network):
    schema = _core.Schema(
        {
            **conftest.BATTERY,
            "properties": {**conftest.BATTERY["properties"], "@frame": {"type": "string"}},
        }
    )
    a, b = network(2)
    owner = a.create_owner(TOPIC, schema)
    a.set_field(owner, "@frame", "first")
    copy = mirror(b, TOPIC, schema)
    assert b.subscribe(copy, "@frame")
    got = incoming(b, "@frame")
    assert (got.data, got.origin) == ("first", "rehydrate")
    assert not b.subscribe(copy, "@frame")
    assert not b.unsubscribe(copy, "@frame")
    assert b.unsubscribe(copy, "@frame")
    with pytest.raises(_core.SynqError, match="isn't on demand"):
        b.subscribe(copy, "voltage")
