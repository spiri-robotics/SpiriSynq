# Liveliness

## Is the owner alive?

Every authoritative object announces that it's alive, using a Zenoh liveliness token. Each
object has its own token, including objects nested in other objects. Mirrors don't announce
themselves.

A mirror reports whether its owner is alive, and emits a signal when the owner goes away or comes
back:

```python
mirror = Counter.from_topic("myhost/counter")
print(mirror.synq_alive)

mirror.synq_signal_alive.connect(lambda alive: print("owner alive:", alive))
```

That is all a mirror does. When the owner goes away, the mirror keeps its last known state and
stays declared. When the owner comes back, the mirror doesn't fetch its state again on its own.
Your callback decides what to do.

### How quickly a departure is noticed

- The owner closes the object, or exits normally: at once.
- The owner's process is killed: at once, because its network connection closes.
- The owner's process hangs, or the network between you fails: after Zenoh's lease runs out,
  about 10 seconds with the default configuration. The lease is set in the Zenoh configuration.

## Is anyone listening?

An owner can tell whether any other process is subscribed to its object, so it can avoid work
nobody will see. It learns this from Zenoh's matching status on its publishers, not from tokens.

```python
battery.synq_has_listeners()           # anyone subscribed to any field
battery.synq_has_listeners("voltage")  # anyone subscribed to this field
```

An owner that accepts incoming writes on a field is itself subscribed to it, so for that field
the answer is always yes. See
[Publishing only when someone is listening](objects.md#publishing-only-when-someone-is-listening).

## Discovery

Use `session.list_topics()` to find the objects on the network. Liveliness tells you which
objects are alive right now; each object's metadata tells you its types and owner.

```python
from SpiriSynq.session import current_session

for metadata in current_session.get().list_topics(type_filter="Counter"):
    print(metadata["topic"], metadata["classes"])
```
