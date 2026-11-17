"""Links: a field's reference to another syncable object, by its topic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from SpiriSynq.session import Session
    from SpiriSynq.syncable_objects import SyncableObject

_NOT_IN_A_TOPIC = set("*$?#")


def check_topic(topic: str, what: str = "topic") -> str:
    """`topic` if it's a concrete zenoh key; `ValueError` saying why if not."""
    chunks = topic.split("/")
    if not topic or "" in chunks:
        raise ValueError(f"Invalid {what} {topic!r}: empty chunks aren't allowed")
    if {".", ".."} & set(chunks):
        raise ValueError(f"Invalid {what} {topic!r}: '.' and '..' chunks aren't allowed")
    if bad := _NOT_IN_A_TOPIC & set(topic):
        raise ValueError(f"Invalid {what} {topic!r}: {''.join(sorted(bad))!r} can't be in a key")
    return topic


@dataclass(frozen=True)
class Link:
    """A reference to another SpiriSynq object by its absolute topic.

    On the wire it is `!Link <topic>`. A field holding a syncable object accepts a `Link` as well
    as the object itself, and both are sent the same way. A link only holds the topic: it doesn't
    keep the target alive or check that it exists until `resolve` mirrors it.
    """

    topic: str

    def __post_init__(self):
        check_topic(self.topic, "link")

    @classmethod
    def to(cls, obj: SyncableObject) -> Link:
        """A link to `obj`, at its absolute topic."""
        return cls(obj.synq_absolute_path)

    def resolve(self, cls: type[SyncableObject], session: Session | None = None, **options):
        """A mirror of the linked object: `cls.from_topic(self.topic)`."""
        return cls.from_topic(self.topic, session=session, **options)


@dataclass(frozen=True, eq=False)
class ObjectNotFound(Link):
    """What a mirror's link field holds when nobody serves the link's topic.

    It isn't retried on its own: `rebind()` tries again, and on success the field holds a
    mirror.
    """

    def rebind(self, timeout: float | None = None):
        """Mirror the target again, and put the mirror in the field. Returns the mirror;
        raises `TimeoutError` if nobody answers this time either."""
        rebind = object.__getattribute__(self, "_rebind")
        return rebind(timeout)
