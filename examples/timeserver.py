
import tracemalloc
tracemalloc.start(10)  # capture 10 levels of stack
from loguru import logger
import sys

logger.configure(handlers=[{"sink": sys.stderr, "level": "TRACE"}])


from dataclasses import dataclass, field
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from typing import List
import datetime
import time

from . import test_gc

@dataclass
class TimeData(SyncableObject):
    """Simple server that publishes the current time every second"""
    time: datetime.datetime = field(default_factory=datetime.datetime.now)
    time_binary: bytes = b''
    # updates: List = field(default_factory=list)



if __name__ == "__main__":
    t = TimeData("timeServer", synq_authoritive=True)
    try:
        while True:

            t.time = datetime.datetime.now()
            t.time_binary = int(datetime.datetime.now().timestamp()).to_bytes(8)
            time.sleep(1)
    except KeyboardInterrupt:
        test_gc.diagnose_gc_leaks("./gc_leaks")
        sys.exit(0)
