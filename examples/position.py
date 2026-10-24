import time
import math

from SpiriSynq.example_types.position import Position, Pose, Offset, Orientation, Accuracy, RootFrame

# Authoritative node — this is the "publisher" side. The drone flies in a local
# NED frame whose origin is a fixed point on the globe.
origin = Position("origin", synq_authoritive=True, relative_to=RootFrame("ECEF"))
origin.mount = Pose(
    offset=Offset.from_geodetic(45.42, -75.69, 60.0),
    orientation=Orientation.ned_to_ecef(45.42, -75.69),
    accuracy=Accuracy(horizontal=2.5, vertical=1.0),
)
drone = Position("drone", synq_authoritive=True, relative_to=origin.synq_absolute_path)
# A camera 10 cm ahead of the drone's centre, in the drone's FRD body frame
camera = Position("drone_camera", synq_authoritive=True,
                  relative_to=drone.synq_absolute_path, mount=Pose(Offset(x=0.1)))

# Observer node — mirrors the authoritative node
observer = Position.from_topic(drone.synq_absolute_path)

print(f"Watching {drone.synq_absolute_path}")
print(f"Observer relative_to: {observer.relative_to}")

try:
    t = 0.0
    while True:
        # Simulate a drone flying a 50 m circle, 100 m above the origin (NED: z is down)
        drone.mount = Pose(
            offset=Offset(x=50.0 * math.cos(t), y=50.0 * math.sin(t), z=-100.0),
            orientation=Orientation.from_euler(roll=0.0, pitch=0.0, yaw=t + math.pi / 2),
        )

        if camera.pose is not None:
            lat, lon, height = camera.pose.offset.to_geodetic()
            print(f"camera at {lat:.6f}, {lon:.6f}, {height:.1f} m")

        t += 0.1
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
