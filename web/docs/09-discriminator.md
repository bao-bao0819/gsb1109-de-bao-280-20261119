---
title: Discriminator
group: Advanced
---

# Discriminator

`Discriminator` chooses the concrete dataclass variant when deserializing a union or class hierarchy. A tagged discriminator performs direct lookup and gives the wire format an explicit compatibility contract; an untagged discriminator tries candidate shapes in sequence.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `field` | `None` | Input key containing the variant tag |
| `include_subtypes` | `False` | Include descendants of annotated/configured classes |
| `include_supertypes` | `False` | Include the listed/annotated classes as fallback variants |
| `variant_tagger_fn` | `None` | Compute one tag or a list of tags from a variant class |
| `variant_tags` | `None` | Per-variant accepted input tags; the first one is also the tag written back on serialization |

At least one of `include_subtypes` or `include_supertypes` must be true. Otherwise construction raises `ValueError`.

## Tagged class hierarchy

Use [`Annotated`](https://docs.python.org/3/library/typing.html#typing.Annotated) when only one field needs polymorphic behavior:

```python
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Annotated, Literal

from mashumaro import DataClassDictMixin
from mashumaro.types import Discriminator


@dataclass
class ClientEvent:
    pass


@dataclass
class Connected(ClientEvent):
    type: Literal["connected"] = "connected"
    client_ip: IPv4Address = IPv4Address("127.0.0.1")


@dataclass
class Disconnected(ClientEvent):
    type: Literal["disconnected"] = "disconnected"
    client_ip: IPv4Address = IPv4Address("127.0.0.1")


@dataclass
class Batch(DataClassDictMixin):
    events: list[
        Annotated[
            ClientEvent,
            Discriminator(field="type", include_subtypes=True),
        ]
    ]


batch = Batch.from_dict(
    {
        "events": [
            {"type": "connected", "client_ip": "10.0.0.42"},
            {"type": "disconnected", "client_ip": "10.0.0.43"},
        ]
    }
)

assert isinstance(batch.events[0], Connected)
assert isinstance(batch.events[1], Disconnected)
```

The tag can come from a class attribute or a field declared with [`ClassVar`](https://docs.python.org/3/library/typing.html#typing.ClassVar), [`Final`](https://docs.python.org/3/library/typing.html#typing.Final), [`Literal`](https://docs.python.org/3/library/typing.html#typing.Literal), or a string enum. `Literal` and [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum) fields are especially convenient because the tag is also naturally included in serialization.

By default, only `Final`, `Literal`, and `StrEnum` tag fields are processed
during serialization. An unannotated class attribute or `ClassVar` can select
a variant during deserialization, but it is not emitted as a serialized field.

> [!IMPORTANT]
> The discriminator attribute is looked up in each descendant's own class namespace rather than inherited. A descendant that does not define it is skipped as a tagged variant, but its descendants are still considered independently and remain eligible if they define the attribute themselves. This prevents an inherited tag from accidentally identifying multiple variants.

## Class-level discriminator

Put the discriminator in the base model's `Config` when every use of that base class should deserialize polymorphically:

```python
from dataclasses import dataclass
from typing import Literal

from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig
from mashumaro.types import Discriminator


@dataclass
class Event(DataClassDictMixin):
    class Config(BaseConfig):
        discriminator = Discriminator(
            field="kind", include_subtypes=True
        )


@dataclass
class Created(Event):
    kind: Literal["created"] = "created"
    object_id: int = 0


@dataclass
class Deleted(Event):
    kind: Literal["deleted"] = "deleted"
    object_id: int = 0


event = Event.from_dict({"kind": "deleted", "object_id": 42})
assert event == Deleted(object_id=42)
```

This works for nested `Event` fields and direct `Event.from_dict()` calls.

> [!NOTE]
> A class-level discriminator is activated for a class only when that class defines `Config` in its own namespace and the effective config provides a discriminator, either directly or through config inheritance. Other descendants are deserialized as concrete types rather than becoming additional polymorphic entry points. This ensures that, after `Event.from_dict()` selects `Deleted`, the selected class does not run the same discriminator again.

If an intermediate descendant should also dispatch among its own descendants, opt in explicitly by defining its own `Config`. An empty subclass is sufficient to reuse the parent configuration:

```python
@dataclass
class AuditedEvent(Event):
    class Config(Event.Config):
        pass
```

Do not set `include_supertypes=True` on a class-level discriminator: selecting the configured base as its own fallback would recurse. Use `Annotated` on a union/field when supertypes are needed.

## Discriminated unions

Use a union when variants do not share a useful base class or only a subset of a hierarchy is legal:

```python
from dataclasses import dataclass
from typing import Annotated, Literal

from mashumaro import DataClassDictMixin
from mashumaro.types import Discriminator


@dataclass
class Email:
    channel: Literal["email"] = "email"
    address: str = ""


@dataclass
class SMS:
    channel: Literal["sms"] = "sms"
    number: str = ""


Notification = Annotated[
    Email | SMS,
    Discriminator(field="channel", include_supertypes=True),
]


@dataclass
class Job(DataClassDictMixin):
    notification: Notification


job = Job.from_dict(
    {"notification": {"channel": "sms", "number": "+381..."}}
)
assert isinstance(job.notification, SMS)
```

For a tagged union, variant selection is direct rather than “try every union branch”. This is faster and avoids accidental success when models have overlapping fields.

## Untagged shape matching

Omit `field` when you have to read an existing untagged format whose variants
are distinguishable by their required fields. For example, a legacy alerting
configuration might identify notification targets only by their shape:

```python
from dataclasses import dataclass
from typing import Annotated

from mashumaro import DataClassDictMixin
from mashumaro.types import Discriminator


@dataclass
class NotificationTarget:
    name: str


@dataclass
class EmailTarget(NotificationTarget):
    email: str


@dataclass
class WebhookTarget(NotificationTarget):
    url: str


@dataclass
class AlertingConfig(DataClassDictMixin):
    targets: list[
        Annotated[
            NotificationTarget,
            Discriminator(include_subtypes=True),
        ]
    ]


config = AlertingConfig.from_dict(
    {
        "targets": [
            {"name": "operations inbox", "email": "ops@example.com"},
            {"name": "incident handler", "url": "https://example.com/hook"},
        ]
    }
)

assert config == AlertingConfig(
    targets=[
        EmailTarget(name="operations inbox", email="ops@example.com"),
        WebhookTarget(
            name="incident handler", url="https://example.com/hook"
        ),
    ]
)
```

Shape matching also works with explicitly listed union variants. This is useful
when the listed classes have descendants that should take priority over their
parents:

```python
@dataclass
class TemplatedEmailTarget(EmailTarget):
    template: str


NotificationTargetUnion = Annotated[
    EmailTarget | WebhookTarget,
    Discriminator(
        include_subtypes=True,
        include_supertypes=True,
    ),
]


@dataclass
class AlertingConfig(DataClassDictMixin):
    targets: list[NotificationTargetUnion]


config = AlertingConfig.from_dict(
    {
        "targets": [
            {
                "name": "weekly digest",
                "email": "team@example.com",
                "template": "digest.html",
            }
        ]
    }
)

assert isinstance(config.targets[0], TemplatedEmailTarget)
```

With a plain `EmailTarget | WebhookTarget`, `EmailTarget` is attempted first and
can accept this payload without preserving `template`. The discriminator
discovers `TemplatedEmailTarget` through `include_subtypes=True` and attempts it
before the explicitly listed parent classes. `include_supertypes=True` keeps
those listed classes as fallback candidates.

`email` and `url` make the two shapes distinct. Mashumaro attempts candidate
variants until one deserializes, so this is slower than tag lookup and can be
ambiguous when classes have similar required fields. Use an explicit `channel`
tag whenever you control the contract.

## Fallback to a supertype

Enable both directions on an `Annotated` discriminator to prefer a known subtype and fall back to the base model for a forward-compatible payload:

```python
@dataclass
class AlertingConfig(DataClassDictMixin):
    targets: list[
        Annotated[
            NotificationTarget,
            Discriminator(
                include_subtypes=True,
                include_supertypes=True,
            ),
        ]
    ]
```

Subtypes are attempted first; supertypes are attempted afterward. An input with
only `{"name": "manual escalation"}` can therefore become
`NotificationTarget("manual escalation")`, while a payload containing `email`
still selects `EmailTarget`.

> [!WARNING]
> This fallback is useful for additive evolution, but it also hides unknown variants. If unknown kinds must be rejected, use a tagged discriminator without a broad supertype fallback.

## Custom tag generation

`variant_tagger_fn` receives each variant class and returns its tag:

```python
def class_name_tag(cls):
    return cls.__name__.removesuffix("Event").lower()


class Config(BaseConfig):
    discriminator = Discriminator(
        field="type",
        include_subtypes=True,
        variant_tagger_fn=class_name_tag,
    )
```

Return a list to accept multiple tags for one variant during migrations:

```python
def compatible_tags(cls):
    current = cls.__name__.removesuffix("Event").lower()
    return [current, f"v1:{current}"]
```

Keep tag generation deterministic and collision-free. A tagger maps classes to accepted input tags; it does not automatically inject a missing tag into serialized output. Add a field, hook, or explicit strategy when output must contain it.

## Migrating tags with `variant_tags`

`variant_tags` declares, per variant, every tag accepted on input and the single tag written back on output. It needs a tagged discriminator (`field` set). Map each variant to a list or tuple of tags: the first tag is the current tag emitted on serialization, while the remaining tags are legacy aliases accepted only on input.

Keys may be variant classes or, inside a base class `Config` where subclasses do not exist yet, their names as strings:

```python
from dataclasses import dataclass
from typing import Annotated, Literal, Union

from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig
from mashumaro.types import Discriminator


@dataclass
class Device(DataClassDictMixin):
    class Config(BaseConfig):
        discriminator = Discriminator(
            field="type",
            include_subtypes=True,
            variant_tags={
                # new tag first, legacy tag accepted on input only
                "Thermo": ("thermo", "thermo-legacy"),
                "Pressure": ("pressure",),
            },
        )


@dataclass
class Thermo(Device):
    value: int = 0
    type: Literal["thermo", "thermo-legacy"] = "thermo"


@dataclass
class Pressure(Device):
    value: int = 0
    type: Literal["pressure"] = "pressure"


# A legacy payload is decoded to Thermo ...
legacy = Device.from_dict({"type": "thermo-legacy", "value": 7})
assert isinstance(legacy, Thermo)

# ... but serialization always writes the current tag.
assert legacy.to_dict() == {"value": 7, "type": "thermo"}

# The written value reads back as the same variant.
assert isinstance(Device.from_dict(legacy.to_dict()), Thermo)
```

When a variant keeps a `Literal` tag field, widen that literal to cover every tag the variant accepts, so reading either spelling back stays valid.

Tags are the only evidence used to choose a variant; matching never falls back to field shape. An unknown tag therefore raises `SuitableVariantNotFoundError` even when the rest of the payload matches a variant exactly.

### Independent flows may reuse a spelling

Each discriminator builds its own tag-to-variant map. Two independent data flows can legally use the same tag spelling for different variants; the resolution stays local to each flow. Within a single flow, however, two variants must not declare the same tag. That configuration raises `DuplicatedVariantTagError` before any variant is selected, even for a payload that only carries an unrelated legal tag, and regardless of the order in which the variants are defined.

## Error behavior

| Situation | Exception |
|---|---|
| Configured tag key is missing | `MissingDiscriminatorError` |
| Tag has no registered variant | `SuitableVariantNotFoundError` |
| Same tag declared by two variants of one flow | `DuplicatedVariantTagError` |
| Untagged candidates all fail inside a field | Usually wrapped as `InvalidFieldValue` |
| Neither subtype nor supertype inclusion enabled | `ValueError` when creating `Discriminator` |
| `variant_tags` used without a `field`, or with invalid keys/values | `ValueError` when creating `Discriminator` |

Catch these exceptions at an input boundary to produce a stable API error. Their attributes expose the field/tag or variant information; see [Errors and Troubleshooting](#/docs/errors-and-troubleshooting).

## Best practices

- Prefer a `Literal` or `StrEnum` dataclass field for a self-serializing tag.
- Keep tag values stable even if Python class names change.
- Use `Annotated` for local union behavior and `Config` for hierarchy-wide behavior.
- Avoid untagged matching when variants overlap or the list may grow large.
- Test missing, unknown, legacy, and duplicate tags explicitly.
