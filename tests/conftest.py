import socket
import pytest
import zenoh
from SpiriSynq.session import Session, current_session

_seed_port: int = 0
_seed: zenoh.Session | None = None


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def zenoh_test_config() -> zenoh.Config:
    """Return a TCP-only zenoh config pointing at the test seed session."""
    conf = zenoh.Config()
    conf.insert_json5("connect/endpoints", f'["tcp/127.0.0.1:{_seed_port}"]')
    conf.insert_json5("scouting/multicast/enabled", "false")
    return conf


def pytest_sessionstart(session):
    global _seed_port, _seed
    _seed_port = _find_free_port()
    conf = zenoh.Config()
    conf.insert_json5("listen/endpoints", f'["tcp/127.0.0.1:{_seed_port}"]')
    conf.insert_json5("scouting/multicast/enabled", "false")
    _seed = zenoh.open(conf)


def pytest_sessionfinish(session, exitstatus):
    if _seed is not None:
        _seed.close()


@pytest.fixture(autouse=True)
def zenoh_current_session():
    """Override current_session with a TCP-only session for the duration of each test."""
    s = Session(config=zenoh_test_config())
    token = current_session.set(s)
    yield s
    current_session.reset(token)
    s.close()
