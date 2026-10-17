"""Build dataset/pairs.jsonl from dataset/queries.jsonl.

Run from the repo root (after build_queries):  python -m generator.build_pairs

For every target query this emits, in order:
  1. hand-written predictions from generator/pairs/<schema>.py (label + reason written by hand)
  2. automatic equivalence-preserving rewrites   (generator/mutations.py, label True)
  3. automatic breaking mutations                 (generator/mutations.py, label False)
  4. one malformed prediction (null, empty, prose, markdown, two statements, ...; label False)

Sanity checks enforced while building:
  * every prediction labeled equivalent must parse with PostgreSQL's parser and resolve
    against the schema (so a typo can never be labeled a match);
  * hand-written non-equivalent predictions must also be valid unless marked "INVALID:",
    and those marked INVALID must actually fail;
  * automatic breaking mutations must change the parse tree, and (unless they are the
    quoting mutations, which must fail) must still be valid SQL;
  * no duplicate (query, prediction) pairs.
Selection is deterministic: a per-query RNG is seeded from the query id.
"""
import importlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

from sqlglot.schema import MappingSchema

from generator.build_queries import validate_query
from generator.mutations import (EQ_MUTATIONS, EXPECT_INVALID, MALFORMED_KINDS, NEQ_MUTATIONS,
                                 malformed, parse, pg_ast, roundtrip_faithful)
from generator.schema_meta import SCHEMA_META
from sqleval.schema import load_catalog

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "dataset" / "queries.jsonl"
SCHEMAS_DIR = ROOT / "dataset" / "schemas"
OUT = ROOT / "dataset" / "pairs.jsonl"

AUTO_EQ_PER_QUERY = 3
AUTO_NEQ_PER_QUERY = 3

# Purely cosmetic rewrites: must leave the PostgreSQL parse tree unchanged. At most one per query,
# so the automatic positives are dominated by structural rewrites.
COSMETIC_EQ = {"trailing_semicolon", "leading_comment", "reformat_oneline", "reformat_pretty",
               "lowercase", "uppercase", "neq_operator_style"}
# Shape/validity-only negatives (column count/order, identifier quoting). At most one per query,
# so the automatic negatives are dominated by semantic mutations.
STRUCTURAL_NEQ = {"swap_columns", "drop_column", "unquote_identifiers", "quote_identifiers"}


def tiered(names, rng, low_tier, low_quota):
    """Shuffle, then order: high-tier names first, `low_quota` low-tier names next, remaining low-tier last."""
    names = list(names)
    rng.shuffle(names)
    high = [n for n in names if n not in low_tier]
    low = [n for n in names if n in low_tier]
    return low[:low_quota] + high + low[low_quota:]


def ddl_names(ddl: str) -> set[str]:
    """Mixed-case table and column names as spelled in an unquoted DDL file."""
    body = re.sub(r"--[^\n]*", "", ddl)
    return {w for w in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", body) if w != w.lower() and w != w.upper()}


def is_valid(sql, schema) -> bool:
    try:
        validate_query(sql, schema)
        return True
    except Exception:
        return False


def main() -> int:
    queries = [json.loads(l) for l in QUERIES.read_text().splitlines() if l.strip()]
    schemas, hand = {}, {}
    for sid in sorted({q["schema_id"] for q in queries}):
        ddl = (SCHEMAS_DIR / f"{sid}.sql").read_text()
        schemas[sid] = (MappingSchema(load_catalog(ddl).sqlglot_schema(), dialect="postgres", normalize=False),
                        ddl_names(ddl))
        try:
            hand[sid] = importlib.import_module(f"generator.pairs.{sid}").PAIRS
        except ModuleNotFoundError:
            hand[sid] = {}

    rows, errors = [], 0
    stats = Counter()

    def emit(q, predicted, equivalent, source, category, note):
        rows.append({"id": None, "schema_id": q["schema_id"], "ground_truth": q["sql"],
                     "predicted": predicted, "equivalent": equivalent,
                     "query_id": q["id"], "source": source, "category": category, "note": note})
        stats[(source, equivalent)] += 1

    for qi, q in enumerate(queries):
        sid, gt = q["schema_id"], q["sql"]
        schema, names = schemas[sid]
        meta = SCHEMA_META[sid]
        num = int(q["id"].rsplit("_", 1)[1])
        rng = random.Random(q["id"])
        gt_ast = pg_ast(gt)
        seen = {re.sub(r"\s+", " ", gt.strip())}

        def fresh(sql):
            key = re.sub(r"\s+", " ", sql.strip()) if isinstance(sql, str) else repr(sql)
            if key in seen:
                return False
            seen.add(key)
            return True

        # 1. hand-written
        for equivalent, pred, note in hand[sid].get(num, []):
            pred = pred.strip()
            expect_invalid = note.startswith("INVALID:")
            valid = is_valid(pred, schema)
            if (equivalent or not expect_invalid) and not valid:
                try:
                    validate_query(pred, schema)
                except Exception as e:
                    print(f"{q['id']}: hand prediction should be valid but is not: {e}\n    {pred[:160]}")
                errors += 1
                continue
            if expect_invalid and valid:
                print(f"{q['id']}: hand prediction marked INVALID but validates\n    {pred[:160]}")
                errors += 1
                continue
            if not equivalent and pg_ast(pred) == gt_ast:
                print(f"{q['id']}: non-equivalent hand prediction parses identically to the target")
                errors += 1
                continue
            if fresh(pred):
                emit(q, pred, equivalent, "hand", "invalid_sql" if expect_invalid else "semantic", note)

        ctx = {"faithful": roundtrip_faithful(gt), "tree": parse(gt), "quoting": meta["quoting"],
               "naming": meta["naming"], "ddl_names": names}

        # 2. automatic equivalence-preserving rewrites
        eq_names = tiered(EQ_MUTATIONS, rng, COSMETIC_EQ, 1)
        taken = 0
        for name in eq_names:
            if taken >= AUTO_EQ_PER_QUERY:
                break
            out = EQ_MUTATIONS[name](gt, ctx)
            if not out or not fresh(out):
                continue
            if not is_valid(out, schema):
                print(f"{q['id']}: eq mutation {name} produced invalid SQL\n    {out[:160]}")
                errors += 1
                continue
            if name in COSMETIC_EQ and pg_ast(out) != gt_ast:
                print(f"{q['id']}: cosmetic mutation {name} changed the parse tree\n    {out[:160]}")
                errors += 1
                continue
            emit(q, out, True, "auto", name, "equivalence-preserving rewrite")
            taken += 1

        # 3. automatic breaking mutations
        neq_names = tiered(NEQ_MUTATIONS, rng, STRUCTURAL_NEQ, 1)
        neq_names = neq_names[1:] + neq_names[:1]  # semantic mutations first, one structural after
        taken = 0
        for name in neq_names:
            if taken >= AUTO_NEQ_PER_QUERY:
                break
            out = NEQ_MUTATIONS[name](gt, ctx)
            if not out or pg_ast(out) == gt_ast or not fresh(out):
                continue
            valid = is_valid(out, schema)
            if (name in EXPECT_INVALID) == valid:
                continue  # quoting mutation that happened to resolve, or a mutation that broke parsing
            emit(q, out, False, "auto", name, "breaking mutation")
            taken += 1

        # 4. one malformed prediction, cycling through the kinds
        first_table = next(iter(ctx["tree"].find_all(__import__("sqlglot").exp.Table))).sql(dialect="postgres")
        for k in range(len(MALFORMED_KINDS)):
            kind = MALFORMED_KINDS[(qi + k) % len(MALFORMED_KINDS)]
            out = malformed(gt, kind, first_table)
            if kind not in ("null", "dml_statement") and (out is None or (out and is_valid(out, schema))):
                continue
            emit(q, out, False, "malformed", kind, "not exactly one valid PostgreSQL SELECT")
            break

    if errors:
        print(f"\n{errors} error(s); {OUT} not written")
        return 1

    for i, r in enumerate(rows, start=1):
        r["id"] = f"p{i:05d}"
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    n_eq = sum(r["equivalent"] for r in rows)
    print(f"wrote {len(rows)} pairs ({n_eq} equivalent, {len(rows) - n_eq} not) to {OUT.relative_to(ROOT)}")
    for (source, eq), n in sorted(stats.items()):
        print(f"  {source:10s} {'equivalent' if eq else 'not equivalent':15s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
