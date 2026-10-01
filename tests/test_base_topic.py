"""Tests for how a Session picks its default base_topic."""

import pytest


# ── Default base topic ────────────────────────────────────────────────────────

@pytest.fixture
def base_topic_file(monkeypatch, tmp_path):
    """Point _default_base_topic at a temp file instead of /etc/spirisynq_base_topic."""
    from SpiriSynq import session as session_mod
    path = tmp_path / "spirisynq_base_topic"
    real_path = session_mod.Path
    monkeypatch.setattr(
        session_mod, "Path",
        lambda p: path if p == "/etc/spirisynq_base_topic" else real_path(p),
    )
    monkeypatch.delenv("SPIRI_SYNQ_BASE_TOPIC", raising=False)
    monkeypatch.setattr(session_mod.socket, "gethostname", lambda: "kernel_hostname")
    return path


def test_default_base_topic_env_wins(base_topic_file, monkeypatch):
    from SpiriSynq.session import _default_base_topic
    base_topic_file.write_text("uav7\n")
    monkeypatch.setenv("SPIRI_SYNQ_BASE_TOPIC", "from_env")
    assert _default_base_topic() == "from_env"


def test_default_base_topic_reads_file(base_topic_file):
    from SpiriSynq.session import _default_base_topic
    base_topic_file.write_text("uav7\n")
    assert _default_base_topic() == "uav7"


@pytest.mark.parametrize("contents", [None, "", "  \n"])
def test_default_base_topic_falls_back_to_hostname(base_topic_file, contents):
    from SpiriSynq.session import _default_base_topic
    if contents is not None:
        base_topic_file.write_text(contents)
    assert _default_base_topic() == "kernel_hostname"
