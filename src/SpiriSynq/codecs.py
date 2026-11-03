from abc import ABC, abstractmethod
from typing import Any
import zenoh


class Codec(ABC):
    """Base class for custom encode/decode pairs.

    Subclass and set ``python_type`` and ``zenoh_schema`` as class attributes,
    then implement :meth:`encode` and :meth:`decode`.  Register with
    :meth:`Session.register_codec`.

    - :meth:`encode` is selected by matching ``python_type`` (via MRO) against
      the Python value being published.
    - :meth:`decode` is selected by matching ``zenoh_schema`` against the
      encoding reported by an incoming zenoh sample.

    Example::

        class JpegCodec(Codec):
            python_type = np.ndarray
            zenoh_schema = zenoh.Encoding.IMAGE_JPEG

            def encode(self, value: np.ndarray) -> tuple[bytes, zenoh.Encoding]:
                return encode_jpeg(value), self.zenoh_schema

            def decode(self, sample: zenoh.Sample) -> np.ndarray:
                return decode_jpeg(sample.payload.to_bytes())
    """

    python_type: type
    zenoh_schema: zenoh.Encoding

    @abstractmethod
    def encode(self, value: Any) -> "tuple[bytes | str, zenoh.Encoding]": ...

    @abstractmethod
    def decode(self, sample: zenoh.Sample) -> Any: ...


class BytesCodec(Codec):
    """Sends ``bytes`` values as raw ``APPLICATION_OCTET_STREAM``.

    Without this codec, bytes fields are YAML-serialised (base64), which
    adds ~33 % overhead and requires a full YAML parse on receive.
    Registered on every :class:`Session` by default.
    """

    python_type = bytes
    zenoh_schema = zenoh.Encoding.ZENOH_BYTES

    def encode(self, value: bytes) -> tuple[bytes, zenoh.Encoding]:
        return value, zenoh.Encoding.ZENOH_BYTES

    def decode(self, sample: zenoh.Sample) -> bytes:
        return sample.payload.to_bytes()


BUILTIN_CODECS: list[Codec] = [BytesCodec()]
