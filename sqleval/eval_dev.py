"""Developer tool: list the harness's mistakes on a labeled pairs file.

python -m sqleval.eval_dev --schemas dataset/schemas --pairs dataset/pairs.jsonl [--show fp|fn|all] [--limit N]
"""
import argparse
import json
from pathlib import Path

from .cli import load_schemas, score_pair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", type=Path, default=Path("dataset/schemas"))
    ap.add_argument("--pairs", type=Path, default=Path("dataset/pairs.jsonl"))
    ap.add_argument("--show", choices=["fp", "fn", "all"], default="all")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--source")
    args = ap.parse_args()
    schemas = load_schemas(args.schemas)
    shown = 0
    for line in args.pairs.read_text().splitlines():
        p = json.loads(line)
        if args.source and p.get("source") != args.source:
            continue
        v = score_pair(p, schemas)
        if v.match == p["equivalent"]:
            continue
        kind = "FP" if v.match else "FN"
        if args.show != "all" and kind.lower() != args.show:
            continue
        print(f"--- {kind} {p['id']} {p.get('query_id')} [{p.get('source')}/{p.get('category')}] {v.reason}")
        print(f"    note: {p.get('note')}")
        print("    GT:  " + p["ground_truth"].replace("\n", " "))
        print("    PR:  " + str(p["predicted"]).replace("\n", " "))
        shown += 1
        if shown >= args.limit:
            break


if __name__ == "__main__":
    main()
