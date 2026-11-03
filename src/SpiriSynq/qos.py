from dataclasses import dataclass, field

import zenoh


@dataclass(frozen=True)
class SynqQoS:
    """Zenoh QoS for publishes of one field.

    Declare on a SyncableObject as ``<field>_qos: ClassVar[SynqQoS]``, e.g.
    ``image_qos: ClassVar[SynqQoS] = SynqQoS(priority=zenoh.Priority.DATA_HIGH)``.
    QoS is local to the sender; it is never synced, rehydrated or included in
    the schema.
    """

    # zenoh's enums are unhashable, so they need default_factory and a custom __hash__
    priority: zenoh.Priority = field(default_factory=lambda: zenoh.Priority.DATA)
    congestion_control: zenoh.CongestionControl = field(
        default_factory=lambda: zenoh.CongestionControl.DROP
    )
    express: bool = False

    def __hash__(self):
        return hash((int(self.priority), int(self.congestion_control), self.express))

    def __post_init__(self):
        # Fail at class definition rather than on the first publish.
        if not isinstance(self.priority, zenoh.Priority):
            raise TypeError(f"priority must be a zenoh.Priority, got {self.priority!r}")
        if not isinstance(self.congestion_control, zenoh.CongestionControl):
            raise TypeError(
                f"congestion_control must be a zenoh.CongestionControl, "
                f"got {self.congestion_control!r}"
            )

    def put_kwargs(self) -> dict:
        """Keyword arguments for ``zenoh.Session.put``."""
        return {
            "priority": self.priority,
            "congestion_control": self.congestion_control,
            "express": self.express,
        }
