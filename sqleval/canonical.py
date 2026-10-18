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
from decimal import Decimal, InvalidOperation
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from . import semantic
from .schema import Catalog

# sqlglot splits INTERVAL '<n> <unit>' with an unanchored findall, so '0.5 hours' became '5 hours' and
# '-1 day' lost its sign. Split only a whole '<integer> <unit>' string; anything else stays a literal.
exp.INTERVAL_STRING_RE = re.compile(r"^\s*([0-9]+)\s*([a-zA-Z]+)\s*$")
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
        self.keep_root_order = False  # whether the outermost query's row order is compared
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
            if isinstance(o.get("alias"), dict):  # FROM-item aliases and column lists (a function's output, a whole row)
                aliases.add(o["alias"].get("aliasname"))
                aliases.update(c.get("String", {}).get("sval") for c in o["alias"].get("colnames", []))
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


def _expand_grouping_set_ordinals(tree):
    """ROLLUP (1) / CUBE / GROUPING SETS ordinals name select-list entries, as in a plain GROUP BY
    (sqlglot's qualify only expands the plain ones)."""
    for sel in tree.find_all(exp.Select):
        group = sel.args.get("group")
        if not group:
            continue
        for item in (x for k in _GROUPING_SET_ARGS for x in group.args.get(k) or []):
            for lit in list(item.find_all(exp.Literal)):
                if lit.is_int and isinstance(lit.parent, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple, exp.Paren)):
                    pos = int(lit.this)
                    if not 1 <= pos <= len(sel.expressions):
                        raise Invalid("grouping_error")  # GROUP BY position is not in select list
                    lit.replace(_unalias(sel.expressions[pos - 1]).copy())


def _between_kinds(sql):
    """The kinds of BETWEEN in the query, as PostgreSQL's parser sees them."""
    import json
    from pglast.parser import parse_sql_json
    try:
        text = parse_sql_json(sql)
    except Exception:
        return set()
    return {m[1] for m in re.findall(r'"kind":\s*"AEXPR_(NOT_)?(BETWEEN(?:_SYM)?)"', text)}


def _rewrite_is_unknown(sql):
    """sqlglot cannot parse `x IS [NOT] UNKNOWN`, which for a boolean is `x IS [NOT] NULL`."""
    import json
    from pglast.parser import parse_sql_json
    try:
        text = parse_sql_json(sql)
    except Exception:
        return sql
    if '"IS_UNKNOWN"' not in text and '"IS_NOT_UNKNOWN"' not in text:
        return sql
    tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(sql)
    for i in range(len(tokens) - 1, 0, -1):
        t = tokens[i]
        prev = tokens[i - 1] if tokens[i - 1].token_type != sqlglot.tokens.TokenType.NOT else tokens[i - 2]
        if t.text.upper() == "UNKNOWN" and prev.token_type == sqlglot.tokens.TokenType.IS:
            sql = sql[:t.start] + "NULL" + sql[t.end + 1:]
    return sql


def _parse_between_symmetric(sql):
    """sqlglot reads `BETWEEN SYMMETRIC x AND y` as BETWEEN a column named symmetric. PostgreSQL
    defines it as (a >= x AND a <= y) OR (a >= y AND a <= x): drop the keyword and expand. Only when
    every BETWEEN in the query is symmetric, so the expanded nodes are known."""
    kinds = _between_kinds(sql)
    if "BETWEEN_SYM" not in kinds:
        return None
    if "BETWEEN" in kinds:
        raise Unsupported("mixed BETWEEN SYMMETRIC")
    tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(sql)
    cut = [t for prev, t in zip(tokens, tokens[1:])
           if prev.token_type == sqlglot.tokens.TokenType.BETWEEN and t.text.lower() == "symmetric"]
    for t in reversed(cut):
        sql = sql[:t.start] + sql[t.end + 1:]
    tree = sqlglot.parse_one(sql, read="postgres")

    def expand(n):
        if not isinstance(n, exp.Between):
            return n
        a, lo, hi = n.this, n.args["low"], n.args["high"]
        lv, hv = _num_value(lo), _num_value(hi)
        if lv is not None and hv is not None:
            return exp.Between(this=a, low=(lo if lv <= hv else hi).copy(), high=(hi if lv <= hv else lo).copy())
        fwd = exp.And(this=exp.GTE(this=a.copy(), expression=lo.copy()), expression=exp.LTE(this=a.copy(), expression=hi.copy()))
        rev = exp.And(this=exp.GTE(this=a.copy(), expression=hi.copy()), expression=exp.LTE(this=a.copy(), expression=lo.copy()))
        return exp.Paren(this=exp.Or(this=exp.Paren(this=fwd), expression=exp.Paren(this=rev)))
    return tree.transform(expand)


def _natural_to_using(tree, ctx):
    """NATURAL JOIN is JOIN ... USING (the column names both sides have), or a cross join when they
    share none. sqlglot reads it as a join without a condition, so it is resolved here or refused."""
    for sel in tree.find_all(exp.Select):
        joins = sel.args.get("joins") or []
        for i, j in enumerate(joins):
            if (j.args.get("method") or "").upper() != "NATURAL":
                continue
            frm = sel.args.get("from")
            left_nodes = [frm.this] + [jj.this for jj in joins[:i]] if frm is not None else None
            left = [semantic._source_columns(n, ctx) for n in left_nodes] if left_nodes else None
            right = semantic._source_columns(j.this, ctx)
            if right is None or left is None or any(c is None for c in left):
                raise Unsupported("NATURAL JOIN over unknown columns")
            common = [c for c in dict.fromkeys(x for cols in left for x in cols) if c in right]
            if any(sum(c in cols for cols in left) > 1 for c in common):
                raise Invalid("ambiguous_natural_join")  # common column name appears more than once
            j.set("method", None)
            if common:
                j.set("using", [exp.to_identifier(c, quoted=True) for c in common])
            elif (j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL"):
                j.set("on", exp.true())
            else:
                j.set("kind", "CROSS")


# PostgreSQL aggregates that sqlglot parses as plain function calls
_PG_AGGREGATES = {"mode", "every", "json_agg", "jsonb_agg", "json_object_agg", "jsonb_object_agg", "xmlagg",
                  "bit_and", "bit_or", "bit_xor", "range_agg", "range_intersect_agg", "regr_avgx", "regr_avgy",
                  "regr_count", "regr_intercept", "regr_r2", "regr_slope", "regr_sxx", "regr_sxy", "regr_syy",
                  "covar_pop", "covar_samp", "var_pop", "var_samp", "stddev_pop", "stddev_samp"}
_HYPOTHETICAL = {"rank", "dense_rank", "percent_rank", "cume_dist"}


def _mark_aggregates(tree):
    """Make those aggregates AggFunc nodes, so every aggregate-aware rule and check sees them
    (rank() WITHIN GROUP (...) is the hypothetical-set aggregate, not the window function)."""
    def tf(n):
        if isinstance(n, exp.Anonymous):
            name = str(n.this).lower()
            if name == "every" and len(n.expressions) == 1:
                return exp.LogicalAnd(this=n.expressions[0].copy())  # the SQL-standard bool_and
            if name in _PG_AGGREGATES or name in _HYPOTHETICAL and isinstance(n.parent, exp.WithinGroup):
                return exp.AnonymousAggFunc(this=name, expressions=[e.copy() for e in n.expressions])
        return n
    return tree.transform(tf)


def parse_postgres(sql: str) -> exp.Expression:
    """sqlglot's parse, with the PostgreSQL syntax it misreads or rejects handled first."""
    sql = _rewrite_is_unknown(sql)
    return _parse_between_symmetric(sql) or sqlglot.parse_one(sql, read="postgres")


def parse_and_qualify(sql: str, ctx: Context) -> exp.Expression:
    try:
        tree = parse_postgres(sql)
    except Unsupported:
        raise
    except Exception as e:
        raise Unsupported(f"sqlglot_parse: {e}")
    if tree is None:
        raise Unsupported("sqlglot_parse: empty")
    tree = _mark_aggregates(tree)
    _expand_grouping_set_ordinals(tree)
    if ctx.catalog is None:
        try:
            return qualify(tree, dialect="postgres", validate_qualify_columns=False, quote_identifiers=True)
        except Exception as e:
            raise Unsupported(f"qualify: {e}")
    tree = normalize_identifiers(tree, dialect="postgres")
    _check_tables(tree, ctx)
    _natural_to_using(tree, ctx)
    semantic.check_names(tree, ctx)
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
    semantic.check_types(tree, ctx)
    _check_grouping(tree, ctx)
    return tree


_GROUPING_SET_ARGS = ("rollup", "cube", "grouping_sets")
MAX_GROUPING_SETS = 4096


def _has_grouping_sets(sel) -> bool:
    """GROUP BY uses ROLLUP, CUBE or GROUPING SETS, so one query computes several groupings."""
    group = sel.args.get("group")
    return bool(group) and any(group.args.get(k) for k in _GROUPING_SET_ARGS)


def _group_items(group):
    """Every GROUP BY item: the plain expressions, then the ROLLUP / CUBE / GROUPING SETS nodes."""
    return [*group.expressions, *(x for k in _GROUPING_SET_ARGS for x in group.args.get(k) or [])]


def _set_elements(e):
    """One grouping element as a list of expressions: (a, b) -> [a, b], () -> []."""
    if isinstance(e, exp.Paren):
        return _set_elements(e.this)
    if isinstance(e, exp.Tuple):
        return [x for y in e.expressions for x in _set_elements(y)]
    return [e]


def _group_keys(group):
    """All grouping expressions, including those inside ROLLUP / CUBE / GROUPING SETS."""
    keys = []
    for item in _group_items(group):
        if isinstance(item, (exp.Rollup, exp.Cube, exp.GroupingSets)):
            keys += [x for e in item.expressions for x in _set_elements(e)]
        else:
            keys += _set_elements(item)
    return keys


def _grouping_sets(group):
    """The grouping sets a GROUP BY denotes, each a list of expressions. PostgreSQL takes the cross
    product of the items: a plain expression is one set, ROLLUP its prefixes, CUBE every subset,
    GROUPING SETS its listed sets. GROUP BY DISTINCT drops repeated sets; otherwise they repeat rows."""
    def item_sets(item):
        if isinstance(item, (exp.Rollup, exp.Cube)):
            parts = [_set_elements(e) for e in item.expressions]
            if isinstance(item, exp.Rollup):
                return [sum(parts[:i], []) for i in range(len(parts), -1, -1)]
            if 2 ** len(parts) > MAX_GROUPING_SETS:
                raise Unsupported("CUBE too large")
            return [sum((p for j, p in enumerate(parts) if mask >> j & 1), []) for mask in range(2 ** len(parts))]
        if isinstance(item, exp.GroupingSets):
            return [_set_elements(e) for e in item.expressions]
        return [_set_elements(item)]
    sets = [[]]
    for item in _group_items(group):
        sets = [s + t for s in sets for t in item_sets(item)]
        if len(sets) > MAX_GROUPING_SETS:
            raise Unsupported("too many grouping sets")
    if group.args.get("all") is False:  # GROUP BY DISTINCT
        seen, uniq = set(), []
        for s in sets:
            key = frozenset(e.sql() for e in s)
            if key not in seen:
                seen.add(key)
                uniq.append(s)
        sets = uniq
    return sets


def _check_grouping(tree, ctx):
    """PostgreSQL rejects a grouped (or aggregating) SELECT that uses a column outside an aggregate
    unless it is grouped or its table's PRIMARY KEY is grouped (UNIQUE keys do not count). It also
    rejects SELECT DISTINCT ... ORDER BY <expression not in the select list>. A column grouped in
    any ROLLUP / CUBE / GROUPING SETS set counts as grouped (it is NULL in the other sets)."""
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
        gset = {g.sql() for g in (_group_keys(group) if group else [])}
        # with grouping sets, PostgreSQL uses a primary key only if it is grouped in every set
        common = set.intersection(*({e.sql() for e in s} for s in _grouping_sets(group))) \
            if _has_grouping_sets(sel) else gset
        pk_grouped = set()
        for alias, node, _ in _sources(sel):
            if isinstance(node, exp.Table) and ctx.table(node.name) and ctx.table(node.name).primary_key and all(
                    exp.column(k, table=alias, quoted=True).sql() in common for k in ctx.table(node.name).primary_key):
                pk_grouped.add(alias)

        own = {a for a, _, _ in _sources(sel)}

        def ok(e):
            # lenient where unsure: never reject what PostgreSQL would accept
            if isinstance(e, exp.Subquery):
                # a subquery may read only grouped columns of this query (outside its own aggregates)
                inner = {a for s in e.find_all(exp.Select) for a, _, _ in _sources(s)}
                return all(c.table not in own or c.table in inner or c.sql() in gset or c.table in pk_grouped
                           or any(isinstance(a, exp.AggFunc) for a in _ancestors(c, e))
                           for c in e.find_all(exp.Column))
            if e.sql() in gset or isinstance(e, (exp.AggFunc, exp.Filter, exp.Window, exp.WithinGroup,
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


def _inline_named_windows(tree):
    """OVER w with WINDOW w AS (...) is that window spelled out: copy the named definition's
    PARTITION BY, ORDER BY and frame into each reference (a reference may add its own ORDER BY or
    frame, as PostgreSQL allows). The serializer ignores the WINDOW clause, so every reference must
    be resolved; an unknown or nested name is not canonicalized."""
    for sel in list(tree.find_all(exp.Select)):
        defs = sel.args.get("windows")
        if not defs:
            continue
        named = {}
        for d in defs:
            if not isinstance(d.this, exp.Identifier) or d.args.get("alias"):
                raise Unsupported("window definition")
            named[d.this.name] = d
        for w in list(sel.find_all(exp.Window)):
            ref = w.args.get("alias")
            if _owner_select(w) is not sel or not isinstance(ref, exp.Identifier) or w.parent is None \
                    or isinstance(w.parent, exp.Select) and w.arg_key == "windows":
                continue
            d = named.get(ref.name)
            if d is None or w.args.get("partition_by") or (w.args.get("order") and d.args.get("order")):
                raise Unsupported("window reference")
            w.set("alias", None)
            if d.args.get("partition_by"):
                w.set("partition_by", [p.copy() for p in d.args["partition_by"]])
            if not w.args.get("order") and d.args.get("order"):
                w.set("order", d.args["order"].copy())
            if not w.args.get("spec") and d.args.get("spec"):
                w.set("spec", d.args["spec"].copy())
        sel.set("windows", None)
    if any(isinstance(w.args.get("alias"), exp.Identifier) for w in tree.find_all(exp.Window)
           if not (isinstance(w.parent, exp.Select) and w.arg_key == "windows")):
        raise Unsupported("window reference")
    return tree


def _normalize_grouping_sets(tree):
    """ROLLUP / CUBE / GROUPING SETS / mixed GROUP BY lists -> one explicit GROUPING SETS list, so
    ROLLUP (a) and GROUPING SETS ((a), ()) compare equal. Within a set, order and repeats don't
    matter; the sets themselves are unordered but a repeated set repeats its rows, so repeats are
    kept. A single non-empty set is an ordinary GROUP BY."""
    for sel in list(tree.find_all(exp.Select)):
        if not _has_grouping_sets(sel):
            continue
        sets = []
        for s in _grouping_sets(sel.args["group"]):
            uniq = {e.sql(): e for e in s}
            sets.append([uniq[k].copy() for k in sorted(uniq)])
        sets.sort(key=lambda s: [e.sql() for e in s])
        if len(sets) == 1 and sets[0]:
            sel.set("group", exp.Group(expressions=sets[0]))
        else:
            sel.set("group", exp.Group(grouping_sets=[exp.GroupingSets(
                expressions=[exp.Tuple(expressions=s) for s in sets])]))
    return tree


def _literal_value(e):
    """A comparable value for a plain literal, or None. Strings are compared without trailing
    blanks, since CHAR(n) comparison ignores them."""
    if not isinstance(e, exp.Literal):
        return None
    if e.is_string:
        return ("s", e.this.rstrip(" "))
    try:
        return ("n", Decimal(e.this))
    except (InvalidOperation, ValueError):
        return None


def _rows_distinct(sel, ctx) -> bool:
    """A SELECT whose output rows are provably pairwise distinct: DISTINCT, or a projection that
    _drop_redundant_distinct would make DISTINCT redundant for (grouping keys in the output, a
    single-row aggregate, or a key of every source table in the output)."""
    d = sel.args.get("distinct")
    if d:
        return not d.args.get("on")
    if sel.args.get("with"):
        return False
    probe = sel.copy()
    probe.set("distinct", exp.Distinct())
    _drop_redundant_distinct(probe, Resolver(ctx, probe), ctx)
    return not probe.args.get("distinct")


def _branches_disjoint(a, b, ctx) -> bool:
    """No row of SELECT a can equal a row of SELECT b. Proven by one output position that is NULL
    in one branch and never NULL in the other, or holds different literals; or, for two scans of
    the same table that output one of its NOT NULL keys at the same positions, by filtering some
    column to different literals (equal keys would mean the same row satisfying both filters)."""
    if len(a.expressions) != len(b.expressions):
        return False
    ra, rb = Resolver(ctx, a), Resolver(ctx, b)
    na, nb = _scope_nonnull(a), _scope_nonnull(b)
    for pa, pb in zip(a.expressions, b.expressions):
        xa, xb = _unalias(pa), _unalias(pb)
        if isinstance(xa, exp.Null) and rb.not_null(xb, nb):
            return True
        if isinstance(xb, exp.Null) and ra.not_null(xa, na):
            return True
        va, vb = _literal_value(xa), _literal_value(xb)
        if va and vb and va[0] == vb[0] and va != vb:
            return True

    def single_table(s):
        frm = s.args.get("from")
        if s.args.get("joins") or not frm or not isinstance(frm.this, exp.Table):
            return None
        alias = frm.this.alias_or_name
        tname = Resolver(ctx, s).base_table(alias)
        return (alias, tname) if tname else None

    def literal_filters(s, alias):
        out = {}
        for c in _conjuncts(s.args["where"].this) if s.args.get("where") else []:
            if isinstance(c, exp.EQ):
                for col, lit in ((c.this, c.expression), (c.expression, c.this)):
                    if isinstance(col, exp.Column) and col.table == alias and _literal_value(lit):
                        out.setdefault(col.name, set()).add(_literal_value(lit))
        return out
    sa, sb = single_table(a), single_table(b)
    if not sa or not sb or sa[1] != sb[1]:
        return False

    def positions(s, alias):
        return {_unalias(p).name: i for i, p in enumerate(s.expressions)
                if isinstance(_unalias(p), exp.Column) and _unalias(p).table == alias}
    pos_a, pos_b = positions(a, sa[0]), positions(b, sb[0])
    if not any(all(k in pos_a and pos_a[k] == pos_b.get(k) for k in key) for key in ctx.keys(sa[1])):
        return False
    fa, fb = literal_filters(a, sa[0]), literal_filters(b, sb[0])
    return any(len(fa[c]) == 1 and len(fb.get(c, ())) == 1 and fa[c] != fb[c] for c in fa)


def _union_of_distinct_rows_to_all(tree, ctx):
    """A UNION B equals A UNION ALL B when each branch's rows are distinct and no row can be in
    both branches, since UNION then has no duplicates to remove."""
    def tf(n):
        if isinstance(n, exp.Union) and n.args.get("distinct") is not False and not n.args.get("by_name") \
                and isinstance(n.this, exp.Select) and isinstance(n.expression, exp.Select) \
                and not any(s.args.get(k) for s in (n.this, n.expression) for k in ("limit", "offset")) \
                and _rows_distinct(n.this, ctx) and _rows_distinct(n.expression, ctx) \
                and _branches_disjoint(n.this, n.expression, ctx):
            n = n.copy()
            n.set("distinct", False)
        return n
    return tree.transform(tf)


def _select_star_of_setop(tree):
    """SELECT [DISTINCT] <every column, in order> FROM (<set operation>) t is the set operation;
    with DISTINCT, its chain of UNION ALL nodes becomes UNION (DISTINCT over a union of bags is the
    union of sets). INTERSECT / EXCEPT nodes inside are left as they are."""
    def tf(n):
        if not isinstance(n, exp.Select) or n.args.get("joins") or n.args.get("with"):
            return n
        frm = n.args.get("from")
        sub = frm.this if frm else None
        if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, (exp.Union, exp.Intersect, exp.Except)):
            return n
        if any(n.args.get(k) for k in ("where", "group", "having", "order", "limit", "offset")) or n.find(exp.Window) \
                or (n.args.get("distinct") and n.args["distinct"].args.get("on")):
            return n
        q, alias = sub.this, sub.alias_or_name
        cols = sub.args["alias"].args.get("columns") if sub.args.get("alias") else None
        names = [c.name for c in cols] if cols else [p.alias_or_name for p in _leftmost_select(q).expressions]
        projs = [_unalias(p) for p in n.expressions]
        if len(projs) != len(names) or not all(
                isinstance(p, exp.Column) and p.table == alias and p.name == nm for p, nm in zip(projs, names)):
            return n
        out = q.copy()
        if n.args.get("distinct"):
            node = out
            while isinstance(node, exp.Union):
                node.set("distinct", True)
                if not isinstance(node.this, exp.Union):
                    break
                node = node.this
            if not isinstance(out, exp.Union):
                return n  # DISTINCT over INTERSECT ALL / EXCEPT ALL is not the plain set operation
        return out
    return tree.transform(tf)


def _group_by_to_distinct(tree):
    """SELECT e1..en ... GROUP BY e1..en (the same set, no aggregate, HAVING or window) is
    SELECT DISTINCT e1..en: each group is one distinct output row."""
    for sel in list(tree.find_all(exp.Select)):
        group = sel.args.get("group")
        if not group or _has_grouping_sets(sel) or sel.args.get("having") or sel.find(exp.Window) \
                or (sel.args.get("distinct") and sel.args["distinct"].args.get("on")):
            continue
        if any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)):
            continue
        outs = {_unalias(p).sql() for p in sel.expressions}
        if outs == {g.sql() for g in group.expressions} and not any(isinstance(_unalias(p), exp.Star) for p in sel.expressions):
            sel.set("group", None)
            sel.set("distinct", exp.Distinct())
    return tree


def _subst_alias(node, alias, defs):
    """Replace every alias.c column under node by a copy of defs[c] (in place)."""
    for c in list(node.find_all(exp.Column)):
        if c.table == alias:
            c.replace(defs[c.name].copy())
    return node


def _plain_projection(g) -> bool:
    """g's select list has no set-returning, unknown or volatile function (each input row gives one
    output row, and evaluating an expression twice gives the same value)."""
    return _deterministic(g) and not any(
        isinstance(x, (exp.Unnest, exp.Explode, exp.GenerateSeries, exp.Anonymous)) for p in g.expressions for x in p.walk())


def _select_branches(q):
    """The SELECTs of a chain of UNION ALL nodes, or None."""
    if isinstance(q, exp.Union) and q.args.get("distinct") is False and not any(
            q.args.get(k) for k in ("order", "limit", "offset", "with")):
        a, b = _select_branches(q.this), _select_branches(q.expression)
        return a + b if a is not None and b is not None else None
    return [q] if isinstance(q, exp.Select) else None


def _unpivot_to_union(tree):
    """SELECT [DISTINCT] unnest(ARRAY[e1..en]) FROM S WHERE w, and the same with
    CROSS JOIN LATERAL (VALUES (e1), .., (en)) v(c) read as v.c, are
    SELECT e1 FROM S WHERE w UNION [ALL] ... UNION [ALL] SELECT en FROM S WHERE w: each source row
    yields one row per element (UNION when the outer SELECT is DISTINCT)."""
    def tf(n):
        if not isinstance(n, exp.Select) or any(n.args.get(k) for k in ("group", "having", "limit", "offset", "with", "order")) \
                or (n.args.get("distinct") and n.args["distinct"].args.get("on")) or not n.args.get("from"):
            return n
        if any(_owner_select(a) is n for a in n.find_all(exp.AggFunc)) or n.find(exp.Window):
            return n
        distinct = bool(n.args.get("distinct"))
        branches = None
        # unnest(ARRAY[...]) as the only output
        if len(n.expressions) == 1:
            u = _unalias(n.expressions[0])
            if isinstance(u, (exp.Explode, exp.Unnest)) and isinstance(u.this, exp.Array) and u.this.expressions \
                    and not (isinstance(u, exp.Unnest) and len(u.expressions) != 1):
                name = n.expressions[0].alias_or_name
                branches = []
                for e in u.this.expressions:
                    b = n.copy()
                    b.set("distinct", None)
                    b.set("expressions", [exp.alias_(e.copy(), name, quoted=True)])
                    branches.append(b)
        # CROSS JOIN LATERAL (VALUES ...) v(c..)
        if branches is None:
            joins = n.args.get("joins") or []
            for idx, j in enumerate(joins):
                lat = j.this
                if not isinstance(lat, exp.Lateral) or not isinstance(lat.this, exp.Subquery) \
                        or not isinstance(lat.this.this, exp.Values) or j.args.get("side") \
                        or (j.args.get("on") is not None and not (isinstance(j.args["on"], exp.Boolean) and j.args["on"].this)):
                    continue
                alias = lat.args.get("alias")
                cols = [c.name for c in alias.args.get("columns") or []] if alias else []
                rows = lat.this.this.expressions
                if not cols or not rows or any(not isinstance(r, exp.Tuple) or len(r.expressions) != len(cols) for r in rows):
                    break
                if any((jj.args.get("side") or jj.args.get("using")) for jj in joins):
                    break
                v = alias.name
                if any(c.table == v and c.name not in cols for c in n.find_all(exp.Column)):
                    break
                branches = []
                for r in rows:
                    b = n.copy()
                    b.set("distinct", None)
                    b.set("joins", [x for k, x in enumerate(b.args["joins"]) if k != idx] or None)
                    _subst_alias(b, v, dict(zip(cols, r.expressions)))
                    branches.append(b)
                break
        if not branches:
            return n
        out = branches[0]
        for b in branches[1:]:
            out = exp.Union(this=out, expression=b, distinct=distinct)
        if len(branches) == 1 and distinct:
            out.set("distinct", exp.Distinct())
        return out
    return tree.transform(tf, copy=False)


def _rewrite_laterals(tree, ctx, keep_outer_order):
    """LATERAL subqueries as joins. For JOIN [LEFT | CROSS] LATERAL (q) x [ON c]:
      q plain (no aggregate, grouping, DISTINCT, window or LIMIT), inner: the join with q's tables,
        q's WHERE and c joining the outer query; x.col reads q's expression
      q plain over one table T, LEFT, outputs columns only: LEFT JOIN T ON <q's WHERE> AND c
      q aggregates without GROUP BY (exactly one row per outer row), ON TRUE: each x.col is the scalar
        subquery (SELECT <its expression> FROM .. WHERE ..)
      q ... ORDER BY o LIMIT 1, inner, from a single keyed table o: SELECT DISTINCT ON (<o's key>) ...
        ORDER BY <o's key>, o (one row per outer row; ties are arbitrary either way)
      q ... ORDER BY o LIMIT n over one table T correlated only by T.f = <outer column>, inner:
        JOIN (SELECT .., T.f, ROW_NUMBER() OVER (PARTITION BY T.f ORDER BY o) FROM T WHERE ..) x
        ON x.f = <outer column> WHERE x.rn <= n (the rows of T with the same f, ranked alike)."""
    def true_on(j):
        on = j.args.get("on")
        return on is None or (isinstance(on, exp.Boolean) and on.this)

    for sel in list(tree.find_all(exp.Select)):
        changed = True
        while changed:
            changed = False
            joins = sel.args.get("joins") or []
            for idx, j in enumerate(joins):
                lat = j.this
                if not isinstance(lat, exp.Lateral) or not isinstance(lat.this, exp.Subquery) \
                        or not isinstance(lat.this.this, exp.Select) or j.args.get("using"):
                    continue
                alias = lat.args.get("alias")
                if alias is None or alias.args.get("columns"):
                    continue
                x, q = alias.name, lat.this.this
                side = (j.args.get("side") or "").upper()
                if side not in ("", "LEFT") or not q.args.get("from") or q.args.get("with") or q.args.get("offset") \
                        or q.find(exp.Window) or not _plain_projection(q):
                    continue
                defs = {p.alias_or_name: _unalias(p) for p in q.expressions}
                if any(c.table == x and c.name not in defs for c in sel.find_all(exp.Column)):
                    continue
                for o in (q.args["order"].expressions if q.args.get("order") else []):
                    if isinstance(o.this, exp.Column) and not o.this.table and o.this.name in defs:
                        o.set("this", defs[o.this.name].copy())  # ORDER BY <output name>
                own_aggs = any(_owner_select(a) is q for a in q.find_all(exp.AggFunc))
                qconds = _conjuncts(q.args["where"].this) if q.args.get("where") else []
                on = [] if true_on(j) else _conjuncts(j.args["on"])
                qsrc = [q.args["from"].this] + [jj.this for jj in q.args.get("joins") or []]
                qjoins = q.args.get("joins") or []
                plain = not own_aggs and not any(q.args.get(k) for k in ("group", "having", "distinct", "limit"))
                new_sel = None
                if plain and side == "":
                    new_joins = joins[:idx] + [exp.Join(this=qsrc[0].copy())] + [jj.copy() for jj in qjoins] + joins[idx + 1:]
                    sel.set("joins", new_joins)
                    _subst_alias(sel, x, defs)
                    extra = [c.copy() for c in qconds] + [_subst_alias(c.copy(), x, defs) for c in on]
                    _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else []) + extra)
                    changed = True
                elif side == "LEFT" and true_on(j) and _lateral_neighbour(sel, j, q, x, defs, ctx):
                    changed = True
                elif plain and side == "LEFT" and not qjoins and isinstance(qsrc[0], exp.Table) \
                        and all(isinstance(d, exp.Column) and d.table == qsrc[0].alias_or_name for d in defs.values()):
                    cond = [c.copy() for c in qconds] + [_subst_alias(c.copy(), x, defs) for c in on]
                    joins[idx] = exp.Join(this=qsrc[0].copy(), on=_and_all(cond) or exp.true(), side="LEFT")
                    sel.set("joins", joins)
                    _subst_alias(sel, x, defs)
                    changed = True
                elif own_aggs and not any(q.args.get(k) for k in ("group", "having", "distinct", "limit")) \
                        and (true_on(j) or side == ""):
                    # exactly one row per outer row: an inner join's ON just filters the outer rows
                    def scalar(d):
                        sub = q.copy()
                        sub.set("expressions", [d.copy()])
                        sub.set("order", None)
                        return exp.Subquery(this=sub)
                    scalars = {k: scalar(d) for k, d in defs.items()}
                    sel.set("joins", [jj for k, jj in enumerate(joins) if k != idx] or None)
                    _subst_alias(sel, x, scalars)
                    if on:
                        _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
                                   + [_subst_alias(c.copy(), x, scalars) for c in on])
                    changed = True
                elif not own_aggs and not any(q.args.get(k) for k in ("group", "having", "distinct")) \
                        and q.args.get("order") and _lit_int((q.args.get("limit") or exp.Limit()).expression) \
                        and isinstance(q.args["limit"], exp.Limit) and side == "" and true_on(j):
                    n = _lit_int(q.args["limit"].expression)
                    frm = sel.args.get("from").this if sel.args.get("from") else None
                    # top-1 from a single outer table: DISTINCT ON its key
                    if n == 1 and len(joins) == 1 and isinstance(frm, exp.Table) and not any(
                            sel.args.get(k) for k in ("group", "having", "distinct", "limit", "offset")) \
                            and not sel.find(exp.Window) and not any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)):
                        o = frm.alias_or_name
                        tname = frm.name
                        pk = ctx.table(tname).primary_key if ctx.table(tname) else None
                        if pk:
                            keys = [exp.column(k, table=o, quoted=True) for k in pk]
                            order = sel.args.get("order")
                            ok = order is None and not (sel.parent is None and keep_outer_order) or order is not None and \
                                len(order.expressions) == len(keys) and all(
                                    not oo.args.get("desc") and oo.this.sql() == k.sql() for oo, k in zip(order.expressions, keys))
                            if ok:
                                sel.set("joins", [exp.Join(this=qsrc[0].copy())] + [jj.copy() for jj in qjoins])
                                _subst_alias(sel, x, defs)
                                _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
                                           + [c.copy() for c in qconds])
                                sel.set("distinct", exp.Distinct(on=exp.Tuple(expressions=[k.copy() for k in keys])))
                                sel.set("order", exp.Order(expressions=[exp.Ordered(this=k.copy(), nulls_first=False) for k in keys]
                                                           + [oo.copy() for oo in q.args["order"].expressions]))
                                changed = True
                    # top-n correlated by one equality: ROW_NUMBER per correlation value
                    if not changed and not qjoins and isinstance(qsrc[0], exp.Table):
                        t = qsrc[0].alias_or_name
                        corr = [c for c in qconds if any(r.table != t for r in _col_refs(c))]
                        if len(corr) == 1 and isinstance(corr[0], exp.EQ) and not corr[0].find(exp.Subquery):
                            a, b = corr[0].this, corr[0].expression
                            f, outer = (a, b) if isinstance(a, exp.Column) and a.table == t else (b, a)
                            rest_refs = [r for p in q.expressions for r in _col_refs(p)] + _col_refs(q.args["order"]) \
                                + [r for c in qconds if c is not corr[0] for r in _col_refs(c)]
                            if isinstance(f, exp.Column) and f.table == t and isinstance(outer, exp.Column) and outer.table != t \
                                    and all(r.table == t for r in rest_refs):
                                z = q.copy()
                                z.set("order", None)
                                z.set("limit", None)
                                _where_set(z, [c.copy() for c in qconds if c is not corr[0]])
                                win = exp.Window(this=exp.RowNumber(), partition_by=[f.copy()],
                                                 order=q.args["order"].copy(), over="OVER")
                                z.set("expressions", [p.copy() for p in z.expressions]
                                      + [exp.alias_(f.copy(), "_corr", quoted=True), exp.alias_(win, "_rn", quoted=True)])
                                joins[idx] = exp.Join(this=exp.Subquery(this=z, alias=exp.TableAlias(this=exp.to_identifier(x, quoted=True))),
                                                      on=exp.EQ(this=exp.column("_corr", table=x, quoted=True), expression=outer.copy()))
                                sel.set("joins", joins)
                                _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
                                           + [exp.LTE(this=exp.column("_rn", table=x, quoted=True), expression=exp.Literal.number(n))])
                                changed = True
                if changed:
                    break
    return tree



def _lateral_neighbour(sel, j, q, x, defs, ctx) -> bool:
    """FROM T o LEFT JOIN LATERAL (SELECT T.c.. FROM T WHERE T.k = o.k .. AND T.d < o.d ORDER BY T.d DESC
    LIMIT 1) x ON TRUE, with (k.., d) a NOT NULL key of T, reads the previous row of o's k-group in d
    order: x.c is LAG(o.c) OVER (PARTITION BY o.k.. ORDER BY o.d). With T.d > o.d ... ORDER BY T.d it
    is the next row, LEAD. Rewrites sel in place; False if the shape does not fit."""
    frm = sel.args.get("from").this if sel.args.get("from") else None
    # the window only sees sel's own rows, the lookup searches all of T: sel must read every row of T
    if not isinstance(frm, exp.Table) or q.args.get("joins") or not isinstance(q.args["from"].this, exp.Table) \
            or q.args["from"].this.name != frm.name or not ctx.table(frm.name) \
            or sel.args.get("where") or len(sel.args.get("joins") or []) != 1 \
            or any(sel.args.get(k) for k in ("group", "having", "distinct", "limit", "offset")) \
            or any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)) \
            or any(q.args.get(k) for k in ("group", "having", "distinct", "offset")) \
            or not isinstance(q.args.get("limit"), exp.Limit) or _lit_int(q.args["limit"].expression) != 1 \
            or not q.args.get("order") or len(q.args["order"].expressions) != 1:
        return False
    o, t = frm.alias_or_name, q.args["from"].this.alias_or_name
    if not all(isinstance(d, exp.Column) and d.table == t for d in defs.values()):
        return False
    eqs, cmp_ = [], None
    for c in _conjuncts(q.args["where"].this) if q.args.get("where") else []:
        a, b = c.this, c.expression
        if isinstance(c, exp.EQ) and isinstance(a, exp.Column) and isinstance(b, exp.Column):
            a, b = (a, b) if a.table == t else (b, a)
            if a.table == t and b.table == o and a.name == b.name:
                eqs.append(a.name)
                continue
        if isinstance(c, (exp.LT, exp.GT)) and isinstance(a, exp.Column) and isinstance(b, exp.Column) and cmp_ is None \
                and {a.table, b.table} == {t, o} and a.name == b.name:
            before = (isinstance(c, exp.LT)) == (a.table == t)  # T.d < o.d
            cmp_ = (a.name, before)
            continue
        return False
    if cmp_ is None:
        return False
    d, before = cmp_
    ordd = q.args["order"].expressions[0]
    if not isinstance(ordd.this, exp.Column) or ordd.this.table != t or ordd.this.name != d \
            or bool(ordd.args.get("desc")) != before:
        return False
    if sorted(eqs + [d]) not in [sorted(k) for k in ctx.keys(frm.name)]:
        return False
    fn = exp.Lag if before else exp.Lead
    win = {name: exp.Window(this=fn(this=exp.column(col.name, table=o, quoted=True)),
                            partition_by=[exp.column(k, table=o, quoted=True) for k in eqs],
                            order=exp.Order(expressions=[exp.Ordered(this=exp.column(d, table=o, quoted=True), nulls_first=False)]),
                            over="OVER")
           for name, col in defs.items()}
    sel.set("joins", [jj for jj in sel.args["joins"] if jj is not j] or None)
    _subst_alias(sel, x, win)
    return True


def _whole_window_to_scalar(tree):
    """In a SELECT without grouping or aggregates, AGG(x) OVER () aggregates all of its rows: it is
    the scalar subquery (SELECT AGG(x) FROM <the same FROM> WHERE <the same WHERE>)."""
    for sel in list(tree.find_all(exp.Select)):
        if any(sel.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "with")) or not sel.args.get("from"):
            continue
        if any(_owner_select(a) is sel and not any(isinstance(z, exp.Window) for z in _ancestors(a, sel))
               for a in sel.find_all(exp.AggFunc)) or not _deterministic(sel):
            continue
        for w in list(sel.find_all(exp.Window)):
            if _owner_select(w) is not sel or w.args.get("partition_by") or w.args.get("order") or w.args.get("spec") \
                    or not isinstance(w.this, (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)) or w.this.find(exp.Window):
                continue
            sub = exp.Select(expressions=[w.this.copy()])
            for k in ("from", "joins", "where"):
                if sel.args.get(k):
                    sub.set(k, sel.args[k].copy() if k != "joins" else [jj.copy() for jj in sel.args[k]])
            w.replace(exp.Subquery(this=sub))
    return tree


def _distinct_over_union_all(tree):
    """SELECT DISTINCT .. FROM (A UNION ALL B) x [JOIN ..] WHERE p is
    (SELECT DISTINCT .. FROM (A) x .. WHERE p) UNION (SELECT DISTINCT .. FROM (B) x .. WHERE p):
    inner joins distribute over UNION ALL, and DISTINCT over a union of bags is the UNION of sets."""
    def tf(n):
        if not isinstance(n, exp.Select) or not n.args.get("distinct") or n.args["distinct"].args.get("on") \
                or any(n.args.get(k) for k in ("group", "having", "limit", "offset", "with", "order")) or n.find(exp.Window):
            return n
        if any(_owner_select(a) is n for a in n.find_all(exp.AggFunc)):
            return n
        srcs = _sources(n)
        if any(j is not None and (j.args.get("side") or j.args.get("using")) for _, _, j in srcs):
            return n
        for k, (a, node, j) in enumerate(srcs):
            if not isinstance(node, exp.Subquery) or not node.alias or (node.args.get("alias") and node.args["alias"].args.get("columns")):
                continue
            branches = _select_branches(node.this)
            if not branches or len(branches) < 2:
                continue
            names = [p.alias_or_name for p in branches[0].expressions]
            if any(len(b.expressions) != len(names) for b in branches):
                continue
            outs = []
            for b in branches:
                b = b.copy()  # a UNION's columns take the first branch's names
                b.set("expressions", [exp.Alias(this=_unalias(p), alias=exp.to_identifier(nm, quoted=True))
                                      for p, nm in zip(b.expressions, names)])
                c = n.copy()
                target = c.args["from"].this if k == 0 else c.args["joins"][k - 1].this
                target.set("this", b)
                outs.append(c)
            out = outs[0]
            for o in outs[1:]:
                out = exp.Union(this=out, expression=o, distinct=True)
            return out
        return n
    return tree.transform(tf, copy=False)


def _merge_simple_derived(tree):
    """A derived table that only filters and projects (no DISTINCT, grouping, aggregate, window,
    LIMIT or set-returning function) can be read in place: its tables join the outer query, its
    WHERE joins the outer WHERE, and x.c reads its expression for c. It must be the FROM item or an
    inner join (with the outer query's outer joins on other tables only after it); one in an
    EXISTS may also be DISTINCT (handled by _flatten_exists_derived)."""
    for _ in range(8):
        changed = False
        for sel in list(tree.find_all(exp.Select)):
            srcs = _sources(sel)
            for k, (a, node, j) in enumerate(srcs):
                if not isinstance(node, exp.Subquery) or not node.alias or not isinstance(node.this, exp.Select) \
                        or (node.args.get("alias") and node.args["alias"].args.get("columns")):
                    continue
                g = node.this
                if any(g.args.get(k2) for k2 in ("distinct", "group", "having", "limit", "offset", "with")) \
                        or not g.args.get("from") or g.find(exp.Window) or not _plain_projection(g) \
                        or any(_owner_select(x) is g for x in g.find_all(exp.AggFunc)):
                    continue
                gjoins = g.args.get("joins") or []
                defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
                gt = g.args["from"].this
                if j is not None and (j.args.get("side") or "").upper() == "LEFT" and not gjoins and isinstance(gt, exp.Table) \
                        and not j.args.get("using") and all(isinstance(d, exp.Column) and d.table == gt.alias_or_name
                                                            for d in defs.values()) \
                        and not any(c.table == a and c.name not in defs for c in sel.find_all(exp.Column)):
                    # LEFT JOIN (SELECT cols FROM T WHERE w) x ON c  ->  LEFT JOIN T ON c AND w: the rows of T
                    # that match are the same, and an unmatched row reads NULL columns either way
                    on = [_subst_alias(c.copy(), a, defs) for c in _conjuncts(j.args.get("on"))]
                    gconds = _conjuncts(g.args["where"].this) if g.args.get("where") else []
                    j.set("this", gt.copy())
                    j.set("on", _and_all(on + [c.copy() for c in gconds]) or exp.true())
                    _subst_alias(sel, a, defs)
                    changed = True
                    break
                if j is not None and (j.args.get("side") or j.args.get("using") or any(gj.args.get("side") for gj in gjoins)):
                    continue
                if j is None and any((jj.args.get("side") or "").upper() in ("RIGHT", "FULL") for _, _, jj in srcs[1:]):
                    continue
                if j is not None and any(jj is not None and jj.args.get("side") for _, _, jj in srcs):
                    continue
                defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
                if any(c.table == a and c.name not in defs for c in sel.find_all(exp.Column)):
                    continue
                gconds = _conjuncts(g.args["where"].this) if g.args.get("where") else []
                joins = list(sel.args.get("joins") or [])
                if j is None:
                    sel.args["from"].set("this", g.args["from"].this.copy())
                    sel.set("joins", [gj.copy() for gj in gjoins] + joins or None)
                else:
                    on = _conjuncts(j.args.get("on"))
                    pos = joins.index(j)
                    joins[pos:pos + 1] = [exp.Join(this=g.args["from"].this.copy())] + [gj.copy() for gj in gjoins]
                    sel.set("joins", joins)
                    gconds = gconds + on
                _subst_alias(sel, a, defs)
                _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
                           + [c.copy() for c in gconds])
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    return tree


def _distinct_set_branches(tree):
    """A UNION / INTERSECT / EXCEPT without ALL removes duplicates from its inputs, so a plain
    branch may as well be SELECT DISTINCT (which lets join and EXISTS spellings meet)."""
    for n in list(tree.find_all(exp.Union, exp.Intersect, exp.Except)):
        if n.args.get("distinct") is False:
            continue
        for b in (n.this, n.expression):
            if isinstance(b, exp.Select) and not any(b.args.get(k) for k in ("group", "having", "limit", "offset", "distinct")) \
                    and not b.find(exp.Window) and not any(_owner_select(a) is b for a in b.find_all(exp.AggFunc)):
                b.set("distinct", exp.Distinct())
    return tree


def _split_in_intersect(sel):
    """x IN (A INTERSECT B), as a WHERE conjunct, is x IN (A) AND x IN (B): membership in both."""
    where = sel.args.get("where")
    if not where:
        return
    out, changed = [], False
    for c in _conjuncts(where.this):
        q = c.args.get("query") if isinstance(c, exp.In) else None
        q = q.this if isinstance(q, exp.Subquery) else q
        if isinstance(q, exp.Intersect):
            parts, stack = [], [q]
            while stack:
                n = stack.pop()
                if isinstance(n, exp.Intersect):
                    stack += [n.this, n.expression]
                elif isinstance(n, exp.Select) and len(n.expressions) == 1:
                    parts.append(n)
                else:
                    parts = None
                    break
            if parts:
                out += [exp.In(this=c.this.copy(), query=exp.Subquery(this=p.copy())) for p in parts]
                changed = True
                continue
        out.append(c)
    if changed:
        _where_set(sel, out)


def _tree_walk_cte_union_to_all(tree, ctx):
    """In a recursive CTE that walks a single-parent hierarchy, UNION equals UNION ALL: every row is
    reached along exactly one path, so UNION has nothing to remove. The CTE must be

        seed:      SELECT ..., s.K, ... FROM T s [JOIN T p ON p.K = s.P] WHERE <closed>
        recursive: SELECT ..., x.K, ... FROM T x JOIN cte ON x.P = cte.<K's column> [WHERE ...]

    with K a single-column NOT NULL key of T in the same output position in both terms, P a column
    of T, and a seed that is closed: every seed row's parent is NULL (s.P IS NULL), one literal
    (s.P = v), or one row p picked by a unique key (p.U = v). Then each row's key is in the CTE at
    most once (by induction: once per occurrence of its parent, and a seed row's parent is never in
    the CTE, which would take a cycle). On cyclic data the UNION ALL form never terminates; like
    the dataset's labels, that is treated as having no result to compare."""
    def eq_sides(c):
        return [(c.this, c.expression), (c.expression, c.this)] if isinstance(c, exp.EQ) else []

    def column_of(e, alias):
        return e.name if isinstance(e, exp.Column) and e.table == alias else None

    def single_key(tname):
        return {k[0] for k in ctx.keys(tname) if len(k) == 1}

    def check(cte):
        u = cte.this
        if not isinstance(u, exp.Union) or u.args.get("distinct") is False or u.args.get("by_name") \
                or any(u.args.get(k) for k in ("order", "limit", "offset")):
            return False
        seed, rec, name = u.this, u.expression, cte.alias_or_name
        if not isinstance(seed, exp.Select) or not isinstance(rec, exp.Select) \
                or len(seed.expressions) != len(rec.expressions):
            return False
        for s in (seed, rec):
            if s.args.get("group") or s.args.get("having") or s.args.get("limit") or s.args.get("offset") \
                    or (s.args.get("distinct") and s.args["distinct"].args.get("on")) \
                    or any(_owner_select(a) is s for a in s.find_all(exp.AggFunc)) or s.find(exp.Window):
                return False
        cols = cte.args["alias"].args.get("columns") if cte.args.get("alias") else None
        out_names = [c.name for c in cols] if cols else [p.alias_or_name for p in seed.expressions]
        if len(out_names) != len(seed.expressions):
            return False

        # recursive term: FROM T x JOIN cte ON x.P = cte.<col i>, with x.K at position i
        frm, joins = rec.args.get("from"), rec.args.get("joins") or []
        if not frm or not isinstance(frm.this, exp.Table) or len(joins) != 1:
            return False
        x, tname = frm.this.alias_or_name, frm.this.name
        j = joins[0]
        if j.args.get("side") or not isinstance(j.this, exp.Table) or j.this.name != name \
                or not ctx.table(tname) or tname == name:
            return False
        c_alias = j.this.alias_or_name
        on = _conjuncts(j.args.get("on"))
        if len(on) != 1:
            return False
        link = next(((column_of(a, x), b.name) for a, b in eq_sides(on[0])
                     if column_of(a, x) and isinstance(b, exp.Column) and b.table == c_alias), None)
        if not link or link[1] not in out_names:
            return False
        parent_col, i = link[0], out_names.index(link[1])
        key = column_of(_unalias(rec.expressions[i]), x)
        if key not in single_key(tname) or any(t.name == name for t in seed.find_all(exp.Table)):
            return False
        if any(t.name == name for p in rec.expressions for t in p.find_all(exp.Table)):
            return False

        # seed: FROM T s [JOIN T p ON p.K = s.P], s.K at position i, closed
        frm, joins = seed.args.get("from"), seed.args.get("joins") or []
        if not frm or not isinstance(frm.this, exp.Table) or frm.this.name != tname or len(joins) > 1:
            return False
        s_alias = frm.this.alias_or_name
        if column_of(_unalias(seed.expressions[i]), s_alias) != key:
            return False
        conds = _conjuncts(seed.args["where"].this) if seed.args.get("where") else []
        for c in conds:
            if isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and column_of(c.this, s_alias) == parent_col:
                return not joins  # every seed row is a root
            if any(column_of(a, s_alias) == parent_col and _literal_value(b) for a, b in eq_sides(c)):
                return not joins  # every seed row has the same parent
        if len(joins) != 1:
            return False
        jp = joins[0]
        if jp.args.get("side") or not isinstance(jp.this, exp.Table) or jp.this.name != tname:
            return False
        p_alias = jp.this.alias_or_name
        on = _conjuncts(jp.args.get("on"))
        if len(on) != 1 or not any(column_of(a, p_alias) == key and column_of(b, s_alias) == parent_col
                                   for a, b in eq_sides(on[0])):
            return False
        # the one parent row: a unique key of T equals a literal
        return any(column_of(a, p_alias) in single_key(tname) and _literal_value(b)
                   for c in conds for a, b in eq_sides(c))

    w = tree.args.get("with")
    if w and w.args.get("recursive"):
        for cte in w.expressions:
            try:
                if check(cte):
                    cte.this.set("distinct", False)
            except Exception:
                pass  # an unexpected shape: leave the CTE as it is
    return tree


def _grand_total_union_to_grouping_sets(tree, ctx):
    """G UNION ALL T, where G is SELECT ... GROUP BY k1..kn and T is G's grand total, equals
    G with GROUP BY GROUPING SETS ((k1..kn), ()). T is the grand total when it canonicalizes like G
    without GROUP BY and with every grouping key outside an aggregate replaced by NULL, which is
    exactly what the () set computes (one row, also on empty input, since G aggregates). Either
    branch order is accepted: UNION ALL is unordered."""
    def plain_branch(s):
        return (isinstance(s, exp.Select) and not s.args.get("having") and not s.args.get("distinct")
                and not s.args.get("with") and not any(s.args.get(k) for k in ("order", "limit", "offset"))
                and not any(p.find(exp.Window, exp.Subquery) for p in s.expressions)
                and not any(isinstance(n, exp.Anonymous) and n.name.upper() == "GROUPING"
                            for p in s.expressions for n in p.walk()))

    def null_keys(p, keys):
        if p.sql() in keys:
            return exp.Null()
        p = p.copy()
        hits = [n for n in p.walk(prune=lambda n: isinstance(n, exp.AggFunc) or n.sql() in keys)
                if n is not p and n.sql() in keys]
        for n in hits:
            n.replace(exp.Null())
        return p

    def total_of(g, t):
        group = g.args.get("group")
        if not plain_branch(g) or not plain_branch(t) or not group or _has_grouping_sets(g) \
                or t.args.get("group") or len(g.expressions) != len(t.expressions):
            return False
        if not any(_owner_select(a) is g for p in g.expressions for a in p.find_all(exp.AggFunc)):
            return False  # without an aggregate the total branch would return one row per input row
        keys = {k.sql() for k in group.expressions}
        candidate = g.copy()
        candidate.set("group", None)
        candidate.set("expressions", [null_keys(p, keys) for p in g.expressions])
        try:
            return canonicalize_tree(candidate, ctx, False) == canonicalize_tree(t.copy(), ctx, False)
        except Exception:
            return False  # e.g. a correlated branch that cannot be canonicalized on its own

    def tf(n):
        if not isinstance(n, exp.Union) or n.args.get("distinct") is not False or n.args.get("by_name") \
                or n.args.get("with"):
            return n
        # a UNION's ORDER BY names the first branch's output columns (qualify turns ordinals into
        # those names); as ordinals it moves onto either branch, otherwise only onto the first
        order = n.args.get("order")
        first = n.this
        positions = {p.alias_or_name: i for i, p in enumerate(first.expressions, start=1)} \
            if isinstance(first, exp.Select) else {}
        by_position = None
        if order:
            ords = []
            for o in order.expressions:
                k = o.this
                if isinstance(k, exp.Column) and not k.table and k.name in positions:
                    o = o.copy()
                    o.set("this", exp.Literal.number(positions[k.name]))
                elif not (isinstance(k, exp.Literal) and k.is_int):
                    break
                ords.append(o)
            else:
                by_position = exp.Order(expressions=ords)
        for g, t in ((n.this, n.expression), (n.expression, n.this)):
            if (not order or by_position or g is first) and total_of(g, t):
                out = g.copy()
                keys = list(g.args["group"].expressions)
                out.set("group", exp.Group(grouping_sets=[exp.GroupingSets(expressions=[
                    exp.Tuple(expressions=[k.copy() for k in keys]), exp.Tuple(expressions=[])])]))
                if by_position:  # ordinals -> g's select-list expressions, as qualify resolves them
                    keys_out = []
                    for o in by_position.expressions:
                        pos = int(o.this.this)
                        if not 1 <= pos <= len(g.expressions):
                            return n
                        o = o.copy()
                        o.set("this", _unalias(g.expressions[pos - 1]).copy())
                        keys_out.append(o)
                    out.set("order", exp.Order(expressions=keys_out))
                elif order:
                    out.set("order", order.copy())
                for k in ("limit", "offset"):
                    if n.args.get(k):
                        out.set(k, n.args[k].copy())
                return _normalize_grouping_sets(out)
        return n
    return tree.transform(tf)


def _normalize_clauses(tree):
    for sel in list(tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except)):
        lim = sel.args.get("limit")
        if isinstance(lim, exp.Fetch):
            cnt = lim.args.get("count")
            cnt = cnt.copy() if cnt is not None else exp.Literal.number(1)  # FETCH FIRST ROW ONLY
            if lim.args.get("with_ties"):
                # WITH TIES can return more than n rows: keep it distinct from LIMIT n
                sel.set("limit", exp.Fetch(direction="FIRST", count=cnt, with_ties=True))
            else:
                sel.set("limit", exp.Limit(expression=cnt))
        for key in ("limit", "offset"):  # LIMIT '5' is LIMIT 5: the literal is read as a bigint
            node = sel.args.get(key)
            e = node.args.get("expression") if isinstance(node, (exp.Limit, exp.Offset)) else None
            if isinstance(e, exp.Literal) and e.is_string and re.fullmatch(r"\s*\+?\d+\s*", e.this):
                node.set("expression", exp.Literal.number(str(int(e.this))))
    for j in tree.find_all(exp.Join):
        if (j.args.get("kind") or "").upper() in ("INNER", "OUTER"):
            j.set("kind", None)
        if (j.args.get("kind") or "").upper() == "CROSS":
            j.set("kind", None)
    for e in tree.find_all(exp.Extract):
        if isinstance(e.this, (exp.Var, exp.Identifier)):
            e.set("this", exp.var(e.this.name.upper()))
    return tree


_SET_RETURNING = (exp.Unnest, exp.Explode, exp.GenerateSeries)


def _drop_noop_row_limits(tree):
    """OFFSET 0 skips nothing. LIMIT n (n >= 1) changes nothing on a SELECT that aggregates without
    GROUP BY: it returns exactly one row (none if HAVING fails), unless a set-returning function in
    the select list multiplies it."""
    for q in list(tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except)):
        off = q.args.get("offset")
        if off is not None and _lit_int(off.expression) == 0:
            q.set("offset", None)
        lim = q.args.get("limit")
        n = _lit_int(lim.expression) if isinstance(lim, exp.Limit) else None
        if not isinstance(q, exp.Select) or n is None or n < 1 or q.args.get("offset") or q.args.get("group"):
            continue
        if not any(_owner_select(a) is q for p in q.expressions for a in p.find_all(exp.AggFunc)):
            continue
        if any(isinstance(x, _SET_RETURNING) or (isinstance(x, exp.Anonymous) and not isinstance(x.parent, exp.Window))
               for p in q.expressions for x in p.walk()):
            continue  # an unknown function might return a set
        if q.find(exp.Window) or (q.args.get("distinct") and q.args["distinct"].args.get("on")):
            continue
        q.set("limit", None)
    return tree


_VOLATILE = {"random", "setseed", "nextval", "setval", "clock_timestamp", "timeofday", "gen_random_uuid",
             "uuid_generate_v4", "txid_current", "pg_sleep"}


def _deterministic(q) -> bool:
    return not any(isinstance(n, exp.Rand) or (isinstance(n, exp.Anonymous) and n.name.lower() in _VOLATILE)
                   for n in q.walk())


def _inline_ctes(tree):
    """A non-recursive CTE is the same as a derived table at each reference: once, or several times
    when it is deterministic (each read of a deterministic SELECT gives the same rows)."""
    w = tree.args.get("with")
    if not w or w.args.get("recursive"):
        return tree
    for cte in list(w.expressions):
        name = cte.alias_or_name
        refs = [t for t in tree.find_all(exp.Table) if t.name == name and not t.args.get("db")]
        if not refs or (len(refs) > 1 and not _deterministic(cte.this)):
            continue
        for ref in refs:
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


def _prune_derived_columns(tree):
    """A derived table's columns that nothing reads can go: removing a column from a SELECT without
    DISTINCT keeps its rows (a lone constant stands in when no column is read)."""
    read = {}
    for c in tree.find_all(exp.Column):
        if c.table:
            read.setdefault(c.table, set()).add(c.name)
    for sub in list(tree.find_all(exp.Subquery)):
        q = sub.this
        if not isinstance(sub.parent, (exp.From, exp.Join)) or not sub.alias or not isinstance(q, exp.Select) \
                or q.args.get("distinct") or (sub.args.get("alias") and sub.args["alias"].args.get("columns")):
            continue
        used = read.get(sub.alias, set())
        keep = [p for p in q.expressions if p.alias_or_name in used]
        if len(keep) == len(q.expressions):
            continue
        q.set("expressions", keep or [exp.alias_(exp.Literal.number(1), "_unused", quoted=True)])
    return tree


def _positional_derived_columns(tree):
    """Rename the output columns of CTEs and derived tables to _c0, _c1, ... (their names are
    internal to the query; only positions matter)."""
    def sort_key(p, sel):
        tables = {a: (n.name if isinstance(n, exp.Table) else "?") for a, n, _ in _sources(sel)}
        e = _unalias(p).copy()
        for c in e.find_all(exp.Column):
            c.set("table", exp.to_identifier(tables.get(c.table, "^"), quoted=True))
        return e.sql()

    def rename_outputs(query, sort=False):
        if sort and isinstance(query, exp.Select) and not (query.args.get("distinct") and query.args["distinct"].args.get("on")):
            # a derived table's columns are read by name, so their order does not matter
            query.set("expressions", sorted(query.expressions, key=lambda p: sort_key(p, query)))
        names = []
        for i, p in enumerate(_leftmost_select(query).expressions):
            names.append(p.alias_or_name)
        for br in _set_branches(query):
            if not isinstance(br, exp.Select):
                continue
            new_exprs = []
            for i, p in enumerate(br.expressions):
                # in place, not a copy: derived tables nested in p are renamed by this same pass
                new_exprs.append(exp.Alias(this=_unalias(p), alias=exp.to_identifier(f"_c{i}", quoted=True)))
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
            listed = bool(sub.args.get("alias") and sub.args["alias"].args.get("columns"))
            mapping = rename_outputs(sub.this, sort=not listed)
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


_INTERVAL_UNITS = {"YEAR": ("m", 12), "MONTH": ("m", 1), "WEEK": ("d", 7), "DAY": ("d", 1),
                   "HOUR": ("s", 3600), "MINUTE": ("s", 60), "SECOND": ("s", 1)}


def _interval_value(e):
    """(months, days, seconds) of an INTERVAL '<n> <unit>' literal with an integer n, else None."""
    if not isinstance(e, exp.Interval) or not isinstance(e.this, exp.Literal):
        return None
    text = e.this.this.strip()
    unit = e.args.get("unit")
    unit = unit.name if unit is not None else None
    if unit is None:
        m = re.fullmatch(r"(-?\d+)\s*([A-Za-z]+)", text)
        if not m:
            return None
        text, unit = m.group(1), m.group(2)
    if not re.fullmatch(r"-?\d+", text):
        return None
    u = unit.upper()
    u = u[:-1] if u.endswith("S") else u
    if u not in _INTERVAL_UNITS:
        return None
    field, mult = _INTERVAL_UNITS[u]
    n = int(text) * mult
    return (n, 0, 0) if field == "m" else (0, n, 0) if field == "d" else (0, 0, n)


def _add_interval(value, months, days, seconds):
    """PostgreSQL timestamp + interval: months first (clamping the day to the month's end), then
    days, then the time part."""
    import calendar
    y, m = divmod(value.month - 1 + months, 12)
    y, m = value.year + y, m + 1
    if not 1 <= y <= 9999:
        raise ValueError("out of range")
    day = min(value.day, calendar.monthrange(y, m)[1])
    out = value.replace(year=y, month=m, day=day)
    return out + _dt.timedelta(days=days, seconds=seconds)


def _fold_date_arithmetic(node):
    """DATE / TIMESTAMP literal +/- INTERVAL literal -> TIMESTAMP literal; DATE literal +/- integer
    -> DATE literal (PostgreSQL's result types)."""
    if not isinstance(node, (exp.Add, exp.Sub)):
        return None
    sign = -1 if isinstance(node, exp.Sub) else 1
    for a, b in ((node.this, node.expression), (node.expression, node.this)):
        if sign < 0 and a is not node.this:
            break  # interval - date is not defined
        tl = _typed_literal(a)
        if tl is None:
            continue
        kind, val = tl
        try:
            iv = _interval_value(b)
            if iv is not None:
                start = _dt.datetime(val.year, val.month, val.day) if kind == "DATE" else val
                return _make_typed("TIMESTAMP", _add_interval(start, *(sign * x for x in iv)))
            n = _lit_int(b)
            if n is not None and kind == "DATE":
                return _make_typed("DATE", val + _dt.timedelta(days=sign * n))
        except (ValueError, OverflowError):
            return None
    return None


def _num_literal(e):
    """A numeric literal in its canonical spelling (10000.0 and 1e4 -> 10000), for comparisons."""
    if not isinstance(e, exp.Literal) or e.is_string:
        return None
    try:
        d = Decimal(e.this)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    s = str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
    return None if s == e.this else exp.Literal.number(s)


def _is_count(e):
    if isinstance(e, exp.Filter):
        e = e.this
    return isinstance(e, exp.Count)


def _count_subquery(e):
    """The SELECT of a scalar (SELECT COUNT(*) FROM ... WHERE ...) without GROUP BY, or None."""
    if not isinstance(e, exp.Subquery) or not isinstance(e.this, exp.Select):
        return None
    s = e.this
    if len(s.expressions) != 1 or any(s.args.get(k) for k in ("group", "having", "limit", "offset", "with", "distinct")):
        return None
    c = _unalias(s.expressions[0])
    if isinstance(c, exp.Count) and isinstance(c.this, (exp.Star, exp.Literal)) and not isinstance(c.parent, exp.Filter) \
            and not s.find(exp.Window):
        return s
    return None


def _like_matches(pattern, s, ci):
    """PostgreSQL LIKE / ILIKE with the default escape character."""
    rx, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            rx += re.escape(pattern[i + 1])
            i += 2
            continue
        rx += ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        i += 1
    return re.fullmatch(rx, s, re.S | (re.I if ci else 0)) is not None


_NUMERIC_TYPES = re.compile(r"^(INT|INTEGER|BIGINT|SMALLINT|DECIMAL|NUMERIC|DOUBLE|DOUBLE PRECISION|REAL|FLOAT)\b")


def _coerced_number(lit, other, R):
    """An untyped string literal next to a numeric column takes the column's type: '5' is 5. Only
    plain decimal spellings; anything else is left alone (or is an error, caught by the gate)."""
    if not (isinstance(lit, exp.Literal) and lit.is_string and isinstance(other, exp.Column)):
        return None
    t = R.col_type(other)
    if not t or not _NUMERIC_TYPES.match(t):
        return None
    text = lit.this.strip().lstrip("+")
    if t.split("(")[0] in _INT_TYPES:
        return exp.Literal.number(str(int(text))) if re.fullmatch(r"\d+", text) else None
    return exp.Literal.number(text) if re.fullmatch(r"\d+(\.\d*)?|\.\d+", text) else None


_BOOL_WORDS = {"t": True, "true": True, "y": True, "yes": True, "on": True, "1": True,
               "f": False, "false": False, "n": False, "no": False, "off": False, "0": False}


def _coerced_boolean(lit, other, R):
    """An untyped string literal next to a boolean column is that boolean: 't', 'yes', 'on' are TRUE."""
    if not (isinstance(lit, exp.Literal) and lit.is_string and isinstance(other, exp.Column)):
        return None
    if (R.col_type(other) or "") not in ("BOOLEAN", "BOOL"):
        return None
    v = lit.this.strip().lower()
    hits = {b for w, b in _BOOL_WORDS.items() if w == v or len(v) >= (2 if w[0] == "o" else 1) and w.startswith(v)}
    return exp.Boolean(this=hits.pop()) if len(hits) == 1 else None


def _coerce_string_literals(node, R):
    if isinstance(node, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ)):
        for k, o in (("this", "expression"), ("expression", "this")):
            b = _coerced_boolean(node.args.get(k), node.args.get(o), R)
            if b is not None:
                node.set(k, b)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        for k, o in (("this", "expression"), ("expression", "this")):
            num = _coerced_number(node.args.get(k), node.args.get(o), R)
            if num is not None:
                node.set(k, num)
    elif isinstance(node, exp.In) and not node.args.get("query"):
        node.set("expressions", [_coerced_number(v, node.this, R) or v for v in node.expressions])
    elif isinstance(node, exp.Between):
        for k in ("low", "high"):
            num = _coerced_number(node.args.get(k), node.this, R)
            if num is not None:
                node.set(k, num)
    elif isinstance(node, exp.Coalesce) and isinstance(node.this, exp.Column):
        node.set("expressions", [_coerced_number(v, node.this, R) or v for v in node.expressions])
    return node


def _reversed_order(order):
    """ORDER BY with every key reversed, NULLs placement included (ASC NULLS LAST <-> DESC NULLS FIRST)."""
    out = []
    for o in order.expressions:
        desc = bool(o.args.get("desc"))
        nf = o.args.get("nulls_first")
        nf = desc if nf is None else bool(nf)
        out.append(exp.Ordered(this=o.this.copy(), desc=not desc, nulls_first=not nf))
    return exp.Order(expressions=out)


def _frame_bounds(spec):
    """(start, end) of a frame as 'UP' (unbounded preceding), 'P' (n preceding), 'C' (current row),
    'F' (n following), 'UF' (unbounded following); None when it has an EXCLUDE clause or is unknown."""
    if spec is None or spec.args.get("exclude"):
        return None

    def b(v, side):
        v, side = str(v or "").upper(), str(side or "").upper()
        if v == "UNBOUNDED":
            return "UP" if side == "PRECEDING" else "UF"
        if v == "CURRENT ROW":
            return "C"
        return "P" if side == "PRECEDING" else "F" if side == "FOLLOWING" else None
    start = b(spec.args.get("start"), spec.args.get("start_side"))
    end = b(spec.args.get("end"), spec.args.get("end_side")) if spec.args.get("end") else "C"
    return (start, end) if start and end else None


def _total_order_in_scope(w, R):
    """The window's ORDER BY sorts the rows of its partitions strictly: its SELECT reads one table
    (no joins, no GROUP BY) and the ORDER BY keys include a key of that table."""
    sel = _owner_select(w)
    if sel is None or sel.args.get("joins") or sel.args.get("group") or not w.args.get("order"):
        return False
    frm = sel.args.get("from")
    if frm is None or not isinstance(frm.this, exp.Table):
        return False
    alias, tname = frm.this.alias_or_name, R.base_table(frm.this.alias_or_name)
    if not tname:
        return False
    cols = {o.this.name for o in w.args["order"].expressions
            if isinstance(o.this, exp.Column) and o.this.table == alias}
    return any(set(k) <= cols for k in R.ctx.keys(tname))


def _norm_value_window(node, R):
    """first_value / last_value / nth_value(x, 1) depend only on the first or last row of the frame."""
    if not isinstance(node, exp.Window) or node.args.get("alias"):
        return node
    f = node.this
    if isinstance(f, exp.NthValue) and _lit_int(f.args.get("offset")) == 1:
        node.set("this", exp.FirstValue(this=f.this.copy()))  # the frame's first row
        f = node.this
    if not isinstance(f, (exp.FirstValue, exp.LastValue)):
        return node
    spec = node.args.get("spec")
    bounds = ("UP", "C") if spec is None else _frame_bounds(spec)
    if bounds is None:
        return node
    if isinstance(f, exp.LastValue) and bounds[1] == "UF" and bounds[0] in ("UP", "P", "C") \
            and _total_order_in_scope(node, R):
        # the partition's last row in a strict order is its first row in the reversed order
        node.set("this", exp.FirstValue(this=f.this.copy()))
        node.set("order", _reversed_order(node.args["order"]))
        bounds = ("UP", "C")
        f = node.this
    if isinstance(f, exp.FirstValue) and bounds[0] == "UP" and bounds[1] in ("C", "F", "UF"):
        # the frame starts at the partition's first row and is never empty
        node.set("spec", exp.WindowSpec(kind="RANGE", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW")
                 if node.args.get("order") else None)
    return node


def _bool_never_null(e, R):
    """A boolean expression that is TRUE or FALSE, never NULL."""
    if isinstance(e, (exp.Is, exp.Exists, exp.Boolean)):
        return True
    if isinstance(e, exp.Column):
        return R.not_null(e)
    if isinstance(e, exp.Not):
        return _bool_never_null(e.this, R)
    if isinstance(e, (exp.And, exp.Or)):
        return _bool_never_null(e.this, R) and _bool_never_null(e.expression, R)
    if isinstance(e, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        return _never_null(e.this, R, frozenset()) and _never_null(e.expression, R, frozenset())
    return False


def _is_test(e, value):
    """e is `x IS TRUE` / `x IS FALSE` (value True / False): return x."""
    if isinstance(e, exp.Is) and isinstance(e.expression, exp.Boolean) and e.expression.this is value:
        return e.this
    return None


def _truth_context(e):
    """Where only TRUE counts (WHERE, HAVING, ON, a CASE condition), NULL and FALSE are alike: so
    `x IS TRUE` is x and COALESCE(x, FALSE) is x. Holds through AND / OR, not through NOT."""
    if isinstance(e, (exp.And, exp.Or)):
        return type(e)(this=_truth_context(e.this), expression=_truth_context(e.expression))
    x = _is_test(e, True)
    if x is not None:
        return x
    if isinstance(e, exp.Coalesce) and len(e.expressions) == 1 and isinstance(e.expressions[0], exp.Boolean) \
            and e.expressions[0].this is False:
        return e.this
    return e


def _norm_boolean(node, R):
    # truth contexts
    if isinstance(node, (exp.Where, exp.Having)) and node.this is not None:
        node.set("this", _truth_context(node.this))
    elif isinstance(node, exp.Join) and node.args.get("on") is not None:
        node.set("on", _truth_context(node.args["on"]))
    elif isinstance(node, exp.Case) and node.this is None:
        for i in node.args.get("ifs") or []:
            i.set("this", _truth_context(i.this))
        # CASE WHEN NOT c THEN a ELSE b END is CASE WHEN c THEN b ELSE a END when c is never NULL
        ifs = node.args.get("ifs") or []
        if len(ifs) == 1 and isinstance(ifs[0].this, exp.Not) and node.args.get("default") is not None \
                and _bool_never_null(ifs[0].this.this, R):
            return exp.Case(ifs=[exp.If(this=ifs[0].this.this.copy(), true=node.args["default"].copy())],
                            default=ifs[0].args["true"].copy())
    # x = TRUE is x, x = FALSE is NOT x, x <> TRUE is NOT x (the same NULL as x)
    if isinstance(node, (exp.EQ, exp.NEQ)):
        for x, b in ((node.this, node.expression), (node.expression, node.this)):
            if isinstance(b, exp.Boolean) and not isinstance(x, exp.Boolean):
                return x.copy() if (b.this is True) == isinstance(node, exp.EQ) else exp.Not(this=x.copy())
    # x IS [NOT] DISTINCT FROM TRUE / FALSE is x IS [NOT] TRUE / FALSE
    if isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ)) and isinstance(node.expression, exp.Boolean):
        test = exp.Is(this=node.this.copy(), expression=exp.Boolean(this=node.expression.this))
        return test if isinstance(node, exp.NullSafeEQ) else exp.Not(this=test)
    # COALESCE(x, FALSE) is x IS TRUE; COALESCE(x, TRUE) is x IS NOT FALSE
    if isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and isinstance(node.expressions[0], exp.Boolean):
        if node.expressions[0].this is False:
            return exp.Is(this=node.this.copy(), expression=exp.Boolean(this=True))
        return exp.Not(this=exp.Is(this=node.this.copy(), expression=exp.Boolean(this=False)))
    # x IS TRUE / FALSE on a never-NULL x is x / NOT x
    for value in (True, False):
        x = _is_test(node, value)
        if x is not None and _bool_never_null(x, R):
            return x.copy() if value else exp.Not(this=x.copy())
        x = _is_test(node.this, value) if isinstance(node, exp.Not) else None
        if x is not None and _bool_never_null(x, R):
            return exp.Not(this=x.copy()) if value else x.copy()
    # x IS NULL OR NOT x is x IS NOT TRUE; x IS NULL OR x is x IS NOT FALSE (and the AND duals)
    if isinstance(node, (exp.Or, exp.And)) and not isinstance(node.parent, type(node)):
        parts = (_disjuncts if isinstance(node, exp.Or) else _conjuncts)(node)
        keyed = {p.sql() for p in parts}
        merged, used = [], set()
        for p in parts:
            if p.sql() in used:
                continue
            if isinstance(node, exp.Or) and isinstance(p, exp.Is) and isinstance(p.expression, exp.Null):
                x = p.this
                neg, pos = exp.Not(this=x.copy()).sql(), x.sql()
                hit = neg if neg in keyed - used else pos if pos in keyed - used else None
                if hit:
                    used |= {p.sql(), hit}
                    merged.append(exp.Not(this=exp.Is(this=x.copy(), expression=exp.Boolean(this=hit == neg))))
            if isinstance(node, exp.And) and isinstance(p, exp.Not) and isinstance(p.this, exp.Is) \
                    and isinstance(p.this.expression, exp.Null):
                x = p.this.this
                neg, pos = exp.Not(this=x.copy()).sql(), x.sql()
                hit = pos if pos in keyed - used else neg if neg in keyed - used else None
                if hit:
                    used |= {p.sql(), hit}
                    merged.append(exp.Is(this=x.copy(), expression=exp.Boolean(this=hit == pos)))
        if used:
            rest = [p.copy() for p in parts if p.sql() not in used]
            return (_or_all if isinstance(node, exp.Or) else _and_all)(merged + rest)
    # De Morgan: NOT (a OR b) is NOT a AND NOT b; NOT (a AND b) is NOT a OR NOT b (three-valued too)
    if isinstance(node, exp.Not) and isinstance(node.this, (exp.Or, exp.And)):
        dual = exp.And if isinstance(node.this, exp.Or) else exp.Or
        return dual(this=exp.Not(this=node.this.this.copy()), expression=exp.Not(this=node.this.expression.copy()))
    # NOT bool_or(NOT x) is bool_and(x), and the dual (NULL inputs are skipped by both)
    if isinstance(node, exp.Not) and isinstance(node.this, (exp.LogicalOr, exp.LogicalAnd)) \
            and isinstance(node.this.this, exp.Not):
        dual = exp.LogicalAnd if isinstance(node.this, exp.LogicalOr) else exp.LogicalOr
        return dual(this=node.this.this.this.copy())
    # a never-NULL boolean cast to an integer is CASE WHEN x THEN 1 ELSE 0 END
    if isinstance(node, exp.Cast) and node.to.this in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT) \
            and isinstance(node.this, exp.Column) and (R.col_type(node.this) or "") in ("BOOLEAN", "BOOL") \
            and R.not_null(node.this) and node.to.this == exp.DataType.Type.INT:
        return exp.Case(ifs=[exp.If(this=node.this.copy(), true=exp.Literal.number(1))], default=exp.Literal.number(0))
    return node


_TS_LITERAL = re.compile(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(\.\d{1,6})?)?)?\s*"
                         r"(Z|[+-]\d{2}(?::?\d{2})?)?\s*")


def _canonical_instant(text, kind):
    """The canonical spelling of a literal read as `kind` ('tstz', 'ts' or 'date'): a local
    'YYYY-MM-DD HH:MM:SS[.ffffff]' (session time zone), or a UTC instant ending in '+00' when a
    timestamptz literal names its offset. None when the text is not a plain ISO date/time."""
    m = _TS_LITERAL.fullmatch(text)
    if not m:
        return None
    y, mo, d, hh, mm, ss, frac, off = m.groups()
    try:
        v = _dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0),
                         int((frac or ".0")[1:].ljust(6, "0")))
    except ValueError:
        return None
    if kind == "date":
        v = v.replace(hour=0, minute=0, second=0, microsecond=0)
    suffix = ""
    if off and kind == "tstz":
        if off != "Z":
            sign = -1 if off[0] == "-" else 1
            digits = off[1:].replace(":", "")
            v -= sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        suffix = "+00"
    return v.strftime("%Y-%m-%d %H:%M:%S") + (f".{v.microsecond:06d}" if v.microsecond else "") + suffix


def _instant_operand(e, R, utc_wall=False):
    """A literal compared with a timestamptz column, as the canonical CAST('<instant>' AS TIMESTAMPTZ)."""
    kinds = {exp.DataType.Type.TIMESTAMPTZ: "tstz", exp.DataType.Type.TIMESTAMP: "ts", exp.DataType.Type.DATE: "date"}
    if isinstance(e, exp.Literal) and e.is_string:
        text, kind = e.this, "tstz"
    elif isinstance(e, exp.Cast) and isinstance(e.this, exp.Literal) and e.this.is_string and e.to.this in kinds \
            and not e.to.expressions:
        text, kind = e.this.this, kinds[e.to.this]
    else:
        return None
    if utc_wall:  # compared with ts AT TIME ZONE 'UTC': a wall-clock time in UTC
        if kind == "tstz":
            return None
        c = _canonical_instant(text, kind)
        return None if c is None else exp.Cast(this=exp.Literal.string(c + "+00"), to=exp.DataType.build("TIMESTAMPTZ"))
    c = _canonical_instant(text, kind)
    return None if c is None else exp.Cast(this=exp.Literal.string(c), to=exp.DataType.build("TIMESTAMPTZ"))


_UNIT_MICROS = {"MICROSECOND": 1, "MILLISECOND": 1000, "SECOND": 10 ** 6, "SEC": 10 ** 6, "S": 10 ** 6,
                "MINUTE": 60 * 10 ** 6, "MIN": 60 * 10 ** 6, "M": 60 * 10 ** 6, "HOUR": 3600 * 10 ** 6,
                "HR": 3600 * 10 ** 6, "H": 3600 * 10 ** 6, "DAY": 86400 * 10 ** 6, "D": 86400 * 10 ** 6,
                "WEEK": 7 * 86400 * 10 ** 6, "W": 7 * 86400 * 10 ** 6}


def _interval_micros(e):
    """(micros, has_days) of an INTERVAL literal made of seconds..weeks (no months or years), counting
    a day as 24 hours, as interval comparison does; None otherwise."""
    if not isinstance(e, exp.Interval) or not isinstance(e.this, exp.Literal):
        return None
    text = e.this.this.strip()
    unit = e.args.get("unit")
    if unit is not None:
        text = f"{text} {unit.name}"
    total, days = Decimal(0), False
    m = re.fullmatch(r"(-?)(\d+):(\d{2})(?::(\d{2}(?:\.\d+)?))?", text)
    if m:
        v = Decimal(m.group(2)) * 3600 + Decimal(m.group(3)) * 60 + Decimal(m.group(4) or 0)
        total = (-v if m.group(1) else v) * 10 ** 6
    else:
        for num, u in re.findall(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", text):
            u = u.upper()
            u = u[:-1] if u.endswith("S") and u[:-1] in _UNIT_MICROS else u
            if u not in _UNIT_MICROS:
                return None
            days = days or u in ("DAY", "D", "WEEK", "W")
            total += Decimal(num) * _UNIT_MICROS[u]
        if not re.fullmatch(r"(\s*-?\d+(?:\.\d+)?\s*[A-Za-z]+\s*)+", text):
            return None
    if total != total.to_integral_value():
        return None
    return int(total), days


def _micros_interval(n):
    return exp.Interval(this=exp.Literal.string(str(n)), unit=exp.var("MICROSECONDS"))


def _is_ts(e, R):
    return isinstance(e, exp.Column) and (R.col_type(e) or "") in ("TIMESTAMP", "TIMESTAMPTZ")


def _norm_datetime(node, R):
    # timezone(zone, ts) is ts AT TIME ZONE zone; date(x) is x::date
    if isinstance(node, exp.Anonymous) and node.name.lower() == "timezone" and len(node.expressions) == 2:
        return exp.AtTimeZone(this=node.expressions[1].copy(), zone=node.expressions[0].copy())
    if isinstance(node, exp.Date) and node.this is not None and not any(v for k, v in node.args.items() if k != "this"):
        return exp.Cast(this=node.this.copy(), to=exp.DataType.build("DATE"))
    # LAG / LEAD (x, 1) is LAG / LEAD (x)
    if isinstance(node, (exp.Lag, exp.Lead)) and _lit_int(node.args.get("offset")) == 1 and node.args.get("default") is None:
        node.set("offset", None)
    if not isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        if isinstance(node, (exp.Between, exp.In)) and R.col_type(node.this) == "TIMESTAMPTZ":
            for k in ("low", "high") if isinstance(node, exp.Between) else ():
                v = _instant_operand(node.args.get(k), R)
                if v is not None:
                    node.set(k, v)
        return node
    flip = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}
    # CAST(tstz AS DATE) compared with a date: the date's local midnights bound the instant
    for x, d, op in ((node.this, node.expression, type(node)), (node.expression, node.this, flip[type(node)])):
        if isinstance(x, exp.Cast) and x.to.this == exp.DataType.Type.DATE and R.col_type(x.this) == "TIMESTAMPTZ" \
                and op is not exp.NEQ:
            text = d.this if isinstance(d, exp.Literal) and d.is_string else \
                d.this.this if isinstance(d, exp.Cast) and d.to.this == exp.DataType.Type.DATE and isinstance(d.this, exp.Literal) else None
            day = _canonical_instant(text, "date") if text is not None else None
            if day is None:
                continue
            nxt = (_dt.datetime.strptime(day, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            lo, hi = (exp.Cast(this=exp.Literal.string(v), to=exp.DataType.build("TIMESTAMPTZ")) for v in (day, nxt))
            ts = x.this
            if op is exp.EQ:
                return exp.And(this=exp.GTE(this=ts.copy(), expression=lo), expression=exp.LT(this=ts.copy(), expression=hi))
            return {exp.GTE: lambda: exp.GTE(this=ts.copy(), expression=lo), exp.LT: lambda: exp.LT(this=ts.copy(), expression=lo),
                    exp.LTE: lambda: exp.LT(this=ts.copy(), expression=hi), exp.GT: lambda: exp.GTE(this=ts.copy(), expression=hi)}[op]()
    for k, o in (("this", "expression"), ("expression", "this")):
        a, b = node.args.get(k), node.args.get(o)
        # a literal compared with a timestamptz column names an instant
        if isinstance(b, exp.Column) and R.col_type(b) == "TIMESTAMPTZ":
            v = _instant_operand(a, R)
            if v is not None:
                node.set(k, v)
        # (ts AT TIME ZONE 'UTC') compared with a timestamp literal is ts compared with that UTC instant
        if isinstance(b, exp.AtTimeZone) and isinstance(b.args.get("zone"), exp.Literal) \
                and b.args["zone"].this.strip().upper() == "UTC" and R.col_type(b.this) == "TIMESTAMPTZ":
            v = _instant_operand(a, R, utc_wall=True)
            if v is not None:
                node.set(k, v)
                node.set(o, b.this.copy())
    a, b = node.this, node.expression
    # A op B + c (c a fixed number of seconds..hours) is A - B op c: the difference of two timestamps
    # is exact, and adding a time-only interval adds exactly that much
    for left, right, op in ((a, b, type(node)), (b, a, flip[type(node)])):
        if _is_ts(left, R) and isinstance(right, exp.Add):
            for base, iv in ((right.this, right.expression), (right.expression, right.this)):
                im = _interval_micros(iv)
                if _is_ts(base, R) and im is not None and not im[1]:
                    return op(this=exp.Sub(this=left.copy(), expression=base.copy()), expression=iv.copy())
    # EXTRACT(EPOCH FROM A - B) op n is A - B op INTERVAL 'n seconds' (a timestamp difference has no months)
    for x, lit, op in ((a, b, type(node)), (b, a, flip[type(node)])):
        if isinstance(x, exp.Extract) and str(x.this.name).upper() == "EPOCH" and isinstance(x.expression, exp.Sub) \
                and _is_ts(x.expression.this, R) and _is_ts(x.expression.expression, R):
            v = _num_value(lit)
            if v is not None and (v * 10 ** 6) == (v * 10 ** 6).to_integral_value():
                return op(this=x.expression.copy(), expression=_micros_interval(int(v * 10 ** 6)))
    # an interval literal compared with an interval: its length (a day counts as 24 hours)
    for x, iv, k in ((a, b, "expression"), (b, a, "this")):
        im = _interval_micros(iv)
        if im is not None and isinstance(x, exp.Sub) and _is_ts(x.this, R) and _is_ts(x.expression, R):
            node.set(k, _micros_interval(im[0]))
    return node


def _or_all(parts):
    parts = [p for p in parts if p is not None]
    out = parts[0]
    for p in parts[1:]:
        out = exp.Or(this=out, expression=p)
    return out


def _plain_text_arg(e):
    return isinstance(e, exp.Literal) and e.is_string or isinstance(e, (exp.Coalesce, exp.Lower, exp.Upper, exp.Trim))


def _norm_expr_extra(node, R: Resolver):
    """Exact identities added on top of _norm_expr's (each holds for NULL inputs too)."""
    node = _coerce_string_literals(node, R)
    node = _norm_value_window(node, R)
    node = _norm_boolean(node, R)
    node = _norm_datetime(node, R)
    # --- CONCAT of arguments that are never NULL is || (CONCAT only differs by skipping NULLs) ------
    if isinstance(node, exp.Concat) and len(node.expressions) >= 2 \
            and all(_never_null(a, R, frozenset()) for a in node.expressions) \
            and any(_plain_text_arg(a) or R.col_type(a) and semantic.type_category(R.col_type(a)) == ("S", None)
                    for a in node.expressions):
        out = node.expressions[0].copy()
        for a in node.expressions[1:]:
            out = exp.DPipe(this=out, expression=a.copy(), safe=True)
        return out
    # --- btrim(x) / trim(x, ' ') is trim(x); trimming spaces commutes with changing letter case ------
    if isinstance(node, exp.Anonymous) and node.name.lower() == "btrim" and 1 <= len(node.expressions) <= 2:
        node = exp.Trim(this=node.expressions[0].copy(),
                        expression=node.expressions[1].copy() if len(node.expressions) == 2 else None)
    if isinstance(node, exp.Trim):
        chars = node.args.get("expression")
        if isinstance(chars, exp.Literal) and chars.is_string and chars.this == " ":
            node.set("expression", None)
        if str(node.args.get("position") or "").upper() == "BOTH":
            node.set("position", None)
        if isinstance(node.this, (exp.Lower, exp.Upper)) and node.args.get("expression") is None:
            case = node.this
            return type(case)(this=exp.Trim(this=case.this.copy(), position=node.args.get("position")))
    # --- GROUPING(a, b, ..) is the bit mask 2^(n-1) * GROUPING(a) + ... + GROUPING(last) --------------
    if isinstance(node, exp.Anonymous) and node.name.lower() == "grouping" and len(node.expressions) > 1:
        n = len(node.expressions)
        out = None
        for i, a in enumerate(node.expressions):
            g = exp.Anonymous(this="GROUPING", expressions=[a.copy()])
            term = g if i == n - 1 else exp.Mul(this=exp.Literal.number(2 ** (n - 1 - i)), expression=g)
            out = term if out is None else exp.Add(this=out, expression=term)
        return out
    # --- CAST(integer AS NUMERIC / BIGINT) compared with a number: the integer itself ------------------
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between)):
        keys = ("this", "expression") if not isinstance(node, exp.Between) else ("this",)
        others = [node.args.get(k) for k in (("low", "high") if isinstance(node, exp.Between) else keys)]
        for k in keys:
            c = node.args.get(k)
            if isinstance(c, exp.Cast) and isinstance(c.this, exp.Column) and not c.to.expressions \
                    and c.to.this in (exp.DataType.Type.DECIMAL, exp.DataType.Type.BIGINT) \
                    and (R.col_type(c.this) or "").split("(")[0] in _INT_TYPES \
                    and all(o is c or _num_value(o) is not None for o in others):
                node.set(k, c.this.copy())
    # --- GREATEST / LEAST ------------------------------------------------------------------------------
    if isinstance(node, (exp.Greatest, exp.Least)):
        args = [node.this, *node.expressions]
        keys = [a.sql() for a in args]
        changed = False
        for i, a in enumerate(args):
            # COALESCE(x, y) next to y: NULLs are ignored, and y is already there
            if isinstance(a, exp.Coalesce) and len(a.expressions) == 1 and a.expressions[0].sql() in keys[:i] + keys[i + 1:]:
                args[i] = a.this.copy()
                changed = True
        if changed:
            return type(node)(this=args[0], expressions=args[1:])
    if isinstance(node, exp.Case) and node.this is None and len(node.args.get("ifs") or []) == 1 \
            and node.args.get("default") is not None:
        cond, x, c = node.args["ifs"][0].this, node.args["ifs"][0].args.get("true"), node.args["default"]
        if isinstance(cond, (exp.GT, exp.GTE, exp.LT, exp.LTE)) and _never_null(c, R, frozenset()) \
                and _deterministic(x) and _deterministic(c):
            a, b = cond.this, cond.expression
            op = type(cond)
            if a.sql() == c.sql() and b.sql() == x.sql():  # c < x  is  x > c
                a, b, op = b, a, {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}[op]
            if a.sql() == x.sql() and b.sql() == c.sql():
                # CASE WHEN x > c THEN x ELSE c END: a NULL x falls into ELSE, as GREATEST ignores it
                f = exp.Greatest if op in (exp.GT, exp.GTE) else exp.Least
                return f(this=x.copy(), expressions=[c.copy()])
    # --- date arithmetic on literals -------------------------------------------------------------
    folded = _fold_date_arithmetic(node)
    if folded is not None:
        return folded
    # --- x = ANY (ARRAY[...]) is x IN (...); x <> ALL (ARRAY[...]) is x NOT IN (...) ----------------
    quant = node.expression if isinstance(node, (exp.EQ, exp.NEQ)) else None
    if isinstance(quant, exp.Anonymous) and quant.name.upper() == "ALL" and len(quant.expressions) == 1:
        quant = exp.All(this=quant.expressions[0])  # sqlglot reads <> ALL (ARRAY[...]) as a function call
    if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(quant, exp.Any if isinstance(node, exp.EQ) else exp.All):
        arr = quant.this
        arr = arr.this if isinstance(arr, exp.Paren) else arr
        if isinstance(arr, exp.Array) and arr.expressions:
            test = exp.In(this=node.this, expressions=[v.copy() for v in arr.expressions])
            return test if isinstance(node, exp.EQ) else exp.Not(this=test)
    # --- row comparisons: (a, b) = (x, y) is a = x AND b = y; <> is the OR of <> ---------------------
    if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(node.this, exp.Tuple) and isinstance(node.expression, exp.Tuple) \
            and len(node.this.expressions) == len(node.expression.expressions) >= 1:
        parts = [type(node)(this=a.copy(), expression=b.copy())
                 for a, b in zip(node.this.expressions, node.expression.expressions)]
        out = parts[0]
        for p in parts[1:]:
            out = (exp.And if isinstance(node, exp.EQ) else exp.Or)(this=out, expression=p)
        return out
    # --- IS [NOT] DISTINCT FROM between never-NULL operands is = / <> -------------------------------
    if isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ)) and not isinstance(node.expression, exp.Null) \
            and all(R.not_null(s) for s in (node.this, node.expression)):
        return (exp.EQ if isinstance(node, exp.NullSafeEQ) else exp.NEQ)(this=node.this, expression=node.expression)
    # --- COALESCE(x, c) = c is x IS NULL OR x = c --------------------------------------------------
    if isinstance(node, exp.EQ):
        for co, c in ((node.this, node.expression), (node.expression, node.this)):
            d = co.expressions[0] if isinstance(co, exp.Coalesce) and len(co.expressions) == 1 else None
            if isinstance(d, exp.Literal) and isinstance(c, exp.Literal) and d.is_string == c.is_string \
                    and (d.this == c.this if d.is_string else _literal_value(d) == _literal_value(c)):
                x = co.this
                return exp.Or(this=exp.Is(this=x.copy(), expression=exp.Null()),
                              expression=exp.EQ(this=x.copy(), expression=c.copy()))
    # --- char_length is length ------------------------------------------------------------------
    if isinstance(node, exp.Anonymous) and node.name.lower() in ("char_length", "character_length") \
            and len(node.expressions) == 1:
        return exp.Length(this=node.expressions[0].copy())
    # --- strpos(s, p) > 0 is s LIKE '%p%' (p without wildcards) --------------------------------------
    if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
        for pos, k, flipped in ((node.this, node.expression, False), (node.expression, node.this, True)):
            if isinstance(pos, exp.StrPosition) and not pos.args.get("position") and not pos.args.get("occurrence"):
                s, p = pos.this, pos.args.get("substr")
            elif isinstance(pos, exp.Anonymous) and pos.name.lower() == "strpos" and len(pos.expressions) == 2:
                s, p = pos.expressions
            else:
                continue
            pat, v = _lit_str(p), _lit_int(k)
            if pat is None or v is None or any(ch in pat for ch in "%_\\"):
                continue
            op = type(node)
            if flipped:  # k op pos  ->  pos op' k
                op = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(op, op)
            found = (op is exp.GT and v == 0) or (op is exp.GTE and v == 1) or (op is exp.NEQ and v == 0)
            missing = (op is exp.EQ and v == 0) or (op is exp.LT and v == 1) or (op is exp.LTE and v == 0)
            if found or missing:
                like = exp.Like(this=s.copy(), expression=exp.Literal.string(f"%{pat}%"))
                return like if found else exp.Not(this=like)
    # --- simple CASE is a searched CASE of equalities ---------------------------------------------
    if isinstance(node, exp.Case) and node.this is not None and _deterministic(node.this):
        x = node.this
        ifs = [exp.If(this=exp.EQ(this=x.copy(), expression=i.this.copy()), true=i.args.get("true").copy())
               for i in node.args.get("ifs") or []]
        out = exp.Case(ifs=ifs)
        if node.args.get("default") is not None:
            out.set("default", node.args["default"].copy())
        return out
    # --- an enum cast to text, matched against a pattern: the labels that match --------------------
    # an enum's text compared with <> one of its labels, or IN a list of its labels, is the enum test
    if isinstance(node, (exp.NEQ, exp.In)) and isinstance(node.this, exp.Cast) and isinstance(node.this.this, exp.Column) \
            and node.this.to.this in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR) and not node.this.to.expressions:
        info = R.column(node.this.this)
        vals = [node.expression] if isinstance(node, exp.NEQ) else (None if node.args.get("query") else node.expressions)
        if info and info.enum_labels and vals and all(_lit_str(v) in info.enum_labels for v in vals):
            col = node.this.this.copy()
            if isinstance(node, exp.NEQ):
                return exp.NEQ(this=col, expression=exp.Literal.string(_lit_str(vals[0])))
            return exp.In(this=col, expressions=[exp.Literal.string(_lit_str(v)) for v in vals])
    if isinstance(node, (exp.Like, exp.ILike, exp.EQ)) and isinstance(node.this, exp.Cast) \
            and isinstance(node.this.this, exp.Column) and _lit_str(node.expression) is not None \
            and node.this.to.this in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR) and not node.this.to.expressions:
        info = R.column(node.this.this)
        if info and info.enum_labels and not node.args.get("escape"):
            pat = _lit_str(node.expression)
            hits = [l for l in info.enum_labels
                    if (l == pat if isinstance(node, exp.EQ) else _like_matches(pat, l, isinstance(node, exp.ILike)))]
            if hits:
                col = node.this.this.copy()
                if len(hits) == 1:
                    return exp.EQ(this=col, expression=exp.Literal.string(hits[0]))
                return exp.In(this=col, expressions=[exp.Literal.string(h) for h in hits])
    # --- (SELECT SUM(x) ...) IS [NOT] NULL, x never NULL: NULL exactly when there are no rows -------
    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
        inner = _norm_expr_extra(node.this, R)
        if isinstance(inner, exp.Not) and isinstance(inner.this, exp.Exists):
            return inner.this
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and isinstance(node.this, exp.Subquery) \
            and isinstance(node.this.this, exp.Select):
        q = node.this.this
        a = _unalias(q.expressions[0]) if len(q.expressions) == 1 else None
        if isinstance(a, (exp.Sum, exp.Avg, exp.Min, exp.Max)) and not isinstance(a.this, exp.Distinct) \
                and not isinstance(a.parent, exp.Filter) and _owner_select(a) is q \
                and not any(q.args.get(k) for k in ("group", "having", "limit", "offset", "with", "distinct")) \
                and not q.find(exp.Window) and _never_null(a.this, R, _scope_nonnull(q)):
            sub = q.copy()
            sub.set("expressions", [exp.Literal.number(1)])
            return exp.Not(this=exp.Exists(this=sub))
    # --- (SELECT COUNT(*) ...) = 0 is NOT EXISTS (...); > 0 is EXISTS ------------------------------
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        for q, k, flipped in ((node.this, node.expression, False), (node.expression, node.this, True)):
            s = _count_subquery(q)
            v = _lit_int(k)
            if s is None or v is None:
                continue
            op = type(node)
            if flipped:
                op = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(op, op)
            some = (op is exp.GT and v == 0) or (op is exp.GTE and v == 1) or (op is exp.NEQ and v == 0)
            none = (op is exp.EQ and v == 0) or (op is exp.LT and v == 1) or (op is exp.LTE and v == 0)
            if some or none:
                sub = s.copy()
                sub.set("expressions", [exp.Literal.number(1)])
                ex = exp.Exists(this=sub)
                return ex if some else exp.Not(this=ex)
    # --- a - b compared with 0 is a compared with b (exact NUMERIC, or counts: no overflow) ---------
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        for d, z, left in ((node.this, node.expression, True), (node.expression, node.this, False)):
            if isinstance(d, exp.Sub) and _lit_int(z) == 0 and not isinstance(z, exp.Neg) and (
                    _exact_kind(d, R) == "numeric" or (_is_count(d.this) and _is_count(d.expression))):
                a, b = d.this.copy(), d.expression.copy()
                return type(node)(this=a, expression=b) if left else type(node)(this=b, expression=a)
    # --- numeric literals compared in canonical spelling ----------------------------------------------
    if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
        changed = False
        for k in ("this", "expression"):
            lit = _num_literal(node.args.get(k))
            if lit is not None:
                node.set(k, lit)
                changed = True
        if changed:
            return node
    return node


def _norm_expr(node, R: Resolver):
    node = _norm_expr_extra(node, R)
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
    # --- x = ANY (subquery) is x IN (subquery); x <> ALL (subquery) is NOT (x IN (subquery)) ---------
    if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(node.expression, exp.Any if isinstance(node, exp.EQ) else exp.All):
        q = node.expression.this
        if isinstance(q, exp.Subquery):
            q = q.this
        if isinstance(q, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            test = exp.In(this=node.this, query=exp.Subquery(this=q))
            return test if isinstance(node, exp.EQ) else exp.Not(this=test)
    # --- COALESCE skips NULL arguments ------------------------------------------------------------
    if isinstance(node, exp.Coalesce):
        args = [a for a in [node.this, *node.expressions] if not isinstance(a, exp.Null)]
        if len(args) != 1 + len(node.expressions):
            if not args:
                return exp.Null()
            return args[0] if len(args) == 1 else exp.Coalesce(this=args[0], expressions=args[1:])
    # --- same year: date_trunc('year', a) = date_trunc('year', b) ----------------------------------
    if isinstance(node, (exp.EQ, exp.NEQ)) and all(
            isinstance(s, exp.TimestampTrunc) and s.args.get("unit") is not None
            and s.args["unit"].name.upper() == "YEAR" and isinstance(s.this, exp.Column)
            and (R.col_type(s.this) in _DATE_TYPES or R.col_type(s.this) in _TS_TYPES)
            for s in (node.this, node.expression)):
        return type(node)(this=exp.Extract(this=exp.var("YEAR"), expression=node.this.this.copy()),
                          expression=exp.Extract(this=exp.var("YEAR"), expression=node.expression.this.copy()))
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
    # --- a window frame with only a start ends at the current row ------------------------------
    if isinstance(node, exp.WindowSpec) and node.args.get("start") is not None and not node.args.get("end"):
        node.set("end", "CURRENT ROW")
        return node
    # --- LAG / LEAD: offset 1 and a NULL default are the defaults --------------------------------
    if isinstance(node, (exp.Lag, exp.Lead)):
        if _lit_int(node.args.get("offset")) == 1 and node.args.get("default") is None:
            node.set("offset", None)
        if isinstance(node.args.get("default"), exp.Null):
            node.set("default", None)
            if _lit_int(node.args.get("offset")) == 1:
                node.set("offset", None)
    # --- ranking and offset functions ignore the window frame ------------------------------------
    if isinstance(node, exp.Window) and node.args.get("spec") is not None and _frame_insensitive(node.this):
        node.set("spec", None)
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


_FRAMELESS = {"ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST", "NTILE", "LAG", "LEAD"}


def _frame_insensitive(f) -> bool:
    if isinstance(f, (exp.RowNumber, exp.Lag, exp.Lead)):
        return True
    return isinstance(f, exp.Anonymous) and f.name.upper() in _FRAMELESS


def _canonical_window_frames(sel):
    """An aggregate window with ORDER BY and no frame uses RANGE BETWEEN UNBOUNDED PRECEDING AND
    CURRENT ROW. When no two rows of a partition tie on the ORDER BY keys (here: the window's
    PARTITION BY and ORDER BY keys cover every GROUP BY key of its grouped SELECT, and each group is
    one row), RANGE and ROWS frames from UNBOUNDED PRECEDING to CURRENT ROW cover the same rows."""
    group = sel.args.get("group")
    gkeys = {g.sql() for g in group.expressions} if group and not _has_grouping_sets(sel) else None
    for w in sel.find_all(exp.Window):
        if _owner_select(w) is not sel or not w.args.get("order") or not isinstance(w.this, exp.AggFunc):
            continue
        spec = w.args.get("spec")
        if spec is None:
            spec = exp.WindowSpec(kind="RANGE", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW")
            w.set("spec", spec)
        cumulative = (str(spec.args.get("kind") or "").upper() in ("RANGE", "ROWS")
                      and str(spec.args.get("start") or "").upper() == "UNBOUNDED"
                      and str(spec.args.get("start_side") or "").upper() == "PRECEDING"
                      and str(spec.args.get("end") or "").upper() == "CURRENT ROW"
                      and not spec.args.get("end_side") and not spec.args.get("exclude"))
        if not cumulative or str(spec.args.get("kind")).upper() != "RANGE" or not gkeys:
            continue
        covered = {p.sql() for p in w.args.get("partition_by") or []} | \
            {o.this.sql() for o in w.args["order"].expressions}
        if gkeys <= covered:
            spec.set("kind", "ROWS")


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
    # integer-valued aggregates (COUNT, SUM / MIN / MAX of integers) have discrete bounds too
    for agg, lit, left in ((a, b, True), (b, a, False)):
        inner = agg.this if isinstance(agg, exp.Filter) else agg
        if isinstance(inner, (exp.Count, exp.Sum, exp.Min, exp.Max)) and _exact_kind(agg, R) == "int" \
                and _lit_int(lit) is not None:
            # the rewrites over aggregate tests expect strict lower and inclusive upper bounds:
            #   v <= agg -> v-1 < agg      agg < v -> agg <= v-1
            v = _lit_int(lit)
            if isinstance(node, exp.LTE) and not left:
                return exp.LT(this=exp.Literal.number(v - 1), expression=agg)
            if isinstance(node, exp.LT) and left:
                return exp.LTE(this=agg, expression=exp.Literal.number(v - 1))
            return node
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
        if isinstance(node, (exp.LT, exp.LTE)):  # enums sort in declaration order
            labels, i = cinfo.enum_labels, cinfo.enum_labels.index(s)
            inclusive = isinstance(node, exp.LTE)
            hits = labels[:i + inclusive] if col_left else labels[i + (not inclusive):]
            if 0 < len(hits) < len(labels):
                if len(hits) == 1:
                    return exp.EQ(this=col, expression=exp.Literal.string(hits[0]))
                return exp.In(this=col, expressions=[exp.Literal.string(h) for h in sorted(hits)])
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
            elif lkind == "TIMESTAMP" and kind == "DATE" and val.time() == _dt.time(0) and val.tzinfo is None:
                val = val.date()  # a DATE compares with a midnight timestamp as with that date
            elif lkind == "TIMESTAMP" and kind == "DATE" and val.tzinfo is None and isinstance(node, (exp.LT, exp.LTE)):
                # a DATE d is midnight: d < / <= ts (ts later that day) is d <= date(ts), and
                # ts < / <= d is date(ts) < d
                if col_left:
                    return exp.LT(this=col, expression=_make_typed("DATE", val.date() + _dt.timedelta(days=1)))
                return _discrete_bound(exp.LT(this=_make_typed("DATE", val.date()), expression=col), col, False, "DATE",
                                       val.date(), lambda v: _make_typed("DATE", v), lambda v, st: _shift("DATE", v, st))
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


def _group_counts_to_exists(sel, R, ctx):
    """A SELECT that groups one row per row of an outer table o (GROUP BY covers o's primary key,
    every grouping key, output and ORDER BY reads only o) and joins child tables only to test a
    HAVING count is a semi-join on o:
      WHERE x IN (v1..vn) ... HAVING COUNT(DISTINCT x) = n
          -> EXISTS (<children> ... AND x = v1) AND ... AND EXISTS (... AND x = vn)
      one child c with primary key (f, a), joined by c.f = o.k ... HAVING COUNT(*) > 1
          -> EXISTS (SELECT 1 FROM c x JOIN c y ON x.f = y.f AND x.a <> y.a WHERE x.f = o.k ...)
    (two rows of c for one o differ in a). Conditions on o alone stay outside."""
    group, having = sel.args.get("group"), sel.args.get("having")
    if not group or not having or _has_grouping_sets(sel) or sel.args.get("distinct") or sel.find(exp.Window):
        return
    srcs = _sources(sel)
    if len(srcs) < 2 or any(not isinstance(n, exp.Table) or not R.base_table(a) or (j is not None and j.args.get("side"))
                            for a, n, j in srcs):
        return
    o = srcs[0][0]
    others = {a for a, _, _ in srcs[1:]}
    otab = ctx.table(R.base_table(o))
    gkeys = list(group.expressions)
    if not otab.primary_key or not all(isinstance(g, exp.Column) and g.table == o for g in gkeys) \
            or not all(exp.column(k, table=o, quoted=True).sql() in {g.sql() for g in gkeys} for k in otab.primary_key):
        return
    outside = [p for p in sel.expressions] + (list(sel.args["order"].expressions) if sel.args.get("order") else [])
    if any(c.table != o for p in outside for c in _col_refs(p)) or any(p.find(exp.AggFunc, exp.Subquery) for p in outside):
        return
    conds = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
    for _, _, j in srcs[1:]:
        conds += _conjuncts(j.args.get("on"))
    if any(c.find(exp.Subquery, exp.Exists) for c in conds):
        return
    outer_conds = [c for c in conds if all(r.table == o for r in _col_refs(c))]
    child_conds = [c for c in conds if not all(r.table == o for r in _col_refs(c))]
    hv = _conjuncts(having.this)
    if len(hv) != 1 or not isinstance(hv[0], (exp.EQ, exp.LT)):
        return
    h = hv[0]

    def child_select(extra, tables=None):
        tables = tables or [n for a, n, _ in srcs[1:]]
        sub = exp.Select(expressions=[exp.Literal.number(1)]).from_(tables[0].copy())
        for t in tables[1:]:
            sub.append("joins", exp.Join(this=t.copy()))
        _where_set(sub, [c.copy() for c in child_conds] + extra)
        return exp.Exists(this=sub)

    exists = None
    # COUNT(DISTINCT x) = n with x IN (n literals)
    if isinstance(h, exp.EQ):
        cnt, n = (h.this, h.expression) if isinstance(h.this, exp.Count) else (h.expression, h.this)
        n = _lit_int(n)
        if isinstance(cnt, exp.Count) and isinstance(cnt.this, exp.Distinct) and len(cnt.this.expressions) == 1 \
                and n is not None and not isinstance(cnt.parent, exp.Filter):
            x = cnt.this.expressions[0]
            if isinstance(x, exp.Column) and x.table in others:
                for c in child_conds:
                    vals = None
                    if isinstance(c, exp.In) and not c.args.get("query") and c.this.sql() == x.sql():
                        vals = list(c.expressions)
                    elif isinstance(c, exp.EQ) and x.sql() in (c.this.sql(), c.expression.sql()):
                        vals = [c.expression if c.this.sql() == x.sql() else c.this]
                    if vals and all(_literal_value(v) for v in vals) and len({_literal_value(v) for v in vals}) == len(vals) == n:
                        child_conds.remove(c)
                        exists = [child_select([exp.EQ(this=x.copy(), expression=v.copy())]) for v in vals]
                        break
    # 1 < COUNT(*) over one child keyed by (join column, a)
    elif isinstance(h, exp.LT) and _lit_int(h.this) == 1 and isinstance(h.expression, exp.Count) \
            and not isinstance(h.expression.parent, exp.Filter) and len(srcs) == 2 \
            and (isinstance(h.expression.this, (exp.Star, exp.Literal))
                 or (isinstance(h.expression.this, exp.Column) and R.not_null(h.expression.this))):  # counts rows
        c_alias, c_node = srcs[1][0], srcs[1][1]
        ctab = ctx.table(R.base_table(c_alias))
        links = [c for c in child_conds if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column)
                 and isinstance(c.expression, exp.Column) and {c.this.table, c.expression.table} == {o, c_alias}]
        if len(links) == 1 and len(ctab.primary_key) == 2:
            link = links[0]
            f = link.this if link.this.table == c_alias else link.expression
            ok_col = link.expression if f is link.this else link.this
            rest = [k for k in ctab.primary_key if k != f.name]
            filters = [c for c in child_conds if c is not link]
            if len(rest) == 1 and [ok_col.name] in ctx.keys(otab.name) and all(r.table == c_alias for c in filters for r in _col_refs(c)):
                xa, ya = f"{c_alias}_x", f"{c_alias}_y"

                def as_alias(e, a):
                    e = e.copy()
                    for col in e.find_all(exp.Column):
                        if col.table == c_alias:
                            col.set("table", exp.to_identifier(a, quoted=True))
                    return e
                tx = exp.Table(this=c_node.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(xa, quoted=True)))
                ty = exp.Table(this=c_node.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(ya, quoted=True)))
                sub = exp.Select(expressions=[exp.Literal.number(1)]).from_(tx)
                sub.append("joins", exp.Join(this=ty))
                _where_set(sub, [exp.EQ(this=exp.column(f.name, table=xa, quoted=True), expression=ok_col.copy()),
                                 exp.EQ(this=exp.column(f.name, table=ya, quoted=True), expression=exp.column(f.name, table=xa, quoted=True)),
                                 exp.NEQ(this=exp.column(rest[0], table=xa, quoted=True), expression=exp.column(rest[0], table=ya, quoted=True))]
                           + [as_alias(c, xa) for c in filters] + [as_alias(c, ya) for c in filters])
                exists = [exp.Exists(this=sub)]
    if not exists:
        return
    sel.set("joins", None)
    sel.set("group", None)
    sel.set("having", None)
    _where_set(sel, [c.copy() for c in outer_conds] + exists)


def _exact_kind(e, R):
    """'int' or 'numeric' when e has an exact numeric type (integer or NUMERIC/DECIMAL), else None.
    Sums of exact values are exact, so their order and grouping do not matter."""
    if isinstance(e, exp.Column):
        t = (R.col_type(e) or "").upper()
        return "int" if t in _INT_TYPES else "numeric" if t.startswith(("NUMERIC", "DECIMAL")) else None
    if isinstance(e, exp.Literal) and not e.is_string:
        return "int" if re.fullmatch(r"\d+", e.this) else "numeric"
    if isinstance(e, (exp.Neg, exp.Paren)):
        return _exact_kind(e.this, R)
    if isinstance(e, exp.Cast):
        t = e.to.sql(dialect="postgres").upper()
        return "int" if t in _INT_TYPES else "numeric" if t.startswith(("NUMERIC", "DECIMAL")) else None
    if isinstance(e, exp.Filter):
        return _exact_kind(e.this, R)
    if isinstance(e, exp.Count):
        return "int"
    if isinstance(e, (exp.Sum, exp.Min, exp.Max, exp.Avg)) and not isinstance(e.this, exp.Distinct):
        k = _exact_kind(e.this, R)
        return None if k is None else "numeric" if isinstance(e, exp.Avg) else k
    parts = [e.this, e.expression] if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)) \
        else [e.this, *e.expressions] if isinstance(e, exp.Coalesce) else None
    if parts is None:
        return None
    kinds = [_exact_kind(p, R) for p in parts]
    if None in kinds:
        return None
    return "numeric" if "numeric" in kinds else "int"


def _never_null(e, R, nonnull) -> bool:
    if isinstance(e, exp.Literal):
        return True
    if isinstance(e, exp.Column):
        return R.not_null(e, nonnull)
    if isinstance(e, (exp.Neg, exp.Paren, exp.Cast)):
        return _never_null(e.this, R, nonnull)
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return _never_null(e.this, R, nonnull) and _never_null(e.expression, R, nonnull)
    if isinstance(e, exp.Coalesce):
        return any(_never_null(a, R, nonnull) for a in [e.this, *e.expressions])
    return False


def _distribute(e):
    """Multiply out products over sums: a * (b - c) -> a * b - a * c."""
    if isinstance(e, (exp.Add, exp.Sub)):
        return type(e)(this=_distribute(e.this), expression=_distribute(e.expression))
    if isinstance(e, exp.Mul):
        a, b = _distribute(e.this), _distribute(e.expression)
        if isinstance(a, (exp.Add, exp.Sub)):
            return type(a)(this=_distribute(exp.Mul(this=a.this, expression=b.copy())),
                           expression=_distribute(exp.Mul(this=a.expression, expression=b.copy())))
        if isinstance(b, (exp.Add, exp.Sub)):
            return type(b)(this=_distribute(exp.Mul(this=a.copy(), expression=b.this)),
                           expression=_distribute(exp.Mul(this=a.copy(), expression=b.expression)))
        return exp.Mul(this=a, expression=b)
    return e


def _is_null_test(p) -> bool:
    return isinstance(p, exp.Is) and isinstance(p.expression, exp.Null) or \
        isinstance(p, exp.Not) and isinstance(p.this, exp.Is) and isinstance(p.this.expression, exp.Null)


def _negate_null_test(p):
    return p.this.copy() if isinstance(p, exp.Not) else exp.Not(this=p.copy())


def _empty_input_irrelevant(n, sel) -> bool:
    """Whether n's value on an empty input can never show: every group of a plain GROUP BY has rows,
    and an arithmetic operand next to a bare SUM/AVG/MIN/MAX of the same SELECT is NULL then anyway."""
    if sel.args.get("group") and not _has_grouping_sets(sel):
        return True
    p = n.parent
    if isinstance(p, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        other = p.expression if p.this is n else p.this
        return isinstance(other, (exp.Sum, exp.Avg, exp.Min, exp.Max)) and _owner_select(other) is sel \
            and not isinstance(other.parent, exp.Filter) and not isinstance(other.this, exp.Distinct)
    return False


def _canonical_sums(sel, R):
    """Exact-arithmetic identities of SUM, for this SELECT's own aggregates:
      SUM(a * (b - c) ...)                -> SUM(a * b) - SUM(a * c) ...  (exact, non-NULL terms)
      SUM(CASE WHEN p THEN x END)         -> SUM(x) FILTER (WHERE p)
      SUM(CASE WHEN p THEN x ELSE 0 END)  -> COALESCE(SUM(x) FILTER (WHERE p), 0)   (x never NULL)
      SUM(CASE WHEN p THEN 1 ELSE 0 END)  -> COUNT(*) FILTER (WHERE p)
      SUM(COALESCE(x, 0))                 -> COALESCE(SUM(x), 0)
    The last three differ only on an empty input (NULL vs 0), so they apply only where that cannot
    show. AVG(<integer column>::numeric) is AVG(<integer column>): AVG of integers is already a
    numeric sum divided by the count."""
    nonnull = _scope_nonnull(sel)

    def own(n):
        return _owner_select(n) is sel and not any(isinstance(x, exp.Window) for x in _ancestors(n, sel))
    for a in list(sel.find_all(exp.Avg)):
        arg = a.this
        if own(a) and isinstance(arg, exp.Cast) and isinstance(arg.this, exp.Column) \
                and (R.col_type(arg.this) or "").upper() in _INT_TYPES \
                and arg.to.this in (exp.DataType.Type.DECIMAL,) and not arg.to.expressions:
            a.set("this", arg.this.copy())
    for _ in range(2):  # the split creates SUMs the other rules then apply to
        for s in list(sel.find_all(exp.Sum)):
            if not own(s) or isinstance(s.this, exp.Distinct) or s.parent is None:
                continue
            filt = s.parent if isinstance(s.parent, exp.Filter) else None
            node = filt or s
            arg = _distribute(s.this)
            terms, signs = [], []

            def collect(e, sign):
                if isinstance(e, exp.Add):
                    collect(e.this, sign)
                    collect(e.expression, sign)
                elif isinstance(e, exp.Sub):
                    collect(e.this, sign)
                    collect(e.expression, -sign)
                else:
                    terms.append(e)
                    signs.append(sign)
            collect(arg, 1)
            if len(terms) > 1 and all(_exact_kind(t, R) and _never_null(t, R, nonnull) for t in terms):
                def summed(t):
                    out = exp.Sum(this=t.copy())
                    return exp.Filter(this=out, expression=filt.expression.copy()) if filt else out
                expr = summed(terms[0]) if signs[0] > 0 else exp.Neg(this=summed(terms[0]))
                for t, sg in zip(terms[1:], signs[1:]):
                    expr = (exp.Add if sg > 0 else exp.Sub)(this=expr, expression=summed(t))
                node.replace(expr)
                continue
            if filt:
                continue
            c = s.this
            if isinstance(c, exp.Case) and not c.this and len(c.args.get("ifs") or []) == 1:
                p, x, default = c.args["ifs"][0].this, c.args["ifs"][0].args.get("true"), c.args.get("default")
                if default is None or isinstance(default, exp.Null):
                    s.replace(exp.Filter(this=exp.Sum(this=x.copy()), expression=exp.Where(this=p.copy())))
                elif {_lit_int(x), _lit_int(default)} == {1, -1} and _is_null_test(p) and _empty_input_irrelevant(s, sel):
                    # a NULL test is never NULL: each row counts +1 or -1
                    plus, minus = (p, _negate_null_test(p)) if _lit_int(x) == 1 else (_negate_null_test(p), p)
                    s.replace(exp.Sub(
                        this=exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(this=plus.copy())),
                        expression=exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(this=minus.copy()))))
                elif _lit_int(default) == 0 and _never_null(x, R, nonnull) and _empty_input_irrelevant(s, sel):
                    if _lit_int(x) == 1:
                        s.replace(exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(this=p.copy())))
                    else:
                        s.replace(exp.Coalesce(this=exp.Filter(this=exp.Sum(this=x.copy()), expression=exp.Where(this=p.copy())),
                                               expressions=[exp.Literal.number(0)]))
                continue
            if isinstance(c, exp.Coalesce) and len(c.expressions) == 1 and _lit_int(c.expressions[0]) == 0 \
                    and _empty_input_irrelevant(s, sel):
                s.replace(exp.Coalesce(this=exp.Sum(this=c.this.copy()), expressions=[exp.Literal.number(0)]))


def _counts_rows(c, R, nonnull, arg=None) -> bool:
    """COUNT(*), or COUNT([DISTINCT] x) of a never-NULL x, or COUNT(arg) itself: zero exactly when
    the aggregated input has no rows (or, for COUNT(arg), no non-NULL arg)."""
    if not isinstance(c, exp.Count) or isinstance(c.parent, exp.Filter):
        return False
    x = c.this.expressions[0] if isinstance(c.this, exp.Distinct) and len(c.this.expressions) == 1 else c.this
    if isinstance(x, (exp.Star, exp.Literal)):
        return True
    return _never_null(x, R, nonnull) or (arg is not None and not isinstance(c.this, exp.Distinct) and x.sql() == arg.sql())


def _avg_from_ratio(sel, R):
    """SUM(x) / COUNT(x) is AVG(x) for an exact x: PostgreSQL's AVG is the NUMERIC sum divided by the
    count of non-NULL values (NULL when there are none, as SUM(x) is then). An integer sum must be cast
    to plain NUMERIC first (integer division otherwise). COUNT(*) stands for COUNT(x) when x is never
    NULL, and NULLIF(<count>, 0) changes nothing (the sum is NULL whenever the count is 0)."""
    nonnull = _scope_nonnull(sel)
    for d in list(sel.find_all(exp.Div)):
        if _owner_select(d) is not sel or any(isinstance(x, exp.Window) for x in _ancestors(d, sel)):
            continue
        num, den = d.this, d.expression
        cast = isinstance(num, exp.Cast) and num.to.this == exp.DataType.Type.DECIMAL and not num.to.expressions
        s_ = num.this if cast else num
        if not isinstance(s_, exp.Sum) or isinstance(s_.this, exp.Distinct) or isinstance(s_.parent, exp.Filter):
            continue
        x = s_.this
        kind = _exact_kind(x, R)
        if kind is None or (kind == "int" and not cast):
            continue
        if isinstance(den, exp.Nullif) and _lit_int(den.expression) == 0:
            den = den.this
        if not isinstance(den, exp.Count) or isinstance(den.parent, exp.Filter) or isinstance(den.this, exp.Distinct):
            continue
        if not (den.this.sql() == x.sql() or (isinstance(den.this, (exp.Star, exp.Literal)) and _never_null(x, R, nonnull))):
            continue
        d.replace(exp.Avg(this=x.copy()))


def _drop_noop_nullif(sel, R):
    """X / NULLIF(C, 0) is X / C when C = 0 already makes X NULL: C counts the rows of sel's input (so
    it is never 0 in a plain GROUP BY, and on an empty input every SUM / AVG / MIN / MAX of sel is
    NULL), and X is strict in such an aggregate."""
    nonnull = _scope_nonnull(sel)
    grouped = sel.args.get("group") and not _has_grouping_sets(sel)

    def strict_aggs(e):
        if isinstance(e, (exp.Sum, exp.Avg, exp.Min, exp.Max)) and not isinstance(e.parent, exp.Filter) \
                and _owner_select(e) is sel:
            return [e]
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return strict_aggs(e.this) + strict_aggs(e.expression)
        if isinstance(e, (exp.Neg, exp.Cast)):
            return strict_aggs(e.this)
        return []
    for n in list(sel.find_all(exp.Nullif)):
        d = n.parent
        if not isinstance(d, exp.Div) or d.expression is not n or _lit_int(n.expression) != 0 \
                or _owner_select(n) is not sel or any(isinstance(x, exp.Window) for x in _ancestors(n, sel)):
            continue
        c = n.this
        aggs = strict_aggs(d.this)
        if grouped and _counts_rows(c, R, nonnull) or any(_counts_rows(c, R, nonnull, a.this) for a in aggs):
            n.replace(c.copy())


def _drop_null_in_list_items(tree):
    """In a truth context (a WHERE / HAVING / inner ON condition, reached only through AND and OR),
    a NULL in an IN list can only turn FALSE into NULL, and both reject the row: drop it. Likewise
    x LIKE '%' is TRUE for every non-NULL x and NULL otherwise, which there is x IS NOT NULL."""
    def visit(c):
        if isinstance(c, (exp.And, exp.Or)):
            return type(c)(this=visit(c.this), expression=visit(c.expression))
        if isinstance(c, exp.Paren):
            return exp.Paren(this=visit(c.this))
        if isinstance(c, (exp.Like, exp.ILike)) and not c.args.get("escape") and _lit_str(c.expression) is not None \
                and set(_lit_str(c.expression)) == {"%"}:
            return exp.Not(this=exp.Is(this=c.this, expression=exp.Null()))  # TRUE unless NULL
        if isinstance(c, exp.In) and not c.args.get("query") and not c.args.get("unnest") \
                and any(isinstance(v, exp.Null) for v in c.expressions):
            vals = [v for v in c.expressions if not isinstance(v, exp.Null)]
            return exp.In(this=c.this, expressions=vals) if vals else exp.false()
        return c
    for sel in tree.find_all(exp.Select):
        for k in ("where", "having"):
            if sel.args.get(k):
                sel.args[k].set("this", visit(sel.args[k].this))
        for j in sel.args.get("joins") or []:
            if not j.args.get("side") and j.args.get("on") is not None:
                j.set("on", visit(j.args["on"]))
    return tree


def _aggregate_never_null(e, sel, R, nonnull) -> bool:
    """An aggregate of sel that cannot be NULL: COUNT always; SUM / AVG / MIN / MAX of a never-NULL
    argument when every group has rows (a plain GROUP BY)."""
    if _owner_select(e) is not sel or any(isinstance(x, exp.Window) for x in _ancestors(e, sel)):
        return False
    if isinstance(e, exp.Count):
        return True
    if isinstance(e, (exp.Sum, exp.Avg, exp.Min, exp.Max)) and not isinstance(e.parent, exp.Filter) \
            and sel.args.get("group") and not _has_grouping_sets(sel):
        arg = e.this.expressions[0] if isinstance(e.this, exp.Distinct) and len(e.this.expressions) == 1 else e.this
        return _never_null(arg, R, nonnull)
    return False


def _default_null_ordering(sel, R):
    """NULLS FIRST / LAST cannot matter for a sort key that is never NULL: use the default."""
    order = sel.args.get("order")
    if not order:
        return
    nonnull = _scope_nonnull(sel)
    for o in order.expressions:
        if isinstance(o, exp.Ordered) and (R.not_null(o.this, nonnull) or _aggregate_never_null(o.this, sel, R, nonnull)):
            o.set("nulls_first", bool(o.args.get("desc")))


def _scope_rewrites(sel: exp.Select, R: Resolver, ctx: Context):
    _default_null_ordering(sel, R)
    _avg_from_ratio(sel, R)
    _drop_noop_nullif(sel, R)
    _canonical_sums(sel, R)
    _latest_row_to_distinct_on(sel, R, ctx)
    _right_to_left(sel)
    _scalar_eq_to_join(sel, R, ctx)
    _scalar_subqueries_to_joins(sel, R, ctx)
    _decorrelate_with_exists(sel, R, ctx)
    _decorrelate_aggregate(sel, R, ctx)
    R.refresh(sel.root())
    _promote_fk_left_joins(sel, R, ctx)
    _split_in_intersect(sel)
    _in_to_exists(sel, R)
    _left_join_anti_to_not_exists(sel, R, ctx)
    R.refresh(sel.root())
    _self_key_join_elimination(sel, R, ctx)
    _fk_exists_to_null_test(sel, R, ctx)
    _split_independent_exists(sel, R)
    _pull_up_exists(sel, R, ctx)
    R.refresh(sel.root())
    _eliminate_fk_joins(sel, R, ctx)
    R.refresh(sel.root())
    _left_join_count_zero_to_not_exists(sel, R, ctx)
    _left_join_count_having_to_inner(sel, R, ctx)
    _group_counts_to_exists(sel, R, ctx)
    _drop_redundant_not_null(sel, R)
    _count_col_to_star(sel, R, ctx)
    _reduce_group_by(sel, R, ctx)
    _drop_key_group_by(sel, R, ctx)
    _canonical_window_frames(sel)
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
    if len(cands) > 1:
        _decorrelate_counts(sel, list(cands.values()), pks, ctx)
        return
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


def _decorrelate_with_exists(sel, R, ctx):
    """SELECT .., (SELECT AGG(y) FROM S WHERE p) .. FROM O WHERE EXISTS (SELECT 1 FROM S WHERE p),
    with every scalar subquery reading the same S and p as the EXISTS, is
    SELECT .., AGG(y) .. FROM O JOIN S WHERE p GROUP BY <O's primary keys>: the outer rows kept are
    those with at least one matching row, and each group holds one outer row and its matches."""
    if any(sel.args.get(k) for k in ("group", "having", "distinct", "with", "limit", "offset")) or sel.find(exp.Window) \
            or any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)) or not sel.args.get("where"):
        return
    srcs = _sources(sel)
    if not srcs or any(not isinstance(n, exp.Table) or not R.base_table(a) or not ctx.table(R.base_table(a)).primary_key
                       or (j is not None and j.args.get("side")) for a, n, j in srcs):
        return

    def body(q):
        b = q.copy()
        b.set("expressions", [exp.Literal.number(1)])
        b.set("order", None)
        return serialize(b)
    conds = _conjuncts(sel.args["where"].this)
    for c in conds:
        if not isinstance(c, exp.Exists) or not isinstance(c.this, exp.Select):
            continue
        e = c.this
        esrc = _sources(e)
        if not _is_simple_select(e) or e.args.get("distinct") or not esrc or any(
                not isinstance(n, exp.Table) or (j is not None and j.args.get("side")) for _, n, j in esrc):
            continue
        key = body(e)
        enames = [n.name for _, n, _ in esrc]
        if len(set(enames)) != len(enames):
            continue  # a repeated table: which alias plays which role is not determined by name
        subs = [q for p in sel.expressions for q in p.find_all(exp.Subquery)
                if _owner_select(q) is sel and isinstance(q.this, exp.Select)]
        if not subs:
            continue
        aggs = []
        for q in subs:
            s2 = q.this
            a = _unalias(s2.expressions[0]) if len(s2.expressions) == 1 else None
            if not isinstance(a, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)) or _owner_select(a) is not s2 \
                    or any(s2.args.get(k) for k in ("group", "having", "limit", "offset", "distinct", "with")) \
                    or body(s2) != key:
                aggs = None
                break
            aggs.append((q, s2, a))
        if not aggs:
            continue
        # read every subquery's aggregate through the EXISTS's aliases
        by_name = {n.name: a for a, n, _ in esrc}
        if any(sorted(n.name for _, n, _ in _sources(s2)) != sorted(enames) for _, s2, _ in aggs):
            continue
        for q, s2, a in aggs:
            amap = {x: by_name[n.name] for x, n, _ in _sources(s2)}
            v = a.copy()
            for col in v.find_all(exp.Column):
                if col.table in amap:
                    col.set("table", exp.to_identifier(amap[col.table], quoted=True))
            q.replace(v)
        for _, n, j in esrc:
            sel.append("joins", exp.Join(this=n.copy()))
        extra = (_conjuncts(e.args["where"].this) if e.args.get("where") else []) + \
            [cc for _, _, j in esrc if j is not None for cc in _conjuncts(j.args.get("on"))]
        _where_set(sel, [x for x in conds if x is not c] + [x.copy() for x in extra])
        sel.set("group", exp.Group(expressions=[exp.column(k, table=a, quoted=True) for a, _, _ in srcs
                                                for k in ctx.table(R.base_table(a)).primary_key]))
        R.refresh(sel.root())
        return


def _decorrelate_counts(sel, cands, pks, ctx):
    """Several correlated (SELECT COUNT(*) FROM t x WHERE <conds>) in the select list: LEFT JOIN each
    t x ON <conds> and count COUNT(DISTINCT x.<pk>), GROUP BY the outer primary keys. The joins can
    multiply rows, but each x row is still counted once, and a NULL-extended x counts 0."""
    for q, s, agg, x, tname in cands:
        pk = ctx.table(tname).primary_key
        if not (isinstance(agg, exp.Count) and isinstance(agg.this, (exp.Star, exp.Literal))) or len(pk) != 1:
            return
        if _owner_select(q) is not sel or not any(q is _unalias(p) or q in list(p.find_all(exp.Subquery))
                                                  for p in sel.expressions):
            return
        if sel.args.get("where") and any(n is q for n in sel.args["where"].find_all(exp.Subquery)):
            return
    repl = {}
    for q, s, agg, x, tname in cands:
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        sel.append("joins", exp.Join(this=s.args["from"].this.copy(), on=_and_all([c.copy() for c in conds]),
                                     side="LEFT"))
        pk = exp.column(ctx.table(tname).primary_key[0], table=x, quoted=True)
        repl[q.sql()] = exp.Count(this=exp.Distinct(expressions=[pk]), big_int=True)

    def swap(n):
        return repl[n.sql()].copy() if isinstance(n, exp.Subquery) and n.sql() in repl else n
    sel.set("expressions", [p.transform(swap) for p in sel.expressions])
    if sel.args.get("order"):
        sel.set("order", sel.args["order"].transform(swap))
    sel.set("group", exp.Group(expressions=pks))


def _left_join_count_having_to_inner(sel, R, ctx):
    """With one group per outer row (GROUP BY covers the other tables' primary keys), a HAVING
    COUNT(b.c) > k (k >= 0) discards exactly the NULL-extended groups of LEFT JOIN b."""
    group, having = sel.args.get("group"), sel.args.get("having")
    if not group or not having or _has_grouping_sets(sel):
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


def _left_join_count_zero_to_not_exists(sel, R, ctx):
    """With one group per outer row (GROUP BY covers the other tables' primary keys), HAVING
    COUNT(b.c) = 0 for a NOT NULL column c of LEFT JOIN b keeps exactly the outer rows without a
    match: drop the join and test NOT EXISTS (SELECT 1 FROM b WHERE <ON>). b must not be read
    anywhere else (its columns would be NULL in every surviving group)."""
    group, having = sel.args.get("group"), sel.args.get("having")
    if not group or not having or _has_grouping_sets(sel) or sel.find(exp.Window):
        return
    gkeys = {g.sql() for g in group.expressions}
    for j in sel.args.get("joins") or []:
        if (j.args.get("side") or "").upper() != "LEFT" or not isinstance(j.this, exp.Table) or j.args.get("using"):
            continue
        b = j.this.alias_or_name
        btab = R.base_table(b)
        if not btab or j.args.get("on") is None:
            continue
        others = [a for a, _, _ in _sources(sel) if a != b]
        if not others or any(not R.base_table(a) or not ctx.table(R.base_table(a)).primary_key or R.nullable_side(a)
                             or not all(exp.column(k, table=a, quoted=True).sql() in gkeys
                                        for k in ctx.table(R.base_table(a)).primary_key) for a in others):
            continue
        hit = None
        for c in _conjuncts(having.this):
            for cnt, z in ((c.this, c.expression), (c.expression, c.this)):
                if isinstance(c, (exp.EQ, exp.LTE)) and (isinstance(c, exp.EQ) or cnt is c.this) \
                        and isinstance(cnt, exp.Count) and not isinstance(cnt.parent, exp.Filter) \
                        and isinstance(cnt.this, exp.Column) and cnt.this.table == b and _lit_int(z) == 0:
                    col = ctx.table(btab).columns.get(cnt.this.name)
                    if col and col.not_null:
                        hit = (c, cnt)
            if hit:
                break
        if not hit:
            continue
        allowed = set(map(id, _col_refs(j.args["on"]))) | {id(hit[1].this)}
        if any(col.table == b and id(col) not in allowed for col in sel.find_all(exp.Column)):
            continue
        sub = exp.Select(expressions=[exp.Literal.number(1)]).from_(j.this.copy())
        sub.set("where", exp.Where(this=j.args["on"].copy()))
        sel.set("joins", [x for x in sel.args["joins"] if x is not j] or None)
        rest = [c for c in _conjuncts(having.this) if c is not hit[0]]
        sel.set("having", exp.Having(this=_and_all(rest)) if rest else None)
        _where_set(sel, (_conjuncts(sel.args["where"].this) if sel.args.get("where") else [])
                   + [exp.Not(this=exp.Exists(this=sub))])
        return


def _drop_key_group_by(sel, R, ctx):
    """A GROUP BY with no aggregate, HAVING or window that covers a key of every source table puts
    each joined row in its own group (a join yields each combination of source rows once, and the
    keys tell the combinations apart): it changes nothing."""
    group = sel.args.get("group")
    if not group or _has_grouping_sets(sel) or sel.args.get("having") or sel.find(exp.Window) \
            or (sel.args.get("distinct") and sel.args["distinct"].args.get("on")):
        return
    if any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)):
        return
    by = _alias_columns(group.expressions)
    srcs = _sources(sel)
    if srcs and all(isinstance(n, exp.Table) and R.base_table(a)
                    and any(set(k) <= by.get(a, set()) for k in ctx.keys(R.base_table(a))) for a, n, _ in srcs):
        sel.set("group", None)


def _split_independent_exists(sel, R):
    """EXISTS (SELECT ... FROM A, B WHERE <A's tests> AND <B's tests>) is EXISTS (A ...) AND
    EXISTS (B ...) when no condition links A and B. Equalities first route through outer columns:
    with a.x = o.k and b.y = a.x, the second is b.y = o.k (equality of same-typed values is
    transitive, and either way the row needs all three non-NULL and equal)."""
    where = sel.args.get("where")
    if not where:
        return
    out, changed = [], False
    for c in _conjuncts(where.this):
        s = c.this if isinstance(c, exp.Exists) else None
        srcs = _sources(s) if isinstance(s, exp.Select) else []
        if len(srcs) < 2 or not _is_simple_select(s) or s.args.get("distinct") \
                or any(not isinstance(n, exp.Table) or (j is not None and (j.args.get("side") or j.args.get("using")))
                       for _, n, j in srcs):
            out.append(c)
            continue
        local = {a for a, _, _ in srcs}
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        for _, _, j in srcs:
            if j is not None:
                conds += _conjuncts(j.args.get("on"))
        parent = {}

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k
        cols, keep = {}, []
        for cc in conds:
            if isinstance(cc, exp.EQ) and isinstance(cc.this, exp.Column) and isinstance(cc.expression, exp.Column) \
                    and R.col_type(cc.this) and R.col_type(cc.this) == R.col_type(cc.expression):
                for z in (cc.this, cc.expression):
                    cols[z.sql()] = z
                    parent.setdefault(z.sql(), z.sql())
                parent[find(cc.this.sql())] = find(cc.expression.sql())
            else:
                keep.append(cc)
        classes = {}
        for k in parent:
            classes.setdefault(find(k), []).append(k)
        for members in classes.values():
            outer = sorted(m for m in members if cols[m].table not in local)
            locs = sorted(m for m in members if cols[m].table in local)
            if outer:  # every member equals the first outer column
                keep += [exp.EQ(this=cols[m].copy(), expression=cols[outer[0]].copy()) for m in locs + outer[1:]]
            else:
                keep += [exp.EQ(this=cols[locs[0]].copy(), expression=cols[m].copy()) for m in locs[1:]]
        comp = {a: a for a in local}

        def root(a):
            while comp[a] != a:
                a = comp[a]
            return a
        for cc in keep:
            hit = sorted({col.table for col in _col_refs(cc) if col.table in local})
            for a in hit[1:]:
                comp[root(a)] = root(hit[0])
        if any(cc.find(exp.Subquery, exp.Exists) for cc in keep):
            out.append(c)  # a nested query could link the tables in ways not visible here
            continue
        groups = {}
        for a, n, _ in srcs:
            groups.setdefault(root(a), []).append(n)
        if len(groups) < 2:
            out.append(c)
            continue
        for g, tables in groups.items():
            mine = {t.alias_or_name for t in tables}
            sub = exp.Select(expressions=[exp.Literal.number(1)]).from_(tables[0].copy())
            for t in tables[1:]:
                sub.append("joins", exp.Join(this=t.copy()))
            _where_set(sub, [cc.copy() for cc in keep
                             if {col.table for col in _col_refs(cc) if col.table in local} <= mine])
            out.append(exp.Exists(this=sub))
        changed = True
    if changed:
        _where_set(sel, out)


def _flatten_exists_derived(tree):
    """EXISTS (SELECT 1 FROM (SELECT [DISTINCT] e.. FROM J WHERE w) d WHERE p) is
    EXISTS (SELECT 1 FROM J WHERE w AND p[d.c := e]) for a plain derived table: EXISTS only asks
    whether a row exists, so duplicates and the projection do not matter."""
    for ex in list(tree.find_all(exp.Exists)):
        s = ex.this
        if not isinstance(s, exp.Select) or s.args.get("joins") or not s.args.get("from") \
                or any(s.args.get(k) for k in ("group", "having", "limit", "offset", "with")):
            continue
        d = s.args["from"].this
        if not isinstance(d, exp.Subquery) or not isinstance(d.this, exp.Select) or not d.alias:
            continue
        g = d.this
        if any(g.args.get(k) for k in ("group", "having", "limit", "offset", "with")) or g.find(exp.Window) \
                or (g.args.get("distinct") and g.args["distinct"].args.get("on")) \
                or any(_owner_select(a) is g for a in g.find_all(exp.AggFunc)) or not g.args.get("from"):
            continue
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        refs = [c for c in s.find_all(exp.Column) if c.table == d.alias]
        if any(c.name not in defs for c in refs):
            continue
        for c in refs:
            c.replace(defs[c.name].copy())
        conds = (_conjuncts(g.args["where"].this) if g.args.get("where") else []) \
            + (_conjuncts(s.args["where"].this) if s.args.get("where") else [])
        s.set("from", g.args["from"].copy())
        s.set("joins", [j.copy() for j in g.args.get("joins") or []] or None)
        _where_set(s, [c.copy() for c in conds])
    return tree


def _exists_self_lookup(tree, R, ctx):
    """EXISTS (SELECT ... FROM T b WHERE b.k = a.k AND p), with a an outer row of the same table T
    (not NULL-extended) and k a NOT NULL key of T, finds exactly the row a itself: it is p with b
    read as a (TRUE when p is empty)."""
    def tf(n):
        if not isinstance(n, exp.Exists) or not isinstance(n.this, exp.Select):
            return n
        s = n.this
        one = _base_single_source(s, R)
        if not one or not _is_simple_select(s) or s.args.get("distinct"):
            return n
        b, tname = one
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        for key in ctx.keys(tname):
            links, outer = [], None
            for k in key:
                for c in conds:
                    if not (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column)):
                        continue
                    for x, y in ((c.this, c.expression), (c.expression, c.this)):
                        if x.table == b and x.name == k and y.table != b and y.name == k \
                                and R.base_table(y.table) == tname and not R.nullable_side(y.table) \
                                and (outer is None or outer == y.table):
                            links.append(c)
                            outer = y.table
                            break
                    else:
                        continue
                    break
            if outer is None or len(links) != len(key):
                continue
            rest = [c.copy() for c in conds if not any(c is l for l in links)]
            for r in rest:
                for col in r.find_all(exp.Column):
                    if col.table == b:
                        col.set("table", exp.to_identifier(outer, quoted=True))
            return _and_all(rest) or exp.true()
        return n
    return tree.transform(tf, copy=False)


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
    if sel.args.get("distinct") and sel.args["distinct"].args.get("on"):
        sel.set("distinct", sel.args["distinct"].transform(tf, copy=False))
    if sel.args.get("where"):
        out = []
        for c in _conjuncts(sel.args["where"].this):
            defining = isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) \
                and isinstance(c.expression, exp.Column) and c.this.sql() in rep and c.expression.sql() in rep
            out.append(c if defining else c.transform(tf, copy=False))
        _where_set(sel, out)


def _right_to_left(sel):
    """a RIGHT JOIN b ON c [further joins] is b LEFT JOIN a ON c [the same further joins]: the
    first join produces the same rows either way, and the rest apply to that result."""
    joins = sel.args.get("joins") or []
    if joins and (joins[0].args.get("side") or "").upper() == "RIGHT" and not joins[0].args.get("using"):
        j = joins[0]
        left = sel.args["from"].this
        sel.args["from"].set("this", j.this.copy())
        sel.set("joins", [exp.Join(this=left.copy(), on=j.args.get("on"), side="LEFT")] + [x.copy() for x in joins[1:]])


def _scalar_subqueries_to_joins(sel, R, ctx):
    """(SELECT x.c FROM t x WHERE x.key = outer.col) in the SELECT list, where x.key is a key of t,
    returns at most one row: identical to LEFT JOIN t x ON x.key = outer.col and selecting x.c. The
    same holds inside a larger select-list, GROUP BY or ORDER BY expression; copies of one subquery
    (as GROUP BY 1 makes) share one join."""
    outer_aliases = {a for a, _, _ in _sources(sel)}
    new_joins = []
    seen = {}
    places = list(sel.expressions) + list(sel.args["group"].expressions if sel.args.get("group") else []) \
        + [o.this for o in (sel.args["order"].expressions if sel.args.get("order") else [])]
    nested = [q for p in places for q in p.find_all(exp.Subquery)
              if q is not _unalias(p) and _owner_select(q) is sel and isinstance(q.this, exp.Select)
              and not isinstance(q.parent, (exp.From, exp.Join, exp.Exists, exp.In, exp.Lateral))
              and not any(isinstance(z, (exp.AggFunc, exp.Window)) for z in _ancestors(q, sel))]
    for q in nested:
        key = serialize(q.this.copy())
        if key in seen:
            q.replace(seen[key].copy())
            continue
        s = q.this
        if not _is_simple_select(s) or s.args.get("joins") or s.args.get("distinct") or len(s.expressions) != 1:
            continue
        src = s.args["from"].this
        if not isinstance(src, exp.Table) or not ctx.table(src.name):
            continue
        x = src.alias_or_name
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        key_cols, corr = {}, []
        for c in conds:
            l, r = (c.this, c.expression) if isinstance(c, exp.EQ) else (None, None)
            if isinstance(r, exp.Column) and r.table == x:
                l, r = r, l
            if isinstance(l, exp.Column) and l.table == x and isinstance(r, exp.Column) and r.table in outer_aliases:
                key_cols[l.name] = r
                corr.append(c)
            else:
                corr = None
                break
        if corr is None or not any(set(k) <= set(key_cols) for k in ctx.keys(src.name)):
            continue
        val = _unalias(s.expressions[0])
        if any(c.table != x for c in _col_refs(val)):
            continue
        new_joins.append(exp.Join(this=src.copy(), on=_and_all([c.copy() for c in corr]), side="LEFT"))
        seen[key] = val.copy()
        q.replace(val.copy())
    for i, p in enumerate(sel.expressions):
        sub = _unalias(p)
        if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, exp.Select):
            continue
        s = sub.this
        if s.args.get("joins") and _is_simple_select(s) and not s.args.get("distinct") and len(s.expressions) == 1:
            chain = _scalar_chain_joins(s, outer_aliases, R, ctx)
            if chain:
                val = _unalias(s.expressions[0])
                new_joins += chain
                sel.expressions[i] = val.copy() if not isinstance(p, exp.Alias) else exp.alias_(val.copy(), p.alias)
            continue
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


def _scalar_chain_joins(s, outer_aliases, R, ctx):
    """LEFT JOINs equal to scalar subquery s (inner joins of base tables x1..xn), or None. Each x_k
    must be fixed to at most one row by key equalities on outer columns, literals and x_(k-1), and
    from k = 2 on those equalities must use x_(k-1); the value must read only x_n. Then a missing
    link NULL-extends every later table, so the value is NULL exactly when s returns no row, and no
    join fans out. Each condition goes into the ON of the last table it mentions."""
    srcs = _sources(s)
    if any(not isinstance(n, exp.Table) or not R.base_table(a) or (j is not None and j.args.get("side"))
           for a, n, j in srcs):
        return None
    conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
    for _, _, j in srcs:
        if j is not None:
            conds += _conjuncts(j.args.get("on"))
    if any(c.find(exp.Subquery, exp.Exists, exp.AggFunc) for c in conds):
        return None
    local = {a: n for a, n, _ in srcs}
    order, known = [], set(outer_aliases)
    while len(order) < len(local):
        prev = order[-1] if order else None
        nxt = None
        for a in local:
            if a in order:
                continue
            eq_cols, uses_prev = [], False
            for c in conds:
                if not isinstance(c, exp.EQ):
                    continue
                for l, r in ((c.this, c.expression), (c.expression, c.this)):
                    if isinstance(l, exp.Column) and l.table == a:
                        refs = _col_refs(r)
                        if (refs and all(x.table in known for x in refs)) or \
                                (not refs and isinstance(r, (exp.Literal, exp.Cast))):
                            eq_cols.append(l.name)
                            uses_prev |= any(x.table == prev for x in refs)
            if _key_covered(eq_cols, R.base_table(a), ctx) and (prev is None or uses_prev):
                nxt = a
                break
        if nxt is None:
            return None
        order.append(nxt)
        known.add(nxt)
    if any(c.table != order[-1] for c in _col_refs(_unalias(s.expressions[0]))):
        return None
    rank = {a: i for i, a in enumerate(order)}
    on = {a: [] for a in order}
    for c in conds:
        mine = [rank[x.table] for x in _col_refs(c) if x.table in rank]
        if any(x.table not in rank and x.table not in outer_aliases for x in _col_refs(c)):
            return None
        on[order[max(mine) if mine else 0]].append(c.copy())
    return [exp.Join(this=local[a].copy(), on=_and_all(on[a]), side="LEFT") for a in order]


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

    def positive(e):
        """Inside OR (a truth context), x IN (S) and EXISTS (... AND y = x) reject the same rows."""
        if isinstance(e, exp.Or):
            return exp.Or(this=positive(e.this), expression=positive(e.expression))
        if isinstance(e, exp.In) and e.args.get("query") is not None:
            sub = _subquery_select(e)
            if isinstance(sub, exp.Select) and _is_simple_select(sub) and len(sub.expressions) == 1:
                s2 = sub.copy()
                s2.set("distinct", None)
                conds = _conjuncts(s2.args["where"].this) if s2.args.get("where") else []
                _where_set(s2, [cc.copy() for cc in conds]
                           + [exp.EQ(this=_unalias(sub.expressions[0]).copy(), expression=e.this.copy())])
                s2.set("expressions", [exp.Literal.number(1)])
                return exp.Exists(this=s2)
        return e
    out = []
    for c in _conjuncts(where.this):
        if isinstance(c, exp.Or):
            out.append(positive(c))
            continue
        neg = isinstance(c, exp.Not)
        node = c.this if neg else c
        if isinstance(node, exp.In) and node.args.get("query") is not None:
            sub = _subquery_select(node)
            xs = list(node.this.expressions) if isinstance(node.this, exp.Tuple) else [node.this]
            if isinstance(sub, exp.Select) and _is_simple_select(sub) and len(sub.expressions) == len(xs):
                ys = [_unalias(e) for e in sub.expressions]
                if neg and not all(R.not_null(x, _scope_nonnull(sel)) and R.not_null(y, _scope_nonnull(sub))
                                   for x, y in zip(xs, ys)):
                    out.append(c)
                    continue
                s2 = sub.copy()
                s2.set("distinct", None)
                conds = _conjuncts(s2.args["where"].this) if s2.args.get("where") else []
                _where_set(s2, [cc.copy() for cc in conds]
                           + [exp.EQ(this=y.copy(), expression=x.copy()) for x, y in zip(xs, ys)])
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
        derived = isinstance(j.this, exp.Subquery) and j.this.alias and isinstance(j.this.this, exp.Select)
        if (j.args.get("side") or "").upper() != "LEFT" or not (isinstance(j.this, exp.Table) or derived):
            continue
        b = j.this.alias_or_name
        btab = R.base_table(b)
        if not btab and not derived:
            continue
        hit = None
        for c in conds:
            if isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and isinstance(c.this, exp.Column) \
                    and c.this.table == b:
                col = ctx.table(btab).columns.get(c.this.name) if btab else None
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
    # a grouping-set key is NULL in the super-aggregate rows, so only plain GROUP BY keys count
    group_keys = {g.sql() for g in (sel.args["group"].expressions
                                    if sel.args.get("group") and not _has_grouping_sets(sel) else [])}
    having = sel.args.get("having")
    if having:
        for c in _conjuncts(having.this):
            if isinstance(c, _STRICT_CMP):
                strict_on.update(s.sql() for s in (c.this, c.expression) if s.sql() in group_keys)
    keep = []
    nonnull = _scope_nonnull(sel)
    for c in conds:
        if isinstance(c, exp.EQ) and c.this.sql() == c.expression.sql() and isinstance(c.this, exp.Column) \
                and R.not_null(c.this, nonnull - {c.this.sql()}):
            continue  # x = x is TRUE when x is never NULL
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
    if not group or _has_grouping_sets(sel):
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
        if _has_grouping_sets(sel):
            return  # super-aggregate rows can coincide with ordinary groups
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
    elif _output_determines_sources(sel, R, ctx):
        sel.set("distinct", None)


def _key_target(sel, e, R, ctx, need_not_null=True):
    """(T, k, kind) when output e of SELECT sel always names one existing row of base table T by its
    single-column NOT NULL key k: e is T.k itself (kind "key") or a foreign key column referencing
    T(k) (kind "fk"), and e is never NULL in sel (unless need_not_null is False)."""
    if not isinstance(e, exp.Column) or (need_not_null and not R.not_null(e, _scope_nonnull(sel))):
        return None
    tname = R.base_table(e.table)
    if not tname or R.nullable_side(e.table):
        return None
    if [e.name] in ctx.keys(tname):
        return tname, e.name, "key"
    col = ctx.table(tname).columns.get(e.name)
    for fk in ctx.table(tname).foreign_keys:
        if fk.columns == [e.name] and len(fk.ref_columns) == 1 and fk.ref_columns in ctx.keys(fk.ref_table):
            ref = ctx.table(fk.ref_table).columns.get(fk.ref_columns[0])
            if col and ref and col.type == ref.type:
                return fk.ref_table, fk.ref_columns[0], "fk"
    return None


def _value_sets_to_semi_joins(tree, R, ctx):
    """A deduplicated set of values that each name one existing row of T by its key k is the set of
    T rows satisfying a semi-join, and T.k is unique, so:
      A UNION B ...         -> SELECT t.k FROM T t WHERE <A test> OR <B test> ...
      SELECT DISTINCT e ... -> SELECT t.k FROM T t WHERE EXISTS (SELECT 1 ... AND e = t.k)
      SELECT COUNT(DISTINCT e) ... (no GROUP BY) -> SELECT COUNT(*) FROM T t WHERE EXISTS (...)
    A branch's test is EXISTS (SELECT 1 FROM <branch> WHERE <conds> AND e = t.k), or just its
    conditions on t when it reads T alone and outputs T.k. A UNION ALL qualifies when its branches
    are provably distinct and disjoint (it has no duplicates)."""
    used = {a for s in tree.find_all(exp.Select) for a, _, _ in _sources(s)}
    counter = [0]

    def fresh_alias():
        while True:
            counter[0] += 1
            name = f"_sj{counter[0]}"
            if name not in used:
                used.add(name)
                return name

    def plain(s):
        return isinstance(s, exp.Select) and _is_simple_select(s) and len(s.expressions) == 1 and not s.args.get("order")

    def test_for(b, e, kind, t, k):
        conds = [c.copy() for c in _conjuncts(b.args["where"].this)] if b.args.get("where") else []
        if kind == "key" and not b.args.get("joins") and isinstance(b.args["from"].this, exp.Table):
            x = e.table
            for c in conds:
                for col in c.find_all(exp.Column):
                    if col.table == x:
                        col.set("table", exp.to_identifier(t, quoted=True))
            return _and_all(conds) if conds else exp.true()
        sub = b.copy()
        sub.set("distinct", None)
        _where_set(sub, conds + [exp.EQ(this=e.copy(), expression=exp.column(k, table=t, quoted=True))])
        sub.set("expressions", [exp.Literal.number(1)])
        return exp.Exists(this=sub)

    def semi_join(branches, need_not_null=True):
        targets = []
        for b in branches:
            e = _unalias(b.expressions[0])
            tgt = _key_target(b, e, R, ctx, need_not_null)
            if not tgt:
                return None
            targets.append((b, e, tgt))
        if len({(tg[0], tg[1]) for _, _, tg in targets}) != 1:
            return None
        tname, k = targets[0][2][0], targets[0][2][1]
        t = fresh_alias()
        tests = [test_for(b, e, kind, t, k) for b, e, (_, _, kind) in targets]
        out = exp.Select(expressions=[exp.column(k, table=t, quoted=True)]).from_(
            exp.Table(this=exp.to_identifier(tname, quoted=True), alias=exp.TableAlias(this=exp.to_identifier(t, quoted=True))))
        if not any(isinstance(x, exp.Boolean) and x.this for x in tests):
            cond = tests[0]
            for x in tests[1:]:
                cond = exp.Or(this=cond, expression=x)
            out.set("where", exp.Where(this=cond))
        return out

    def carry(n, out, e):
        """Move n's ORDER BY / LIMIT / OFFSET onto out. The one output column holds t.k's values, so
        a key naming it (by output name, or as the expression e) sorts the same way on t.k."""
        if out is None:
            return n
        if n.args.get("order"):
            order = n.args["order"].copy()
            for o in order.expressions:
                key = o.this
                if (isinstance(key, exp.Column) and not key.table) or (e is not None and key.sql() == e.sql()):
                    o.set("this", out.expressions[0].copy())
                else:
                    return n
            out.set("order", order)
        for k in ("limit", "offset"):
            if n.args.get(k):
                out.set(k, n.args[k].copy())
        return out

    def keep_name(n, out):
        """The rewritten query keeps n's output column name (an enclosing query reads it by name)."""
        first = _leftmost_select(n)
        if out is not n and isinstance(out, exp.Select) and isinstance(first, exp.Select) and first.expressions \
                and isinstance(first.expressions[0], exp.Alias) and len(out.expressions) == 1:
            out.set("expressions", [exp.alias_(_unalias(out.expressions[0]), first.expressions[0].alias, quoted=True)])
        return out

    def tf(n):
        return keep_name(n, rewrite(n))

    def rewrite(n):
        if isinstance(n, exp.Union) and not n.args.get("with"):
            if n.args.get("distinct") is False:
                ok = isinstance(n.this, exp.Select) and isinstance(n.expression, exp.Select) \
                    and _rows_distinct(n.this, ctx) and _rows_distinct(n.expression, ctx) \
                    and _branches_disjoint(n.this, n.expression, ctx)
                branches = [n.this, n.expression] if ok else None
            else:
                branches, stack = [], [n]
                while stack:
                    m = stack.pop()
                    if isinstance(m, exp.Union) and m.args.get("distinct") is not False \
                            and (m is n or not any(m.args.get(k) for k in ("order", "limit", "offset"))):
                        stack += [m.expression, m.this]
                    elif plain(m):
                        branches.append(m)
                    else:
                        branches = None
                        break
            if branches and all(plain(b) for b in branches):
                return carry(n, semi_join(branches), None)
            return n
        if not isinstance(n, exp.Select) or n.args.get("with"):
            return n
        if n.args.get("distinct") and not n.args["distinct"].args.get("on"):
            b = n.copy()
            b.set("order", None)
            b.set("limit", None)
            b.set("offset", None)
            if plain(b):
                return carry(n, semi_join([b]), _unalias(n.expressions[0]))
            return n
        if not n.args.get("group") and not n.args.get("having") and not n.args.get("distinct") and len(n.expressions) == 1:
            cnt = _unalias(n.expressions[0])
            if isinstance(cnt, exp.Count) and isinstance(cnt.this, exp.Distinct) and len(cnt.this.expressions) == 1 \
                    and not isinstance(cnt.parent, exp.Filter) and not n.find(exp.Window):
                b = n.copy()
                b.set("expressions", [cnt.this.expressions[0].copy()])
                for k in ("order", "limit", "offset"):
                    b.set(k, None)
                if plain(b):
                    out = semi_join([b], need_not_null=False)
                    if out is not None:
                        out.set("expressions", [exp.Count(this=exp.Star(), big_int=True)])
                        return carry(n, out, cnt)
        return n
    return tree.transform(tf)


def _merge_grouped_derived(tree, R, ctx):
    """A non-aggregating SELECT over one grouped derived table t = (SELECT g.., AGG.. FROM .. GROUP BY
    g..) and base tables O (inner joins only, each with a primary key) is that grouping done in
    place: FROM <t's sources> JOIN O WHERE <t's WHERE> AND <outer conditions not reading t's
    aggregates> GROUP BY g.., <O's primary keys> HAVING <t's HAVING> AND <outer conditions reading
    them>, with t's columns replaced by their definitions. Each group is one (t row, O row
    combination) pair; conditions linking O to t's grouping keys are constant within a group, so the
    aggregates see exactly t's rows for that group."""
    def merge(sel):
        if sel.args.get("group") or sel.args.get("having") or sel.args.get("with") \
                or (sel.args.get("distinct") and sel.args["distinct"].args.get("on")) \
                or any(_owner_select(a) is sel and not any(isinstance(x, exp.Window) for x in _ancestors(a, sel))
                       for a in sel.find_all(exp.AggFunc)):
            return None  # window functions are fine: they run after grouping either way
        srcs = _sources(sel)
        derived = [(a, n, j) for a, n, j in srcs if isinstance(n, exp.Subquery)]
        if len(derived) != 1 or any(j is not None and j.args.get("side") for _, _, j in srcs):
            return None
        t, sub, _ = derived[0]
        g = sub.this
        if not isinstance(g, exp.Select) or not g.args.get("group") or _has_grouping_sets(g) \
                or any(g.args.get(k) for k in ("distinct", "limit", "offset", "with")) or g.find(exp.Window):
            return None
        keys = {k.sql() for k in g.args["group"].expressions}
        if not all(isinstance(k, exp.Column) for k in g.args["group"].expressions):
            return None
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        aggregate_cols = {name for name, d in defs.items() if d.sql() not in keys}
        others = [(a, n) for a, n, _ in srcs if a != t]
        for a, n in others:
            tname = R.base_table(a) if isinstance(n, exp.Table) else None
            if not tname or not ctx.table(tname).primary_key:
                return None
        if any(c.table == t and c.name not in defs for c in sel.find_all(exp.Column)):
            return None

        def subst(e):
            e = e.copy()
            for c in list(e.find_all(exp.Column)):
                if c.table == t:
                    c.replace(defs[c.name].copy())
            return defs[e.name].copy() if isinstance(e, exp.Column) and e.table == t else e

        def reads_aggregate(e):
            return any(c.table == t and c.name in aggregate_cols for c in e.find_all(exp.Column))
        conds = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
        for _, _, j in srcs:
            if j is not None:
                conds += _conjuncts(j.args.get("on"))
        out = g.copy()
        out.set("order", None)
        for a, n in others:
            out.append("joins", exp.Join(this=n.copy()))
        where = (_conjuncts(out.args["where"].this) if out.args.get("where") else []) \
            + [subst(c) for c in conds if not reads_aggregate(c)]
        having = (_conjuncts(out.args["having"].this) if out.args.get("having") else []) \
            + [subst(c) for c in conds if reads_aggregate(c)]
        _where_set(out, where)
        out.set("having", exp.Having(this=_and_all(having)) if having else None)
        gkeys = [k.copy() for k in g.args["group"].expressions]
        for a, n in others:
            gkeys += [exp.column(k, table=a, quoted=True) for k in ctx.table(R.base_table(a)).primary_key]
        out.set("group", exp.Group(expressions=gkeys))
        out.set("expressions", [exp.alias_(subst(_unalias(p)), p.alias_or_name, quoted=True) if isinstance(p, exp.Alias)
                                else subst(p) for p in sel.expressions])
        out.set("distinct", sel.args.get("distinct"))
        for k in ("order", "limit", "offset"):
            if sel.args.get(k):
                out.set(k, subst(sel.args[k]) if k == "order" else sel.args[k].copy())
        return out

    for _ in range(4):  # merging one level can expose the next
        changed = False
        for sel in list(tree.find_all(exp.Select)):
            out = merge(sel)
            if out is not None:
                if sel is tree:
                    tree = out
                else:
                    sel.replace(out)
                changed = True
                break
        if not changed:
            break
        R.refresh(tree)
    return tree


def _left_join_grouped_derived(tree, R, ctx):
    """o LEFT JOIN (SELECT k, AGG(..).. FROM b WHERE w GROUP BY k) t ON t.k = o.c, in a SELECT that
    does not aggregate and reads t only through its aggregates, is o LEFT JOIN b ON b.k = o.c AND w
    grouped by the outer tables' primary keys, with each AGG over b: a group holds one outer row and
    its matches. A count must be read as COALESCE(t.n, 0) (no match: no t row, while the count of a
    NULL-extended row is 0); SUM / AVG / MIN / MAX are NULL without a match either way."""
    for sel in list(tree.find_all(exp.Select)):
        wrapped = _aggregate_over_rows(sel, R, ctx)
        if wrapped is not None:  # SELECT AGG(e) FROM <rows>  ->  SELECT AGG(z._c0) FROM (SELECT e FROM <rows>) z
            outer, sel = wrapped
            if outer.parent is None:
                tree = outer
        if any(sel.args.get(k) for k in ("group", "having", "with", "distinct")) or sel.find(exp.Window) \
                or any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)):
            continue
        srcs = _sources(sel)
        derived = [(a, n, j) for a, n, j in srcs if isinstance(n, exp.Subquery)]
        if len(derived) != 1 or derived[0][2] is None or (derived[0][2].args.get("side") or "").upper() != "LEFT":
            continue
        t, sub, j = derived[0]
        g = sub.this
        others = [(a, n, jj) for a, n, jj in srcs if a != t]
        if any(not isinstance(n, exp.Table) or not R.base_table(a) or not ctx.table(R.base_table(a)).primary_key
               or (jj is not None and jj.args.get("side")) for a, n, jj in others):
            continue
        b = _base_single_source(g, R) if isinstance(g, exp.Select) else None
        if not b or not g.args.get("group") or _has_grouping_sets(g) or len(g.args["group"].expressions) != 1 \
                or any(g.args.get(k) for k in ("having", "distinct", "limit", "offset", "with")) or g.find(exp.Window) \
                or not ctx.table(b[1]).primary_key:
            continue
        k = g.args["group"].expressions[0]
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        on = _conjuncts(j.args.get("on"))
        if len(on) != 1 or not isinstance(on[0], exp.EQ) or not isinstance(k, exp.Column):
            continue
        tk, oc = (on[0].this, on[0].expression) if isinstance(on[0].this, exp.Column) and on[0].this.table == t \
            else (on[0].expression, on[0].this)
        if not (isinstance(tk, exp.Column) and tk.table == t and defs.get(tk.name) is not None
                and defs[tk.name].sql() == k.sql() and isinstance(oc, exp.Column) and oc.table != t):
            continue
        aggs = {name: d for name, d in defs.items() if d.sql() != k.sql()}
        if not all(isinstance(d, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)) for d in aggs.values()):
            continue
        refs = [c for c in sel.find_all(exp.Column) if c.table == t and not any(c is x for x in (tk,))]
        if sel.args.get("where") and any(c.table == t for c in sel.args["where"].find_all(exp.Column)):
            continue
        ok = True
        repl = []
        for c in refs:
            d = aggs.get(c.name)
            if d is None:
                ok = False
                break
            if isinstance(d, exp.Count):
                co = c.parent
                if not (isinstance(co, exp.Coalesce) and co.this is c and len(co.expressions) == 1
                        and _lit_int(co.expressions[0]) == 0):
                    ok = False
                    break
                repl.append((co, d))
            else:
                repl.append((c, d))
        if not ok:
            continue
        bt = g.args["from"].this
        pk = exp.column(ctx.table(b[1]).primary_key[0], table=b[0], quoted=True)
        for node, d in repl:
            new_agg = d.copy()
            if isinstance(new_agg, exp.Count) and isinstance(new_agg.this, (exp.Star, exp.Literal)):
                new_agg.set("this", pk.copy())
            node.replace(new_agg)
        conds = [exp.EQ(this=k.copy(), expression=oc.copy())] + \
            (_conjuncts(g.args["where"].this) if g.args.get("where") else [])
        joins = [jj if jj is not j else exp.Join(this=bt.copy(), on=_and_all([c.copy() for c in conds]), side="LEFT")
                 for jj in sel.args["joins"]]
        sel.set("joins", joins)
        sel.set("group", exp.Group(expressions=[exp.column(kk, table=a, quoted=True) for a, _, _ in others
                                                for kk in ctx.table(R.base_table(a)).primary_key]))
    R.refresh(tree)
    return tree


def _aggregate_over_rows(sel, R, ctx):
    """SELECT AGG(e) FROM <rows> (one aggregate, nothing else) is SELECT AGG(z._c0) FROM
    (SELECT e AS _c0 FROM <rows>) z. Done only when the inner SELECT then fits
    _left_join_grouped_derived, so that rule can reach a LEFT JOIN of a grouped derived table under
    an aggregate. Returns (new outer SELECT, inner SELECT) or None."""
    own = [a for a in sel.find_all(exp.AggFunc) if _owner_select(a) is sel]
    if len(own) != 1 or len(sel.expressions) != 1 or _unalias(sel.expressions[0]) is not own[0] \
            or isinstance(own[0].this, (exp.Distinct, exp.Star)) or isinstance(own[0].parent, exp.Filter) \
            or any(sel.args.get(k) for k in ("group", "having", "with", "distinct", "order", "limit", "offset")) \
            or sel.find(exp.Window) or not any(isinstance(n, exp.Subquery) and j is not None
                                               and (j.args.get("side") or "").upper() == "LEFT" for _, n, j in _sources(sel)):
        return None
    agg = own[0]
    inner = sel.copy()
    inner.set("expressions", [exp.Alias(this=agg.this.copy(), alias=exp.to_identifier("_c0", quoted=True))])
    probe = inner.copy()
    if _left_join_grouped_derived(probe, R, ctx).sql() == inner.sql():
        return None
    new_agg = agg.copy()
    new_agg.set("this", exp.column("_c0", table="_agg_rows", quoted=True))
    head = sel.expressions[0]
    outer = exp.Select(expressions=[exp.Alias(this=new_agg, alias=head.args["alias"].copy())
                                    if isinstance(head, exp.Alias) else new_agg]).from_(
        exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("_agg_rows", quoted=True))))
    if sel.parent is not None:
        sel.replace(outer)
    return outer, inner


def _count_over_distinct_derived(tree, R, ctx):
    """SELECT t.g.., COUNT(t.x) FROM (SELECT DISTINCT g.., x FROM S WHERE w) t GROUP BY t.g.. is
    SELECT g.., COUNT(DISTINCT x) FROM S WHERE w GROUP BY g..: both count the distinct non-NULL x
    per g. COUNT(*) does too when x is never NULL."""
    def tf(n):
        if not isinstance(n, exp.Select) or n.args.get("joins") or n.args.get("where") \
                or any(n.args.get(k) for k in ("having", "distinct", "with", "limit", "offset", "order")) or n.find(exp.Window):
            return n
        frm = n.args.get("from")
        sub = frm.this if frm else None
        if not isinstance(sub, exp.Subquery) or not sub.alias or not isinstance(sub.this, exp.Select):
            return n
        t, g = sub.alias, sub.this
        if not g.args.get("distinct") or g.args["distinct"].args.get("on") \
                or any(g.args.get(k) for k in ("group", "having", "limit", "offset", "with")) or g.find(exp.Window) \
                or any(_owner_select(a) is g for a in g.find_all(exp.AggFunc)):
            return n
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        group = n.args.get("group")
        if group and _has_grouping_sets(n):
            return n
        keys = list(group.expressions) if group else []
        if not all(isinstance(kk, exp.Column) and kk.table == t and kk.name in defs for kk in keys):
            return n
        counts = [p for p in map(_unalias, n.expressions) if isinstance(p, exp.Count)]
        if len(counts) != 1 or isinstance(counts[0].parent, exp.Filter) or isinstance(counts[0].this, exp.Distinct):
            return n
        cnt = counts[0]
        key_names = {kk.name for kk in keys}
        rest = [name for name in defs if name not in key_names]
        if len(rest) != 1:
            return n
        x = defs[rest[0]]
        if isinstance(cnt.this, exp.Column):
            if cnt.this.table != t or cnt.this.name != rest[0]:
                return n
        elif not (isinstance(cnt.this, (exp.Star, exp.Literal)) and R.not_null(x, _scope_nonnull(g))):
            return n
        if any(not (isinstance(p, exp.Count) or (isinstance(p, exp.Column) and p.table == t and p.name in key_names))
               for p in map(_unalias, n.expressions)):
            return n
        out = g.copy()
        out.set("distinct", None)
        new_exprs = []
        for p in n.expressions:
            e = _unalias(p)
            v = exp.Count(this=exp.Distinct(expressions=[x.copy()]), big_int=True) if isinstance(e, exp.Count) \
                else defs[e.name].copy()
            new_exprs.append(exp.alias_(v, p.alias, quoted=True) if isinstance(p, exp.Alias) else v)
        out.set("expressions", new_exprs)
        out.set("group", exp.Group(expressions=[defs[kk.name].copy() for kk in keys]) if keys else None)
        return out
    return tree.transform(tf, copy=False)


def _avg_of_group_sums(tree, R, ctx):
    """SELECT AVG(t.s) FROM (SELECT SUM(x) AS s FROM ... GROUP BY k) t is SUM(x) / COUNT(DISTINCT k)
    over the inner rows: the average of the group totals is the grand total over the number of
    groups. Needs k never NULL (no NULL group, which AVG would count) and x never NULL (no NULL
    group total, which AVG would skip). An integer x is cast to numeric, as AVG of a bigint is;
    on no rows both sides are NULL (SUM is NULL, so the division by zero yields NULL)."""
    def tf(n):
        if not isinstance(n, exp.Select) or len(n.expressions) != 1 or n.args.get("joins") \
                or any(n.args.get(k) for k in ("where", "group", "having", "distinct", "with", "limit", "offset")):
            return n
        avg = _unalias(n.expressions[0])
        frm = n.args.get("from")
        sub = frm.this if frm else None
        if not isinstance(avg, exp.Avg) or not isinstance(avg.this, exp.Column) or isinstance(avg.parent, exp.Filter) \
                or not isinstance(sub, exp.Subquery) or avg.this.table != sub.alias:
            return n
        g = sub.this
        if not isinstance(g, exp.Select) or not g.args.get("group") or _has_grouping_sets(g) \
                or any(g.args.get(k) for k in ("having", "distinct", "limit", "offset", "with")) or g.find(exp.Window) \
                or len(g.args["group"].expressions) != 1:
            return n
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        s = defs.get(avg.this.name)
        k = g.args["group"].expressions[0]
        nonnull = _scope_nonnull(g)
        if not isinstance(s, exp.Sum) or isinstance(s.this, exp.Distinct) or isinstance(s.parent, exp.Filter) \
                or not _never_null(k, R, nonnull) or not _never_null(s.this, R, nonnull):
            return n
        kind = _exact_kind(s.this, R)
        if kind is None:
            return n
        total = exp.Sum(this=s.this.copy())
        if kind == "int":
            total = exp.Cast(this=total, to=exp.DataType.build("NUMERIC"))
        out = g.copy()
        out.set("group", None)
        out.set("order", None)
        out.set("expressions", [exp.Div(this=total, expression=exp.Count(this=exp.Distinct(expressions=[k.copy()]),
                                                                          big_int=True), typed=True)])
        return out
    return tree.transform(tf)


def _aggregates_to_scalar_subqueries(tree) -> bool:
    """An ungrouped aggregate SELECT (exactly one row) whose output already mixes in a scalar
    subquery is the same as a SELECT without FROM in which each of its aggregates is its own scalar
    subquery over the original FROM / WHERE: SELECT COUNT(*) / (SELECT ...) FROM t is
    SELECT (SELECT COUNT(*) FROM t) / (SELECT ...)."""
    changed = False
    for sel in list(tree.find_all(exp.Select)):
        if not sel.args.get("from") or any(sel.args.get(k) for k in
                                           ("group", "having", "distinct", "order", "limit", "offset", "with")):
            continue
        if sel.find(exp.Window):
            continue
        aggs = [a for p in sel.expressions for a in p.find_all(exp.AggFunc) if _owner_select(a) is sel]
        has_scalar = any(isinstance(q, exp.Subquery) and _owner_select(q) is sel
                         for p in sel.expressions for q in p.find_all(exp.Subquery))
        if not aggs or not has_scalar:
            continue
        agg_ids = {id(x) for a in aggs for x in a.walk()}
        sub_ids = {id(x) for p in sel.expressions for q in p.find_all(exp.Subquery) for x in q.walk()}
        if any(id(c) not in agg_ids and id(c) not in sub_ids for p in sel.expressions for c in p.find_all(exp.Column)):
            continue  # a bare column outside the aggregates
        for a in aggs:
            node = a.parent if isinstance(a.parent, exp.Filter) else a
            base = exp.Select(expressions=[node.copy()])
            for k in ("from", "joins", "where"):
                if sel.args.get(k):
                    base.set(k, sel.args[k].copy() if k != "joins" else [j.copy() for j in sel.args[k]])
            node.replace(exp.Subquery(this=base))
        for k in ("from", "joins", "where"):
            sel.set(k, None)
        changed = True
    return changed


def _row_number_to_distinct_on(tree, keep_outer_order):
    """SELECT ... FROM (SELECT ..., ROW_NUMBER() OVER (PARTITION BY P ORDER BY O) AS rn FROM ...) t
    WHERE rn = 1 [ORDER BY P] keeps the first row of each P group in O order: that is
    SELECT DISTINCT ON (P) ... ORDER BY P, O. When O has ties both pick an arbitrary first row; like
    two queries with the same ORDER BY ... LIMIT, they are treated as equal. DISTINCT ON's ORDER BY
    also orders the output, so an unordered outermost query is left alone when row order counts."""
    def tf(n):
        if not isinstance(n, exp.Select) or n.args.get("joins") \
                or any(n.args.get(k) for k in ("group", "having", "distinct", "with", "limit", "offset")) \
                or any(p.find(exp.Window) for p in n.expressions):  # the outer query's own windows only
            return n
        if n.parent is None and keep_outer_order and not n.args.get("order"):  # outermost query
            return n  # the rewrite would promise an output order the query does not have
        frm = n.args.get("from")
        sub = frm.this if frm else None
        if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, exp.Select) or not n.args.get("where"):
            return n
        g, t = sub.this, sub.alias
        if any(g.args.get(k) for k in ("distinct", "limit", "offset", "with")):
            return n
        conds = _conjuncts(n.args["where"].this)
        if len(conds) != 1 or not isinstance(conds[0], exp.EQ):
            return n
        c = conds[0]
        rn = c.this if isinstance(c.this, exp.Column) else c.expression
        one = c.expression if rn is c.this else c.this
        if not isinstance(rn, exp.Column) or rn.table != t or _lit_int(one) != 1:
            return n
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        w = defs.get(rn.name)
        if not isinstance(w, exp.Window) or not isinstance(w.this, exp.RowNumber) or w.args.get("spec") \
                or not w.args.get("partition_by") or not w.args.get("order"):
            return n
        if any(col.table == t and col.name == rn.name for p in n.expressions for col in p.find_all(exp.Column)):
            return n
        if any(col.table == t and col.name not in defs for col in n.find_all(exp.Column)):
            return n
        part = list(w.args["partition_by"])
        if n.args.get("order"):  # only an ordering by P itself, ascending, survives as DISTINCT ON's order
            keys = n.args["order"].expressions
            mapped = [defs[o.this.name] if isinstance(o.this, exp.Column) and o.this.table == t else None for o in keys]
            if len(keys) != len(part) or any(o.args.get("desc") or m is None or m.sql() != p.sql()
                                             for o, m, p in zip(keys, mapped, part)):
                return n

        def subst(e):
            e = e.copy()
            if isinstance(e, exp.Column) and e.table == t:
                return defs[e.name].copy()
            for col in list(e.find_all(exp.Column)):
                if col.table == t:
                    col.replace(defs[col.name].copy())
            return e
        out = g.copy()
        out.set("expressions", [exp.alias_(subst(_unalias(p)), p.alias_or_name, quoted=True) if isinstance(p, exp.Alias)
                                else subst(p) for p in n.expressions])
        out.set("distinct", exp.Distinct(on=exp.Tuple(expressions=[p.copy() for p in part])))
        out.set("order", exp.Order(expressions=[exp.Ordered(this=p.copy(), nulls_first=False) for p in part]
                                   + [o.copy() for o in w.args["order"].expressions]))
        return out
    return tree.transform(tf)


def _row_number_filter_to_limit(tree, keep_outer_order):
    """SELECT .. FROM (SELECT .., ROW_NUMBER() OVER (ORDER BY O) AS rn FROM ..) t WHERE rn <= n
    [ORDER BY O] keeps the first n rows in O order: SELECT .. ORDER BY O LIMIT n (ties at the cut are
    arbitrary either way). The outer order must be O itself, or absent where row order does not count."""
    def tf(n):
        if not isinstance(n, exp.Select) or n.args.get("joins") \
                or any(n.args.get(k) for k in ("group", "having", "distinct", "with", "limit", "offset")) \
                or any(p.find(exp.Window) for p in n.expressions):
            return n
        frm = n.args.get("from")
        sub = frm.this if frm else None
        if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, exp.Select) or not n.args.get("where"):
            return n
        g, t = sub.this, sub.alias
        if any(g.args.get(k) for k in ("distinct", "limit", "offset", "with", "order")):
            return n
        conds = _conjuncts(n.args["where"].this)
        if len(conds) != 1:
            return n
        c = conds[0]
        rn, k = None, None
        if isinstance(c, (exp.LTE, exp.LT)) and isinstance(c.this, exp.Column):
            rn, k = c.this, _lit_int(c.expression)
            k = k - 1 if k is not None and isinstance(c, exp.LT) else k
        elif isinstance(c, exp.EQ):
            rn = c.this if isinstance(c.this, exp.Column) else c.expression
            k = 1 if _lit_int(c.expression if rn is c.this else c.this) == 1 else None
        if not isinstance(rn, exp.Column) or rn.table != t or not k or k < 1:
            return n
        defs = {p.alias_or_name: _unalias(p) for p in g.expressions}
        w = defs.get(rn.name)
        if not isinstance(w, exp.Window) or not isinstance(w.this, exp.RowNumber) or w.args.get("partition_by") \
                or not w.args.get("order"):
            return n
        if any(col.table == t and col.name == rn.name for p in n.expressions for col in p.find_all(exp.Column)):
            return n
        if any(col.table == t and col.name not in defs for col in n.find_all(exp.Column)):
            return n

        def subst(e):
            e = e.copy()
            if isinstance(e, exp.Column) and e.table == t:
                return defs[e.name].copy()
            return _subst_alias(e, t, defs)
        order = w.args["order"]
        if n.args.get("order"):
            mine = [subst(o) for o in n.args["order"].expressions]
            if len(mine) != len(order.expressions) or any(
                    (a.this.sql(), bool(a.args.get("desc")), a.args.get("nulls_first")) !=
                    (b.this.sql(), bool(b.args.get("desc")), b.args.get("nulls_first"))
                    for a, b in zip(mine, order.expressions)):
                return n
        elif n.parent is None and keep_outer_order:
            return n
        out = g.copy()
        out.set("expressions", [exp.alias_(subst(_unalias(p)), p.alias_or_name, quoted=True) if isinstance(p, exp.Alias)
                                else subst(p) for p in n.expressions])
        out.set("order", order.copy())
        out.set("limit", exp.Limit(expression=exp.Literal.number(k)))
        return out
    return tree.transform(tf, copy=False)


def _latest_row_to_distinct_on(sel, R, ctx):
    """WHERE h.c = (SELECT MAX(s.c) FROM H s WHERE s.f = X ...), with h joined only by h.f = X, keeps
    for each row of the other tables the h row with the largest c. When (f..., c) holds a NOT NULL
    unique key of H and c is NOT NULL, that row is unique, so this is
    SELECT DISTINCT ON (<other tables' primary keys>) ... ORDER BY <those keys>, h.c DESC.
    Its ORDER BY also orders the output, so the outermost query is left alone when row order counts."""
    if any(sel.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "with")) \
            or not sel.args.get("where") or sel.find(exp.Window):
        return
    if sel.parent is None and R.keep_root_order:
        return
    srcs = _sources(sel)
    if len(srcs) < 2 or any(not isinstance(n, exp.Table) or not R.base_table(a) or (j is not None and j.args.get("side"))
                            for a, n, j in srcs):
        return
    conds = _conjuncts(sel.args["where"].this)
    for _, _, j in srcs:
        if j is not None:
            conds += _conjuncts(j.args.get("on"))
    for m in _conjuncts(sel.args["where"].this):  # the MAX test itself must sit in WHERE
        if not isinstance(m, exp.EQ):
            continue
        for hc, q in ((m.this, m.expression), (m.expression, m.this)):
            if not isinstance(hc, exp.Column) or not isinstance(q, exp.Subquery) or not isinstance(q.this, exp.Select):
                continue
            s = q.this
            one = _base_single_source(s, R)
            htab = R.base_table(hc.table)
            if not one or one[1] != htab or len(s.expressions) != 1:
                continue
            mx = _unalias(s.expressions[0])
            if not isinstance(mx, exp.Max) or not isinstance(mx.this, exp.Column) or mx.this.name != hc.name \
                    or any(s.args.get(k) for k in ("group", "having", "distinct", "joins", "limit")):
                continue
            x = one[0]
            corr = {}
            for c in (_conjuncts(s.args["where"].this) if s.args.get("where") else []):
                pair = None
                if isinstance(c, exp.EQ):
                    for a, b in ((c.this, c.expression), (c.expression, c.this)):
                        if isinstance(a, exp.Column) and a.table == x and not any(r.table == x for r in _col_refs(b)) \
                                and (_col_refs(b) or _literal_value(b)):
                            pair = (a.name, b)
                if pair is None:
                    corr = None
                    break
                corr[pair[0]] = pair[1]
            if not corr:
                continue
            h = hc.table
            col = ctx.table(htab).columns.get(hc.name)
            if not col or not col.not_null or not any(set(k) <= set(corr) | {hc.name} for k in ctx.keys(htab)):
                continue
            # h must be linked by exactly h.f = X for each correlated f, and appear in no other condition
            links = {}
            for c in conds:
                if c is m or not any(r.table == h for r in _col_refs(c)):
                    continue
                ok = False
                if isinstance(c, exp.EQ):
                    for a, b in ((c.this, c.expression), (c.expression, c.this)):
                        if isinstance(a, exp.Column) and a.table == h and a.name in corr \
                                and b.sql() == corr[a.name].sql() and not any(r.table == h for r in _col_refs(b)):
                            links[a.name] = c
                            ok = True
                if not ok:
                    return
            if set(links) != set(corr):
                continue
            others = [(a, n) for a, n, _ in srcs if a != h]
            if any(not ctx.table(R.base_table(a)).primary_key for a, _ in others):
                return
            keys = [exp.column(k, table=a, quoted=True) for a, _ in others for k in ctx.table(R.base_table(a)).primary_key]
            _where_set(sel, [c for c in _conjuncts(sel.args["where"].this) if c is not m])
            sel.set("distinct", exp.Distinct(on=exp.Tuple(expressions=[k.copy() for k in keys])))
            sel.set("order", exp.Order(expressions=[exp.Ordered(this=k.copy(), nulls_first=False) for k in keys]
                                       + [exp.Ordered(this=hc.copy(), desc=True, nulls_first=True)]))
            return


def _split_correlated_or_exists(tree):
    """EXISTS (S WHERE c AND (a OR b)) = EXISTS (S WHERE c AND a) OR EXISTS (S WHERE c AND b),
    applied when every disjunct is correlated (refers outside S)."""
    def tf(n):
        if not isinstance(n, exp.Exists) or not isinstance(n.this, exp.Select) or not _is_simple_select(n.this):
            return n
        s = n.this
        conds = _conjuncts(s.args["where"].this) if s.args.get("where") else []
        ors = [c for c in conds if isinstance(c, exp.Or)]
        if len(ors) != 1:
            return n
        local = {a for a, _, _ in _sources(s)}
        parts = _disjuncts(ors[0])
        if not all(any(c.table not in local for c in _col_refs(p)) for p in parts):
            return n
        rest = [c for c in conds if c is not ors[0]]
        out = None
        for p in parts:
            sub = s.copy()
            _where_set(sub, [c.copy() for c in rest] + [p.copy()])
            ex = exp.Exists(this=sub)
            out = ex if out is None else exp.Or(this=out, expression=ex)
        return out
    return tree.transform(tf)


def _output_determines_sources(sel, R, ctx) -> bool:
    """The output columns of a non-aggregating SELECT over base tables (inner joins only) fix one
    row of every source: start from the projected columns, follow equalities with literals and
    known columns, and mark a table determined once a NOT NULL key of it is known (then all its
    columns are). Two equal output rows then come from the same row combination, which a join of
    keyed tables produces once, so the output has no duplicates."""
    srcs = _sources(sel)
    if not srcs or any(not isinstance(n, exp.Table) or not R.base_table(a) or (j is not None and j.args.get("side"))
                       for a, n, j in srcs):
        return False
    if sel.args.get("group") or any(_owner_select(a) is sel for a in sel.find_all(exp.AggFunc)) or sel.find(exp.Window):
        return False
    conds = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
    for _, _, j in srcs:
        if j is not None:
            conds += _conjuncts(j.args.get("on"))
    known = {p.sql() for p in map(_unalias, sel.expressions) if isinstance(p, exp.Column)}
    determined = set()

    def is_known(e):
        return isinstance(e, (exp.Literal, exp.Cast)) and not _col_refs(e) or \
            (isinstance(e, exp.Column) and (e.sql() in known or e.table in determined))
    changed = True
    while changed:
        changed = False
        for c in conds:
            if isinstance(c, exp.EQ):
                for l, r in ((c.this, c.expression), (c.expression, c.this)):
                    if isinstance(l, exp.Column) and l.sql() not in known and is_known(r):
                        known.add(l.sql())
                        changed = True
        for a, _, _ in srcs:
            if a not in determined and any(all(exp.column(k, table=a, quoted=True).sql() in known for k in key)
                                           for key in ctx.keys(R.base_table(a))):
                determined.add(a)
                changed = True
    return len(determined) == len(srcs)


def _except_on_key_to_not_exists(tree, R, ctx):
    """L EXCEPT R, with L's output columns never NULL, equals
    SELECT [DISTINCT] <L cols> FROM <L> WHERE <L conds> AND NOT EXISTS (SELECT 1 FROM <R> WHERE
    <R conds> AND r1 = l1 AND ...): EXCEPT keeps the distinct L rows with no equal R row, and with
    no NULLs on the left, equality is the same test. DISTINCT is left out when L's rows are already
    distinct. L EXCEPT ALL R is the same thing when L's rows are distinct."""
    def tf(n):
        if not isinstance(n, exp.Except):
            return n
        left, right = n.this, n.expression
        if not isinstance(left, exp.Select) or not isinstance(right, exp.Select) \
                or len(left.expressions) != len(right.expressions) \
                or not _is_simple_select(left) or not _is_simple_select(right):
            return n
        nonnull = _scope_nonnull(left)
        ks = [_unalias(p) for p in left.expressions]
        if not all(R.not_null(k, nonnull) for k in ks):
            return n
        left_distinct = bool(left.args.get("distinct")) or _output_determines_sources(left, R, ctx)
        if n.args.get("distinct") is False and not left_distinct:
            return n
        sub = right.copy()
        sub.set("distinct", None)
        conds = _conjuncts(sub.args["where"].this) if sub.args.get("where") else []
        eqs = [exp.EQ(this=_unalias(e).copy(), expression=k.copy()) for e, k in zip(sub.expressions, ks)]
        _where_set(sub, conds + eqs)
        sub.set("expressions", [exp.Literal.number(1)])
        out = left.copy()
        lconds = _conjuncts(out.args["where"].this) if out.args.get("where") else []
        _where_set(out, lconds + [exp.Not(this=exp.Exists(this=sub))])
        out.set("distinct", None if left_distinct else exp.Distinct())
        out.set("with", n.args.get("with"))
        if n.args.get("order"):  # it names the left branch's outputs: resolve them to expressions
            outputs = {p.alias_or_name: _unalias(p) for p in left.expressions}
            order = n.args["order"].copy()
            for o in order.expressions:
                k = o.this
                if isinstance(k, exp.Column) and not k.table and k.name in outputs:
                    o.set("this", outputs[k.name].copy())
                elif not (isinstance(k, exp.Column) and k.table):
                    return n
            out.set("order", order)
        for k in ("limit", "offset"):
            if n.args.get(k):
                out.set(k, n.args[k].copy())
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


def _is_null(x):
    return exp.Is(this=x.copy(), expression=exp.Null())


def _canonical_counts(tree) -> bool:
    """Counting rows that satisfy a condition has one spelling: COUNT(*) FILTER (WHERE cond).
      COUNT(*) - COUNT(c)          -> COUNT(*) FILTER (WHERE c IS NULL)
      COUNT(c) [FILTER (WHERE p)]  -> COUNT(*) FILTER (WHERE [p AND] c IS NOT NULL)
    and, in a SELECT without GROUP BY / HAVING whose aggregates all carry the same filter p, the
    filter moves to WHERE (a single group either way, and the same aggregate of no rows)."""
    changed = False

    def plain_count_col(n):
        return isinstance(n, exp.Count) and isinstance(n.this, exp.Column) and not isinstance(n.parent, exp.Filter)

    def tf(n):
        nonlocal changed
        if isinstance(n, exp.Sub) and isinstance(n.this, exp.Count) and isinstance(n.this.this, exp.Star) \
                and not isinstance(n.this.parent, exp.Filter) and plain_count_col(n.expression):
            changed = True
            return exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(this=_is_null(n.expression.this)))
        if isinstance(n, exp.Filter) and isinstance(n.this, exp.Count) and isinstance(n.this.this, exp.Column):
            changed = True
            return exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(
                this=exp.And(this=n.expression.this.copy(), expression=exp.Not(this=_is_null(n.this.this)))))
        if plain_count_col(n):
            changed = True
            return exp.Filter(this=exp.Count(this=exp.Star(), big_int=True), expression=exp.Where(this=exp.Not(this=_is_null(n.this))))
        return n
    tree = tree.transform(tf, copy=False)

    for sel in list(tree.find_all(exp.Select)):
        if sel.args.get("group") or sel.args.get("having") or sel.find(exp.Window) \
                or (sel.args.get("distinct") and sel.args["distinct"].args.get("on")):
            continue
        aggs = [a for p in sel.expressions for a in p.find_all(exp.AggFunc) if _owner_select(a) is sel]
        if not aggs or not all(isinstance(a.parent, exp.Filter) for a in aggs):
            continue
        conds = {a.parent.expression.this.sql() for a in aggs}
        if len(conds) != 1:
            continue
        cond = aggs[0].parent.expression.this.copy()
        for a in aggs:
            a.parent.replace(a.copy())
        where = _conjuncts(sel.args["where"].this) if sel.args.get("where") else []
        _where_set(sel, where + _conjuncts(cond))
        changed = True
    return changed


_CMP_OPS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


def _case_subject(cond, found):
    """Collect (subject sql, subject, numeric constants) of a CASE condition built from NULL tests and
    comparisons of one expression with numeric literals; False if the condition has another shape."""
    if isinstance(cond, (exp.And, exp.Or)):
        return _case_subject(cond.this, found) and _case_subject(cond.expression, found)
    if isinstance(cond, exp.Not):
        return _case_subject(cond.this, found)
    if isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null):
        x = cond.this
    elif isinstance(cond, _CMP_OPS):
        a, b = cond.this, cond.expression
        x, lit = (a, b) if _num_value(b) is not None else (b, a)
        if _num_value(lit) is None or _num_value(x) is not None:
            return False
        found["consts"].add(_num_value(lit))
    else:
        return False
    if not isinstance(x, exp.Column):
        return False
    if found["x"] is None:
        found["x"] = x
    return found["x"].sql() == x.sql()


def _num_value(e):
    if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal):
        v = _num_value(e.this)
        return -v if v is not None else None
    if isinstance(e, exp.Literal) and not e.is_string:
        try:
            d = Decimal(e.this)
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    return None


def _eval_cond(cond, xsql, v):
    """Three-valued truth of a CASE condition at subject value v (None = NULL)."""
    if isinstance(cond, exp.And):
        a, b = _eval_cond(cond.this, xsql, v), _eval_cond(cond.expression, xsql, v)
        return False if a is False or b is False else None if a is None or b is None else True
    if isinstance(cond, exp.Or):
        a, b = _eval_cond(cond.this, xsql, v), _eval_cond(cond.expression, xsql, v)
        return True if a is True or b is True else None if a is None or b is None else False
    if isinstance(cond, exp.Not):
        a = _eval_cond(cond.this, xsql, v)
        return None if a is None else not a
    if isinstance(cond, exp.Is):
        return v is None
    a, b = cond.this, cond.expression
    if a.sql() == xsql:
        l, r = v, _num_value(b)
    else:
        l, r = _num_value(a), v
    if l is None or r is None:
        return None
    return {exp.EQ: l == r, exp.NEQ: l != r, exp.LT: l < r, exp.LTE: l <= r,
            exp.GT: l > r, exp.GTE: l >= r}[type(cond)]


def _canonical_case(node, R):
    """Spell equivalent CASE expressions alike.
      COALESCE(CASE ... END, d) with every THEN a non-NULL literal -> CASE ... ELSE COALESCE(e, d) END
      a CASE testing one never-NULL enum column against all of its labels -> its last WHEN is the ELSE
      a CASE whose WHEN conditions only compare one column with numeric literals (and test it for
      NULL) is a step function of that column: it is rebuilt from that function (the result for
      NULL, then each maximal range of equal results, by increasing upper bound), so the branch
      order and the way each range is written no longer matter."""
    if isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and isinstance(node.this, exp.Case) \
            and all(isinstance(i.args.get("true"), exp.Literal) for i in node.this.args.get("ifs") or []):
        case = node.this.copy()
        default = case.args.get("default")
        d = node.expressions[0].copy()
        case.set("default", d if default is None or isinstance(default, exp.Null)
                 else exp.Coalesce(this=default.copy(), expressions=[d]))
        node = case
    if not isinstance(node, exp.Case) or node.this is not None or not node.args.get("ifs"):
        return node
    ifs = node.args["ifs"]
    # exhaustive tests of a NOT NULL enum column
    if node.args.get("default") is None:
        col, labels = None, set()
        for i in ifs:
            c = i.this
            vals = [c.expression] if isinstance(c, exp.EQ) else list(c.expressions) if isinstance(c, exp.In) \
                and not c.args.get("query") else None
            if not vals or not isinstance(c.this, exp.Column) or (col is not None and col.sql() != c.this.sql()):
                col = None
                break
            col = c.this
            labels |= {_lit_str(v) for v in vals}
        info = R.column(col) if col is not None else None
        if info and info.enum_labels and R.not_null(col) and labels == set(info.enum_labels):
            node = node.copy()
            last = node.args["ifs"][-1]
            node.set("ifs", node.args["ifs"][:-1])
            node.set("default", last.args["true"].copy())
            if not node.args["ifs"]:
                return node.args["default"]
            ifs = node.args["ifs"]
    # step functions of one column
    found = {"x": None, "consts": set()}
    if not all(_case_subject(i.this, found) for i in ifs) or not found["consts"]:
        return node
    x, xsql = found["x"], found["x"].sql()
    results = [i.args["true"] for i in ifs] + [node.args.get("default") or exp.Null()]

    def branch(v):
        for k, i in enumerate(ifs):
            if _eval_cond(i.this, xsql, v) is True:
                return k
        return len(ifs)
    pts = sorted(found["consts"])
    samples = [pts[0] - 1]
    for k, b in enumerate(pts):
        samples.append(b)
        samples.append((b + pts[k + 1]) / 2 if k + 1 < len(pts) else b + 1)
    # each sample stands for a region: an open interval (even index) or one point (odd index)
    regions = []
    for k, v in enumerate(samples):
        r = results[branch(v)].sql()
        if regions and regions[-1][1] == r:
            regions[-1][2] = k
        else:
            regions.append([k, r, k])
    by_sql = {r.sql(): r for r in results}
    out_ifs = []
    if not R.not_null(x):
        out_ifs.append(exp.If(this=exp.Is(this=x.copy(), expression=exp.Null()),
                              true=results[branch(None)].copy()))
    for start, r, end in regions[:-1]:
        # the region ends at sample index `end`: an open interval before pts[end // 2] (x < b) or the
        # point pts[(end - 1) // 2] itself (x <= b)
        bound = exp.LT(this=x.copy(), expression=exp.Literal.number(str(pts[end // 2]))) if end % 2 == 0 \
            else exp.LTE(this=x.copy(), expression=exp.Literal.number(str(pts[(end - 1) // 2])))
        out_ifs.append(exp.If(this=bound, true=by_sql[r].copy()))
    last = by_sql[regions[-1][1]]
    if not out_ifs:
        return last.copy()
    out = exp.Case(ifs=out_ifs)
    if not isinstance(last, exp.Null):
        out.set("default", last.copy())
    return out


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

    def _Anonymous(self, n):
        # an unquoted function name folds to lowercase, like any identifier
        return f"FN:{str(n.this).lower()}(" + ",".join(self(a) for a in n.expressions) + ")"

    def _AnonymousAggFunc(self, n):
        return f"AGG:{str(n.this).lower()}(" + ",".join(self(a) for a in n.expressions) + ")"

    def _Greatest(self, n):
        return "GREATEST{" + ",".join(sorted(set(self(a) for a in [n.this, *n.expressions]))) + "}"

    def _Least(self, n):
        return "LEAST{" + ",".join(sorted(set(self(a) for a in [n.this, *n.expressions]))) + "}"

    def _Values(self, n):
        rows = [self(r) for r in n.expressions]
        if isinstance(n.parent, (exp.From, exp.Join)):
            rows.sort()  # the rows of a FROM item are a bag
        return f"VALUES[{','.join(rows)}]:{self(n.args.get('alias'))}"

    def _Window(self, n):
        """Fixed argument order; PARTITION BY is a set (its order does not matter)."""
        parts = [self(n.this)]
        if n.args.get("partition_by"):
            parts.append("part={" + ",".join(sorted(set(self(p) for p in n.args["partition_by"]))) + "}")
        for k in ("order", "spec", "alias"):
            if n.args.get(k):
                parts.append(f"{k}={self(n.args[k])}")
        return "WIN(" + ";".join(parts) + ")"

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
        out = "GROUP{" + ",".join(sorted(set(self(e) for e in n.expressions))) + "}"
        if any(n.args.get(k) for k in _GROUPING_SET_ARGS):
            sets = sorted("(" + ",".join(sorted(set(self(e) for e in s))) + ")" for s in _grouping_sets(n))
            out += "SETS[" + ",".join(sets) + "]"  # repeats kept: a repeated set repeats its rows
        return out

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
    tree = _inline_named_windows(tree)
    tree = _normalize_grouping_sets(tree)
    tree = _normalize_clauses(tree)
    tree = _drop_noop_row_limits(tree)
    _strip_orders(tree, keep_outer_order)  # orders that cannot matter, before rules that would trip on them
    tree = _inline_ctes(tree)
    tree = _unpivot_to_union(tree)
    tree = _select_star_of_setop(tree)
    tree = _union_of_distinct_rows_to_all(tree, ctx)
    tree = _tree_walk_cte_union_to_all(tree, ctx)
    tree = _grand_total_union_to_grouping_sets(tree, ctx)
    tree = _unique_aliases(tree)
    tree = _rewrite_laterals(tree, ctx, keep_outer_order)
    tree = _whole_window_to_scalar(tree)
    tree = _distinct_over_union_all(tree)
    tree = _unique_aliases(tree)  # the rewrites above copy subqueries and their aliases
    tree = _resolve_order_aliases(tree)
    tree = _group_by_to_distinct(tree)
    tree = _prune_derived_columns(tree)
    tree = _positional_derived_columns(tree)
    tree = _merge_simple_derived(tree)
    # without a schema every catalog lookup returns "unknown", so only schema-free rewrites fire
    R = Resolver(ctx, tree)
    R.keep_root_order = keep_outer_order
    tree = _except_on_key_to_not_exists(tree, R, ctx)
    R.refresh(tree)
    tree = _merge_grouped_derived(tree, R, ctx)
    R.refresh(tree)
    tree = _left_join_grouped_derived(tree, R, ctx)
    tree = _count_over_distinct_derived(tree, R, ctx)
    R.refresh(tree)
    tree = _avg_of_group_sums(tree, R, ctx)
    R.refresh(tree)
    tree = _row_number_to_distinct_on(tree, keep_outer_order)
    tree = _row_number_filter_to_limit(tree, keep_outer_order)
    R.refresh(tree)
    tree = _value_sets_to_semi_joins(tree, R, ctx)
    tree = _distinct_set_branches(tree)
    R.refresh(tree)
    tree = _drop_null_in_list_items(tree)
    tree = tree.transform(lambda n: _norm_expr(n, R))
    R.refresh(tree)
    for sel in [s for s in tree.walk(bfs=False) if isinstance(s, exp.Select)]:
        _decorrelate_with_exists(sel, R, ctx)  # before inner rewrites make the EXISTS and the subqueries differ
    for _ in range(2):  # rewrites in outer scopes can enable rewrites in inner ones
        R.refresh(tree)
        for sel in reversed([s for s in tree.walk(bfs=False) if isinstance(s, exp.Select)]):
            _scope_rewrites(sel, R, ctx)
        tree = tree.transform(lambda n: _norm_expr(n, R))
        tree = _flatten_exists_derived(tree)
        R.refresh(tree)
        tree = _exists_self_lookup(tree, R, ctx)
        tree = _unique_aliases(_split_correlated_or_exists(tree))  # the split copies subquery aliases
    if _canonical_counts(tree):  # filters moved to WHERE get the usual WHERE normalization
        R.refresh(tree)
        for sel in reversed([s for s in tree.walk(bfs=False) if isinstance(s, exp.Select)]):
            _scope_rewrites(sel, R, ctx)
        tree = tree.transform(lambda n: _norm_expr(n, R))
        tree = _flatten_exists_derived(tree)
        R.refresh(tree)
        tree = _exists_self_lookup(tree, R, ctx)
    if _aggregates_to_scalar_subqueries(tree):
        tree = _unique_aliases(tree)  # each new subquery copies the FROM's aliases
    R.refresh(tree)
    tree = tree.transform(lambda n: _canonical_case(n, R), copy=False)  # once, after comparisons are normalized
    _clean_exists(tree)
    _strip_orders(tree, keep_outer_order)
    return serialize(tree)
