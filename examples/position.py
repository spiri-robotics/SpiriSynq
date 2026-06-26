import time
import math

from SpiriSynq.example_types.position import Position, Offset, Orientation, Accuracy, RootFrame

# Authoritative node — this is the "publisher" side
drone = Position("drone", synq_authoritive=True)
drone.relative_to = RootFrame("ECEF")
drone.mount_accuracy = Accuracy(horizontal=2.5, vertical=1.0)

# Observer node — mirrors the authoritative node
observer = Position.from_topic(drone.synq_absolute_path)

print(f"Watching {drone.synq_absolute_path}")
print(f"Observer relative_to: {observer.relative_to}")

try:
    t = 0.0
    while True:
        # Simulate a drone flying a circle at 100m altitude
        x = 1_000_000.0 + 50.0 * math.cos(t)
        y = 1_000_000.0 + 50.0 * math.sin(t)
        z = 100.0

        drone.mount_offset = Offset(x=x, y=y, z=z)
        drone.mount_orientation = Orientation.from_euler(roll=0.0, pitch=0.0, yaw=t)

        print(
            f"published offset=({x:.1f}, {y:.1f}, {z:.1f})  "
            f"observer offset={observer.offset}"
        )

        t += 0.1
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
