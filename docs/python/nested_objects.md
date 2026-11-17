# Nested objects and links

A field can hold another syncable object:

```python
from dataclasses import dataclass, field

from SpiriSynq.syncable_objects import SyncableObject


@dataclass
class Camera(SyncableObject):
    exposure: float = 0.01


@dataclass
class Robot(SyncableObject):
    camera: Camera | None = None
```

Each syncable object is published once, at its own topic. The parent doesn't copy the child's
state: on the wire, the parent's field is a *link*, the child's topic:

```yaml
camera: !Link myhost/robot/camera
```

The parent's schema says only that `camera` is a link to a `Camera`. The child's fields are in
the child's own schema, and a mirror fetches the child's state from the child's owner.

## Assigning a child

```python
robot = Robot("./robot", synq_authoritive=True)
robot.camera = Camera("./robot/camera", synq_authoritive=True)
robot.camera.exposure = 0.02   # published on myhost/robot/camera/exposure
```

By default a child's topic is its parent's topic followed by the field name:
`myhost/robot/camera`. A link can point anywhere, though; the child doesn't have to live under
its parent.

:::{note}
Children don't yet get their default topic automatically. Give each child its topic when you
create it, as above.
:::

## Objects, mirrors and links are interchangeable

A field holding a syncable object accepts any of:

- an authoritative object;
- a mirror of an object owned elsewhere;
- a `Link` to a topic.

```python
from SpiriSynq.link import Link

robot.camera = Camera("./robot/camera", synq_authoritive=True)  # owned here
robot.camera = Camera.from_topic("otherhost/camera")             # owned elsewhere
robot.camera = Link("otherhost/camera")                          # just the topic
```

All three are published the same way, as a link to the object's topic.

## Reassigning and closing

Assigning a new value to the field replaces the old one. If nothing else refers to the old
object, Python garbage-collects it, which closes it.

Children outlive their parents. Closing `robot` leaves `robot.camera` open, and its owner keeps
serving it.

## Mirrors follow links

When you mirror the parent, the mirror follows its links by default: `mirror.camera` is a mirror
of the camera, so you can reach through the tree:

```python
robot = Robot.from_topic("myhost/robot")
print(robot.camera.exposure)
```

When the link changes on the owner, the mirror switches to mirroring the new target.

To turn following off, pass `follow_links=False`. The mirror's link fields then hold `Link`s,
which you can resolve yourself:

```python
robot = Robot.from_topic("myhost/robot", follow_links=False)
camera = robot.camera.resolve(Camera)
```

## Collections of children

A list or dict of syncable objects is a collection of links:

```python
@dataclass
class Fleet(SyncableObject):
    robots: dict[str, Robot] = field(default_factory=dict)
```

On the wire, `robots` is a mapping from names to links. A mirror of the fleet follows each link.

## When a link can't be followed

- **The target has the wrong type.** A link to something that isn't a `Camera` in a
  `Camera` field is bad incoming data, handled like any other (see
  [Bad incoming data](objects.md#bad-incoming-data)).
- **Nobody serves the topic.** The field holds an `ObjectNotFound` placeholder, which carries the
  link's topic. It isn't retried on its own. Call `rebind()` to try again, for example once the
  owner has started; on success the field holds a mirror.

```python
from SpiriSynq.link import ObjectNotFound

if isinstance(robot.camera, ObjectNotFound):
    robot.camera.rebind()
```
