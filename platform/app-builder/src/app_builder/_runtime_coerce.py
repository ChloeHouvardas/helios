"""The value-coercion rules, in exactly one place.

This module exists to prevent a drift bug that is easy to write and hard to see.
Two things need the same rules:

* the `store.py` generated into each app, which coerces values at write time;
* `seed_from_json`, which coerces fixture values at load time.

If each had its own copy, a fixture could be accepted at seed time and refused at
write time — or, worse, the reverse: refused at seed time and silently accepted
later, which is how a wrong number gets into a database.

So there is one implementation, here. `schema.py` embeds this file's source text
into the generated `store.py` via `inspect.getsource`, which means the generated
app is still standalone (it cannot import `app_builder`) while the two paths are
guaranteed identical, because they are literally the same code.

Everything here is stdlib-only and self-contained for that reason: the source text
is dropped into a generated module that has no access to this package.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["StoreError", "coerce_value"]


class StoreError(ValueError):
    """A value the store will not write.

    Named rather than coerced, because a silent conversion is how a wrong number
    arrives without an error — and in a financial app that is the worst failure
    mode available.
    """


def coerce_value(declared: str, entity: str, field: str, value: Any) -> Any:
    """Coerce one value to what its column holds, or raise `StoreError`.

    `declared` is the spec field type. Every branch here is a value that would
    otherwise be written in a form a SQL CHECK cannot catch or a reader would
    misparse.
    """
    if value is None:
        return None

    if declared == "decimal":
        # The float refusal, and the reason it lives here rather than in the DDL:
        # SQLite applies column affinity BEFORE a CHECK runs, so binding the float
        # 12.5 into a TEXT column arrives as the string '12.5' and passes every
        # constraint. No pure-SQL rule can tell a decimal string from a float that
        # happens to stringify. `Decimal(0.1)` is not a fix either — it bakes in
        # binary rounding at the moment of conversion.
        if isinstance(value, bool):
            raise StoreError(
                f"{entity}.{field} is a decimal field and got {value!r}; bool is an "
                "int subclass and must not be read as money")
        if isinstance(value, float):
            raise StoreError(
                f"{entity}.{field} is a decimal field and got the float {value!r}; "
                "money is never a float. Pass a Decimal, an int, or a decimal "
                'string such as "12.50".')
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, int):
            return str(value)
        text = str(value).strip()
        try:
            # Normalise through Decimal so '  12.50 ' and '12.50' store alike,
            # and so a non-decimal string is rejected here with the field named
            # rather than by a CHECK constraint that names only the column.
            return str(Decimal(text))
        except InvalidOperation:
            raise StoreError(
                f"{entity}.{field} is a decimal field and got {value!r}, which is "
                'not a decimal string; write e.g. "12.50"') from None

    if declared == "integer":
        if isinstance(value, bool):
            raise StoreError(
                f"{entity}.{field} is an integer field and got {value!r}; bool is "
                "an int subclass and must not be stored as 0 or 1")
        if isinstance(value, float):
            if value != int(value):
                raise StoreError(
                    f"{entity}.{field} is an integer field and got the float "
                    f"{value!r}; pass a whole number, or use a decimal field")
            return int(value)
        if isinstance(value, int):
            return value
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            raise StoreError(
                f"{entity}.{field} is an integer field and got {value!r}; write a "
                "whole number, or use a decimal field for money") from None

    if declared == "boolean":
        # SQLite has no boolean type; BOOLEAN affinity is INTEGER, so 0/1 is what
        # gets stored. Accept the Python spellings rather than demanding 0 and 1.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1"):
                return 1
            if lowered in ("false", "no", "0", ""):
                return 0
            raise StoreError(
                f"{entity}.{field} is a boolean field and got {value!r}; write "
                "true or false")
        return 1 if value else 0

    if declared == "json":
        if isinstance(value, str):
            try:
                json.loads(value)
            except json.JSONDecodeError as exc:
                raise StoreError(
                    f"{entity}.{field} is a json field and got {value!r}, which is "
                    f"not valid JSON ({exc}); write a JSON document as a string") from None
            return value
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise StoreError(
                f"{entity}.{field} is a json field and got {value!r}, which cannot "
                f"be serialised ({exc})") from None

    if declared == "date":
        if isinstance(value, datetime):
            # A datetime in a date column would store a value the CHECK rejects.
            # Truncating would silently change the meaning, so say which it is.
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise StoreError(
                f"{entity}.{field} is a date field and got {value!r}; write an ISO "
                'date such as "2026-10-15"') from None

    if declared == "datetime":
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day).isoformat()
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            raise StoreError(
                f"{entity}.{field} is a datetime field and got {value!r}; write an "
                'ISO timestamp such as "2026-10-15T09:30:00"') from None

    # string, and anything the spec model would already have refused.
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value)
