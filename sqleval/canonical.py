"""Rewrite a PostgreSQL query into a canonical form so that equivalent queries compare equal.

Every rewrite here must preserve the query's result on *every* database that satisfies the
schema (types, primary keys, NOT NULL, foreign keys). When a rewrite's precondition cannot be
proven from the schema, the rewrite is skipped -- a missed equivalence costs a false negative,
an unsound rewrite would cost a false positive.

Pipeline (see canonicalize()):
  parse -> resolve names against the schema (sqlglot qualify, PostgreSQL case folding)
  -> structural clean-up (parens, FETCH, CTE inlining, unique aliases, positional derived columns)
  -> expression rewrites (comparison orientation, BETWEEN, enums, date/integer bounds, ...)
  -> per-SELECT rewrites, innermost first (semi/anti-joins, FK join promotion, GROUP BY and
     DISTINCT key reduction, COUNT(col), redundant IS NOT NULL, ...)
  -> order-insensitive serialization (sorted conjuncts, sorted inner-join relations, sorted
     GROUP BY keys, commutative operands) with canonical alias names.
"""
import datetime as _dt
import itertools
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from .schema import Catalog
from .validate import Invalid


class Unsupported(Exception):
    """The query could not be canonicalized (not a statement about the query's validity)."""


MAX_ALIAS_PERMUTATIONS = 720

_STRICT_CMP = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Like, exp.ILike)
_FLIP_NOT = {exp.EQ: exp.NEQ, exp.NEQ: exp.EQ, exp.LT: exp.GTE, exp.GTE: exp.LT, exp.GT: exp.LTE, exp.LTE: exp.GT}
_AGGS_IGNORING_ORDER = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
_DATE_TYPES = {"DATE"}
_TS_TYPES = {"TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE"}
_INT_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT"}


# =============================================================================== context

class Context:
    """Per-schema state shared by all queries of that schema."""

    def __init__(self, catalog: Catalog | None):
        self.catalog = catalog
        self.schema = MappingSchema(catalog.sqlglot_schema(), dialect="postgres", normalize=False) \
            if catalog else None

    def table(self, name):
        return self.catalog.tables.get(name) if self.catalog else None

    def keys(self, name):
        """Candidate keys: the primary key and UNIQUE column sets whose columns are all NOT NULL."""
        t = self.table(name)
        if not t:
            return []
        out = [list(t.primary_key)] if t.primary_key else []
        for u in t.unique:
            if u and all(t.columns.get(c) and t.columns[c].not_null for c in u):
                out.append(list(u))
        return out


# =============================================================================== helpers

def _conjuncts(e):
    if e is None:
        return []
    if isinstance(e, exp.And):
        return _conjuncts(e.this) + _conjuncts(e.expression)
    return [e]


def _disjuncts(e):
    if isinstance(e, exp.Or):
        return _disjuncts(e.this) + _disjuncts(e.expression)
    return [e]


def _and_all(parts):
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = exp.And(this=out, expression=p)
    return out


def _unalias(e):
    return e.this if isinstance(e, exp.Alias) else e


def _sources(select: exp.Select):
    """[(alias, node, join_or_None)] for the FROM item and joins of a SELECT."""
    out = []
    frm = select.args.get("from")
    if frm is not None:
        out.append((frm.this.alias_or_name, frm.this, None))
    for j in select.args.get("joins") or []:
        out.append((j.this.alias_or_name, j.this, j))
    return out


def _owner_select(node):
    p = node.parent
    while p is not None and not isinstance(p, exp.Select):
        p = p.parent
    return p


def _table_name(t: exp.Table) -> str:
    return t.name


def _col_refs(e):
    return [c for c in e.find_all(exp.Column) if c.table]


def _replace_children_with(node, mapping):
    """Return node with any sub-expression whose sql is in mapping replaced (copy)."""
    def tf(n):
        k = n.sql() if isinstance(n, exp.Column) else None
        return mapping[k].copy() if k in mapping else n
    return node.transform(tf)


# =============================================================================== resolver

class Resolver:
    """Look up the base table, catalog column and outer-join nullability of a qualified column."""

    def __init__(self, ctx: Context, tree: exp.Expression):
        self.ctx = ctx
        self.cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        self.refresh(tree)

    def refresh(self, tree):
        self.sources = {}
        for sel in tree.find_all(exp.Select):
            for alias, node, _ in _sources(sel):
                self.sources[alias] = node

    def base_table(self, alias):
        node = self.sources.get(alias)
        if isinstance(node, exp.Table) and node.name not in self.cte_names and self.ctx.table(node.name):
            return node.name
        return None

    def column(self, col: exp.Column):
        tname = self.base_table(col.table)
        if not tname:
            return None
        return self.ctx.table(tname).columns.get(col.name)

    def nullable_side(self, alias) -> bool:
        """True if rows of `alias` can be NULL-extended by an outer join in its SELECT."""
        node = self.sources.get(alias)
        if node is None:
            return True
        sel = _owner_select(node)
        if sel is None:
            return True
        srcs = _sources(sel)
        idx = next((i for i, (a, _, _) in enumerate(srcs) if a == alias), None)
        if idx is None:
            return True
        for i, (_, _, j) in enumerate(srcs):
            if j is None:
                continue
            side = (j.args.get("side") or "").upper()
            if side == "FULL" and (i == idx or i > idx):
                return True
            if side == "LEFT" and i == idx:
                return True
            if side == "RIGHT" and i > idx:
                return True
        return False

    def col_type(self, col):
        c = self.column(col) if isinstance(col, exp.Column) else None
        return c.type.upper() if c else None

    def not_null(self, e, scope_nonnull=frozenset()) -> bool:
        """True if expression e can never be NULL in its SELECT (schema NOT NULL, not on the NULL
        side of an outer join), or is constrained non-NULL by the SELECT's own WHERE."""
        e = _unalias(e)
        if isinstance(e, exp.Literal):
            return True
        if isinstance(e, exp.Column):
            if e.sql() in scope_nonnull:
                return True
            c = self.column(e)
            return bool(c and c.not_null and not self.nullable_side(e.table))
        return False


def _scope_nonnull(select: exp.Select):
    """Columns that the SELECT's WHERE / inner ON conditions force to be non-NULL."""
    conds = _conjuncts(select.args["where"].this) if select.args.get("where") else []
    for j in select.args.get("joins") or []:
        if not j.args.get("side") and j.args.get("on") is not None:
            conds += _conjuncts(j.args["on"])
    out = set()
    for c in conds:
        if isinstance(c, exp.Not) and isinstance(c.this, exp.Is) and isinstance(c.this.expression, exp.Null):
            if isinstance(c.this.this, exp.Column):
                out.add(c.this.this.sql())
        elif isinstance(c, _STRICT_CMP):
            for side in (c.this, c.expression):
                if isinstance(side, exp.Column):
                    out.add(side.sql())
    return frozenset(out)


# =============================================================================== stage 1: parse

def _pg_columns_exist(sql, ctx) -> bool:
    """Every column name PostgreSQL's parser sees exists in some table or is an output alias."""
    import json
    from pglast.parser import parse_sql_json
    try:
        data = json.loads(parse_sql_json(sql))
    except Exception:
        return False
    names, aliases = set(), set()

    def walk(o):
        if isinstance(o, dict):
            if "ColumnRef" in o:
                fields = o["ColumnRef"].get("fields", [])
                if fields and "String" in fields[-1]:
                    names.add(fields[-1]["String"]["sval"])
            if "ResTarget" in o and o["ResTarget"].get("name"):
                aliases.add(o["ResTarget"]["name"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    known = {c for t in ctx.catalog.tables.values() for c in t.columns} | aliases
    return names <= known


def _check_tables(tree, ctx):
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Identifier) and t.name not in ctes and not ctx.table(t.name):
            raise Invalid("unknown_table")


def parse_and_qualify(sql: str, ctx: Context) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception as e:
        raise Unsupported(f"sqlglot_parse: {e}")
    if tree is None:
        raise Unsupported("sqlglot_parse: empty")
    if ctx.catalog is None:
        try:
            return qualify(tree, dialect="postgres", validate_qualify_columns=False, quote_identifiers=True)
        except Exception as e:
            raise Unsupported(f"qualify: {e}")
    tree = normalize_identifiers(tree, dialect="postgres")
    _check_tables(tree, ctx)
    try:
        tree = qualify(tree, schema=ctx.schema, dialect="postgres", validate_qualify_columns=True,
                       quote_identifiers=True)
    except OptimizeError as e:
        msg = str(e)
        if "could not be resolved" in msg or "Unknown" in msg or "Ambiguous" in msg or "ambiguous" in msg:
            if _pg_columns_exist(sql, ctx):
                # PostgreSQL's parser sees only real columns: sqlglot mis-parsed the query
                raise Unsupported(f"qualify: {msg}")
            raise Invalid("unresolved_column")
        raise Unsupported(f"qualify: {msg}")
    except Exception as e:
        raise Unsupported(f"qualify: {e}")
    _check_grouping(tree, ctx)
    return tree


def _check_grouping(tree, ctx):
    """PostgreSQL rejects a grouped (or aggregating) SELECT that uses a column outside an aggregate
    unless it is grouped or its table's PRIMARY KEY is grouped (UNIQUE keys do not count). It also
    rejects SELECT DISTINCT ... ORDER BY <expression not in the select list>."""
    for sel in tree.find_all(exp.Select):
        group = sel.args.get("group")
        own_aggs = [a for a in sel.find_all(exp.AggFunc)
                    if _owner_select(a) is sel and not any(isinstance(x, exp.Window) for x in _ancestors(a, sel))]
        aliases = {p.alias for p in sel.expressions if isinstance(p, exp.Alias)}
        if sel.args.get("distinct") and not sel.args["distinct"].args.get("on") and sel.args.get("order"):
            proj = {_unalias(p).sql() for p in sel.expressions}
            for o in sel.args["order"].expressions:
                t = o.this
                if t.sql() not in proj and not (isinstance(t, exp.Column) and not t.table and t.name in aliases):
                    raise Invalid("distinct_order_by")
        if not group and not own_aggs:
            continue
        gset = {g.sql() for g in (group.expressions if group else [])}
        pk_grouped = set()
        for alias, node, _ in _sources(sel):
            if isinstance(node, exp.Table) and ctx.table(node.name) and ctx.table(node.name).primary_key and all(
                    exp.column(k, table=alias, quoted=True).sql() in gset for k in ctx.table(node.name).primary_key):
                pk_grouped.add(alias)

        def ok(e):
            # lenient where unsure: never reject what PostgreSQL would accept
            if e.sql() in gset or isinstance(e, (exp.AggFunc, exp.Filter, exp.Window, exp.Subquery,
                                                 exp.Literal, exp.Star)):
                return True
            if isinstance(e, exp.Column):
                if not e.table:
                    return e.name in aliases
                return e.table in pk_grouped or e.table not in {a for a, _, _ in _sources(sel)}
            return all(ok(c) for c in e.iter_expressions())
        for part in [*sel.expressions, sel.args.get("having"), sel.args.get("order")]:
            if part is not None and not ok(_unalias(part) if isinstance(part, exp.Alias) else part):
                raise Invalid("grouping_error")


# =============================================================================== stage 2: structure

def _strip_parens(tree):
    return tree.transform(lambda n: n.this if isinstance(n, exp.Paren) else n)


def _normalize_clauses(tree):
    for sel in list(tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except)):
        lim = sel.args.get("limit")
        if isinstance(lim, exp.Fetch):
            cnt = lim.args.get("count")
            sel.set("limit", exp.Limit(expression=cnt.copy()) if cnt is not None else None)
    for j in tree.find_all(exp.Join):
        if (j.args.get("kind") or "").upper() in ("INNER", "OUTER"):
            j.set("kind", None)
        if (j.args.get("kind") or "").upper() == "CROSS":
            j.set("kind", None)
    for e in tree.find_all(exp.Extract):
        if isinstance(e.this, (exp.Var, exp.Identifier)):
            e.set("this", exp.var(e.this.name.upper()))
    return tree


def _inline_ctes(tree):
    """A non-recursive CTE referenced exactly once is the same as a derived table."""
    w = tree.args.get("with")
    if not w or w.args.get("recursive"):
        return tree
    for cte in list(w.expressions):
        name = cte.alias_or_name
        refs = [t for t in tree.find_all(exp.Table) if t.name == name and not t.args.get("db")]
        if len(refs) != 1:
            continue
        ref = refs[0]
        alias = ref.alias_or_name
        sub = exp.Subquery(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
        ref.replace(sub)
        cte.pop()
    if not w.expressions:
        tree.set("with", None)
    return tree


def _rename_refs(scope_node, old, new, defines):
    """Rename column qualifiers `old` -> `new` inside scope_node, not descending into nested
    SELECTs that define their own `old` alias."""
    stack = [scope_node]
    while stack:
        n = stack.pop()
        if isinstance(n, exp.Column) and n.table == old:
            n.set("table", exp.to_identifier(new, quoted=True))
        for child in n.iter_expressions():
            if isinstance(child, exp.Select) and child is not scope_node and old in defines(child):
                continue
            stack.append(child)


def _unique_aliases(tree):
    def defines(sel):
        return {a for a, _, _ in _sources(sel)}

    used = set()
    for sel in tree.find_all(exp.Select, bfs=False):
        for alias, node, _ in _sources(sel):
            if alias not in used:
                used.add(alias)
                continue
            k = 2
            while f"{alias}_{k}" in used:
                k += 1
            new = f"{alias}_{k}"
            used.add(new)
            node.set("alias", exp.TableAlias(this=exp.to_identifier(new, quoted=True),
                                             columns=node.args["alias"].args.get("columns")
                                             if node.args.get("alias") else None))
            _rename_refs(sel, alias, new, defines)
    return tree


def _leftmost_select(q):
    while isinstance(q, (exp.Union, exp.Intersect, exp.Except)):
        q = q.this
    return q


def _set_branches(q):
    if isinstance(q, (exp.Union, exp.Intersect, exp.Except)):
        return _set_branches(q.this) + _set_branches(q.expression)
    return [q]


def _resolve_order_aliases(tree):
    """ORDER BY <output alias> -> ORDER BY <expression> (qualify resolves positions to aliases)."""
    for sel in tree.find_all(exp.Select):
        order = sel.args.get("order")
        if not order:
            continue
        aliases = {p.alias: p.this for p in sel.expressions if isinstance(p, exp.Alias)}
        for o in order.expressions:
            t = o.this
            if isinstance(t, exp.Column) and not t.table and t.name in aliases:
                o.set("this", aliases[t.name].copy())
    return tree


def _positional_derived_columns(tree):
    """Rename the output columns of CTEs and derived tables to _c0, _c1, ... (their names are
    internal to the query; only positions matter)."""
    def rename_outputs(query):
        names = []
        for i, p in enumerate(_leftmost_select(query).expressions):
            names.append(p.alias_or_name)
        for br in _set_branches(query):
            if not isinstance(br, exp.Select):
                continue
            new_exprs = []
            for i, p in enumerate(br.expressions):
                new_exprs.append(exp.alias_(_unalias(p).copy(), f"_c{i}", quoted=True))
            br.set("expressions", new_exprs)
        return {n: f"_c{i}" for i, n in enumerate(names)}

    def rename_cols(alias, mapping):
        for c in tree.find_all(exp.Column):
            if c.table == alias and c.name in mapping:
                c.set("this", exp.to_identifier(mapping[c.name], quoted=True))

    w = tree.args.get("with")
    for cte in (w.expressions if w else []):
        cols = cte.args["alias"].args.get("columns") if cte.args.get("alias") else None
        mapping = rename_outputs(cte.this)
        if cols:
            mapping = {c.name: f"_c{i}" for i, c in enumerate(cols)}
            cte.args["alias"].set("columns", [exp.to_identifier(f"_c{i}", quoted=True) for i in range(len(cols))])
        for t in tree.find_all(exp.Table):
            if t.name == cte.alias_or_name:
                rename_cols(t.alias_or_name, mapping)
    for sub in list(tree.find_all(exp.Subquery)):
        if isinstance(sub.parent, (exp.From, exp.Join)) and sub.alias:
            mapping = rename_outputs(sub.this)
            rename_cols(sub.alias, mapping)
    return tree


# =============================================================================== stage 3: expressions

def _lit_str(e):
    return e.this if isinstance(e, exp.Literal) and e.is_string else None


def _lit_int(e):
    if isinstance(e, exp.Literal) and not e.is_string and re.fullmatch(r"-?\d+", e.this):
        return int(e.this)
    if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal):
        v = _lit_int(e.this)
        return -v if v is not None else None
    return None


def _typed_literal(e):
    """(kind, python value) for DATE/TIMESTAMP literals: CAST('..' AS DATE), DATE '..'."""
    if isinstance(e, exp.Cast) and isinstance(e.this, exp.Literal) and e.this.is_string:
        t = e.to.sql(dialect="postgres").upper()
        s = e.this.this.strip()
        try:
            if t in _DATE_TYPES:
                return "DATE", _dt.date.fromisoformat(s)
            if t in _TS_TYPES:
                return "TIMESTAMP", _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _make_typed(kind, value):
    s = value.isoformat() if kind == "DATE" else value.isoformat(sep=" ")
    return exp.Cast(this=exp.Literal.string(s), to=exp.DataType.build(kind))


def _shift(kind, value, step):
    if kind == "DATE":
        return value + _dt.timedelta(days=step)
    return value + _dt.timedelta(microseconds=step)


def _norm_expr(node, R: Resolver):
    # --- boolean structure -----------------------------------------------------------
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Not):
            return inner.this
        if type(inner) in _FLIP_NOT:
            return _FLIP_NOT[type(inner)](this=inner.this, expression=inner.expression)
    if isinstance(node, exp.NullSafeEQ) and isinstance(node.expression, exp.Null):
        return exp.Is(this=node.this, expression=exp.Null())
    if isinstance(node, exp.NullSafeNEQ) and isinstance(node.expression, exp.Null):
        return exp.Not(this=exp.Is(this=node.this, expression=exp.Null()))
    if isinstance(node, exp.Between) and not node.args.get("symmetric"):
        x = node.this
        return exp.And(this=exp.GTE(this=x.copy(), expression=node.args["low"]),
                       expression=exp.LTE(this=x.copy(), expression=node.args["high"]))
    if isinstance(node, exp.In) and not node.args.get("query") and len(node.expressions) == 1 \
            and not node.args.get("unnest"):
        return exp.EQ(this=node.this, expression=node.expressions[0])
    # --- COALESCE spelled as CASE --------------------------------------------------------
    if isinstance(node, exp.Case) and not node.this and len(node.args.get("ifs") or []) == 1:
        cond = node.args["ifs"][0]
        c, then, default = cond.this, cond.args.get("true"), node.args.get("default")
        if isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and default is not None \
                and c.this.sql() == default.sql():
            return exp.Coalesce(this=default, expressions=[then])
        if isinstance(c, exp.Not) and isinstance(c.this, exp.Is) and isinstance(c.this.expression, exp.Null) \
                and default is not None and c.this.this.sql() == then.sql():
            return exp.Coalesce(this=then, expressions=[default])
    # --- COUNT(<non-null literal>) is COUNT(*) --------------------------------------------
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Literal):
        node.set("this", exp.Star())
        return node
    # --- case-insensitive matching -------------------------------------------------------
    if isinstance(node, exp.RegexpILike):
        pat = _lit_str(node.expression)
        if pat is not None and re.fullmatch(r"[A-Za-z0-9 _]*", pat) and "_" not in pat:
            return exp.ILike(this=node.this, expression=exp.Literal.string(f"%{pat}%"))
    if isinstance(node, exp.Like) and isinstance(node.this, exp.Lower):
        pat = _lit_str(node.expression)
        if pat is not None and pat == pat.lower():
            return exp.ILike(this=node.this.this, expression=node.expression)
    # --- comparisons -----------------------------------------------------------------------
    if isinstance(node, (exp.GT, exp.GTE)):
        cls = exp.LT if isinstance(node, exp.GT) else exp.LTE
        node = cls(this=node.expression, expression=node.this)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.In)):
        node = _norm_comparison(node, R)
    return node


def _norm_comparison(node, R: Resolver):
    # Column on one side, literal on the other: coerce/validate the literal by the column type.
    if isinstance(node, exp.In):
        col = node.this
        if isinstance(col, exp.Column) and not node.args.get("query"):
            labels = (R.column(col).enum_labels if R.column(col) else None)
            if labels:
                vals = []
                for v in node.expressions:
                    v = _strip_enum_cast(v)
                    s = _lit_str(v)
                    if s is None:
                        return node
                    if s not in labels:
                        raise Invalid("invalid_enum_label")
                    vals.append(s)
                vals = sorted(set(vals))
                if len(vals) == 1:
                    return exp.EQ(this=col, expression=exp.Literal.string(vals[0]))
                return exp.In(this=col, expressions=[exp.Literal.string(v) for v in vals])
        return node

    a, b = node.this, node.expression
    col, lit, col_left = (a, b, True) if isinstance(a, (exp.Column, exp.Extract)) else (b, a, False)
    if not isinstance(col, (exp.Column, exp.Extract)):
        return node

    # EXTRACT(YEAR FROM <date/timestamp column>) <op> <int>  ->  range on the column
    if isinstance(col, exp.Extract):
        unit = col.this.name.upper() if col.this is not None else ""
        y = _lit_int(lit)
        inner = col.expression
        t = R.col_type(inner) if isinstance(inner, exp.Column) else None
        kind = "DATE" if t in _DATE_TYPES else "TIMESTAMP" if t in _TS_TYPES else None
        if unit == "YEAR" and y is not None and kind and 1 <= y <= 9998:
            def start(yy):
                v = _dt.date(yy, 1, 1) if kind == "DATE" else _dt.datetime(yy, 1, 1)
                return _make_typed(kind, v)
            op = type(node)
            if op is exp.EQ:
                return exp.And(this=exp.LTE(this=start(y), expression=inner.copy()),
                               expression=exp.LT(this=inner.copy(), expression=start(y + 1)))
            if op is exp.LT:   # col_left: year < y  -> col < y-01-01 ; else y < year -> y+1-01-01 <= col
                return exp.LT(this=inner.copy(), expression=start(y)) if col_left \
                    else exp.LTE(this=start(y + 1), expression=inner.copy())
            if op is exp.LTE:  # year <= y -> col < (y+1)-01-01 ; y <= year -> y-01-01 <= col
                return exp.LT(this=inner.copy(), expression=start(y + 1)) if col_left \
                    else exp.LTE(this=start(y), expression=inner.copy())
        return node

    cinfo = R.column(col)
    if cinfo is None:
        return node
    ctype = cinfo.type.upper()

    # enums: labels are case-sensitive; an unknown label is a runtime error
    if cinfo.enum_labels:
        lit2 = _strip_enum_cast(lit)
        s = _lit_str(lit2)
        if s is None:
            return node
        if s not in cinfo.enum_labels:
            raise Invalid("invalid_enum_label")
        if isinstance(node, exp.NEQ):
            others = [l for l in cinfo.enum_labels if l != s]
            if len(others) == 1:
                return exp.EQ(this=col, expression=exp.Literal.string(others[0]))
            return exp.In(this=col, expressions=[exp.Literal.string(o) for o in sorted(others)])
        if isinstance(node, exp.EQ):
            return exp.EQ(this=col, expression=exp.Literal.string(s))
        return node

    # dates / timestamps: untyped string literal takes the column's type; discrete bounds
    if ctype in _DATE_TYPES or ctype in _TS_TYPES:
        kind = "DATE" if ctype in _DATE_TYPES else "TIMESTAMP"
        tl = _typed_literal(lit)
        if tl is None and _lit_str(lit) is not None:
            tl = _typed_literal(exp.Cast(this=lit, to=exp.DataType.build(kind)))
        if tl is None:
            return node
        lkind, val = tl
        if lkind != kind:
            if lkind == "DATE" and kind == "TIMESTAMP":
                val = _dt.datetime(val.year, val.month, val.day)
            else:
                return node
        return _discrete_bound(node, col, col_left, kind, val,
                               lambda v: _make_typed(kind, v), lambda v, s: _shift(kind, v, s))

    if ctype in _INT_TYPES:
        v = _lit_int(lit)
        if v is None:
            return node
        return _discrete_bound(node, col, col_left, "INT", v,
                               lambda x: exp.Literal.number(x), lambda x, s: x + s)
    return node


def _discrete_bound(node, col, col_left, kind, val, mk, shift):
    """For a discrete domain:  col <= v  ->  col < v+1   and   v < col  ->  v+1 <= col."""
    op = type(node)
    if op is exp.LTE and col_left:
        return exp.LT(this=col, expression=mk(shift(val, 1)))
    if op is exp.LT and not col_left:
        return exp.LTE(this=mk(shift(val, 1)), expression=col)
    if col_left:
        return op(this=col, expression=mk(val))
    return op(this=mk(val), expression=col)


def _strip_enum_cast(e):
    if isinstance(e, exp.Cast) and isinstance(e.this, exp.Literal) and e.this.is_string \
            and e.to.this == exp.DataType.Type.USERDEFINED:
        return e.this
    return e


# =============================================================================== stage 4: SELECT scopes

def _is_simple_select(s):
    return (isinstance(s, exp.Select) and not s.args.get("group") and not s.args.get("having")
            and not s.args.get("limit") and not s.args.get("offset") and not s.args.get("with")
            and not (s.args.get("distinct") and s.args["distinct"].args.get("on"))
            and not any(p.find(exp.AggFunc) or p.find(exp.Window) for p in s.expressions))


def _subquery_select(node):
    q = node.args.get("query") if isinstance(node, exp.In) else node.this
    if isinstance(q, exp.Subquery):
        q = q.this
    return q


def _where_set(sel, conds):
    sel.set("where", exp.Where(this=_and_all(conds)) if conds else None)


def _scope_rewrites(sel: exp.Select, R: Resolver, ctx: Context):
    _right_to_left(sel)
    _scalar_eq_to_join(sel, R, ctx)
    _scalar_subqueries_to_joins(sel, R, ctx)
    _decorrelate_aggregate(sel, R, ctx)
    R.refresh(sel.root())
    _promote_fk_left_joins(sel, R, ctx)
    _in_to_exists(sel, R)
    _left_join_anti_to_not_exists(sel, R, ctx)
    R.refresh(sel.root())
    _self_key_join_elimination(sel, R, ctx)
    _fk_exists_to_null_test(sel, R, ctx)
    _pull_up_exists(sel, R, ctx)
    R.refresh(sel.root())
    _eliminate_fk_joins(sel, R, ctx)
    R.refresh(sel.root())
    _left_join_count_having_to_inner(sel, R, ctx)
    _drop_redundant_not_null(sel, R)
    _count_col_to_star(sel, R, ctx)
    _reduce_group_by(sel, R, ctx)
    _drop_redundant_distinct(sel, R, ctx)
    _equivalence_classes(sel, R)


def _refs_only(e, aliases):
    return all(c.table in aliases for c in _col_refs(e))


def _base_single_source(s, R):
    """(alias, table) if SELECT s reads a single base table with no joins."""
    if not isinstance(s, exp.Select) or s.args.get("joins") or not s.args.get("from"):
        return None
    src = s.args["from"].this
    if not isinstance(src, exp.Table):
        return None
    t = R.base_table(src.alias_or_name)
    return (src.alias_or_name, t) if t else None


def _key_covered(eq_cols, tname, ctx):
    return any(set(k) <= set(eq_cols) for k in ctx.keys(tname))


def _scalar_eq_to_join(sel, R, ctx):
    """a = (SELECT x.c FROM t x WHERE <key of t> = <literals>)  ->  JOIN t x WHERE x.c = a AND ...
    The subquery yields at most one row; no row makes the comparison NULL (row dropped), exactly
    like the join finding no partner."""
    where = sel.args.get("where")
    if not where:
        return
    out, changed = [], False
    for c in _conjuncts(where.this):
        if isinstance(c, exp.EQ):
            for a, q in ((c.this, c.expression), (c.expression, c.this)):
                if not isinstance(q, exp.Subquery):
                    continue
                s = q.this
                one = _base_single_source(s, R)
                if not one or not _is_simple_select(s) or s.args.get("distinct") or len(s.expressions) != 1:
                    continue
                x, tname = one
                val = _unalias(s.expressions[0])
                conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
                if not isinstance(val, exp.Column) or not _refs_only(s, {x}):
                    continue
                eq_cols = []
                for cc in conds:
                    if isinstance(cc, exp.EQ):
                        cols = [z for z in (cc.this, cc.expression) if isinstance(z, exp.Column)]
                        lits = [z for z in (cc.this, cc.expression) if isinstance(z, (exp.Literal, exp.Cast))]
                        if len(cols) == 1 and len(lits) == 1:
                            eq_cols.append(cols[0].name)
                if not _key_covered(eq_cols, tname, ctx):
                    continue
                sel.append("joins", exp.Join(this=s.args["from"].this.copy()))
                out.extend([cc.copy() for cc in conds] + [exp.EQ(this=val.copy(), expression=a.copy())])
                changed = True
                break
            else:
                out.append(c)
            continue
        out.append(c)
    if changed:
        _where_set(sel, out)


def _decorrelate_aggregate(sel, R, ctx):
    """One correlated aggregate subquery  (SELECT AGG(..) FROM t x WHERE <conds>)  in a plain,
    non-aggregating SELECT over base tables with primary keys equals
    LEFT JOIN t x ON <conds> ... GROUP BY <primary keys of the outer tables> with AGG(..)
    (COUNT(*) counts x's primary key, so the NULL-extended row counts 0)."""
    if sel.args.get("group") or sel.args.get("having") or sel.args.get("distinct") or sel.args.get("with"):
        return
    if any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)) or sel.find(exp.Window):
        return
    srcs = _sources(sel)
    if not srcs or any(not isinstance(n, exp.Table) or not R.base_table(a) for a, n, _ in srcs):
        return
    outer_aliases = {a for a, _, _ in srcs}
    pks = []
    for a, n, _ in srcs:
        t = ctx.table(R.base_table(a))
        if not t.primary_key:
            return
        pks += [exp.column(k, table=a, quoted=True) for k in t.primary_key]
    cands = {}
    for q in sel.find_all(exp.Subquery):
        if _owner_select(q) is not sel or isinstance(q.parent, (exp.From, exp.Join)):
            continue
        s = q.this
        one = _base_single_source(s, R)
        if not one or len(s.expressions) != 1:
            continue
        if s.args.get("group") or s.args.get("having") or s.args.get("limit") or s.args.get("distinct"):
            continue
        agg = _unalias(s.expressions[0])
        if not isinstance(agg, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            continue
        x, tname = one
        if not _refs_only(s, outer_aliases | {x}) or not ctx.table(tname).primary_key:
            continue
        if any(isinstance(n, exp.Subquery) for n in s.find_all(exp.Subquery)):
            continue
        cands.setdefault(q.sql(), (q, s, agg, x, tname))
    if len(cands) != 1:
        return
    _, (q, s, agg, x, tname) = next(iter(cands.items()))
    key = q.sql()
    new_agg = agg.copy()
    if isinstance(new_agg, exp.Count) and isinstance(new_agg.this, exp.Star):
        new_agg.set("this", exp.column(ctx.table(tname).primary_key[0], table=x, quoted=True))
    conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
    sel.append("joins", exp.Join(this=s.args["from"].this.copy(), on=_and_all([c.copy() for c in conds]),
                                 side="LEFT"))

    def swap(n):
        return new_agg.copy() if isinstance(n, exp.Subquery) and n.sql() == key else n
    sel.set("expressions", [p.transform(swap) for p in sel.expressions])
    if sel.args.get("order"):
        sel.set("order", sel.args["order"].transform(swap))
    if sel.args.get("where"):
        keep, having = [], []
        for c in _conjuncts(sel.args["where"].this):
            if any(isinstance(n, exp.Subquery) and n.sql() == key for n in c.walk()):
                having.append(c.transform(swap))
            else:
                keep.append(c)
        _where_set(sel, keep)
        if having:
            sel.set("having", exp.Having(this=_and_all(having)))
    sel.set("group", exp.Group(expressions=pks))


def _left_join_count_having_to_inner(sel, R, ctx):
    """With one group per outer row (GROUP BY covers the other tables' primary keys), a HAVING
    COUNT(b.c) > k (k >= 0) discards exactly the NULL-extended groups of LEFT JOIN b."""
    group, having = sel.args.get("group"), sel.args.get("having")
    if not group or not having:
        return
    gkeys = {g.sql() for g in group.expressions}
    for j in sel.args.get("joins") or []:
        if (j.args.get("side") or "").upper() != "LEFT" or not isinstance(j.this, exp.Table):
            continue
        b = j.this.alias_or_name
        btab = R.base_table(b)
        if not btab:
            continue
        others = [(a, n) for a, n, _ in _sources(sel) if a != b]
        ok = True
        for a, n in others:
            t = R.base_table(a)
            if not t or not ctx.table(t).primary_key or R.nullable_side(a):
                ok = False
                break
            if not all(exp.column(k, table=a, quoted=True).sql() in gkeys for k in ctx.table(t).primary_key):
                ok = False
                break
        if not ok:
            continue
        on_ids = set(map(id, _col_refs(j.args.get("on") or exp.true())))
        bad = False
        for c in sel.find_all(exp.Column):
            if c.table == b and id(c) not in on_ids and not any(
                    isinstance(p, exp.AggFunc) and _owner_select(p) is sel for p in _ancestors(c, sel)):
                bad = True
                break
        if bad:
            continue
        for c in _conjuncts(having.this):
            cnt, k = None, None
            if isinstance(c, exp.LT) and isinstance(c.expression, exp.Count):
                cnt, k = c.expression, _lit_int(c.this)
                k = k if k is not None and k >= 0 else None
            elif isinstance(c, exp.LTE) and isinstance(c.expression, exp.Count):
                cnt, k = c.expression, _lit_int(c.this)
                k = k if k is not None and k >= 1 else None
            if cnt is None or k is None or not isinstance(cnt.this, exp.Column) or cnt.this.table != b:
                continue
            col = ctx.table(btab).columns.get(cnt.this.name)
            if col and col.not_null:
                j.set("side", None)
                return


def _ancestors(node, stop):
    p = node.parent
    while p is not None and p is not stop:
        yield p
        p = p.parent


def _fk_exists_to_null_test(sel, R, ctx):
    """EXISTS (SELECT 1 FROM p x WHERE x.k = a.f), with FK a.f -> p(k), is just a.f IS NOT NULL."""
    where = sel.args.get("where")
    if not where:
        return
    out, changed = [], False
    for c in _conjuncts(where.this):
        neg = isinstance(c, exp.Not) and isinstance(c.this, exp.Exists)
        ex = c.this if neg else c
        if isinstance(ex, exp.Exists):
            s = ex.this
            one = _base_single_source(s, R)
            conds = _conjuncts(s.args["where"].this) if one and s.args.get("where") else []
            if one and _is_simple_select(s) and len(conds) == 1 and isinstance(conds[0], exp.EQ):
                x, ptab = one
                l, r = conds[0].this, conds[0].expression
                if isinstance(r, exp.Column) and r.table == x:
                    l, r = r, l
                if isinstance(l, exp.Column) and l.table == x and isinstance(r, exp.Column) and r.table != x:
                    atab = R.base_table(r.table)
                    if atab and any(fk.ref_table == ptab and fk.columns == [r.name] and fk.ref_columns == [l.name]
                                    for fk in ctx.table(atab).foreign_keys):
                        test = exp.Is(this=r.copy(), expression=exp.Null())
                        out.append(test if neg else exp.Not(this=test))
                        changed = True
                        continue
        out.append(c)
    if changed:
        _where_set(sel, out)


def _pull_up_exists(sel, R, ctx):
    """EXISTS (SELECT ... FROM s WHERE p) as a WHERE conjunct equals an inner join with s when
    (a) the outer SELECT is DISTINCT and does not aggregate (duplicates vanish anyway), or
    (b) the subquery reads one table and its correlation fixes a key (at most one partner row)."""
    where = sel.args.get("where")
    if not where:
        return
    distinct = sel.args.get("distinct") and not sel.args["distinct"].args.get("on")
    aggregating = sel.args.get("group") or any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)) \
        or sel.find(exp.Window)
    outer_aliases = {a for a, _, _ in _sources(sel)}
    out, added = [], []
    for c in _conjuncts(where.this):
        if isinstance(c, exp.Exists) and isinstance(c.this, exp.Select):
            s = c.this
            if _is_simple_select(s) and not s.args.get("distinct") and not any(
                    (j.args.get("side") or "") for j in s.args.get("joins") or []) \
                    and all(isinstance(n, exp.Table) for _, n, _ in _sources(s)):
                inner_conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
                for j in s.args.get("joins") or []:
                    inner_conds += _conjuncts(j.args.get("on"))
                ok = bool(distinct and not aggregating)
                if not ok:
                    ok = _all_determined(s, inner_conds, outer_aliases, R, ctx) and any(
                        r.table in outer_aliases for cc in inner_conds for r in _col_refs(cc))
                if ok:
                    for _, n, _ in _sources(s):
                        added.append(exp.Join(this=n.copy()))
                    out.extend(cc.copy() for cc in inner_conds)
                    continue
        out.append(c)
    if added:
        sel.set("joins", (sel.args.get("joins") or []) + added)
        _where_set(sel, out)


def _all_determined(s, conds, outer_aliases, R, ctx):
    """True if every table of subquery s can match at most one row per outer row: repeatedly mark
    a table determined once equalities fix one of its keys to literals, outer columns or columns
    of already-determined tables."""
    aliases = {a: R.base_table(a) for a, _, _ in _sources(s)}
    if not aliases or not all(aliases.values()):
        return False
    known = set(outer_aliases)
    changed = True
    while changed:
        changed = False
        for a, tname in aliases.items():
            if a in known:
                continue
            eq_cols = []
            for cc in conds:
                if not isinstance(cc, exp.EQ):
                    continue
                for l, r in ((cc.this, cc.expression), (cc.expression, cc.this)):
                    if isinstance(l, exp.Column) and l.table == a:
                        refs = _col_refs(r)
                        if (refs and all(x.table in known for x in refs)) or \
                                (not refs and isinstance(r, (exp.Literal, exp.Cast))):
                            eq_cols.append(l.name)
            if _key_covered(eq_cols, tname, ctx):
                known.add(a)
                changed = True
    return all(a in known for a in aliases)


def _eliminate_fk_joins(sel, R, ctx):
    """A join to parent p x ON x.k = a.f (FK a.f -> p(k), k a key) whose only use of x is x.k adds
    nothing: replace x.k by a.f and drop the join (an inner join also requires a.f IS NOT NULL)."""
    srcs = _sources(sel)
    if len(srcs) < 2:
        return
    has_outer = any((j.args.get("side") or "") for _, _, j in srcs if j is not None)
    inner_conds = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
    for _, _, j in srcs:
        if j is not None and not j.args.get("side"):
            inner_conds += _conjuncts(j.args.get("on"))
    for x, node, j in srcs:
        ptab = R.base_table(x)
        if not ptab or not isinstance(node, exp.Table):
            continue
        side = (j.args.get("side") or "").upper() if j is not None else ""
        if side not in ("", "LEFT"):
            continue
        if side == "LEFT":
            pool = _conjuncts(j.args.get("on"))
            if len(pool) != 1:
                continue  # extra ON conditions can leave x NULL even when a.f is not
        else:
            if has_outer and j is None:
                continue
            pool = inner_conds
        mine = None
        for c in pool:
            if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column):
                l, r = c.this, c.expression
                if r.table == x:
                    l, r = r, l
                if l.table == x and r.table != x:
                    mine = [c]
                    break
        if mine is None:
            continue
        atab = R.base_table(r.table)
        if not atab or R.nullable_side(r.table):
            continue
        if not any(fk.ref_table == ptab and fk.columns == [r.name] and fk.ref_columns == [l.name]
                   for fk in ctx.table(atab).foreign_keys):
            continue
        if [l.name] not in ctx.keys(ptab):
            continue
        pcol, acol = ctx.table(ptab).columns.get(l.name), ctx.table(atab).columns.get(r.name)
        if not pcol or not acol or pcol.type != acol.type:
            continue
        cond_ids = set(map(id, mine[0].find_all(exp.Column)))
        refs = [c for c in sel.find_all(exp.Column) if c.table == x and id(c) not in cond_ids]
        if any(c.name != l.name for c in refs):
            continue
        for c in refs:
            c.replace(r.copy())
        # remove x and its condition
        if side == "LEFT":
            joins = [jj for jj in sel.args["joins"] if jj is not j]
            sel.set("joins", joins or None)
        else:
            if j is None:
                joins = list(sel.args.get("joins") or [])
                first = joins.pop(0)
                sel.args["from"].set("this", first.this.copy())
                rest_conds = _conjuncts(first.args.get("on"))
                sel.set("joins", joins or None)
            else:
                joins = [jj for jj in sel.args["joins"] if jj is not j]
                rest_conds = []
                sel.set("joins", joins or None)
            # gather every inner condition into WHERE, minus the eliminated one
            conds = []
            for jj in sel.args.get("joins") or []:
                if not jj.args.get("side") and jj.args.get("on") is not None:
                    conds += _conjuncts(jj.args["on"])
                    jj.set("on", None)
            conds += rest_conds + (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
            conds += [] if j is None else [c for c in _conjuncts(j.args.get("on")) if c is not mine[0]]
            conds = [c for c in conds if c is not mine[0] and c.sql() != mine[0].sql()]
            if not R.not_null(r, _scope_nonnull(sel)):
                conds.append(exp.Not(this=exp.Is(this=r.copy(), expression=exp.Null())))
            _where_set(sel, conds)
        R.refresh(sel.root())
        return _eliminate_fk_joins(sel, R, ctx)


def _equivalence_classes(sel, R):
    """Inner-join equalities a.x = b.y make the two columns interchangeable in the SELECT list,
    GROUP BY, HAVING and ORDER BY. Each class is rewritten to one representative, chosen by
    (table, column) so the choice does not depend on alias names."""
    conds = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
    for j in sel.args.get("joins") or []:
        if not j.args.get("side"):
            conds += _conjuncts(j.args.get("on"))
    local = {a for a, _, _ in _sources(sel)}
    parent = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    cols = {}
    for c in conds:
        if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column):
            a, b = c.this, c.expression
            if a.table not in local or b.table not in local or R.nullable_side(a.table) or R.nullable_side(b.table):
                continue
            ca, cb = R.column(a), R.column(b)
            if not ca or not cb or ca.type != cb.type:
                continue
            for z in (a, b):
                cols[z.sql()] = z
                parent.setdefault(z.sql(), z.sql())
            parent[find(a.sql())] = find(b.sql())
    if not parent:
        return
    classes = {}
    for k in parent:
        classes.setdefault(find(k), []).append(k)
    rep = {}
    for members in classes.values():
        keyed = sorted(((R.base_table(cols[m].table), cols[m].name), m) for m in members)
        if len(keyed) > 1 and keyed[0][0] == keyed[1][0]:
            continue  # tie between two aliases of the same table/column: no canonical choice
        for m in members:
            rep[m] = cols[keyed[0][1]]
    if not rep:
        return

    def tf(n):
        if isinstance(n, exp.Column) and n.table in local and n.sql() in rep:
            return rep[n.sql()].copy()
        return n
    sel.set("expressions", [p.transform(tf, copy=False) for p in sel.expressions])
    for k in ("group", "having", "order"):
        if sel.args.get(k):
            sel.set(k, sel.args[k].transform(tf, copy=False))
    if sel.args.get("where"):
        out = []
        for c in _conjuncts(sel.args["where"].this):
            defining = isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) \
                and isinstance(c.expression, exp.Column) and c.this.sql() in rep and c.expression.sql() in rep
            out.append(c if defining else c.transform(tf, copy=False))
        _where_set(sel, out)


def _right_to_left(sel):
    joins = sel.args.get("joins") or []
    if len(joins) == 1 and (joins[0].args.get("side") or "").upper() == "RIGHT":
        j = joins[0]
        left = sel.args["from"].this
        sel.args["from"].set("this", j.this.copy())
        sel.set("joins", [exp.Join(this=left.copy(), on=j.args.get("on"), side="LEFT")])


def _scalar_subqueries_to_joins(sel, R, ctx):
    """(SELECT x.c FROM t x WHERE x.key = outer.col) in the SELECT list, where x.key is a key of t,
    returns at most one row: identical to LEFT JOIN t x ON x.key = outer.col and selecting x.c."""
    outer_aliases = {a for a, _, _ in _sources(sel)}
    new_joins = []
    for i, p in enumerate(sel.expressions):
        sub = _unalias(p)
        if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, exp.Select):
            continue
        s = sub.this
        if not _is_simple_select(s) or s.args.get("joins") or s.args.get("distinct") or len(s.expressions) != 1:
            continue
        src = s.args["from"].this
        if not isinstance(src, exp.Table) or not ctx.table(src.name):
            continue
        x = src.alias_or_name
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        key_cols, corr = {}, []
        ok = True
        for c in conds:
            if isinstance(c, exp.EQ):
                l, r = c.this, c.expression
                if isinstance(r, exp.Column) and r.table == x:
                    l, r = r, l
                if isinstance(l, exp.Column) and l.table == x and isinstance(r, exp.Column) and r.table in outer_aliases:
                    key_cols[l.name] = r
                    corr.append(c)
                    continue
            ok = False
        if not ok or not any(set(k) <= set(key_cols) for k in ctx.keys(src.name)):
            continue
        val = _unalias(s.expressions[0])
        if any(c.table not in (x,) for c in _col_refs(val)):
            continue
        new_joins.append(exp.Join(this=src.copy(), on=_and_all([c.copy() for c in corr]), side="LEFT"))
        sel.expressions[i] = val.copy() if not isinstance(p, exp.Alias) else exp.alias_(val.copy(), p.alias)
    if new_joins:
        sel.set("expressions", list(sel.expressions))
        sel.set("joins", (sel.args.get("joins") or []) + new_joins)


def _promote_fk_left_joins(sel, R, ctx):
    """LEFT JOIN b ON b.key = a.fk, where a.fk is non-NULL and references b.key, matches exactly
    one row for every left row: it is an inner join."""
    nonnull = _scope_nonnull(sel)
    for j in sel.args.get("joins") or []:
        if (j.args.get("side") or "").upper() != "LEFT" or not isinstance(j.this, exp.Table):
            continue
        b = j.this.alias_or_name
        btab = R.base_table(b)
        conds = _conjuncts(j.args.get("on"))
        if not btab or not conds or not all(isinstance(c, exp.EQ) for c in conds):
            continue
        pairs = []
        for c in conds:
            l, r = c.this, c.expression
            if isinstance(r, exp.Column) and r.table == b:
                l, r = r, l
            if not (isinstance(l, exp.Column) and l.table == b and isinstance(r, exp.Column) and r.table != b):
                pairs = None
                break
            pairs.append((l.name, r))
        if not pairs:
            continue
        a_alias = {r.table for _, r in pairs}
        if len(a_alias) != 1:
            continue
        a_alias = a_alias.pop()
        atab = R.base_table(a_alias)
        if not atab or R.nullable_side(a_alias):
            continue
        fk_ok = False
        for fk in ctx.table(atab).foreign_keys:
            if fk.ref_table != btab:
                continue
            m = dict(zip(fk.ref_columns, fk.columns))
            if set(m) == {k for k, _ in pairs} and all(m[k] == r.name for k, r in pairs):
                fk_ok = True
        if not fk_ok or not any(set(k) == {k2 for k2, _ in pairs} for k in ctx.keys(btab)):
            continue
        if all(R.not_null(r, nonnull) for _, r in pairs):
            j.set("side", None)


def _in_to_exists(sel, R):
    """x IN (SELECT y ...)      -> EXISTS (SELECT 1 ... AND y = x)      (WHERE conjunct)
       x NOT IN (SELECT y ...)  -> NOT EXISTS (... AND y = x)           (only if x, y never NULL)"""
    where = sel.args.get("where")
    if not where:
        return
    out = []
    for c in _conjuncts(where.this):
        neg = isinstance(c, exp.Not)
        node = c.this if neg else c
        if isinstance(node, exp.In) and node.args.get("query") is not None:
            sub = _subquery_select(node)
            if isinstance(sub, exp.Select) and _is_simple_select(sub) and len(sub.expressions) == 1:
                y = _unalias(sub.expressions[0])
                x = node.this
                if neg and not (R.not_null(x, _scope_nonnull(sel)) and R.not_null(y, _scope_nonnull(sub))):
                    out.append(c)
                    continue
                s2 = sub.copy()
                s2.set("distinct", None)
                conds = _conjuncts(s2.args["where"].this) if s2.args.get("where") else []
                _where_set(s2, [cc.copy() for cc in conds] + [exp.EQ(this=y.copy(), expression=x.copy())])
                s2.set("expressions", [exp.Literal.number(1)])
                ex = exp.Exists(this=s2)
                out.append(exp.Not(this=ex) if neg else ex)
                continue
        out.append(c)
    _where_set(sel, out)


def _left_join_anti_to_not_exists(sel, R, ctx):
    """FROM a LEFT JOIN b ON cond WHERE b.c IS NULL (b.c NOT NULL in the schema, b unused elsewhere)
    keeps exactly the a-rows with no match: NOT EXISTS (SELECT 1 FROM b WHERE cond)."""
    joins = sel.args.get("joins") or []
    where = sel.args.get("where")
    if not joins or not where:
        return
    conds = _conjuncts(where.this)
    for j in list(joins):
        if (j.args.get("side") or "").upper() != "LEFT" or not isinstance(j.this, exp.Table):
            continue
        b = j.this.alias_or_name
        btab = R.base_table(b)
        if not btab:
            continue
        on_cols = {c.name for c in _col_refs(j.args.get("on") or exp.true()) if c.table == b}
        hit = None
        for c in conds:
            if isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and isinstance(c.this, exp.Column) \
                    and c.this.table == b:
                col = ctx.table(btab).columns.get(c.this.name)
                eq_cols = {cc.name for e in _conjuncts(j.args.get("on")) if isinstance(e, exp.EQ)
                           for cc in (e.this, e.expression) if isinstance(cc, exp.Column) and cc.table == b}
                if (col and col.not_null) or c.this.name in eq_cols:
                    hit = c
                    break
        if hit is None:
            continue
        # b must not be referenced anywhere except its ON condition and the IS NULL test
        others = [n for n in sel.walk() if isinstance(n, exp.Column) and n.table == b]
        allowed = set(map(id, _col_refs(j.args.get("on")))) | {id(hit.this)}
        if any(id(n) not in allowed for n in others):
            continue
        sub = exp.Select(expressions=[exp.Literal.number(1)]).from_(j.this.copy())
        sub.set("where", exp.Where(this=j.args["on"].copy()))
        joins.remove(j)
        sel.set("joins", joins or None)
        conds = [c for c in conds if c is not hit] + [exp.Not(this=exp.Exists(this=sub))]
        _where_set(sel, conds)
        return _left_join_anti_to_not_exists(sel, R, ctx)


def _drop_redundant_not_null(sel, R):
    """`x IS NOT NULL` is implied when x is a NOT NULL column (non-nullable side) or when another
    conjunct compares x with a strict operator (NULL would fail it anyway)."""
    where = sel.args.get("where")
    if not where:
        return
    conds = _conjuncts(where.this)
    strict_on = set()
    for c in conds:
        if isinstance(c, _STRICT_CMP):
            strict_on.update(s.sql() for s in (c.this, c.expression))
    for j in sel.args.get("joins") or []:
        if not j.args.get("side"):
            for c in _conjuncts(j.args.get("on")):
                if isinstance(c, _STRICT_CMP):
                    strict_on.update(s.sql() for s in (c.this, c.expression))
    group_keys = {g.sql() for g in (sel.args["group"].expressions if sel.args.get("group") else [])}
    having = sel.args.get("having")
    if having:
        for c in _conjuncts(having.this):
            if isinstance(c, _STRICT_CMP):
                strict_on.update(s.sql() for s in (c.this, c.expression) if s.sql() in group_keys)
    keep = []
    for c in conds:
        if isinstance(c, exp.Not) and isinstance(c.this, exp.Is) and isinstance(c.this.expression, exp.Null):
            x = c.this.this
            if x.sql() in strict_on or (isinstance(x, exp.Column) and R.not_null(x)):
                continue
            if _aggregates_strict_in(sel, x):
                continue
        keep.append(c)
    _where_set(sel, keep)


def _strict_in(e, x) -> bool:
    """e is NULL whenever x is NULL (x itself, or arithmetic with x as an operand)."""
    if e.sql() == x.sql():
        return True
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return _strict_in(e.this, x) or _strict_in(e.expression, x)
    if isinstance(e, (exp.Neg, exp.Cast)):
        return _strict_in(e.this, x)
    return False


def _aggregates_strict_in(sel, x) -> bool:
    """A single-row aggregate SELECT whose aggregates all ignore rows where x is NULL."""
    if sel.args.get("group") or sel.args.get("having") or sel.find(exp.Window):
        return False
    projs = [_unalias(p) for p in sel.expressions]
    if not projs:
        return False
    for p in projs:
        if not isinstance(p, (exp.Avg, exp.Sum, exp.Min, exp.Max, exp.Count)):
            return False
        arg = p.this.expressions[0] if isinstance(p.this, exp.Distinct) and p.this.expressions else p.this
        if isinstance(arg, exp.Star) or not _strict_in(arg, x):
            return False
    return True


def _count_col_to_star(sel, R, ctx):
    """COUNT(col) of a never-NULL column is COUNT(*). On the NULL side of an outer join,
    COUNT(b.<any NOT NULL column>) counts matched rows: normalize it to b's first key column."""
    nonnull = _scope_nonnull(sel)
    for cnt in list(sel.find_all(exp.Count)):
        if _owner_select(cnt) is not sel:
            continue
        arg = cnt.this
        if not isinstance(arg, exp.Column):
            continue
        if R.not_null(arg, nonnull):
            cnt.set("this", exp.Star())
            continue
        col, tname = R.column(arg), R.base_table(arg.table)
        if col and col.not_null and tname and R.nullable_side(arg.table) and ctx.table(tname).primary_key:
            cnt.set("this", exp.column(ctx.table(tname).primary_key[0], table=arg.table, quoted=True))


def _alias_columns(exprs):
    by = {}
    for e in exprs:
        if isinstance(e, exp.Column) and e.table:
            by.setdefault(e.table, set()).add(e.name)
    return by


def _reduce_group_by(sel, R, ctx):
    """Grouping columns of a table that include one of its keys can be replaced by its primary key."""
    group = sel.args.get("group")
    if not group:
        return
    keys = list(group.expressions)
    by = _alias_columns(keys)
    replace = {}
    for alias, cols in by.items():
        tname = R.base_table(alias)
        if not tname:
            continue
        cand = ctx.keys(tname)
        if cand and any(set(k) <= cols for k in cand):
            pk = cand[0]
            replace[alias] = pk
    if not replace:
        return
    new, seen = [], set()
    for k in keys:
        if isinstance(k, exp.Column) and k.table in replace:
            for pkc in replace[k.table]:
                c = exp.column(pkc, table=k.table, quoted=True)
                if c.sql() not in seen:
                    seen.add(c.sql())
                    new.append(c)
            continue
        if k.sql() not in seen:
            seen.add(k.sql())
            new.append(k)
    group.set("expressions", new)


def _drop_redundant_distinct(sel, R, ctx):
    d = sel.args.get("distinct")
    if not d or d.args.get("on"):
        return
    projs = [_unalias(p) for p in sel.expressions]
    proj_sql = {p.sql() for p in projs}
    by = _alias_columns(projs)

    def alias_determined(alias):
        tname = R.base_table(alias)
        return bool(tname) and any(set(k) <= by.get(alias, set()) for k in ctx.keys(tname))

    group = sel.args.get("group")
    if group:
        if all(g.sql() in proj_sql or (isinstance(g, exp.Column) and alias_determined(g.table))
               for g in group.expressions):
            sel.set("distinct", None)
        return
    own_aggs = [a for a in sel.find_all(exp.AggFunc) if _owner_select(a) is sel]
    if own_aggs and not any(p.find(exp.Window) for p in projs):
        sel.set("distinct", None)  # an aggregate without GROUP BY returns a single row
        return
    srcs = _sources(sel)
    if srcs and all(isinstance(n, exp.Table) and alias_determined(a) for a, n, _ in srcs):
        sel.set("distinct", None)


def _except_on_key_to_not_exists(tree, R, ctx):
    """SELECT a.k FROM t a [WHERE p] EXCEPT SELECT <e> FROM ... [WHERE q], with k a key of t (unique,
    NOT NULL, so the left side has no duplicates and no NULLs) equals
    SELECT a.k FROM t a WHERE p AND NOT EXISTS (SELECT 1 FROM ... WHERE q AND <e> = a.k)."""
    def tf(n):
        if not isinstance(n, exp.Except) or not n.args.get("distinct", True) or n.args.get("order") \
                or n.args.get("limit"):
            return n
        left, right = n.this, n.expression
        if not isinstance(left, exp.Select) or not isinstance(right, exp.Select):
            return n
        one = _base_single_source(left, R)
        if not one or len(left.expressions) != 1 or len(right.expressions) != 1 or not _is_simple_select(left) \
                or not _is_simple_select(right) or right.args.get("distinct"):
            return n
        a, tname = one
        k = _unalias(left.expressions[0])
        if not (isinstance(k, exp.Column) and k.table == a and [k.name] in ctx.keys(tname)):
            return n
        sub = right.copy()
        conds = _conjuncts(sub.args["where"].this) if sub.args.get("where") else []
        e = _unalias(sub.expressions[0])
        _where_set(sub, conds + [exp.EQ(this=e.copy(), expression=k.copy())])
        sub.set("expressions", [exp.Literal.number(1)])
        out = left.copy()
        lconds = _conjuncts(out.args["where"].this) if out.args.get("where") else []
        _where_set(out, lconds + [exp.Not(this=exp.Exists(this=sub))])
        out.set("with", n.args.get("with"))
        return out
    return tree.transform(tf)


def _self_key_join_elimination(sel, R, ctx):
    """Inside a subquery, a table t b joined by b.k = a.k to an outer row a of the same table
    (k a key, a not NULL-extended) is that very row: replace b by a."""
    local = {a for a, _, _ in _sources(sel)}
    if len(local) < 1 or not sel.args.get("where"):
        return
    conds = _conjuncts(sel.args["where"].this)
    for c in conds:
        if not (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column)):
            continue
        for b, a in ((c.this, c.expression), (c.expression, c.this)):
            if b.table in local and a.table not in local and b.name == a.name:
                bt, at = R.base_table(b.table), R.base_table(a.table)
                # with a single FROM item there would be nothing left to read from: skip
                if bt and bt == at and [b.name] in ctx.keys(bt) and len(local) > 1 \
                        and not R.nullable_side(a.table) and not R.nullable_side(b.table):
                    old, new = b.table, a.table
                    srcs = _sources(sel)
                    rest = [(al, n, j) for al, n, j in srcs if al != old]
                    if any(j is not None and j.args.get("side") for _, _, j in srcs):
                        continue
                    conds2 = [cc for cc in conds if cc is not c]
                    for _, _, j in srcs:
                        if j is not None and j.args.get("on") is not None:
                            conds2 += _conjuncts(j.args["on"])
                    sel.args["from"].set("this", rest[0][1].copy())
                    sel.set("joins", [exp.Join(this=n.copy()) for _, n, _ in rest[1:]] or None)
                    _where_set(sel, conds2)
                    for col in sel.find_all(exp.Column):
                        if col.table == old:
                            col.set("table", exp.to_identifier(new, quoted=True))
                    R.refresh(sel.root())
                    return


def _clean_exists(tree):
    for ex in tree.find_all(exp.Exists):
        s = ex.this
        if isinstance(s, exp.Select):
            s.set("expressions", [exp.Literal.number(1)])
            if not (s.args.get("distinct") and s.args["distinct"].args.get("on")) and not s.args.get("group"):
                s.set("distinct", None)
            if not s.args.get("limit"):
                s.set("order", None)


def _strip_orders(tree, keep_outer: bool):
    for q in list(tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except)):
        if q is tree and keep_outer:
            continue
        if not q.args.get("order") or q.args.get("limit") or q.args.get("offset"):
            continue
        if isinstance(q, exp.Select) and q.args.get("distinct") and q.args["distinct"].args.get("on"):
            continue
        q.set("order", None)


# =============================================================================== stage 5: serialize

_COMMUTATIVE = (exp.EQ, exp.NEQ, exp.Add, exp.Mul, exp.NullSafeEQ, exp.NullSafeNEQ)
_SKIP_ARGS = {"quoted", "comments"}


class _Ser:
    def __init__(self, amap, cte_map):
        self.amap = amap
        self.cte_map = cte_map

    def __call__(self, n):
        if n is None:
            return "~"
        if isinstance(n, list):
            return "[" + ",".join(self(x) for x in n) + "]"
        if not isinstance(n, exp.Expression):
            return repr(n)
        f = getattr(self, "_" + type(n).__name__, None)
        if f:
            return f(n)
        if isinstance(n, _COMMUTATIVE):
            a, b = sorted([self(n.this), self(n.expression)])
            return f"{type(n).__name__}({a},{b})"
        return self.generic(n)

    def generic(self, n):
        parts = []
        for k, v in n.args.items():
            if k in _SKIP_ARGS or v is None or v is False or v == []:
                continue
            parts.append(f"{k}={self(v)}")
        return f"{type(n).__name__}(" + ",".join(parts) + ")"

    def _Identifier(self, n):
        return n.name

    def _Literal(self, n):
        return f"'{n.this}'" if n.is_string else f"#{n.this}"

    def _Column(self, n):
        t = n.table
        return f"{self.amap.get(t, t)}.{n.name}" if t else f"?.{n.name}"

    def _Table(self, n):
        name = self.cte_map.get(n.name, n.name)
        return f"T({name}:{self.amap.get(n.alias_or_name, n.alias_or_name)})"

    def _Subquery(self, n):
        alias = n.alias
        return f"Q({self(n.this)}:{self.amap.get(alias, alias)})" if alias else f"Q({self(n.this)})"

    def _Alias(self, n):
        return self(n.this)

    def _Paren(self, n):
        return self(n.this)

    def _And(self, n):
        return "AND{" + ",".join(sorted(set(self(c) for c in _conjuncts(n)))) + "}"

    def _Or(self, n):
        return "OR{" + ",".join(sorted(set(self(c) for c in _disjuncts(n)))) + "}"

    def _Ordered(self, n):
        desc = bool(n.args.get("desc"))
        nf = n.args.get("nulls_first")
        nf = desc if nf is None else bool(nf)
        return f"ORD({self(n.this)},{'D' if desc else 'A'},{'NF' if nf else 'NL'})"

    def _CTE(self, n):
        return f"CTE({self.cte_map.get(n.alias_or_name, n.alias_or_name)}:{self(n.this)})"

    def _setop(self, n, name):
        distinct = n.args.get("distinct", True)
        items = []

        def flat(x):
            if type(x) is type(n) and x.args.get("distinct", True) == distinct and not x.args.get("order") \
                    and not x.args.get("limit"):
                flat(x.this)
                flat(x.expression)
            else:
                items.append(self(x))
        flat(n.this)
        flat(n.expression)
        if name != "EXCEPT":
            items.sort()
        extra = "".join(f",{k}={self(n.args[k])}" for k in ("order", "limit", "offset", "with") if n.args.get(k))
        return f"{name}{'' if distinct else 'ALL'}[" + ",".join(items) + "]" + extra

    def _Union(self, n):
        return self._setop(n, "UNION")

    def _Intersect(self, n):
        return self._setop(n, "INTERSECT")

    def _Except(self, n):
        return self._setop(n, "EXCEPT")

    def _Group(self, n):
        return "GROUP{" + ",".join(sorted(set(self(e) for e in n.expressions))) + "}"

    def _eq_classes(self, conds):
        """Column = column conjuncts are rendered as equivalence classes, so a=b AND b=c and
        a=b AND a=c (the same constraint) serialize identically."""
        parent, rest = {}, []

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k
        for c in conds:
            if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column):
                a, b = self(c.this), self(c.expression)
                parent.setdefault(a, a)
                parent.setdefault(b, b)
                parent[find(a)] = find(b)
            else:
                rest.append(self(c))
        classes = {}
        for k in parent:
            classes.setdefault(find(k), []).append(k)
        rest += ["EQC{" + ",".join(sorted(m)) + "}" for m in classes.values()]
        return sorted(set(rest))

    def _Select(self, n):
        rels, conds, outer = [], [], []
        frm = n.args.get("from")
        inner_aliases = set()
        if frm is not None:
            rels.append(self(frm.this))
            inner_aliases.add(frm.this.alias_or_name)
        for j in n.args.get("joins") or []:
            side = (j.args.get("side") or "").upper()
            on = j.args.get("on")
            if not side and not j.args.get("using"):
                refs = {c.table for c in _col_refs(on)} if on is not None else set()
                if not outer or refs <= inner_aliases | {j.this.alias_or_name}:
                    rels.append(self(j.this))
                    inner_aliases.add(j.this.alias_or_name)
                    conds += _conjuncts(on)
                    continue
            outer.append(f"{side or 'INNER'}JOIN({self(j.this)} ON {self(on)})")
        if n.args.get("where"):
            conds += _conjuncts(n.args["where"].this)
        conds_ser = self._eq_classes(conds)
        parts = [
            "with=" + self(n.args.get("with")) if n.args.get("with") else "",
            "distinct=" + self(n.args.get("distinct")) if n.args.get("distinct") else "",
            "sel=" + "[" + ",".join(self(e) for e in n.expressions) + "]",
            "from={" + ",".join(sorted(rels)) + "}",
            "outer=[" + ",".join(outer) + "]" if outer else "",
            "where={" + ",".join(conds_ser) + "}" if conds_ser else "",
            "group=" + self(n.args["group"]) if n.args.get("group") else "",
            "having=" + self(n.args["having"].this) if n.args.get("having") else "",
            "order=" + self(n.args["order"].expressions) if n.args.get("order") else "",
            "limit=" + self(n.args["limit"]) if n.args.get("limit") else "",
            "offset=" + self(n.args["offset"]) if n.args.get("offset") else "",
        ]
        return "SELECT(" + ";".join(p for p in parts if p) + ")"


def _alias_definitions(tree, cte_map):
    defs = []
    for node in tree.walk(bfs=False):
        if isinstance(node, exp.Table) and isinstance(node.parent, (exp.From, exp.Join)):
            defs.append((node.alias_or_name, "T:" + cte_map.get(node.name, node.name)))
        elif isinstance(node, exp.Subquery) and isinstance(node.parent, (exp.From, exp.Join)) and node.alias:
            defs.append((node.alias, "Q"))
    seen, out = set(), []
    for a, sig in defs:
        if a not in seen:
            seen.add(a)
            out.append((a, sig))
    return out


def serialize(tree) -> str:
    w = tree.args.get("with")
    cte_map = {c.alias_or_name: f"cte{i}" for i, c in enumerate(w.expressions)} if w else {}
    defs = _alias_definitions(tree, cte_map)
    groups = {}
    for a, sig in defs:
        groups.setdefault(sig, []).append(a)
    total = 1
    for g in groups.values():
        for k in range(2, len(g) + 1):
            total *= k
    sigs = sorted(groups)
    if total > MAX_ALIAS_PERMUTATIONS:
        amap = {a: f"{sig}#{i}" for sig in sigs for i, a in enumerate(groups[sig])}
        return _Ser(amap, cte_map)(tree)
    best = None
    for combo in itertools.product(*(itertools.permutations(groups[s]) for s in sigs)):
        amap = {a: f"{sig}#{i}" for sig, perm in zip(sigs, combo) for i, a in enumerate(perm)}
        s = _Ser(amap, cte_map)(tree)
        if best is None or s < best:
            best = s
    return best


# =============================================================================== entry points

def has_outer_order(tree) -> bool:
    return bool(tree.args.get("order"))


def prepare(sql: str, ctx: Context):
    """Parse + resolve. Raises Invalid / Unsupported."""
    return parse_and_qualify(sql, ctx)


def canonicalize_tree(tree: exp.Expression, ctx: Context, keep_outer_order: bool) -> str:
    tree = tree.copy()
    tree = _strip_parens(tree)
    tree = _normalize_clauses(tree)
    tree = _inline_ctes(tree)
    tree = _unique_aliases(tree)
    tree = _resolve_order_aliases(tree)
    tree = _positional_derived_columns(tree)
    # without a schema every catalog lookup returns "unknown", so only schema-free rewrites fire
    R = Resolver(ctx, tree)
    tree = _except_on_key_to_not_exists(tree, R, ctx)
    R.refresh(tree)
    tree = tree.transform(lambda n: _norm_expr(n, R))
    for _ in range(2):  # rewrites in outer scopes can enable rewrites in inner ones
        R.refresh(tree)
        for sel in reversed([s for s in tree.walk(bfs=False) if isinstance(s, exp.Select)]):
            _scope_rewrites(sel, R, ctx)
        tree = tree.transform(lambda n: _norm_expr(n, R))
    _clean_exists(tree)
    _strip_orders(tree, keep_outer_order)
    return serialize(tree)
