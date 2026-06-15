"""
Performance and stress tests. Heavier than unit tests — run separately if needed.
"""

import threading
import time
from dataclasses import dataclass

import pytest

from SpiriSynq.session import Session
from SpiriSynq.syncable_objects import SyncableObject


def _wait_for(predicate, timeout=2.0, interval=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def close_test_sessions():
    from SpiriSynq.shutdown import _live_sessions
    before = set(_live_sessions.keys())
    yield
    for sid, session in list(_live_sessions.items()):
        if sid not in before:
            session.close()


def test_concurrent_field_writes():
    """
    Concurrent field writes from multiple threads must not raise or corrupt state.
    Final mirror state must converge to the last written value.
    """
    WRITERS = 8
    WRITES_PER_THREAD = 200

    @dataclass
    class StressObj(SyncableObject):
        counter: int = 0
        label: str = "init"
        value: float = 0.0

    auth = StressObj("stress/thread_writes", synq_authoritive=True)
    mirror_session = Session()
    mirror = StressObj.from_topic(auth.synq_absolute_path, mirror_session)

    time.sleep(0.3)

    errors = []
    errors_lock = threading.Lock()

    def writer(tid):
        for i in range(WRITES_PER_THREAD):
            try:
                auth.counter = tid * 1000 + i
                auth.label = f"t{tid}-{i}"
                auth.value = tid + i * 0.001
            except Exception as e:
                with errors_lock:
                    errors.append((tid, i, repr(e)))

    threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions during concurrent writes: {errors}"

    last_tid = WRITERS - 1
    last_i = WRITES_PER_THREAD - 1
    assert _wait_for(lambda: mirror.counter == last_tid * 1000 + last_i, timeout=3.0), (
        f"Mirror did not converge: counter={mirror.counter}"
    )
