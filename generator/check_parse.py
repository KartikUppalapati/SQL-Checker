"""Check that sqlglot reads every dataset query the way PostgreSQL does. Optional; the harness never runs this.

Run from the repo root:
    python -m generator.check_parse [--show 20] [files.jsonl ...]

The canonicalizer rewrites sqlglot's tree, so a query sqlglot misreads is canonicalized from the
wrong meaning, and a silent misread can make two different queries compare equal. For every query
the gate accepts (pglast, i.e. PostgreSQL's own parser):

    original SQL --pglast--------------------------------------------> tree A
    original SQL --canonical.parse_postgres--> sqlglot --.sql()--pglast--> tree B

A must equal B. Both trees go through _benign() first, which removes differences that do not change
the meaning (unit spelling, NOT x IS TRUE for x IS NOT TRUE, implied defaults, the harness's own
BETWEEN SYMMETRIC / IS UNKNOWN rewrites). Every remaining difference is printed with the paths where
the trees differ, for a person to judge. A query sqlglot cannot parse at all is only counted: the
harness scores it "unsupported", which is safe.

A misread that the generator reproduces exactly (parse and print agree, both wrong) goes unnoticed;
so does any error made after parsing, in qualify or the rewrites.
Exits 1 on an unexplained difference, or when sqlglot's output is not valid PostgreSQL.
"""
import argparse
import json
import logging
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqleval import canonical
from sqleval.validate import Invalid, parse_tree

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = [ROOT / "dataset" / "pairs.jsonl", ROOT / "dataset" / "queries.jsonl"]
SQL_FIELDS = ("ground_truth", "predicted", "sql")

# pglast location fields that validate._normalize does not strip
_LOCATION_KEY = re.compile(r"(^|_)list_(start|end)$")
# functions whose first argument is a field / unit name, which PostgreSQL reads case-insensitively
_UNIT_FUNCS = {"date_trunc", "date_part", "extract", "date_bin"}
_NOT_BOOLTEST = {"IS_TRUE": "IS_NOT_TRUE", "IS_NOT_TRUE": "IS_TRUE", "IS_FALSE": "IS_NOT_FALSE",
                 "IS_NOT_FALSE": "IS_FALSE", "IS_UNKNOWN": "IS_NOT_UNKNOWN", "IS_NOT_UNKNOWN": "IS_UNKNOWN"}
_NOT_OP = {"~~": "!~~", "!~~": "~~", "~~*": "!~~*", "!~~*": "~~*"}
_NOT_BETWEEN = {"AEXPR_BETWEEN": "AEXPR_NOT_BETWEEN", "AEXPR_NOT_BETWEEN": "AEXPR_BETWEEN",
                "AEXPR_BETWEEN_SYM": "AEXPR_NOT_BETWEEN_SYM", "AEXPR_NOT_BETWEEN_SYM": "AEXPR_BETWEEN_SYM"}
_BETWEEN_NAME = {"AEXPR_BETWEEN": "BETWEEN", "AEXPR_NOT_BETWEEN": "NOT BETWEEN",
                 "AEXPR_BETWEEN_SYM": "BETWEEN SYMMETRIC", "AEXPR_NOT_BETWEEN_SYM": "NOT BETWEEN SYMMETRIC"}
FRAMEOPTION_BETWEEN = 0x10  # syntax only: a frame without BETWEEN ends at CURRENT ROW, which has its own bit


def _sval(node):
    return node.get("String", {}).get("sval", "") if isinstance(node, dict) else ""


def _const_text(node):
    """The string of an A_Const string literal, or None."""
    c = node.get("A_Const") if isinstance(node, dict) else None
    return c["sval"]["sval"] if c and "sval" in c else None


def _lower_const(node):
    text = _const_text(node)
    return {"A_Const": {"sval": {"sval": text.lower()}}} if text is not None else node


def _number(node):
    c = node.get("A_Const") if isinstance(node, dict) else None
    if not c:
        return None
    try:
        if "ival" in c:
            return Decimal(c["ival"].get("ival", 0))
        if "fval" in c:
            return Decimal(c["fval"]["fval"])
    except InvalidOperation:
        return None
    return None


def _op(name, left, right):
    return {"A_Expr": {"kind": "AEXPR_OP", "name": [{"String": {"sval": name}}], "lexpr": left, "rexpr": right}}


def _bool(op, args):
    return {"BoolExpr": {"boolop": op, "args": args}}


def _negate(arg):
    """NOT arg as the node PostgreSQL builds for the negated spelling, or None."""
    if "BoolExpr" in arg and arg["BoolExpr"]["boolop"] == "NOT_EXPR":
        return arg["BoolExpr"]["args"][0]
    if "NullTest" in arg:
        nt = dict(arg["NullTest"])
        nt["nulltesttype"] = "IS_NOT_NULL" if nt["nulltesttype"] == "IS_NULL" else "IS_NULL"
        return {"NullTest": nt}
    if "BooleanTest" in arg:
        bt = dict(arg["BooleanTest"])
        bt["booltesttype"] = _NOT_BOOLTEST[bt["booltesttype"]]
        return {"BooleanTest": bt}
    e = arg.get("A_Expr")
    if e is None:
        return None
    name = _sval(e["name"][0]) if e.get("name") else ""
    if e["kind"] == "AEXPR_IN" and name in ("=", "<>"):  # x NOT IN (...) is NOT (x IN (...)), NULLs included
        return {"A_Expr": {**e, "name": [{"String": {"sval": "<>" if name == "=" else "="}}]}}
    if e["kind"] in ("AEXPR_LIKE", "AEXPR_ILIKE") and name in _NOT_OP:
        return {"A_Expr": {**e, "name": [{"String": {"sval": _NOT_OP[name]}}]}}
    if e["kind"] in _NOT_BETWEEN:
        kind = _NOT_BETWEEN[e["kind"]]
        return {"A_Expr": {**e, "kind": kind, "name": [{"String": {"sval": _BETWEEN_NAME[kind]}}]}}
    return None


def _symmetric(e):
    """The harness's own reading of x [NOT] BETWEEN SYMMETRIC lo AND hi (canonical._parse_between_symmetric):
    literal bounds in ascending order, otherwise (x >= lo AND x <= hi) OR (x >= hi AND x <= lo)."""
    x, (lo, hi) = e["lexpr"], e["rexpr"]["List"]["items"]
    negated = e["kind"] == "AEXPR_NOT_BETWEEN_SYM"
    lv, hv = _number(lo), _number(hi)
    if lv is not None and hv is not None:
        kind = "AEXPR_NOT_BETWEEN" if negated else "AEXPR_BETWEEN"
        bounds = [lo, hi] if lv <= hv else [hi, lo]
        return {"A_Expr": {**e, "kind": kind, "name": [{"String": {"sval": _BETWEEN_NAME[kind]}}],
                           "rexpr": {"List": {"items": bounds}}}}
    expanded = _bool("OR_EXPR", [_bool("AND_EXPR", [_op(">=", x, lo), _op("<=", x, hi)]),
                                 _bool("AND_EXPR", [_op(">=", x, hi), _op("<=", x, lo)])])
    return _bool("NOT_EXPR", [expanded]) if negated else expanded


def _benign(o):
    """Rewrite a pglast tree (from validate.parse_tree) so that spellings with the same meaning match."""
    if isinstance(o, list):
        return [_benign(x) for x in o]
    if not isinstance(o, dict):
        return o
    o = {k: _benign(v) for k, v in o.items() if not _LOCATION_KEY.search(k)}

    if "BoolExpr" in o and o["BoolExpr"]["boolop"] == "NOT_EXPR" and len(o["BoolExpr"]["args"]) == 1:
        neg = _negate(o["BoolExpr"]["args"][0])
        if neg is not None:
            return _benign(neg)
    if "BooleanTest" in o and o["BooleanTest"]["booltesttype"] in ("IS_UNKNOWN", "IS_NOT_UNKNOWN"):
        # the harness reads x IS [NOT] UNKNOWN as x IS [NOT] NULL (canonical._rewrite_is_unknown)
        t = "IS_NULL" if o["BooleanTest"]["booltesttype"] == "IS_UNKNOWN" else "IS_NOT_NULL"
        return {"NullTest": {"arg": o["BooleanTest"]["arg"], "nulltesttype": t}}
    e = o.get("A_Expr")
    if e and e["kind"] in ("AEXPR_BETWEEN_SYM", "AEXPR_NOT_BETWEEN_SYM"):
        return _benign(_symmetric(e))

    f = o.get("FuncCall")
    if f:
        names = [_sval(n).lower() for n in f.get("funcname", [])]
        if names and names[-1] in _UNIT_FUNCS and f.get("args"):
            f = {**f, "args": [_lower_const(f["args"][0])] + f["args"][1:]}
        if names == ["pg_catalog", "position"]:  # position(p IN s) is parsed as position(s, p): strpos(s, p)
            f = {**f, "funcname": [{"String": {"sval": "strpos"}}], "funcformat": "COERCE_EXPLICIT_CALL"}
        if names in (["now"], ["pg_catalog", "now"]) and not f.get("args"):  # now() is CURRENT_TIMESTAMP
            return {"SQLValueFunction": {"op": "SVFOP_CURRENT_TIMESTAMP", "typmod": -1}}
        if names in (["date_part"], ["pg_catalog", "date_part"]):
            # sqlglot prints date_part as EXTRACT; the harness keeps them apart (canonical._parse_date_part)
            f = {**f, "funcname": [{"String": {"sval": "pg_catalog"}}, {"String": {"sval": "extract"}}],
                 "funcformat": "COERCE_SQL_SYNTAX"}
        o = {"FuncCall": f}
    tc = o.get("TypeCast")
    if tc and [_sval(n) for n in tc["typeName"]["names"]][-1:] == ["interval"]:
        o = {"TypeCast": {**tc, "arg": _lower_const(tc["arg"])}}
    s = o.get("SortBy")
    if s:  # ASC NULLS LAST and DESC NULLS FIRST are the defaults
        d, n = s["sortby_dir"], s["sortby_nulls"]
        d = "SORTBY_DEFAULT" if d == "SORTBY_ASC" else d
        if (d == "SORTBY_DEFAULT" and n == "SORTBY_NULLS_LAST") or (d == "SORTBY_DESC" and n == "SORTBY_NULLS_FIRST"):
            n = "SORTBY_NULLS_DEFAULT"
        o = {"SortBy": {**s, "sortby_dir": d, "sortby_nulls": n}}
    if "frameOptions" in o:
        o["frameOptions"] &= ~FRAMEOPTION_BETWEEN
    c = o.get("A_Const")
    if c and "fval" in c:
        try:
            o = {"A_Const": {**c, "fval": {"fval": str(Decimal(c["fval"]["fval"]))}}}  # .5 -> 0.5
        except InvalidOperation:
            pass
    return o


def _diffs(a, b, path=""):
    """Paths where two trees differ, with the two subtrees."""
    if isinstance(a, dict) and isinstance(b, dict) and (len(a) != 1 or set(a) == set(b)):
        for k in sorted(set(a) | set(b)):
            yield from _diffs(a.get(k), b.get(k), f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from _diffs(x, y, f"{path}[{i}]")
    elif a != b:
        yield path, a, b


def check(sql):
    """(status, regenerated sql or None, differing paths)."""
    try:
        original = parse_tree(sql)
    except Invalid:
        return "gate_rejects", None, []
    try:
        regenerated = canonical.parse_postgres(sql).sql(dialect="postgres")
    except Exception:
        return "sqlglot_cannot_parse", None, []
    try:
        roundtrip = parse_tree(regenerated)
    except Invalid as e:
        return f"regenerated_invalid:{e}", regenerated, []
    if original == roundtrip:
        return "identical", regenerated, []
    a, b = _benign(original), _benign(roundtrip)
    if a == b:
        return "benign_difference", regenerated, []
    return "DIFFERENT", regenerated, list(_diffs(a, b))


def _read_sql(paths):
    seen = {}
    for path in paths:
        with Path(path).open() as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                obj = json.loads(line)
                for field in SQL_FIELDS:
                    if isinstance(obj.get(field), str) and obj[field].strip():
                        seen.setdefault(obj[field], f"{Path(path).name}:{n} {obj.get('id', '')} {field}")
    return seen


def _short(o, width=160):
    s = json.dumps(o)
    return s if len(s) <= width else s[:width] + "..."


def main(argv=None) -> int:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path, default=DEFAULT_FILES,
                    help="JSONL files; reads the fields " + ", ".join(SQL_FIELDS))
    ap.add_argument("--show", type=int, default=50, help="print at most this many problem queries")
    args = ap.parse_args(argv)

    queries = _read_sql(args.files)
    counts, problems = Counter(), []
    for sql, where in queries.items():
        status, regenerated, diffs = check(sql)
        counts[status.split(":")[0]] += 1
        if status == "DIFFERENT" or status.startswith("regenerated_invalid"):
            problems.append((where, status, sql, regenerated, diffs))

    print(f"sqlglot round trip over {len(queries)} distinct queries")
    for status, n in counts.most_common():
        print(f"  {status:24s} {n}")
    for where, status, sql, regenerated, diffs in problems[:args.show]:
        print(f"\n== {status}  ({where})")
        print(f"   original : {' '.join(sql.split())}")
        print(f"   sqlglot  : {regenerated}")
        for path, a, b in diffs[:4]:
            print(f"   at {path}\n      pg     : {_short(a)}\n      sqlglot: {_short(b)}")
        if len(diffs) > 4:
            print(f"   ... {len(diffs) - 4} more")
    if len(problems) > args.show:
        print(f"\n({len(problems) - args.show} more not shown; raise --show)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
