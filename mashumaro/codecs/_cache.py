"""Weak cache for compiled codec conversion functions.

Cache keys never keep user supplied types, dialects, callbacks or codec
instances alive.  Cached values are weakly referenced and codecs keep at most
one strong reference to the template they use, so releasing a codec also
releases its generated code and everything that code references.
"""

import enum
import threading
import types
import typing
import weakref
from collections.abc import Mapping
from typing import Any

from mashumaro.core.const import Sentinel
from mashumaro.types import SerializationStrategy

_UNSHAREABLE = object()

_DIALECT_OPTIONS = (
    "omit_none",
    "omit_default",
    "serialize_by_alias",
    "namedtuple_as_dict",
    "no_copy_collections",
)


class _RefKey:
    """Compare referents by identity without keeping them alive."""

    __slots__ = ("_ref", "_id")

    def __init__(self, obj: Any):
        self._ref = weakref.ref(obj)
        self._id = id(obj)

    def __hash__(self) -> int:
        return self._id

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, _RefKey) or self._id != other._id:
            return False
        referent = self._ref()
        return referent is not None and referent is other._ref()

    def __repr__(self) -> str:
        return f"ref({self._id})"


def _weak_token(obj: Any) -> Any:
    try:
        return _RefKey(obj)
    except TypeError:
        return _UNSHAREABLE


_DESCRIPTOR_TYPES = (
    types.MemberDescriptorType,
    types.MethodDescriptorType,
    types.GetSetDescriptorType,
    types.WrapperDescriptorType,
)


def _opaque_token(value: Any) -> Any:
    if isinstance(value, staticmethod):
        return ("staticmethod", _value_token(value.__func__))
    if isinstance(value, classmethod):
        return ("classmethod", _value_token(value.__func__))
    if isinstance(value, _DESCRIPTOR_TYPES):
        owner = getattr(value, "__objclass__", None)
        return ("descriptor", _weak_token(owner), value.__name__)
    if (
        isinstance(value, types.BuiltinMethodType)
        and value.__self__ is not None
    ):
        return (
            "builtin_method",
            _weak_token(value.__self__),
            value.__name__,
        )
    return _UNSHAREABLE


def _contains_unshareable(token: Any) -> bool:
    if token is _UNSHAREABLE:
        return True
    if isinstance(token, tuple):
        return any(_contains_unshareable(item) for item in token)
    return False


def _items_token(items: Any) -> Any:
    tokens = tuple(
        (_value_token(key), _value_token(value)) for key, value in items
    )
    if _contains_unshareable(tokens):
        return _UNSHAREABLE
    return ("mapping", tuple(sorted(tokens, key=repr)))


def _value_token(value: Any) -> Any:
    if value is Ellipsis:
        return ("ellipsis",)
    if value is None:
        return ("none",)
    if isinstance(value, enum.Enum):
        return ("enum", _weak_token(type(value)), value.name)
    if isinstance(value, (str, bytes, int, float, complex, bool)):
        return ("primitive", type(value), value)
    if isinstance(value, tuple):
        tokens = tuple(_value_token(item) for item in value)
        if _contains_unshareable(tokens):
            return _UNSHAREABLE
        return ("tuple", tokens)
    if isinstance(value, list):
        tokens = tuple(_value_token(item) for item in value)
        if _contains_unshareable(tokens):
            return _UNSHAREABLE
        return ("list", tokens)
    if isinstance(value, (set, frozenset)):
        tokens = tuple(_value_token(item) for item in value)
        if _contains_unshareable(tokens):
            return _UNSHAREABLE
        return ("set", type(value), tuple(sorted(tokens, key=repr)))
    if isinstance(value, Mapping):
        return _items_token(value.items())
    if isinstance(value, type) or typing.get_origin(value) is not None:
        return _shape_token(value)
    if _is_opaque(value):
        return _opaque_token(value)
    return _weak_token(value)


def _is_opaque(value: Any) -> bool:
    if isinstance(value, (staticmethod, classmethod, *_DESCRIPTOR_TYPES)):
        return True
    return (
        isinstance(value, types.BuiltinMethodType)
        and value.__self__ is not None
    )


def _shape_token(shape_type: Any) -> Any:
    if shape_type is None:
        return ("none",)
    origin = typing.get_origin(shape_type)
    if origin is not None:
        origin_token = _value_token(origin)
        alias_token = _weak_token(type(shape_type))
        arg_tokens = tuple(
            _value_token(arg) for arg in typing.get_args(shape_type)
        )
        if (
            _contains_unshareable(origin_token)
            or _contains_unshareable(alias_token)
            or _contains_unshareable(arg_tokens)
        ):
            return _UNSHAREABLE
        return ("generic", alias_token, origin_token, arg_tokens)
    return _weak_token(shape_type)


def _strategy_value_token(value: Any) -> Any:
    if isinstance(value, SerializationStrategy):
        return ("strategy", _weak_token(value))
    if isinstance(value, Mapping):
        return _items_token(value.items())
    return _value_token(value)


def _dialect_structure_token(dialect: Any) -> Any:
    strategy = getattr(dialect, "serialization_strategy", None)
    if strategy is None:
        strategy_token: Any = None
    else:
        strategy_token = tuple(
            (_value_token(key), _strategy_value_token(value))
            for key, value in strategy.items()
        )
        if _contains_unshareable(strategy_token):
            return _UNSHAREABLE
        strategy_token = tuple(sorted(strategy_token, key=repr))
    options = tuple(
        (name, _value_token(getattr(dialect, name, Sentinel.MISSING)))
        for name in _DIALECT_OPTIONS
    )
    if _contains_unshareable(options):
        return _UNSHAREABLE
    return ("dialect", strategy_token, options)


def _dialect_token(dialect: Any) -> Any:
    if dialect is None:
        return None
    sources = getattr(dialect, "__mashumaro_merge_sources__", None)
    identity = _weak_token(dialect)
    structure = _dialect_structure_token(dialect)
    if structure is _UNSHAREABLE:
        # Distinct dialect classes may inject their own names into generated
        # code, so opaque configurations are only reused as the same class.
        return ("class", identity)
    if sources is not None:
        # Merge creates a fresh class each time.
        return ("merged", _weak_token(sources[0]), structure)
    return ("class", identity, structure)


def _make_key(
    kind: str,
    builder: Any,
    codec_obj: Any,
    shape_type: Any,
    func: Any,
) -> Any:
    key = (
        kind,
        _weak_token(type(builder)),
        _weak_token(type(codec_obj)),
        _shape_token(shape_type),
        builder.format_name,
        builder.allow_postponed_evaluation,
        _value_token(builder.decoder),
        _value_token(builder.encoder),
        _dialect_token(builder.dialect),
        _dialect_token(builder.default_dialect),
        _value_token(func),
    )
    if _contains_unshareable(key):
        return None
    return key


class CodecTemplate:
    __slots__ = (
        "_value",
        "_code",
        "_globals",
        "_codec_names",
        "_name",
        "_defaults",
        "_kwdefaults",
        "_closure",
        "_annotations",
        "_dict",
        "_module",
        "_qualname",
        "_doc",
        "__weakref__",
    )

    def __init__(self, value: Any, codec_obj: Any):
        codec_names: tuple[str, ...] = ()
        if isinstance(value, types.FunctionType):
            codec_names = tuple(
                name
                for name, item in value.__globals__.items()
                if item is codec_obj
            )
        self._codec_names = codec_names
        if codec_names:
            self._value = None
            self._code = value.__code__
            self._globals = {
                name: item
                for name, item in value.__globals__.items()
                if name not in codec_names
            }
            self._name = value.__name__
            self._defaults = value.__defaults__
            self._kwdefaults = value.__kwdefaults__
            self._closure = value.__closure__
            self._annotations = value.__annotations__
            self._dict = dict(value.__dict__)
            self._module = value.__module__
            self._qualname = value.__qualname__
            self._doc = value.__doc__
        else:
            self._value = value

    def materialize(self, codec_obj: Any) -> Any:
        if self._value is not None:
            return self._value
        globals_ = dict(self._globals)
        for name in self._codec_names:
            globals_[name] = codec_obj
        function = types.FunctionType(
            self._code,
            globals_,
            self._name,
            self._defaults,
            self._closure,
        )
        function.__kwdefaults__ = self._kwdefaults
        function.__annotations__ = dict(self._annotations)
        function.__dict__.update(self._dict)
        function.__module__ = self._module
        function.__qualname__ = self._qualname
        function.__doc__ = self._doc
        return function


_TEMPLATE_ATTR = "__mashumaro_codec_template__"
_CACHE: "weakref.WeakValueDictionary[Any, CodecTemplate]" = (
    weakref.WeakValueDictionary()
)
_LOCK = threading.RLock()


def get_template(
    kind: str,
    builder: Any,
    codec_obj: Any,
    shape_type: Any,
    func: Any,
) -> CodecTemplate | None:
    key = _make_key(kind, builder, codec_obj, shape_type, func)
    if key is None:
        return None
    with _LOCK:
        template = _CACHE.get(key)
        if template is not None:
            # Keep the entry alive while the caller binds it to a new codec.
            _CACHE[key] = template
        return template


def remember_template(
    kind: str,
    builder: Any,
    codec_obj: Any,
    shape_type: Any,
    func: Any,
) -> None:
    key = _make_key(kind, builder, codec_obj, shape_type, func)
    if key is None:
        return
    template = CodecTemplate(getattr(codec_obj, kind), codec_obj)
    with _LOCK:
        _CACHE[key] = template
    try:
        setattr(codec_obj, _TEMPLATE_ATTR, template)
    except AttributeError:  # pragma: no cover - codecs are not slotted
        pass


def bind_template(template: CodecTemplate, codec_obj: Any, kind: str) -> None:
    setattr(codec_obj, kind, template.materialize(codec_obj))
    try:
        setattr(codec_obj, _TEMPLATE_ATTR, template)
    except AttributeError:  # pragma: no cover - codecs are not slotted
        pass
