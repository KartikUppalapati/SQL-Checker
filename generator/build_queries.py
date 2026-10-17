"""Validate the hand-authored schemas and target queries and write dataset/queries.jsonl.

Run from the repo root:  python -m generator.build_queries

Every schema must parse with PostgreSQL's own parser (pglast). Every target query must be
exactly one SELECT that parses with pglast and whose tables and columns all resolve
against its schema under PostgreSQL case folding (sqlglot qualify).
"""
import importlib
import json
import pkgutil
import sys
from pathlib import Path

import pglast
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from generator import queries as queries_pkg
from generator.schema_meta import SCHEMA_META
from sqleval.schema import load_catalog, norm_ident

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "dataset" / "schemas"
OUT = ROOT / "dataset" / "queries.jsonl"


def auto_tags(tree: exp.Expression) -> set[str]:
    tags = set()
    selects = list(tree.find_all(exp.Select))
    joins = list(tree.find_all(exp.Join))
    if joins:
        tags.add("join")
        if len(joins) >= 2:
            tags.add("multi_join")
    if any(isinstance(n, exp.AggFunc) for n in tree.walk()):
        tags.add("aggregation")
    if tree.find(exp.Group):
        tags.add("group_by")
    if tree.find(exp.Having):
        tags.add("having")
    if len(selects) > 1 or tree.find(exp.Subquery):
        tags.add("subquery")
    if tree.find(exp.With):
        tags.add("cte")
        if tree.find(exp.With).args.get("recursive"):
            tags.add("recursive_cte")
    if tree.find(exp.Window):
        tags.add("window")
    if tree.find(exp.Union, exp.Intersect, exp.Except):
        tags.add("set_op")
    if tree.args.get("order"):
        tags.add("order_by")
    if tree.args.get("limit"):
        tags.add("limit")
    if tree.find(exp.Exists):
        tags.add("exists")
    if tree.find(exp.Distinct):
        tags.add("distinct")
    if tree.find(exp.Case):
        tags.add("case")
    if tree.find(exp.Coalesce) or tree.find(exp.Is):
        tags.add("null_handling")
    return tags


def validate_query(sql: str, schema: MappingSchema) -> exp.Expression:
    stmts = pglast.parse_sql(sql)
    if len(stmts) != 1 or type(stmts[0].stmt).__name__ != "SelectStmt":
        raise ValueError("target must be exactly one SELECT statement")
    tree = sqlglot.parse_one(sql, read="postgres")
    cte_names = {norm_ident(c.args["alias"].this) for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = norm_ident(t.this)
        if name not in cte_names and not schema.find(exp.table_(exp.to_identifier(name, quoted=True))):
            raise ValueError(f"unknown table {name!r}")
    qualify(tree.copy(), schema=schema, dialect="postgres",
            validate_qualify_columns=True, quote_identifiers=False)
    return tree


def main() -> int:
    errors = 0
    rows = []
    modules = sorted(m.name for m in pkgutil.iter_modules(queries_pkg.__path__))
    schema_files = {p.stem for p in SCHEMAS_DIR.glob("*.sql")}
    missing = schema_files - set(modules)
    if missing:
        print(f"WARNING: schemas without queries: {sorted(missing)}")

    for name in modules:
        mod = importlib.import_module(f"generator.queries.{name}")
        sid = mod.SCHEMA_ID
        ddl = (SCHEMAS_DIR / f"{sid}.sql").read_text()
        pglast.parse_sql(ddl)
        catalog = load_catalog(ddl)
        schema = MappingSchema(catalog.sqlglot_schema(), dialect="postgres", normalize=False)
        meta = SCHEMA_META[sid]
        schema_tags = [*(f"shape:{s}" for s in meta["shape"]), f"naming:{meta['naming']}",
                       f"quoting:{meta['quoting']}", f"flags:{meta['flags']}"]

        seen_questions = set()
        for i, (question, sql, extra_tags) in enumerate(mod.QUERIES, start=1):
            qid = f"{sid}_{i:03d}"
            if question in seen_questions:
                print(f"{qid}: duplicate question")
                errors += 1
            seen_questions.add(question)
            try:
                tree = validate_query(sql, schema)
            except Exception as e:
                print(f"{qid}: INVALID: {type(e).__name__}: {e}\n    {sql[:200]}")
                errors += 1
                continue
            tags = sorted(auto_tags(tree) | set(extra_tags) | set(schema_tags))
            rows.append({"id": qid, "schema_id": sid, "question": question,
                         "sql": sql.strip(), "tags": tags})

    if errors:
        print(f"\n{errors} error(s); {OUT} not written")
        return 1
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} queries across {len(modules)} schemas to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
