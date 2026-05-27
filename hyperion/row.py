"""Row factory types for pluggable cursor row format."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cursor import Cursor


class Row:
    """Named-access row: supports row["col"], row[0], and iteration over values.

    Use as: db.row_factory = Row
    """

    __slots__ = ("_keys", "_values")

    def __init__(self, cursor: "Cursor", row_dict: dict) -> None:
        if cursor.description:
            self._keys: tuple[str, ...] = tuple(d[0] for d in cursor.description)
        else:
            self._keys = tuple(row_dict.keys())
        self._values: tuple = tuple(row_dict.get(k) for k in self._keys)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._keys.index(key)]
        except ValueError:
            raise KeyError(key)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return "<Row {}>".format(dict(zip(self._keys, self._values)))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented


def dict_factory(cursor: "Cursor", row: dict) -> dict:
    """Return the row as-is (dict). This is the default behaviour."""
    return row


def tuple_factory(cursor: "Cursor", row: dict) -> tuple:
    """Return the row as a plain tuple of values in description order."""
    if cursor.description:
        return tuple(row.get(d[0]) for d in cursor.description)
    return tuple(row.values())
