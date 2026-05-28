"""Typed exception hierarchy for Hyperion.

Catching HyperionError catches everything the engine raises intentionally.
Subclasses let callers (and LLM agents) distinguish the kind of failure
without parsing English error strings.
"""


class HyperionError(Exception):
    """Base class for all intentional Hyperion exceptions."""


# ── Parsing ───────────────────────────────────────────────────────────────────

class ParseError(HyperionError, ValueError):
    """SQL could not be parsed. Inherits ValueError for backward compatibility."""


# ── Schema / DDL ──────────────────────────────────────────────────────────────

class SchemaError(HyperionError):
    """An object referenced in DDL or DML does not exist or conflicts with one
    that does."""


class NoSuchTableError(SchemaError):
    """A table (or view) name used in a statement does not exist in the catalog."""


class NoSuchColumnError(SchemaError):
    """A column name used in a statement does not exist in the table schema."""


class NoSuchIndexError(SchemaError):
    """An index name referenced in a statement does not exist."""


class TableExistsError(SchemaError):
    """A CREATE TABLE or RENAME TABLE would create a duplicate name."""


class ColumnExistsError(SchemaError):
    """An ALTER TABLE ADD/RENAME COLUMN would create a duplicate column name."""


class IndexExistsError(SchemaError):
    """A CREATE INDEX would create a duplicate index name."""


# ── Constraints ───────────────────────────────────────────────────────────────

class ConstraintError(HyperionError):
    """A row violates a table constraint."""


class UniqueConstraintError(ConstraintError):
    """A UNIQUE or PRIMARY KEY constraint was violated."""


class NotNullConstraintError(ConstraintError):
    """A NOT NULL constraint was violated."""


class CheckConstraintError(ConstraintError):
    """A CHECK constraint expression evaluated to false."""


class ForeignKeyConstraintError(ConstraintError):
    """A FOREIGN KEY constraint was violated."""


# ── Data / type errors ────────────────────────────────────────────────────────

class DataError(HyperionError):
    """A value could not be coerced to the required type, or overflowed."""


# ── Transaction state ─────────────────────────────────────────────────────────

class TransactionError(HyperionError):
    """A transaction operation was issued in an invalid state."""


# ── Authorization ─────────────────────────────────────────────────────────────

class AuthorizationError(HyperionError):
    """An operation was denied by the authorizer callback."""


# ── Internal ──────────────────────────────────────────────────────────────────

class InternalError(HyperionError):
    """An unexpected internal engine error. Should not reach users in normal
    operation — if raised, it indicates a bug in Hyperion."""
