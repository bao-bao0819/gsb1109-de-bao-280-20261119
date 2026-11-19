"""Contains main apispec classes: `APISpec` and `BasePlugin`"""

from __future__ import annotations

import typing

from .core import APISpec
from .plugin import BasePlugin

if typing.TYPE_CHECKING:
    from .compat import (
        CompatibilityIssue,
        CompatibilityReport,
        compare_specs,
        find_breaking_changes,
        is_compatible,
    )

__all__ = [
    "APISpec",
    "BasePlugin",
    "CompatibilityIssue",
    "CompatibilityReport",
    "compare_specs",
    "find_breaking_changes",
    "is_compatible",
]

_COMPAT_NAMES = frozenset(
    {
        "CompatibilityIssue",
        "CompatibilityReport",
        "compare_specs",
        "find_breaking_changes",
        "is_compatible",
    }
)


def __getattr__(name: str) -> typing.Any:
    if name == "__version__":
        import importlib.metadata
        import warnings

        warnings.warn(
            "The '__version__' attribute is deprecated and will be removed in"
            " in a future version. Use feature detection or"
            " 'importlib.metadata.version(\"apispec\")' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return importlib.metadata.version("apispec")

    if name in _COMPAT_NAMES:
        from . import compat

        return getattr(compat, name)

    raise AttributeError(name)
