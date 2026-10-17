"""Automatic rewrites of a target query, each with a known equivalence label.

Equivalence-preserving rewrites are either purely cosmetic (checked: the PostgreSQL parse tree
is unchanged) or structural rewrites that are sound by construction (alias renaming, conjunct
reordering, inner join <-> implicit join, adding ORDER BY when the target has none).

Breaking rewrites are only applied where they cannot accidentally preserve semantics on every
valid database for this dataset (e.g. comparisons are only flipped against literals, LEFT JOIN
is only tightened where the target's LEFT JOIN is meaningful).
"""
import json
import re

from pglast.parser import parse_sql_json
import sqlglot
from sqlglot import exp


# ---------------------------------------------------------------------------- helpers

def pg_ast(sql: str):
    """PostgreSQL raw parse tree with source locations removed, or None if it doesn't parse."""
    try:
        data = json.loads(parse_sql_json(sql))
    except Exception:
        return None
    return _strip_locations(data.get("stmts", []))


def _strip_locations(o):
    if isinstance(o, dict):
        # NOT (x IS NULL)  ==  x IS NOT NULL  (sqlglot renders the latter as the former)
        b = o.get("BoolExpr")
        if b and b.get("boolop") == "NOT_EXPR" and len(b.get("args", [])) == 1 and "NullTest" in b["args"][0]:
            nt = dict(b["args"][0]["NullTest"])
            nt["nulltesttype"] = "IS_NOT_NULL" if nt.get("nulltesttype") == "IS_NULL" else "IS_NULL"
            return _strip_locations({"NullTest": nt})
        return {k: _strip_locations(v) for k, v in o.items()
                if k not in ("location", "stmt_location", "stmt_len")}
    if isinstance(o, list):
        return [_strip_locations(x) for x in o]
    return o


_SEG_RE = re.compile(r"""('(?:[^']|'')*')|("(?:[^"]|"")*")|(--[^\n]*)|(/\*.*?\*/)""", re.DOTALL)


def map_unquoted(sql: str, fn) -> str:
    """Apply fn to every stretch of SQL text outside string literals, quoted identifiers and comments."""
    out, pos = [], 0
    for m in _SEG_RE.finditer(sql):
        out.append(fn(sql[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(sql[pos:]))
    return "".join(out)


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="postgres")


def render(tree: exp.Expression, pretty: bool = False) -> str:
    return tree.sql(dialect="postgres", pretty=pretty)


def roundtrip_faithful(sql: str) -> bool:
    """True if sqlglot re-renders the query to the same PostgreSQL parse tree."""
    try:
        return pg_ast(render(parse(sql))) == pg_ast(sql)
    except Exception:
        return False


def _outer_select(tree):
    return tree if isinstance(tree, exp.Select) else None


def _is_col_vs_literal(node) -> bool:
    if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return False
    sides = (node.this, node.expression)
    lit = any(isinstance(s, exp.Literal) or (isinstance(s, exp.Cast) and isinstance(s.this, exp.Literal))
              for s in sides)
    other = any(isinstance(s, (exp.Column, exp.AggFunc, exp.Extract)) for s in sides)
    return lit and other


def _conjuncts(cond):
    if isinstance(cond, exp.And):
        return _conjuncts(cond.this) + _conjuncts(cond.expression)
    return [cond]


def _and_all(parts):
    out = parts[0]
    for p in parts[1:]:
        out = exp.And(this=out, expression=p)
    return out


# ---------------------------------------------------------------------------- equivalent

def eq_trailing_semicolon(sql, ctx):
    return sql.rstrip() + ";"


def eq_comment(sql, ctx):
    return "-- generated query\n" + sql


def eq_oneline(sql, ctx):
    if not ctx["faithful"]:
        return None
    return render(parse(sql))


def eq_pretty(sql, ctx):
    if not ctx["faithful"]:
        return None
    return render(parse(sql), pretty=True)


def eq_lowercase(sql, ctx):
    return map_unquoted(sql, str.lower)


def eq_uppercase(sql, ctx):
    return map_unquoted(sql, str.upper)


def eq_neq_operator(sql, ctx):
    if "<>" not in map_unquoted(sql, lambda s: s):
        return None
    return map_unquoted(sql, lambda s: s.replace("<>", "!="))


def eq_count_one(sql, ctx):
    if not re.search(r"COUNT\(\*\)", sql, re.I):
        return None
    return map_unquoted(sql, lambda s: re.sub(r"COUNT\(\*\)", "COUNT(1)", s, flags=re.I))


def eq_add_order_by(sql, ctx):
    tree = ctx["tree"]
    if tree.args.get("order") or tree.args.get("limit") or tree.args.get("offset"):
        return None
    return sql.rstrip() + "\nORDER BY 1"


def eq_rename_aliases(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    used = {i.name.lower() for i in tree.find_all(exp.Identifier)}
    aliases = []
    for ta in tree.find_all(exp.TableAlias):
        if isinstance(ta.parent, exp.Table) and ta.this is not None:
            n = ta.this.name
            if n not in aliases:
                aliases.append(n)
    if not aliases:
        return None
    mapping, k = {}, 1
    for a in aliases:
        while f"t{k}" in used:
            k += 1
        mapping[a] = f"t{k}"
        k += 1
    for ta in tree.find_all(exp.TableAlias):
        if isinstance(ta.parent, exp.Table) and ta.this is not None and ta.this.name in mapping:
            ta.set("this", exp.to_identifier(mapping[ta.this.name]))
    for col in tree.find_all(exp.Column):
        t = col.args.get("table")
        if t is not None and not t.quoted and t.name in mapping:
            col.set("table", exp.to_identifier(mapping[t.name]))
    return render(tree)


def eq_swap_conjuncts(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    changed = False
    if sel is not None and sel.args.get("where"):
        parts = _conjuncts(sel.args["where"].this)
        if len(parts) > 1:
            sel.args["where"].set("this", _and_all(list(reversed(parts))))
            changed = True
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        if isinstance(on, exp.EQ):
            j.set("on", exp.EQ(this=on.expression, expression=on.this))
            changed = True
    return render(tree) if changed else None


def eq_implicit_joins(sql, ctx):
    """FROM a JOIN b ON x JOIN c ON y WHERE w  ->  FROM a, b, c WHERE x AND y AND (w)."""
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    if sel is None or not sel.args.get("joins"):
        return None
    joins = sel.args["joins"]
    conds = []
    for j in joins:
        if j.args.get("side") or j.args.get("kind") not in (None, "INNER") or j.args.get("using") \
                or not j.args.get("on"):
            return None
        conds.append(j.args["on"])
    if sel.args.get("where"):
        conds.append(exp.Paren(this=sel.args["where"].this))
    new_joins = [exp.Join(this=j.this) for j in joins]  # comma joins
    sel.set("joins", new_joins)
    sel.set("where", exp.Where(this=_and_all(conds)))
    out = render(sel)
    return out if ", " in out else None


EQ_MUTATIONS = {
    "trailing_semicolon": eq_trailing_semicolon,
    "leading_comment": eq_comment,
    "reformat_oneline": eq_oneline,
    "reformat_pretty": eq_pretty,
    "lowercase": eq_lowercase,
    "uppercase": eq_uppercase,
    "neq_operator_style": eq_neq_operator,
    "count_one": eq_count_one,
    "add_order_by_unordered_target": eq_add_order_by,
    "rename_aliases": eq_rename_aliases,
    "swap_conjuncts_and_join_sides": eq_swap_conjuncts,
    "implicit_joins": eq_implicit_joins,
}


# ---------------------------------------------------------------------------- not equivalent

_FLIP = {exp.GT: exp.GTE, exp.GTE: exp.GT, exp.LT: exp.LTE, exp.LTE: exp.LT, exp.EQ: exp.NEQ, exp.NEQ: exp.EQ}


def neq_flip_comparison(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    for clause in ("where", "having"):
        node = sel.args.get(clause) if sel is not None else None
        if node is None:
            continue
        for c in _conjuncts(node.this):
            if _is_col_vs_literal(c):
                c.replace(_FLIP[type(c)](this=c.this.copy(), expression=c.expression.copy()))
                return render(tree)
    return None


def neq_change_literal(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    for cmp in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if not _is_col_vs_literal(cmp):
            continue
        for lit in (cmp.this, cmp.expression):
            if isinstance(lit, exp.Literal):
                if lit.is_string:
                    s = lit.this
                    t = s.swapcase() if s.lower() != s.upper() else s + "x"
                    lit.replace(exp.Literal.string(t))
                else:
                    try:
                        lit.replace(exp.Literal.number(int(lit.this) + 1))
                    except ValueError:
                        continue
                return render(tree)
    return None


def neq_drop_conjunct(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    if sel is None or not sel.args.get("where"):
        return None
    parts = _conjuncts(sel.args["where"].this)
    for i, p in enumerate(parts):
        if _is_col_vs_literal(p):
            rest = parts[:i] + parts[i + 1:]
            if rest:
                sel.args["where"].set("this", _and_all(rest))
            else:
                sel.set("where", None)
            return render(tree)
    return None


def neq_swap_columns(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    if sel is None or sel.args.get("distinct") and sel.args["distinct"].args.get("on"):
        return None
    cols = sel.expressions
    if len(cols) < 2 or cols[0].sql() == cols[1].sql():
        return None
    cols[0], cols[1] = cols[1], cols[0]
    sel.set("expressions", cols)
    return render(tree)


def neq_drop_column(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    if sel is None or len(sel.expressions) < 2 or sel.args.get("order"):
        return None
    sel.set("expressions", sel.expressions[:-1])
    return render(tree)


def neq_drop_distinct(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    sel = _outer_select(tree)
    if sel is not None and sel.args.get("distinct") and not sel.args["distinct"].args.get("on"):
        sel.set("distinct", None)
        return render(tree)
    for cnt in tree.find_all(exp.Count):
        if isinstance(cnt.this, exp.Distinct):
            cnt.set("this", cnt.this.expressions[0])
            return render(tree)
    return None


def neq_swap_aggregate(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    swaps = {exp.Sum: exp.Avg, exp.Avg: exp.Sum, exp.Max: exp.Min, exp.Min: exp.Max}
    for node in tree.find_all(exp.Sum, exp.Avg, exp.Max, exp.Min):
        node.replace(swaps[type(node)](this=node.this.copy()))
        return render(tree)
    return None


def neq_left_to_inner(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    for j in tree.find_all(exp.Join):
        if (j.args.get("side") or "").upper() == "LEFT":
            j.set("side", None)
            j.set("kind", None)
            return render(tree)
    return None


def neq_flip_null_check(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    for node in tree.find_all(exp.Is):
        if not isinstance(node.expression, exp.Null):
            continue
        if isinstance(node.parent, exp.Not):
            node.parent.replace(node.copy())
        else:
            node.replace(exp.Not(this=node.copy()))
        return render(tree)
    return None


def neq_integer_division(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    for lit in tree.find_all(exp.Literal):
        if not lit.is_string and re.fullmatch(r"\d+\.0+", lit.this) and isinstance(lit.parent, (exp.Mul, exp.Div)):
            lit.replace(exp.Literal.number(lit.this.split(".")[0]))
            return render(tree)
    return None


def neq_flip_order(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    order = tree.args.get("order")
    if not order:
        return None
    first = order.expressions[0]
    desc = not first.args.get("desc")
    first.set("desc", desc)
    first.set("nulls_first", desc)  # keep PostgreSQL's default NULL placement for the new direction
    return render(tree)


def neq_drop_order_by(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    if not tree.args.get("order"):
        return None
    tree.set("order", None)
    return render(tree)


def neq_change_limit(sql, ctx):
    if not ctx["faithful"]:
        return None
    tree = parse(sql)
    lim = tree.args.get("limit")
    if not lim or not isinstance(lim.expression, exp.Literal):
        return None
    lim.set("expression", exp.Literal.number(int(lim.expression.this) + 1))
    return render(tree)


def neq_unquote_identifiers(sql, ctx):
    """Quoted mixed-case schema: dropping the quotes folds names to lowercase -> no such column."""
    if ctx["quoting"] != "quoted":
        return None
    out = re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"',
                 lambda m: m.group(1) if m.group(1) != m.group(1).lower() else m.group(0), sql)
    return out if out != sql else None


def neq_quote_identifiers(sql, ctx):
    """Unquoted mixed-case schema: quoting the DDL spelling makes the name case-sensitive -> no such table."""
    if ctx["quoting"] != "unquoted" or ctx["naming"] == "snake_case":
        return None
    names = sorted(ctx["ddl_names"], key=len, reverse=True)
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    out = map_unquoted(sql, lambda s: pat.sub(lambda m: f'"{m.group(1)}"', s))
    return out if out != sql else None


NEQ_MUTATIONS = {
    "flip_comparison": neq_flip_comparison,
    "change_literal": neq_change_literal,
    "drop_filter": neq_drop_conjunct,
    "swap_columns": neq_swap_columns,
    "drop_column": neq_drop_column,
    "drop_distinct": neq_drop_distinct,
    "swap_aggregate": neq_swap_aggregate,
    "left_to_inner_join": neq_left_to_inner,
    "flip_null_check": neq_flip_null_check,
    "integer_division": neq_integer_division,
    "flip_order_direction": neq_flip_order,
    "drop_order_by": neq_drop_order_by,
    "change_limit": neq_change_limit,
    "unquote_identifiers": neq_unquote_identifiers,
    "quote_identifiers": neq_quote_identifiers,
}

# Mutations whose output is expected to be invalid against the schema (not merely different).
EXPECT_INVALID = {"unquote_identifiers", "quote_identifiers"}


# ---------------------------------------------------------------------------- malformed predictions

def malformed(sql: str, kind: str, first_table: str):
    if kind == "null":
        return None
    if kind == "empty":
        return ""
    if kind == "markdown_fenced":
        return f"```sql\n{sql}\n```"
    if kind == "two_statements":
        return sql.rstrip().rstrip(";") + ";\nSELECT 1;"
    if kind == "prose":
        return "I'm sorry, I can't answer that question with the available schema."
    if kind == "truncated":
        return sql[: max(10, len(sql) // 2)].rstrip() + " WHERE"
    if kind == "mysql_backticks":
        return map_unquoted(sql, lambda s: re.sub(r"\bFROM\s+(\w+)", r"FROM `\1`", s, count=1)) \
            if "FROM" in sql.upper() else None
    if kind == "dml_statement":
        return f"DELETE FROM {first_table}"
    if kind == "misspelled_keyword":
        return re.sub(r"^\s*SELECT", "SELEC", sql, count=1, flags=re.I)
    raise ValueError(kind)


MALFORMED_KINDS = ["null", "empty", "markdown_fenced", "two_statements", "prose",
                   "truncated", "mysql_backticks", "dml_statement", "misspelled_keyword"]
