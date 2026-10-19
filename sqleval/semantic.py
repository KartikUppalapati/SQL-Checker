"""Errors PostgreSQL raises during parse analysis that the grammar alone does not catch.

A query that fails one of these checks would fail before it returned a row, on every database,
so it is never equivalent to anything. Each check is lenient where it is unsure: it rejects a
query only when PostgreSQL certainly would, and accepts it otherwise.

check_names() runs on the parsed tree before sqlglot's qualify, which silently repairs some of
these errors (it expands an output alias used in WHERE, for example). check_types() runs on the
qualified tree, where every column names its source.
"""
import datetime as _dt
import re

from sqlglot import exp

from .validate import Invalid

# ============================================================================ scopes and sources


def _selects_up(node):
    """The SELECTs enclosing node, innermost first."""
    p = node.parent
    while p is not None:
        if isinstance(p, exp.Select):
            yield p
        p = p.parent


def _owner(node):
    return next(_selects_up(node), None)


def _sources(sel):
    """[(name, node, join)] for the FROM item and joins of a SELECT."""
    out = []
    frm = sel.args.get("from")
    if frm is not None:
        out.append((frm.this.alias_or_name, frm.this, None))
    for j in sel.args.get("joins") or []:
        out.append((j.this.alias_or_name, j.this, j))
    return out


def _outputs(q):
    """The select-list entries of a query (the leftmost branch of a set operation)."""
    while isinstance(q, (exp.Subquery, exp.SetOperation)):
        q = q.this
    return q.expressions if isinstance(q, exp.Select) else None


def _has_star(projs):
    return projs is None or any(isinstance(p, exp.Star) or isinstance(p, exp.Column) and isinstance(p.this, exp.Star)
                                for p in projs)


def _output_names(q):
    """Names of a query's output columns, or None when any is unknown."""
    projs = _outputs(q)
    if _has_star(projs):
        return None
    names = [p.alias_or_name for p in projs]
    return names if all(names) else None


def _cte_body(node, name):
    """The query of the CTE `name` visible from node."""
    p = node.parent
    while p is not None:
        w = p.args.get("with") if isinstance(p, exp.Expression) else None
        if isinstance(w, exp.With):
            for cte in w.expressions:
                if cte.alias_or_name == name:
                    return cte.this
        p = p.parent
    return None


def _source_columns(node, ctx):
    """Column names a FROM item exposes, or None when they cannot be determined."""
    alias = node.args.get("alias")
    if isinstance(alias, exp.TableAlias) and alias.columns:
        return [c.name for c in alias.columns]
    if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
        body = _cte_body(node, node.name)
        if body is not None:
            return _output_names(body)
        t = ctx.table(node.name) if not node.args.get("db") else None
        return list(t.columns) if t else None
    if isinstance(node, exp.Subquery):
        return _output_names(node.this)
    if isinstance(node, exp.Lateral) and isinstance(node.this, exp.Subquery):
        return _output_names(node.this.this)
    return None


# ============================================================================ checks before qualify


def check_names(tree, ctx):
    for sel in list(tree.find_all(exp.Select)):
        _check_duplicate_aliases(sel)
        _check_positions(sel, sel.expressions)
        _check_output_alias_refs(sel, ctx)
        _check_ambiguous(sel, ctx)
        _check_sibling_refs(sel)
        _check_using(sel, ctx)
    for op in list(tree.find_all(exp.SetOperation)):
        _check_set_op_order(op)
    _check_missing_from_entries(tree)


def _check_duplicate_aliases(sel):
    names = [n for n, _, _ in _sources(sel) if n]
    if len(names) != len(set(names)):
        raise Invalid("duplicate_table_alias")  # table name "x" specified more than once


def _check_positions(sel, projs):
    """ORDER BY / GROUP BY <n> must name a select-list entry."""
    if _has_star(projs):
        return
    items = [o.this for o in (sel.args["order"].expressions if sel.args.get("order") else [])]
    if sel.args.get("group"):
        items += sel.args["group"].expressions
    for it in items:
        if isinstance(it, exp.Literal) and it.is_int and not 1 <= int(it.this) <= len(projs):
            raise Invalid("position_not_in_select_list")


def _check_set_op_order(op):
    """ORDER BY on a UNION / INTERSECT / EXCEPT names an output column, by name or position."""
    order = op.args.get("order")
    if not order:
        return
    projs = _outputs(op)
    names = _output_names(op)
    for o in order.expressions:
        it = o.this
        while isinstance(it, exp.Paren):
            it = it.this
        if isinstance(it, exp.Literal) and it.is_int:
            if not _has_star(projs) and not 1 <= int(it.this) <= len(projs):
                raise Invalid("position_not_in_select_list")
        elif isinstance(it, exp.Column) and not it.table and isinstance(it.this, exp.Identifier):
            if names is not None and it.name not in names:
                raise Invalid("set_op_order_by")
        else:
            raise Invalid("set_op_order_by")  # a qualified column or an expression


def _visible_columns(sel, ctx):
    """Input column names visible in sel (its FROM items and those of enclosing queries), or None."""
    cols = set()
    for s in [sel, *_selects_up(sel)]:
        for _, node, _ in _sources(s):
            c = _source_columns(node, ctx)
            if c is None:
                return None
            cols |= set(c)
    return cols


def _clause_of(col, sel):
    """The clause of sel that contains col, as (arg_key, top-level item or None)."""
    node, child = col, None
    while node is not None and node is not sel:
        child, node = node, node.parent
    return (child.arg_key if child is not None else None), child


def _check_output_alias_refs(sel, ctx):
    """An output alias is visible only as a whole ORDER BY / GROUP BY item, never in WHERE, ON or
    HAVING, and never inside an ORDER BY / GROUP BY expression."""
    aliases = {p.alias for p in sel.expressions if isinstance(p, exp.Alias)}
    if not aliases:
        return
    visible = None
    for col in sel.find_all(exp.Column):
        if col.table or not isinstance(col.this, exp.Identifier) or col.name not in aliases or _owner(col) is not sel:
            continue
        key, item = _clause_of(col, sel)
        if key in ("distinct", "windows", None):
            continue
        if key == "expressions" and any(p is col or isinstance(p, exp.Alias) and p.this is col for p in sel.expressions):
            continue  # the entry itself (SELECT x AS x)
        if key in ("order", "group"):
            top = item
            if key == "order":
                top = next((o for o in item.expressions if any(n is col for n in o.this.walk())), None)
                top = top.this if top is not None else None
            else:
                top = next((g for g in item.expressions if any(n is col for n in g.walk())), None)
            while isinstance(top, exp.Paren):
                top = top.this
            if top is col or top is None:
                continue
        if visible is None:
            visible = _visible_columns(sel, ctx)
            if visible is None:
                return
        if col.name not in visible:
            raise Invalid("output_alias_reference")  # column "x" does not exist


def _check_ambiguous(sel, ctx):
    """An unqualified column in the select list, WHERE or HAVING that two FROM items expose."""
    srcs = _sources(sel)
    if len(srcs) < 2 or any(j is not None and (j.args.get("using") or (j.args.get("method") or "").upper() == "NATURAL"
                                               or j.args.get("kind") == "NATURAL") for _, _, j in srcs):
        return
    cols = [_source_columns(node, ctx) for _, node, _ in srcs]
    if any(c is None for c in cols):
        return
    for col in sel.find_all(exp.Column):
        if col.table or not isinstance(col.this, exp.Identifier) or _owner(col) is not sel:
            continue
        key, _ = _clause_of(col, sel)
        if key not in ("expressions", "where", "having"):
            continue
        if sum(col.name in c for c in cols) > 1:
            raise Invalid("ambiguous_column")


def _check_using(sel, ctx):
    """JOIN ... USING (c): c must be a column of the joined item and of an item to its left."""
    srcs = _sources(sel)
    for i, (_, node, j) in enumerate(srcs):
        if j is None or not j.args.get("using"):
            continue
        right = _source_columns(node, ctx)
        left = [_source_columns(n, ctx) for _, n, _ in srcs[:i]]
        for u in j.args["using"]:
            name = u.name
            if right is not None and name not in right:
                raise Invalid("using_column_missing")
            if all(c is not None for c in left) and not any(name in c for c in left):
                raise Invalid("using_column_missing")


def _defined_names(node):
    """Names of the FROM items and CTEs defined anywhere inside node."""
    names = set()
    for s in node.find_all(exp.Select):
        names |= {n for n, _, _ in _sources(s)}
    names |= {c.alias_or_name for c in node.find_all(exp.CTE)}
    return names


def _check_sibling_refs(sel):
    """A derived table in FROM cannot see the other FROM items unless it is LATERAL."""
    srcs = _sources(sel)
    if len(srcs) < 2:
        return
    outer = {n for s in _selects_up(sel) for n, _, _ in _sources(s)}
    for name, node, _ in srcs:
        if not isinstance(node, exp.Subquery):
            continue
        siblings = {n for n, _, _ in srcs if n != name} - outer
        inner = _defined_names(node)
        for col in node.find_all(exp.Column):
            if col.table and not col.args.get("db") and col.table in siblings and col.table not in inner:
                raise Invalid("invalid_from_reference")


def _check_missing_from_entries(tree):
    """A qualifier must name a FROM item of the query or of an enclosing query."""
    for col in tree.find_all(exp.Column):
        if not col.table or col.args.get("db") or col.args.get("catalog"):
            continue
        if not any(col.table == n for s in _selects_up(col) for n, _, _ in _sources(s)):
            raise Invalid("missing_from_entry")


# ============================================================================ types

# PostgreSQL type categories: N numeric, S string, D date/time, T interval, B boolean, E enum.
# A type is (category, detail); U is an untyped string literal, detail its text.

_INT = {"INT", "INTEGER", "BIGINT", "SMALLINT", "INT2", "INT4", "INT8", "SERIAL", "BIGSERIAL", "SMALLSERIAL"}
_DEC = {"DECIMAL", "NUMERIC"}
_FLOAT = {"FLOAT", "DOUBLE", "DOUBLE PRECISION", "REAL", "FLOAT4", "FLOAT8"}
_STR = {"VARCHAR", "TEXT", "CHAR", "CHARACTER", "CHARACTER VARYING", "BPCHAR", "NCHAR", "NVARCHAR", "NAME"}


def type_category(type_str, enum_labels=None):
    if enum_labels:
        return ("E", tuple(enum_labels))
    base = re.sub(r"\(.*\)", "", type_str.upper()).strip()
    if base in _INT:
        return ("N", "int")
    if base in _DEC:
        return ("N", "dec")
    if base in _FLOAT:
        return ("N", "float")
    if base in _STR:
        return ("S", None)
    if base == "DATE":
        return ("D", "date")
    if base in ("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPLTZ"):
        return ("D", "tstz")
    if base in ("TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE", "DATETIME"):
        return ("D", "ts")
    if base in ("BOOLEAN", "BOOL"):
        return ("B", None)
    if base == "INTERVAL":
        return ("T", None)
    return None


_BOOL_WORDS = ("true", "false", "yes", "no")


def literal_converts(text, target) -> bool:
    """Whether PostgreSQL's input function for the target type accepts the literal (lenient)."""
    cat, detail = target
    s = text.strip()
    if cat == "N":
        if detail == "int":
            return bool(re.fullmatch(r"[+-]?(\d(_?\d)*|0[xX](_?[0-9a-fA-F])+|0[oO](_?[0-7])+|0[bB](_?[01])+)", s))
        return bool(re.fullmatch(r"[+-]?((\d(_?\d)*)(\.(\d(_?\d)*)?)?|\.\d(_?\d)*)([eE][+-]?\d+)?", s)
                    or re.fullmatch(r"[+-]?0[xXoObB][0-9a-fA-F_]+", s)
                    or s.lower().lstrip("+-") in ("nan", "infinity", "inf"))
    if cat == "B":
        low = s.lower()
        return bool(low) and (any(w.startswith(low) for w in _BOOL_WORDS) or low in ("on", "of", "off", "1", "0"))
    if cat == "D":
        if re.match(r"\d{4}-\d{1,2}-\d{1,2}[A-SU-Ya-su-y]", s):
            return False  # letters glued to the date (not a T separator or a Z offset)
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?![\d])", s)
        if m and re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}([ T].*)?", s):
            try:
                _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return False
        return True
    return True


class _Typer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.depth = 0

    def column(self, col):
        if not col.table:
            return None
        for sel in _selects_up(col):
            for name, node, _ in _sources(sel):
                if name == col.table:
                    return self.source_column(node, col.name)
        return None

    def source_column(self, node, name):
        alias = node.args.get("alias")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            body = _cte_body(node, node.name)
            if body is not None:
                return self.query_column(body, name, alias)
            t = self.ctx.table(node.name) if not node.args.get("db") else None
            c = t.columns.get(name) if t else None
            return type_category(c.type, c.enum_labels) if c else None
        if isinstance(node, exp.Subquery):
            return self.query_column(node.this, name, alias)
        if isinstance(node, exp.Lateral) and isinstance(node.this, exp.Subquery):
            return self.query_column(node.this.this, name, alias)
        return None

    def query_column(self, q, name, alias=None):
        projs = _outputs(q)
        if _has_star(projs):
            return None
        if isinstance(alias, exp.TableAlias) and alias.columns:
            names = [c.name for c in alias.columns]
            if name not in names or names.index(name) >= len(projs):
                return None
            p = projs[names.index(name)]
        else:
            p = next((p for p in projs if p.alias_or_name == name), None)
        if p is None or self.depth > 20:
            return None
        self.depth += 1
        try:
            t = self.of(p)
        finally:
            self.depth -= 1
        return t if t and t[0] != "U" else None

    def of(self, e):
        while isinstance(e, (exp.Paren, exp.Alias)):
            e = e.this
        if isinstance(e, exp.Column):
            return self.column(e)
        if isinstance(e, exp.Literal):
            return ("U", e.this) if e.is_string else ("N", "int" if e.is_int else "dec")
        if isinstance(e, (exp.Any, exp.All)) or isinstance(e, exp.Anonymous) and str(e.this).upper() in ("ANY", "ALL", "SOME"):
            return None  # the element type of an array or subquery: not inferred
        if isinstance(e, (exp.Boolean, exp.Predicate, exp.Connector, exp.Not, exp.Exists)):
            return ("B", None)
        if isinstance(e, exp.Cast):
            return self.datatype(e.to)
        if isinstance(e, exp.Interval):
            return ("T", None)
        if isinstance(e, exp.Extract):  # EXTRACT returns numeric, date_part double precision
            return ("N", "float" if e.args.get("date_part") else "dec")
        if isinstance(e, (exp.Count, exp.RowNumber, exp.Length, exp.StrPosition)):
            return ("N", "int")
        if isinstance(e, (exp.Lower, exp.Upper, exp.Trim, exp.Concat, exp.DPipe, exp.Substring, exp.TimeToStr,
                          exp.Initcap, exp.GroupConcat)):
            return ("S", None)
        if isinstance(e, exp.CurrentDate):
            return ("D", "date")
        if isinstance(e, (exp.CurrentTimestamp, exp.TimestampTrunc)):
            return ("D", "tstz")
        if isinstance(e, (exp.Min, exp.Max, exp.Neg, exp.Abs)):
            return self.of(e.this)
        if isinstance(e, (exp.Sum, exp.Avg)):
            t = self.of(e.this.expressions[0] if isinstance(e.this, exp.Distinct) and e.this.expressions else e.this)
            if t and t[0] == "T":
                return t
            if not t or t[0] != "N":
                return None
            return ("N", "float") if t[1] == "float" else ("N", "int") if isinstance(e, exp.Sum) and t[1] == "int" \
                else ("N", "dec")
        if isinstance(e, exp.AtTimeZone):
            t = self.of(e.this)
            return ("D", "ts" if t[1] == "tstz" else "tstz") if t and t[0] == "D" else None
        if isinstance(e, exp.Date) and not e.expressions:
            return ("D", "date")
        if isinstance(e, (exp.Round, exp.Ceil, exp.Floor)):
            return self.of(e.this)
        if isinstance(e, (exp.Coalesce, exp.Greatest, exp.Least, exp.Nullif)):
            return self.common([e.this, *e.expressions]) if not isinstance(e, exp.Nullif) else self.of(e.this)
        if isinstance(e, exp.Case):
            return self.common([i.args.get("true") for i in e.args.get("ifs") or []] + [e.args.get("default")])
        if isinstance(e, exp.Window):
            return self.of(e.this)
        if isinstance(e, exp.Subquery) and isinstance(e.this, (exp.Select, exp.SetOperation)):
            projs = _outputs(e.this)
            if projs and len(projs) == 1 and not _has_star(projs):
                self.depth += 1
                try:
                    t = self.of(projs[0]) if self.depth < 20 else None
                finally:
                    self.depth -= 1
                return t if t and t[0] != "U" else None
            return None
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
            a, b = self.of(e.this), self.of(e.expression)
            if not (a and b) or "U" in (a[0], b[0]):
                return None
            if a[0] == "N" and b[0] == "N":
                order = ["int", "dec", "float"]
                return ("N", max(a[1], b[1], key=lambda d: order.index(d) if d in order else 1))
            cats = (a[0], b[0])
            if isinstance(e, exp.Sub) and cats == ("D", "D"):
                return ("N", "int") if a[1] == b[1] == "date" else ("T", None)  # date - date is days
            if isinstance(e, (exp.Add, exp.Sub)) and cats in (("D", "T"), ("T", "D")):
                d = a if a[0] == "D" else b
                return ("D", "ts" if d[1] == "date" else d[1])
            if isinstance(e, (exp.Add, exp.Sub)) and cats in (("D", "N"), ("N", "D")):
                return ("D", "date")
            if cats == ("T", "T") and isinstance(e, (exp.Add, exp.Sub)) or "T" in cats and "N" in cats \
                    and isinstance(e, (exp.Mul, exp.Div)):
                return ("T", None)
            return None
        return None

    def datatype(self, dt):
        if not isinstance(dt, exp.DataType):
            return None
        if dt.this == exp.DataType.Type.USERDEFINED:
            name = str(dt.args.get("kind") or "").split(".")[-1].strip('"')
            labels = (self.ctx.catalog.enums.get(name) or self.ctx.catalog.enums.get(name.lower())) \
                if self.ctx.catalog else None
            return ("E", tuple(labels)) if labels else None
        return type_category(dt.sql(dialect="postgres"))

    def common(self, exprs):
        """The common type of CASE / COALESCE / UNION inputs; raises when PostgreSQL can't match them."""
        types = [self.of(x) for x in exprs if x is not None]
        known = [t for t in types if t and t[0] != "U"]
        for t in known[1:]:
            _require_same(known[0], t)
        if known:
            for t in types:
                if t and t[0] == "U":
                    _require_literal(t[1], known[0])
            return known[0]
        return None


def _require_same(a, b):
    if a[0] != b[0] or a[0] == "E" and a[1] != b[1]:
        raise Invalid("type_mismatch")  # operator does not exist / types cannot be matched


def _require_literal(text, target):
    if target[0] == "E":
        if text not in target[1]:
            raise Invalid("invalid_enum_label")  # invalid input value for enum
    elif not literal_converts(text, target):
        raise Invalid("invalid_literal")  # invalid input syntax for type ...


def _comparable(a, b):
    if a is None or b is None or a[0] == "U" and b[0] == "U":
        return
    if a[0] == "U":
        return _require_literal(a[1], b)
    if b[0] == "U":
        return _require_literal(b[1], a)
    _require_same(a, b)


def _require_boolean(T, e):
    if e is None:
        return
    t = T.of(e)
    if t is None:
        return
    if t[0] == "U":
        return _require_literal(t[1], ("B", None))
    if t[0] != "B":
        raise Invalid("non_boolean_condition")  # argument of WHERE must be type boolean


def _agg_arg(e):
    a = e.this
    if isinstance(a, exp.Distinct):
        a = a.expressions[0] if a.expressions else None
    if isinstance(a, exp.Order):
        a = a.this
    return a


_WINDOW_ONLY = (exp.RowNumber, exp.Lag, exp.Lead, exp.FirstValue, exp.LastValue, exp.NthValue)
_WINDOW_ONLY_NAMES = {"rank", "dense_rank", "ntile", "percent_rank", "cume_dist"}


def _is_window_only(e):
    return isinstance(e, _WINDOW_ONLY) or isinstance(e, exp.Anonymous) and str(e.this).lower() in _WINDOW_ONLY_NAMES


def _scalar_position(sq):
    p = sq.parent
    if isinstance(p, (exp.From, exp.Join, exp.Lateral, exp.Exists, exp.SetOperation, exp.CTE, exp.Any, exp.All,
                      exp.Subquery, exp.Array)) or p is None:
        return False
    if isinstance(p, exp.In) and sq.arg_key == "query":
        return False
    if isinstance(p, exp.Binary) and any(isinstance(x, exp.Tuple) for x in (p.this, p.expression)):
        return False
    return True


def check_types(tree, ctx):
    T = _Typer(ctx)
    for e in tree.walk():
        if isinstance(e, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.NullSafeEQ, exp.NullSafeNEQ)):
            _comparable(T.of(e.this), T.of(e.expression))
        elif isinstance(e, exp.In):
            if e.args.get("query") is not None:
                q = e.args["query"]
                projs = _outputs(q.this if isinstance(q, exp.Subquery) else q)
                width = len(e.this.expressions) if isinstance(e.this, exp.Tuple) else 1
                if not _has_star(projs) and len(projs) != width:
                    raise Invalid("subquery_column_count")
                if width == 1 and not _has_star(projs) and len(projs) == 1:
                    _comparable(T.of(e.this), T.query_column(q.this if isinstance(q, exp.Subquery) else q,
                                                             projs[0].alias_or_name) if projs[0].alias_or_name else None)
            elif e.expressions and not isinstance(e.this, exp.Tuple):
                T.common([e.this, *e.expressions])
        elif isinstance(e, exp.Between):
            T.common([e.this, e.args.get("low"), e.args.get("high")])
        elif isinstance(e, (exp.Like, exp.ILike, exp.SimilarTo, exp.RegexpLike, exp.RegexpILike)):
            t = T.of(e.this)
            if t and t[0] in "NDBET":
                raise Invalid("type_mismatch")  # operator does not exist: integer ~~ unknown
        elif isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
            a, b = T.of(e.this), T.of(e.expression)
            cats = {t[0] for t in (a, b) if t}
            if cats & {"S", "B", "E"}:
                raise Invalid("type_mismatch")  # operator does not exist: character varying + integer
            if a and b and a[0] == "D" and b[0] == "D" and isinstance(e, exp.Add):
                raise Invalid("type_mismatch")
            if "D" in cats and isinstance(e, (exp.Mul, exp.Div, exp.Mod)):
                raise Invalid("type_mismatch")
        elif isinstance(e, (exp.Where, exp.Having)):
            _require_boolean(T, e.this)
        elif isinstance(e, exp.Join):
            _require_boolean(T, e.args.get("on"))
        elif isinstance(e, (exp.And, exp.Or)):
            _require_boolean(T, e.this)
            _require_boolean(T, e.expression)
        elif isinstance(e, exp.Not):
            _require_boolean(T, e.this)
        elif isinstance(e, exp.Case):
            if e.this is not None:
                for i in e.args.get("ifs") or []:
                    _comparable(T.of(e.this), T.of(i.this))
            else:
                for i in e.args.get("ifs") or []:
                    _require_boolean(T, i.this)
            T.common([i.args.get("true") for i in e.args.get("ifs") or []] + [e.args.get("default")])
        elif isinstance(e, (exp.Coalesce, exp.Greatest, exp.Least)):
            T.common([e.this, *e.expressions])
        elif isinstance(e, exp.Nullif):
            _comparable(T.of(e.this), T.of(e.expression))
        elif isinstance(e, (exp.Sum, exp.Avg)):
            t = T.of(_agg_arg(e))
            if t and t[0] in "SBED":
                raise Invalid("aggregate_argument_type")  # function sum(character varying) does not exist
        elif isinstance(e, (exp.Min, exp.Max)):
            t = T.of(_agg_arg(e))
            if t and t[0] == "B":
                raise Invalid("aggregate_argument_type")  # function max(boolean) does not exist
        elif isinstance(e, (exp.LogicalAnd, exp.LogicalOr)):
            t = T.of(_agg_arg(e))
            if t and t[0] in "NSDET":
                raise Invalid("aggregate_argument_type")
        elif isinstance(e, exp.GroupConcat):
            t = T.of(_agg_arg(e))
            if t and t[0] in "NDBE":
                raise Invalid("aggregate_argument_type")  # function string_agg(integer, unknown) does not exist
        elif isinstance(e, exp.SetOperation):
            left, right = _outputs(e.this), _outputs(e.expression)
            if not _has_star(left) and not _has_star(right):
                if len(left) != len(right):
                    raise Invalid("set_op_column_count")
                for a, b in zip(left, right):
                    T.common([a, b])
        elif isinstance(e, exp.Subquery) and _scalar_position(e):
            projs = _outputs(e.this)
            if not _has_star(projs) and len(projs) != 1:
                raise Invalid("subquery_column_count")  # subquery must return only one column
        elif isinstance(e, exp.Round) and e.args.get("decimals") is not None \
                or isinstance(e, exp.Anonymous) and str(e.this).lower() == "trunc" and len(e.expressions) == 2:
            t = T.of(e.this if isinstance(e, exp.Round) else e.expressions[0])
            if t and t[0] == "N" and t[1] == "float":
                raise Invalid("function_signature")  # round(double precision, integer) does not exist
        elif isinstance(e, exp.Cast) and isinstance(e.this, exp.Literal) and e.this.is_string:
            t = T.datatype(e.to)
            if t:
                _require_literal(e.this.this, t)
        elif isinstance(e, exp.Select):
            _check_distinct_on(e)
            _check_limit(e)
    _check_placement(tree)


def _resolve_item(sel, it):
    while isinstance(it, exp.Paren):
        it = it.this
    projs = sel.expressions
    if isinstance(it, exp.Literal) and it.is_int:
        n = int(it.this)
        return projs[n - 1].this.sql() if 1 <= n <= len(projs) and isinstance(projs[n - 1], exp.Alias) \
            else projs[n - 1].sql() if 1 <= n <= len(projs) else None
    if isinstance(it, exp.Column) and not it.table:
        for p in projs:
            if isinstance(p, exp.Alias) and p.alias == it.name:
                return p.this.sql()
    return it.sql()


def _check_distinct_on(sel):
    """SELECT DISTINCT ON expressions must match the initial ORDER BY expressions."""
    d = sel.args.get("distinct")
    on = d.args.get("on") if d else None
    order = sel.args.get("order")
    if on is None or not order or _has_star(sel.expressions):
        return
    on_exprs = on.expressions if isinstance(on, exp.Tuple) else [on]
    remaining = {_resolve_item(sel, x) for x in on_exprs}
    keys = set(remaining)
    for o in order.expressions:
        r = _resolve_item(sel, o.this)
        if r in keys:
            remaining.discard(r)
        elif remaining:
            raise Invalid("distinct_on_order_by")
        else:
            break


def _check_limit(sel):
    for key in ("limit", "offset"):
        node = sel.args.get(key)
        if node is None or isinstance(node, exp.Fetch):
            continue
        e = node.args.get("expression")
        if e is None:
            continue
        if any(_owner(c) is sel for c in e.find_all(exp.Column)):
            raise Invalid("limit_variable")  # argument of LIMIT must not contain variables
        if isinstance(e, exp.Literal) and e.is_string and not literal_converts(e.this, ("N", "int")):
            raise Invalid("invalid_literal")


def _is_window_agg(e):
    p = e.parent
    while isinstance(p, (exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        e, p = p, p.parent
    return isinstance(p, exp.Window) and e.arg_key == "this"


def _own_level(agg, sel):
    """An aggregate belongs to the innermost query whose columns it reads (to its own query when it
    reads none). Lenient: one that reads only outer columns is treated as belonging elsewhere."""
    cols = [c for c in agg.find_all(exp.Column) if _owner(c) is sel]
    if not cols:
        return True
    names = {n for n, _, _ in _sources(sel)}
    return any(not c.table or c.table in names for c in cols)


def _check_placement(tree):
    for e in tree.walk():
        is_agg = isinstance(e, exp.AggFunc) and not _is_window_agg(e)
        is_win = isinstance(e, exp.Window) and not (isinstance(e.parent, exp.Select) and e.arg_key == "windows")
        if _is_window_only(e) and not isinstance(e.parent, (exp.Window, exp.WithinGroup, exp.IgnoreNulls,
                                                            exp.RespectNulls)):
            raise Invalid("window_function_without_over")
        if not (is_agg or is_win):
            continue
        sel = _owner(e)
        if sel is None or is_agg and not _own_level(e, sel):
            continue
        node, child = e.parent, e
        while node is not None and node is not sel:
            if isinstance(node, exp.Subquery) and node.parent is not None:
                break
            if is_agg and isinstance(node, exp.AggFunc) and not _is_window_agg(node):
                raise Invalid("nested_aggregate")  # aggregate function calls cannot be nested
            if is_win and (isinstance(node, exp.Window) or isinstance(node, exp.AggFunc) and not _is_window_agg(node)):
                raise Invalid("nested_window")  # window function calls cannot be nested / inside an aggregate
            if isinstance(node, exp.Join) and child.arg_key == "on":
                raise Invalid("aggregate_placement")  # not allowed in JOIN conditions
            child, node = node, node.parent
        if node is sel and child.arg_key in ("where", "group") or is_win and node is sel and child.arg_key == "having":
            raise Invalid("aggregate_placement")  # not allowed in WHERE / GROUP BY / HAVING
