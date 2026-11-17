"""Stress tests: heavier than the others."""

import threading
from dataclasses import dataclass

from SpiriSynq.syncable_objects import SyncableObject

from .conftest import wait_for


@dataclass
class StressObj(SyncableObject):
    synq_type_id = "7d1e2a3b-0000-4000-8000-0000000000e1:1"
    counter: int = 0
    label: str = "init"
    value: float = 0.0


def test_concurrent_field_writes(other):
    """Writes from many threads at once raise nothing, and a mirror converges on the last."""
    writers, writes = 8, 200
    owner = StressObj("stress/thread_writes", synq_authoritive=True)
    mirror = StressObj.from_topic("host/stress/thread_writes", session=other)
    errors = []

    def writer(tid):
        for i in range(writes):
            try:
                owner.counter = tid * 1000 + i
                owner.label = f"t{tid}-{i}"
                owner.value = tid + i * 0.001
            except Exception as e:  # pragma: no cover - only on failure
                errors.append((tid, i, repr(e)))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    wait_for(
        lambda: (
            (mirror.counter, mirror.label, mirror.value)
            == (owner.counter, owner.label, owner.value)
        ),
        timeout=10,
    )
