from dataclasses import dataclass, field
from SpiriSynq.session import SyncableObject, Session
from typing import List
import datetime
import time

@dataclass
class TimeData(SyncableObject):
    """Simple server that publishes the current time every second"""
    time: datetime.datetime = field(default_factory=datetime.datetime.now)
    time_binary: bytes = b''
    updates: List = field(default_factory=list)

if __name__ == "__main__":
    session=Session()
    t = TimeData()
    session.publish_synced_object("time",t)
    while True:
        t.time = datetime.datetime.now()
        t.time_binary = int(datetime.datetime.now().timestamp()).to_bytes(8)
        time.sleep(1)
