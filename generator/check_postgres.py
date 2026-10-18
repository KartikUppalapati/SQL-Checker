"""Check dataset/pairs.jsonl against a real PostgreSQL server. Optional; the harness never runs this.

Run from the repo root (after build_pairs):
    pip install -r generator/requirements-postgres.txt   # pgserver: an embedded PostgreSQL
    python -m generator.check_postgres [--seeds 40]

1. Validity. Every target and every prediction is planned with EXPLAIN against its schema. A
   prediction must be rejected by PostgreSQL exactly when it is labeled invalid (a hand-written note
   starting with "INVALID:", or a quoting mutation).
2. Results. Each schema is filled with random data many times (types, PRIMARY KEY, UNIQUE, NOT NULL
   and FOREIGN KEY respected; CHECK constraints dropped, as in the equivalence definition), each time
   under a different session time zone. Every pair labeled equivalent must return the same rows as
   its target on every database, compared as multisets: rows that tie under ORDER BY may come back in
   any order, so row order is left to the harness's unit tests. Pairs labeled not equivalent are only
   counted: random data shows most of them differ, but not all (some differences need, say, a
   '#' in a post body).
Exits 1 if either check finds a problem.
"""
import argparse
import datetime
import decimal
import json
import random
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import pgserver
import psycopg2

from sqleval.schema import load_catalog

ROOT = Path(__file__).resolve().parent.parent
ZONES = ["UTC", "America/New_York", "Asia/Kolkata", "Pacific/Chatham", "Europe/Berlin"]
EXPECT_INVALID = {"invalid_sql", "unquote_identifiers", "quote_identifiers"}


def ident(name):
    return '"' + name.replace('"', '""') + '"'


class Database:
    def __init__(self, cur):
        self.cur = cur

    def load(self, schema, ddl, drop_checks=False):
        c = self.cur
        c.execute(f"DROP SCHEMA IF EXISTS {ident(schema)} CASCADE; CREATE SCHEMA {ident(schema)}; "
                  f"SET search_path TO {ident(schema)};")
        c.execute(ddl)
        if drop_checks:
            c.execute("SELECT conrelid::regclass::text, conname FROM pg_constraint k JOIN pg_namespace n "
                      "ON n.oid = k.connamespace WHERE n.nspname = %s AND contype = 'c'", [schema])
            for rel, con in c.fetchall():
                c.execute(f"ALTER TABLE {rel} DROP CONSTRAINT {ident(con)}")

    def explain(self, schema, sql):
        """None if PostgreSQL accepts the query, else the first line of its error."""
        c = self.cur
        try:
            c.execute(f"SET search_path TO {ident(schema)}")
            c.execute("BEGIN")
            c.execute("EXPLAIN " + sql)
            return None
        except Exception as e:
            return str(e).strip().splitlines()[0]
        finally:
            c.execute("ROLLBACK")

    def run(self, sql):
        c = self.cur
        try:
            c.execute("BEGIN")
            c.execute("SET LOCAL statement_timeout = '3s'")
            c.execute(sql)
            return c.fetchall(), None
        except Exception as e:
            return None, str(e).splitlines()[0]
        finally:
            c.execute("ROLLBACK")


# ---------------------------------------------------------------------------------- random data

def literal_pools(sqls):
    """Values the queries mention, so random rows hit their filters."""
    strs, nums, dates = set(), set(), set()
    for s in sqls:
        for m in re.findall(r"'((?:[^']|'')*)'", s):
            m = m.replace("''", "'")
            if re.match(r"\d{4}-\d{2}-\d{2}", m):
                try:
                    dates.add(datetime.date.fromisoformat(m[:10]))
                except ValueError:
                    pass
            else:
                strs.add(m)
        nums |= set(re.findall(r"(?<![\w.'])(\d+(?:\.\d+)?)(?![\w'])", s))
    return sorted(strs), sorted(nums, key=float), sorted(dates) or [datetime.date(2024, 1, 1)]


def random_value(col, rng, pools):
    strs, nums, dates = pools
    t = col.type.upper()
    if col.name == "time_zone":
        return rng.choice(ZONES)
    if col.enum_labels:
        return rng.choice(col.enum_labels)
    if "BOOL" in t:
        return rng.choice([True, False])
    if "INT" in t:
        v = rng.choice([0, 1, 2, 3, 4, 5, 10] + [int(float(n)) for n in nums if float(n) < 3000])
        return v + (rng.choice([-1, 1]) if rng.random() < .2 else 0)
    if any(k in t for k in ("NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT")):
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", t)
        p, s = (int(m.group(1)), int(m.group(2))) if m else (10, 2)
        lim = 10 ** (p - s) - 1
        v = rng.choice([float(n) for n in nums if float(n) <= lim] + [0, 1, 2.5, 3.33, 4, 7.1])
        v += rng.choice([-.5, 0, .25, 1]) if rng.random() < .4 else 0
        return round(max(min(v, lim), -lim), s)
    if t == "DATE" or t.startswith("TIMESTAMP"):
        d = rng.choice(dates) + datetime.timedelta(days=rng.choice([-31, -7, -1, 0, 0, 1, 2, 30, 60]))
        if t == "DATE":
            return d
        return datetime.datetime.combine(d, datetime.time(rng.choice([0, 0, 9, 13, 23]), rng.choice([0, 30])))
    m = re.search(r"\((\d+)\)", t)
    n = int(m.group(1)) if m else 50
    v = rng.choice([s for s in strs if len(s) <= n and "%" not in s and "_" not in s] + ["a", "b", "B", "x y"])
    r = rng.random()
    v = v.lower() if r < .08 else v.upper() if r < .14 else v + " " if r < .18 else v
    return v[:n]


def fill(db, schema, catalog, rng, pools):
    """Delete every row, then insert random rows parent tables first."""
    c = db.cur
    c.execute(f"SET search_path TO {ident(schema)}")
    c.execute(f"SET TIME ZONE '{rng.choice(ZONES)}'")  # equivalence must hold under any session time zone
    order, seen = [], set()

    def visit(t):
        if t in seen:
            return
        seen.add(t)
        for fk in catalog.tables[t].foreign_keys:
            if fk.ref_table != t and fk.ref_table in catalog.tables:
                visit(fk.ref_table)
        order.append(t)
    for t in catalog.tables:
        visit(t)
    for t in reversed(order):
        c.execute(f"DELETE FROM {ident(t)}")
    rows = {}
    for t in order:
        table = catalog.tables[t]
        cols = list(table.columns)
        fks = {fk.columns[0]: fk for fk in table.foreign_keys}
        want = 0 if rng.random() < .1 else rng.choice([1, 3, 6, 9, 12, 12, 15])
        out = []
        for _ in range(want * 3):
            if len(out) >= want:
                break
            row = {}
            for col in table.columns.values():
                fk = fks.get(col.name)
                if fk:
                    parents = [r[fk.ref_columns[0]] for r in (out if fk.ref_table == t else rows.get(fk.ref_table, []))]
                    row[col.name] = None if not parents or not col.not_null and rng.random() < .2 else rng.choice(parents)
                elif not col.not_null and rng.random() < .2:
                    row[col.name] = None
                elif table.primary_key == [col.name] and "INT" in col.type.upper():
                    row[col.name] = len(out) + 1
                else:
                    row[col.name] = random_value(col, rng, pools)
            if any(row[col.name] is None and col.not_null for col in table.columns.values()):
                continue
            try:  # autocommit: a rejected duplicate leaves nothing behind
                c.execute(f"INSERT INTO {ident(t)} ({', '.join(map(ident, cols))}) VALUES ({', '.join(['%s'] * len(cols))})",
                          [row[k] for k in cols])
                out.append(row)
            except psycopg2.errors.UniqueViolation:
                pass
        rows[t] = out


def comparable(rows):
    def v(x):
        if isinstance(x, float):
            return decimal.Decimal(repr(x))
        if isinstance(x, list):
            return tuple(v(y) for y in x)
        return x
    return [tuple(v(x) for x in r) for r in rows]


# ---------------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40, help="random databases per schema")
    args = ap.parse_args()
    pairs = [json.loads(l) for l in (ROOT / "dataset" / "pairs.jsonl").read_text().splitlines() if l.strip()]

    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(tmp, cleanup_mode="stop")
        conn = psycopg2.connect(server.get_uri())
        conn.autocommit = True
        db = Database(conn.cursor())
        print(db.run("SELECT version()")[0][0][0].split(",")[0])
        problems = 0

        by_schema = defaultdict(list)
        for p in pairs:
            by_schema[p["schema_id"]].append(p)
        for schema, rows in sorted(by_schema.items()):
            ddl = (ROOT / "dataset" / "schemas" / f"{schema}.sql").read_text()
            db.load(schema, ddl)

            # 1. validity
            checked, valid = {}, []
            for p in rows:
                gt = p["ground_truth"]
                if gt not in checked:
                    checked[gt] = db.explain(schema, gt)
                    if checked[gt]:
                        problems += 1
                        print(f"{p['query_id']}: PostgreSQL rejects the target: {checked[gt]}")
                if p["source"] == "malformed":
                    continue
                err = db.explain(schema, p["predicted"])
                if bool(err) != (p["category"] in EXPECT_INVALID):
                    problems += 1
                    print(f"{p['id']} ({p['source']}/{p['category']}): labeled "
                          f"{'invalid' if p['category'] in EXPECT_INVALID else 'valid'}, PostgreSQL says {err or 'valid'}")
                elif not err:
                    valid.append(p)

            # 2. results on random databases
            data_schema = f"data_{schema}"
            catalog = load_catalog(ddl)
            db.load(data_schema, ddl, drop_checks=True)
            pools = literal_pools([p["ground_truth"] for p in valid] + [p["predicted"] for p in valid])
            differs = Counter()
            for seed in range(args.seeds):
                fill(db, data_schema, catalog, random.Random(f"{schema}-{seed}"), pools)
                targets = {}
                for p in valid:
                    gt = p["ground_truth"]
                    if gt not in targets:
                        targets[gt] = db.run(gt)
                    (want, e1), (got, e2) = targets[gt], db.run(p["predicted"])
                    if e1 or e2:
                        continue  # a runtime error on this data (division by zero, a time-out)
                    if Counter(comparable(want)) != Counter(comparable(got)):
                        differs[p["id"]] += 1
            shown = sum(1 for p in valid if not p["equivalent"] and differs[p["id"]])
            n_neq = sum(1 for p in valid if not p["equivalent"])
            bad = [p for p in valid if p["equivalent"] and differs[p["id"]]]
            problems += len(bad)
            for p in bad:
                print(f"{p['id']} ({p['source']}/{p['category']}): labeled equivalent but differs on "
                      f"{differs[p['id']]}/{args.seeds} databases\n    {p['predicted'][:160]}")
            print(f"{schema:16s} {len(rows):5d} pairs   not-equivalent pairs shown different: {shown}/{n_neq}")
        conn.close()

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
