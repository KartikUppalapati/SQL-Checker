"""Syntax gate using PostgreSQL's own parser (libpg_query via pglast). No execution, no network."""
import json

from pglast.parser import parse_sql_json


class Invalid(Exception):
    """The prediction is not exactly one valid PostgreSQL query."""


def parse_tree(sql: str):
    """Parse with the PostgreSQL grammar; return the location-free parse tree of the single
    statement. Raises Invalid if the text is not exactly one SELECT (incl. VALUES / set ops / WITH)."""
    if sql is None or not sql.strip():
        raise Invalid("empty")
    try:
        stmts = json.loads(parse_sql_json(sql)).get("stmts", [])
    except Exception:
        raise Invalid("syntax_error")
    if len(stmts) != 1:
        raise Invalid("not_one_statement" if stmts else "empty")
    stmt = stmts[0].get("stmt", {})
    if "SelectStmt" not in stmt:
        raise Invalid("not_a_query")
    return _normalize(stmt)


def _normalize(o):
    if isinstance(o, dict):
        # NOT (x IS NULL) is the same as x IS NOT NULL
        b = o.get("BoolExpr")
        if b and b.get("boolop") == "NOT_EXPR" and len(b.get("args", [])) == 1 and "NullTest" in b["args"][0]:
            nt = dict(b["args"][0]["NullTest"])
            nt["nulltesttype"] = "IS_NOT_NULL" if nt.get("nulltesttype") == "IS_NULL" else "IS_NULL"
            return _normalize({"NullTest": nt})
        return {k: _normalize(v) for k, v in o.items() if k not in ("location", "stmt_location", "stmt_len")}
    if isinstance(o, list):
        return [_normalize(x) for x in o]
    return o
