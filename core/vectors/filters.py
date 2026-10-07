"""
Portable payload-filter grammar shared by every vector backend.

JSON only, no callbacks:

- ``{"field": value}``                      equality (scalars: str / int / float / bool / null)
- ``{"field": {"$in": [v1, v2]}}``          membership
- ``{"field": {"$gt": n}}`` (``$gte`` / ``$lt`` / ``$lte``)  ranges on numbers or strings
- ``{"field": {"$prefix": "abc"}}``         string prefix
- ``{"must": [f1, f2], "must_not": [f3]}``  AND / AND-NOT composition

Several operators under one field combine with AND, as do several fields in one
object. Payload fields are **top-level identifiers only** (no nested paths in v1).
Both backends implement exactly this grammar; anything else raises
``UnsupportedFilter``, which the loopback door (Stage 2) turns into a 400
``unsupported_filter`` instead of guessing.

Two evaluators live here so they can be cross-checked in tests: ``to_sqlite``
compiles a filter to a JSON1 ``WHERE`` clause for the local store, and
``matches`` evaluates it in Python against one payload dict.
"""

import re

FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RANGE_OPS = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}
_SCALARS = (str, int, float, bool, type(None))


class UnsupportedFilter(ValueError):
    """The filter uses a shape or operator outside the portable grammar."""


def validate(flt):
    """Return ``flt`` unchanged when it is a valid filter, ``None`` for "no filter".

    Raises ``UnsupportedFilter`` with a message that names the offending part.
    """
    if flt is None or flt == {}:
        return None
    if not isinstance(flt, dict):
        raise UnsupportedFilter("filter must be a JSON object")
    for key, value in flt.items():
        if key in ("must", "must_not"):
            if not isinstance(value, list):
                raise UnsupportedFilter(f"'{key}' takes a list of filters")
            for sub in value:
                if not isinstance(sub, dict) or not sub:
                    raise UnsupportedFilter(f"'{key}' entries must be non-empty filter objects")
                validate(sub)
            continue
        if not isinstance(key, str) or not FIELD_RE.match(key):
            raise UnsupportedFilter(
                f"unsupported field name {key!r} (top-level identifiers only, no nested paths)"
            )
        _validate_condition(key, value)
    return flt


def _validate_condition(field, cond):
    if isinstance(cond, dict):
        if not cond:
            raise UnsupportedFilter(f"empty condition for {field!r}")
        for op, arg in cond.items():
            if op == "$in":
                if not isinstance(arg, list) or not all(isinstance(v, _SCALARS) for v in arg):
                    raise UnsupportedFilter("'$in' takes a list of scalar values")
            elif op in RANGE_OPS:
                if isinstance(arg, bool) or not isinstance(arg, (int, float, str)):
                    raise UnsupportedFilter(f"'{op}' takes a number or a string")
            elif op == "$prefix":
                if not isinstance(arg, str):
                    raise UnsupportedFilter("'$prefix' takes a string")
            else:
                raise UnsupportedFilter(f"unsupported operator {op!r}")
    elif isinstance(cond, (list, tuple)):
        raise UnsupportedFilter(f"list equality for {field!r} is not supported; use '$in'")
    elif not isinstance(cond, _SCALARS):
        raise UnsupportedFilter(f"unsupported value type for {field!r}")


# --------------------------------------------------------------------------- SQLite


def to_sqlite(flt, column="payload"):
    """Compile ``flt`` to ``(where_sql, params)`` over a JSON1 text column.

    Returns ``("1", [])`` for no filter so callers can always ``AND`` it in. Field
    names are validated identifiers, so inlining them in ``json_extract`` paths
    is safe; every value goes through a bound parameter.
    """
    flt = validate(flt)
    if flt is None:
        return "1", []
    clauses, params = [], []
    for key, value in flt.items():
        if key == "must":
            for sub in value:
                sql, sub_params = to_sqlite(sub, column)
                clauses.append(f"({sql})")
                params.extend(sub_params)
        elif key == "must_not":
            for sub in value:
                sql, sub_params = to_sqlite(sub, column)
                clauses.append(f"NOT ({sql})")
                params.extend(sub_params)
        else:
            sql, sub_params = _condition_sql(key, value, column)
            clauses.append(sql)
            params.extend(sub_params)
    return " AND ".join(clauses), params


def _condition_sql(field, cond, column):
    expr = f"json_extract({column}, '$.{field}')"
    if not isinstance(cond, dict):
        if cond is None:
            return f"{expr} IS NULL", []
        return f"{expr} = ?", [_bind(cond)]

    parts, params = [], []
    for op, arg in cond.items():
        if op == "$in":
            non_null = [v for v in arg if v is not None]
            alternatives = []
            if non_null:
                alternatives.append(f"{expr} IN ({', '.join('?' * len(non_null))})")
                params.extend(_bind(v) for v in non_null)
            if len(non_null) != len(arg):
                alternatives.append(f"{expr} IS NULL")
            parts.append("(" + " OR ".join(alternatives) + ")" if alternatives else "0")
        elif op in RANGE_OPS:
            parts.append(f"{expr} {RANGE_OPS[op]} ?")
            params.append(arg)
        elif op == "$prefix":
            parts.append(f"{expr} LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(arg))
    return "(" + " AND ".join(parts) + ")", params


def _bind(value):
    # json_extract yields 1 / 0 for JSON true / false; bind booleans the same way.
    if isinstance(value, bool):
        return int(value)
    return value


def _like_prefix(prefix):
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


# --------------------------------------------------------------------------- Python


def matches(payload, flt):
    """Evaluate ``flt`` against one payload dict (the reference semantics)."""
    flt = validate(flt)
    if flt is None:
        return True
    payload = payload or {}
    for key, value in flt.items():
        if key == "must":
            if not all(matches(payload, sub) for sub in value):
                return False
        elif key == "must_not":
            if any(matches(payload, sub) for sub in value):
                return False
        elif not _condition_matches(payload.get(key), value):
            return False
    return True


def _condition_matches(actual, cond):
    if not isinstance(cond, dict):
        return _equal(actual, cond)
    for op, arg in cond.items():
        if op == "$in":
            if not any(_equal(actual, v) for v in arg):
                return False
        elif op in RANGE_OPS:
            if actual is None or isinstance(actual, bool):
                return False
            if isinstance(actual, str) != isinstance(arg, str):
                return False
            if not _compare(actual, arg, op):
                return False
        elif op == "$prefix":
            if not isinstance(actual, str) or not actual.startswith(arg):
                return False
    return True


def _equal(actual, expected):
    if isinstance(actual, str) != isinstance(expected, str):
        return False
    return actual == expected


def _compare(actual, arg, op):
    if op == "$gt":
        return actual > arg
    if op == "$gte":
        return actual >= arg
    if op == "$lt":
        return actual < arg
    return actual <= arg
