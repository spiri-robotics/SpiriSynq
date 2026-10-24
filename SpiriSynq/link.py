from dataclasses import dataclass
from typing import TYPE_CHECKING
import zenoh

if TYPE_CHECKING:
    from SpiriSynq.session import Session
    from SpiriSynq.syncable_objects import SyncableObject


@dataclass(frozen=True)
class Link:
    """A reference to another SpiriSynq object by its absolute topic.

    Sent as a tagged scalar: ``!Link myhost/camera``. A link holds only the
    topic -- it doesn't keep the target alive or check that it exists until
    :meth:`resolve` fetches it.

    The topic must be absolute: a relative ``./camera`` would name a different
    object on every node that received it. Use :meth:`to` to link to a live
    object.
    """

    yaml_tag = "!Link"
    topic: str

    def __post_init__(self):
        chunks = self.topic.split("/")
        if {".", ".."} & set(chunks):
            raise ValueError(
                f"Invalid link {self.topic!r}: links must be absolute ('.' and '..' chunks are not allowed)"
            )
        try:
            zenoh.KeyExpr(self.topic)
        except Exception as e:
            raise ValueError(f"Invalid link {self.topic!r}: {e}") from None

    @classmethod
    def to(cls, obj: "SyncableObject") -> "Link":
        """Link to *obj* at its absolute path."""
        return cls(obj.synq_absolute_path)

    def resolve(self, cls: type["SyncableObject"] | None = None, session: "Session | None" = None):
        """Fetch a mirror of the linked object.

        With *cls*, this is ``cls.from_topic(self.topic)``: it registers *cls*
        and checks the result is one. Without it, the target's type must
        already be registered on *session* (the current session by default).
        For a type this process doesn't have, use
        ``session.from_topic_untyped(link.topic)``.
        """
        if session is None:
            from SpiriSynq.session import current_session
            session = current_session.get()
        if cls is not None:
            return cls.from_topic(self.topic, session=session)
        return session.from_topic(self.topic)

    @classmethod
    def to_yaml(cls, representer, data):
        return representer.represent_scalar(cls.yaml_tag, data.topic)

    @classmethod
    def from_yaml(cls, constructor, node):
        return cls(constructor.construct_scalar(node))
