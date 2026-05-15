from dataclasses import dataclass, field
from SpiriSynq.syncable_objects import SyncableObject
from SpiriSynq.session import Session
from typing import List
import datetime
import time
from loguru import logger
import sys

logger.configure(handlers=[{"sink": sys.stderr, "level": "TRACE"}])

@dataclass
class TimeData(SyncableObject):
    """Simple server that publishes the current time every second"""
    time: datetime.datetime = field(default_factory=datetime.datetime.now)
    time_binary: bytes = b''
    updates: List = field(default_factory=list)

if __name__ == "__main__":
    t = TimeData("timeServer", synq_authoritive=True)

    session2=Session()
    t2 = TimeData(synq_session=session2,synq_topic=t.synq_absolute_path)
    
    while True:
        t.time = datetime.datetime.now()
        t.time_binary = int(datetime.datetime.now().timestamp()).to_bytes(8)
        assert t2
        time.sleep(1)
        #print(t2.time)
