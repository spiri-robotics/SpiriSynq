"""Field values cross into the engine and back unchanged."""

import pytest

from SpiriSynq import _core

from .conftest import config


@pytest.fixture
def owner(battery):
    node = _core.Node(config())
    yield node, node.create_owner("robot/battery", battery)
    node.close()


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        7,
        -(2**63),
        2.5,
        float("inf"),
        "on",
        b"\x00\xff",
        [1, "two", [3.0]],
        {"a": 1, 2: "b", None: [], True: {}},
        _core.Tagged("Link", "robot/camera"),
        _core.Tagged("Point", {"x": 1, "y": [_core.Tagged("Unit", "m")]}),
    ],
)
def test_values_round_trip(owner, value):
    node, battery = owner
    assert node.set_field(battery, "anything", value)
    assert node.field(battery, "anything") == value
    # Setting the same value again isn't a change.
    assert not node.set_field(battery, "anything", value)


def test_tuples_and_bytearrays_come_back_as_lists_and_bytes(owner):
    node, battery = owner
    node.set_field(battery, "anything", ((1, 2), bytearray(b"ab")))
    assert node.field(battery, "anything") == [[1, 2], b"ab"]


def test_a_field_with_no_value_gives_the_default(owner):
    node, battery = owner
    assert node.field(battery, "voltage") is None
    assert node.field(battery, "voltage", 0.0) == 0.0


def test_values_python_cant_put_on_the_wire_are_refused(owner):
    node, battery = owner
    with pytest.raises(TypeError, match="a object can't be a field value"):
        node.set_field(battery, "anything", object())
    with pytest.raises(OverflowError):
        node.set_field(battery, "anything", 2**64)
    deep: list = []
    for _ in range(200):
        deep = [deep]
    with pytest.raises(ValueError, match="nested more than 128 deep"):
        node.set_field(battery, "anything", deep)
    # The engine refuses what YAML can't write.
    with pytest.raises(_core.SynqError):
        node.set_field(battery, "anything", {float("nan"): 1})


def test_tagged_values_compare_hash_and_print_by_tag_and_value():
    link = _core.Tagged("Link", "robot/camera")
    assert link == _core.Tagged("Link", "robot/camera")
    assert link != _core.Tagged("Link", "robot/other")
    assert link != _core.Tagged("Other", "robot/camera")
    assert link != "robot/camera"
    assert hash(link) == hash(_core.Tagged("Link", "robot/camera"))
    assert (link.tag, link.value) == ("Link", "robot/camera")
    assert repr(link) == "Tagged(\"Link\", 'robot/camera')"


def test_samples_hold_their_payload_encoding_and_attachment():
    sample = _core.Sample(b"\x89PNG", "image/png", bytearray(b"frame 7"))
    assert (sample.payload, sample.encoding, sample.attachment) == (
        b"\x89PNG",
        "image/png",
        b"frame 7",
    )
    assert _core.Sample(b"x", "text/plain").attachment is None
    assert sample == _core.Sample(b"\x89PNG", "image/png", b"frame 7")
    assert sample != _core.Sample(b"\x89PNG", "image/png")


def test_schemas_are_checked_and_know_their_type(battery):
    assert battery.id == "urn:pypi:spiri-robot:1.0:spirirobot.Battery"
    assert battery.bases == ["urn:pypi:spiri-robot:1.0:spirirobot.Part"]
    with pytest.raises(_core.SynqError):
        _core.Schema({"properties": {}})
