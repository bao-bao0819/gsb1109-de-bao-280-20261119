from __future__ import annotations

from typing import Any, Iterator, TYPE_CHECKING

from ._bits import Tibs
from ._tibs import tibs_from_any

if TYPE_CHECKING:
    from ._bits import TibsLike
    from ._fieldtype import FieldType

__all__ = ["Receipt"]


class Receipt:
    """
    A record of where a single :class:`FieldType` was actually read from during parsing.

    A receipt tree is returned by :meth:`FieldType.parse_with_receipt`. Each node stores the
    ``fieldtype`` that was parsed, the ``start`` and ``end`` bit positions it occupied in the
    input, the ``value`` that was read, and the ``children`` parsed beneath it.

    Only fields that were actually parsed appear in the tree: for an :class:`If` field just the
    taken branch is recorded, and for a :class:`Repeat` field there is one child per iteration
    that really occurred. Branches that were not taken have no node, and so no fabricated span.
    """

    def __init__(self, fieldtype: FieldType, start: int) -> None:
        self.fieldtype = fieldtype
        self.start = start
        self.end: int | None = None
        self.children: list[Receipt] = []
        self._value: Any = None

    @property
    def name(self) -> str:
        """The name of the parsed field, or an empty string if it is unnamed."""
        return self.fieldtype.name or ""

    @property
    def value(self) -> Any:
        """The value of the field, captured at the moment it was parsed."""
        return self._value

    @property
    def bit_length(self) -> int:
        """The number of bits the field occupied."""
        if self.end is None:
            raise ValueError("The parse of this field did not complete, so it has no bit length.")
        return self.end - self.start

    def extract(self, b: TibsLike) -> Tibs:
        """
        Slice the bits this field was parsed from out of the given input.

        :param b: The bits (typically the original parsed input) to slice.
        :return: The ``self.start`` to ``self.end`` slice as a :class:`Tibs`.
        """
        if self.end is None:
            raise ValueError("The parse of this field did not complete, so it has no span to extract.")
        return tibs_from_any(b)[self.start : self.end]

    def walk(self) -> Iterator[Receipt]:
        """Iterate over this node and all its descendants, pre-order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def _line(self) -> str:
        kind = type(self.fieldtype).__name__
        name_str = f" '{self.name}'" if self.name else ""
        end_str = "?" if self.end is None else str(self.end)
        s = f"{kind}{name_str} [{self.start}:{end_str}]"
        if not self.children and self._value is not None:
            s += f" = {self._value!r}"
        return s

    def __str__(self) -> str:
        lines = []
        def visit(node: Receipt, depth: int) -> None:
            lines.append("    " * depth + node._line())
            for child in node.children:
                visit(child, depth + 1)
        visit(self, 0)
        return "\n".join(lines)

    def __repr__(self) -> str:
        end_str = "?" if self.end is None else str(self.end)
        return f"Receipt({type(self.fieldtype).__name__}, {self.start}:{end_str})"


class _ReceiptRecorder:
    """Collects Receipt nodes while a parse is in progress. See FieldType.parse_with_receipt."""

    def __init__(self) -> None:
        self.roots: list[Receipt] = []
        self._stack: list[Receipt] = []
        # Disabled while snapshotting values, as that can trigger nested parses (e.g. Repeat
        # values are unpacked from their stored bits) which must not be recorded.
        self.enabled = True

    def open(self, node: Receipt) -> None:
        if self._stack:
            self._stack[-1].children.append(node)
        else:
            self.roots.append(node)
        self._stack.append(node)

    def abort(self, node: Receipt) -> None:
        self._stack.pop()

    def close(self, node: Receipt, bits_used: int) -> None:
        self._stack.pop()
        node.end = node.start + bits_used
        self.enabled = False
        try:
            node._value = node.fieldtype._get_value()
        except (AttributeError, ValueError):
            node._value = None
        finally:
            self.enabled = True
