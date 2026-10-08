"""
Portable payload-filter grammar shared by every vector backend.

JSON only, no callbacks:

- ``{"field": value}``                      equality (scalars: str / int / float / bool / null)
- ``{"field": {"$in": [v1, v2]}}``          membership
- ``{"field": {"$gt": n}}`` (``$gte`` / ``$lt`` / ``$lte``)  ranges on numbers or strings
- ``{"field": {"$prefix": "abc"}}``         string prefix (case-sensitive)
- ``{"must": [f1, f2], "must_not": [f3]}``  AND / AND-NOT composition

Several operators under one field combine with AND, as do several fields in one
object. Payload fields are **top-level identifiers only** (no nested paths in v1).
Anything else raises ``UnsupportedFilter``, which the loopback door turns into a
400 ``unsupported_filter`` instead of guessing.

Semantics — identical on every backend, which the contract test-suite checks:

- **Types are strict.** A string never equals a number and a boolean never
  equals a number (``{"f": 1}`` does not match ``true``); ranges compare numbers
  with numbers and strings with strings (by code point).
- **Array fields match on any element**, each operator on its own:
  ``{"topics": "rag"}`` matches ``{"topics": ["agents", "rag"]}``. That is
  Qdrant's native rule, adopted by the Local store so a collection answers the
  same on either backend.
- ``{"field": None}`` means *missing or null*; it never matches an element.

Three implementations live here so tests can cross-check them: ``matches`` is
the reference semantics in Python, ``to_sqlite`` compiles a filter to a JSON1
``WHERE`` clause for the Local store, and ``to_qdrant`` compiles it to a Qdrant
filter. Where Qdrant has no exact equivalent (``$prefix``, string ranges, null
equality) ``to_qdrant`` sends a guaranteed **superset** — a subset under
``must_not`` — and marks the result inexact, so the adapter re-checks every hit
with ``matches`` and the answer stays exact.
"""

import re
from dataclasses import dataclass

from core.vectors.points import RESERVED_PREFIX

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
        if key.startswith(RESERVED_PREFIX):
            # The Qdrant adapter keeps its own bookkeeping under this prefix; a
            # filter on it would see that on Qdrant and nothing on Local.
            raise UnsupportedFilter(f"field names starting with {RESERVED_PREFIX!r} are reserved")
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


def _ops(cond):
    """A field's condition as ``(op, arg)`` pairs; a bare value is ``$eq``."""
    return list(cond.items()) if isinstance(cond, dict) else [("$eq", cond)]


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
        elif not all(_op_matches(payload.get(key), op, arg) for op, arg in _ops(value)):
            return False
    return True


def _op_matches(actual, op, arg):
    if op == "$in":
        return any(_op_matches(actual, "$eq", v) for v in arg)
    if op == "$eq" and arg is None:
        return actual is None
    values = actual if isinstance(actual, list) else [actual]
    return any(_scalar_matches(v, op, arg) for v in values)


def _scalar_matches(value, op, arg):
    if op == "$prefix":
        return isinstance(value, str) and value.startswith(arg)
    if _kind(value) != _kind(arg):
        return False
    if op == "$eq":
        return value == arg
    if op == "$gt":
        return value > arg
    if op == "$gte":
        return value >= arg
    if op == "$lt":
        return value < arg
    return value <= arg


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return "other"


# --------------------------------------------------------------------------- SQLite


def to_sqlite(flt, column="payload"):
    """Compile ``flt`` to ``(where_sql, params)`` over a JSON1 text column.

    Returns ``("1", [])`` for no filter so callers can always ``AND`` it in. Field
    names are validated identifiers, so inlining them in ``json_each`` /
    ``json_extract`` paths is safe; every value goes through a bound parameter.
    """
    flt = validate(flt)
    if flt is None:
        return "1", []
    clauses, params = [], []
    for key, value in flt.items():
        if key in ("must", "must_not"):
            for sub in value:
                sql, sub_params = to_sqlite(sub, column)
                clauses.append(f"({sql})" if key == "must" else f"NOT ({sql})")
                params.extend(sub_params)
        else:
            for op, arg in _ops(value):
                sql, op_params = _op_sql(key, op, arg, column)
                clauses.append(sql)
                params.extend(op_params)
    return " AND ".join(clauses), params


def _op_sql(field, op, arg, column):
    path = f"'$.{field}'"
    if op == "$in":
        non_null = [v for v in arg if v is not None]
        alternatives, params = [], []
        if non_null:
            preds = []
            for value in non_null:
                pred, pred_params = _value_pred("$eq", value)
                preds.append(f"({pred})")
                params.extend(pred_params)
            alternatives.append(_any_element(column, path, " OR ".join(preds)))
        if len(non_null) != len(arg):
            alternatives.append(f"json_extract({column}, {path}) IS NULL")
        return ("(" + " OR ".join(alternatives) + ")" if alternatives else "0"), params
    if op == "$eq" and arg is None:
        # json_extract is SQL NULL for a missing key and for JSON null (an array
        # or object comes back as its JSON text), which is exactly missing-or-null.
        return f"json_extract({column}, {path}) IS NULL", []
    pred, params = _value_pred(op, arg)
    return _any_element(column, path, pred), params


def _any_element(column, path, pred):
    """The field's scalar value — or any element of its array — satisfies ``pred``.

    ``json_each`` yields one row (key NULL) for a scalar, one row per element
    (integer key) for an array and none for a missing field. An object's member
    rows carry text keys and are excluded: nested paths are not in the grammar.
    """
    return (
        f"EXISTS (SELECT 1 FROM json_each({column}, {path}) AS je "
        f"WHERE (je.key IS NULL OR typeof(je.key) = 'integer') AND ({pred}))"
    )


def _value_pred(op, arg):
    """A predicate over ``je.type`` / ``je.value`` with strict typing.

    JSON1 reports ``true`` / ``false`` as the integers 1 / 0, so booleans are
    matched on ``je.type`` and numbers exclude them — otherwise ``{"f": 1}``
    would match ``true`` here and nowhere else.
    """
    if op == "$eq":
        if isinstance(arg, bool):
            return ("je.type = 'true'" if arg else "je.type = 'false'"), []
        if isinstance(arg, str):
            return "je.type = 'text' AND je.value = ?", [arg]
        return "je.type IN ('integer', 'real') AND je.value = ?", [arg]
    if op == "$prefix":
        # substr, not LIKE: LIKE ignores ASCII case, the grammar does not.
        return "je.type = 'text' AND substr(je.value, 1, ?) = ?", [len(arg), arg]
    kind = "je.type = 'text'" if isinstance(arg, str) else "je.type IN ('integer', 'real')"
    return f"{kind} AND je.value {RANGE_OPS[op]} ?", [arg]


# --------------------------------------------------------------------------- Qdrant


class _Const:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


MATCH_ALL = _Const("MATCH_ALL")
MATCH_NONE = _Const("MATCH_NONE")
_QDRANT_RANGE = {"$gt": "gt", "$gte": "gte", "$lt": "lt", "$lte": "lte"}


@dataclass(frozen=True)
class QdrantFilter:
    """What ``to_qdrant`` produced.

    ``filter`` is the Qdrant filter object (``None`` = no constraint);
    ``matches_nothing`` lets the adapter answer without a request; ``exact`` is
    False when the server sees a superset and every hit must be re-checked with
    ``matches``.
    """

    filter: dict | None
    exact: bool
    matches_nothing: bool = False


def to_qdrant(flt) -> QdrantFilter:
    """Compile ``flt`` to Qdrant's ``must`` / ``should`` / ``must_not`` + ``match`` / ``range``.

    Each node is built for a *polarity*: under ``must_not`` the condition that is
    negated has to be a subset of the truth, so the negation stays a superset.
    The top-level node is therefore always a superset of the exact answer, and an
    empty superset means nothing can match.
    """
    flt = validate(flt)
    if flt is None:
        return QdrantFilter(None, True)
    node, exact = _q_filter(flt, positive=True)
    if node is MATCH_NONE:
        return QdrantFilter(None, exact, matches_nothing=True)
    if node is MATCH_ALL:
        return QdrantFilter(None, exact)
    if isinstance(node, dict) and set(node) <= {"must", "should", "must_not"}:
        return QdrantFilter(node, exact)
    return QdrantFilter({"must": [node]}, exact)


def _q_filter(flt, positive):
    parts = []
    for key, value in flt.items():
        if key == "must":
            parts.extend(_q_filter(sub, positive) for sub in value)
        elif key == "must_not":
            for sub in value:
                node, exact = _q_filter(sub, not positive)
                parts.append((_q_not(node), exact))
        else:
            parts.extend(_q_op(key, op, arg, positive) for op, arg in _ops(value))
    return _q_and([node for node, _ in parts]), all(exact for _, exact in parts)


def _q_op(field, op, arg, positive):
    if op == "$in":
        parts = [_q_eq(field, value, positive) for value in arg]
        return _q_or([node for node, _ in parts]), all(exact for _, exact in parts)
    if op == "$eq":
        return _q_eq(field, arg, positive)
    if op == "$prefix":
        if arg == "":
            # Every string starts with "": no native condition says that.
            return (MATCH_ALL if positive else MATCH_NONE), False
        if positive:
            # On a field without a full-text index Qdrant's text match is an exact
            # substring match — a superset of "starts with".
            return {"key": field, "match": {"text": arg}}, False
        # A value equal to the prefix certainly starts with it: a safe subset.
        return {"key": field, "match": {"value": arg}}, False
    if isinstance(arg, str):
        # Qdrant ranges are numeric; string ranges are checked in PyRun.
        return (MATCH_ALL if positive else MATCH_NONE), False
    return {"key": field, "range": {_QDRANT_RANGE[op]: arg}}, True


def _q_eq(field, value, positive):
    if value is None:
        # Missing-or-null: is_empty also matches [] (a superset); is_null only an
        # existing null (a subset).
        return ({"is_empty": {"key": field}} if positive else {"is_null": {"key": field}}), False
    if isinstance(value, (bool, str)):
        return {"key": field, "match": {"value": value}}, True
    # match takes integers only; a closed range matches ints and floats alike.
    return {"key": field, "range": {"gte": value, "lte": value}}, True


def _q_and(nodes):
    nodes = [n for n in nodes if n is not MATCH_ALL]
    if any(n is MATCH_NONE for n in nodes):
        return MATCH_NONE
    if not nodes:
        return MATCH_ALL
    return nodes[0] if len(nodes) == 1 else {"must": nodes}


def _q_or(nodes):
    nodes = [n for n in nodes if n is not MATCH_NONE]
    if any(n is MATCH_ALL for n in nodes):
        return MATCH_ALL
    if not nodes:
        return MATCH_NONE
    return nodes[0] if len(nodes) == 1 else {"should": nodes}


def _q_not(node):
    if node is MATCH_ALL:
        return MATCH_NONE
    if node is MATCH_NONE:
        return MATCH_ALL
    return {"must_not": [node]}
