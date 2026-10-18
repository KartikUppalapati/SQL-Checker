"""Syntax gate using PostgreSQL's own parser (libpg_query via pglast). No execution, no network."""
import json

from pglast.parser import parse_sql_json


class Invalid(Exception):
    """The prediction is not exactly one valid PostgreSQL query."""


# Functions from other dialects that PostgreSQL does not have. The grammar accepts any function
# name, and sqlglot quietly maps several of these (IFNULL, NVL, ...) onto PostgreSQL functions.
FOREIGN_FUNCTIONS = frozenset({
    "ifnull", "nvl", "nvl2", "isnull", "iif", "if", "nz", "isnumeric",
    "dateadd", "datediff", "datepart", "datename", "getdate", "getutcdate", "sysdate", "sysdatetime",
    "curdate", "curtime", "date_format", "str_to_date", "unix_timestamp", "from_unixtime", "strftime",
    "julianday", "year", "month", "day", "dayofweek", "dayofmonth", "dayofyear", "weekday", "hour",
    "minute", "second", "eomonth", "last_day", "add_months", "months_between",
    "len", "charindex", "instr", "locate", "ucase", "lcase", "mid", "group_concat", "listagg",
})


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
    if any(name in FOREIGN_FUNCTIONS for name in _function_names(stmt)):
        raise Invalid("unknown_function")
    return _normalize(stmt)


def _function_names(o):
    """Unqualified names of the function calls in a parse tree."""
    if isinstance(o, dict):
        f = o.get("FuncCall")
        if f and len(f.get("funcname", [])) == 1:
            yield f["funcname"][0].get("String", {}).get("sval", "").lower()
        for v in o.values():
            yield from _function_names(v)
    elif isinstance(o, list):
        for x in o:
            yield from _function_names(x)


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
