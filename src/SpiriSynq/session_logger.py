import sys
import uuid
import functools
from dataclasses import dataclass, field
from threading import Thread
from loguru import logger


# --- Logging setup ---

def formatter(record):
    session = record["extra"].get("session_id")
    session_part = (
        f" | session=<yellow>{str(session)[:4]}</yellow>"
        if session
        else " | session=<yellow>none</yellow>"
    )
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>"
        " | <level>{level: <8}</level>"
        " | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan>"
        f"{session_part}"
        " | <level>{message}</level>\n{exception}"
    )

logger.remove()
logger.add(sys.stderr, format=formatter, colorize=True, level="DEBUG")


# --- Metaclass (still useful for non-thread method calls) ---

def with_session_context(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            zenoh_session = getattr(self, "zenoh_session", None)
            session_id = str(zenoh_session.id()) if zenoh_session is not None else None
            with logger.contextualize(session_id=session_id):
                return method(self, *args, **kwargs)
        except Exception:
            return method(self, *args, **kwargs)
    return wrapper


class SessionMeta(type):
    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)
        for attr_name, attr_value in namespace.items():
            if callable(attr_value) and not attr_name.startswith("_"):
                setattr(cls, attr_name, with_session_context(attr_value))
        return cls


class SessionBase(metaclass=SessionMeta):
    pass


@dataclass
class Session(SessionBase, Thread):
    name: str
    zenoh_session: object = field(default=None)

    def __post_init__(self):
        Thread.__init__(self, daemon=True, name=self.name)

    def run(self):
        # Set context once for the entire lifetime of this thread
        session_id = str(self.zenoh_session.id()) if self.zenoh_session else None
        with logger.contextualize(session_id=session_id):
            self._run()

    def _run(self):
        """Override this in subclasses instead of run()."""
        raise NotImplementedError

    def __del__(self):
        try:
            logger.info(f"Session '{self.name}' destroyed")
        except Exception:
            pass
