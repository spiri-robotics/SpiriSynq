from dataclasses import dataclass, field
from SpiriVector.session import SyncableObject, Session
import datetime
import time

@dataclass
class TimeData(SyncableObject):
    """Simple server that publishes the current time every second"""
    time: datetime.datetime = field(default_factory=datetime.datetime.now)

if __name__ == "__main__":
    session=Session()
    t = TimeData()
    session.publish_synced_object("time",t)
    while True:
        t.time = datetime.datetime.now()
        time.sleep(1)
