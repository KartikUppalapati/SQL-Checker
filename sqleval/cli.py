import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import get_context
from pathlib import Path

from .equivalence import Verdict, compare

PARALLEL_THRESHOLD = 400  # below this many pairs, worker start-up costs more than it saves

_SCHEMAS: dict[str, str] = {}


def _quiet():
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def load_schemas(schemas_dir: Path) -> dict[str, str]:
    if not schemas_dir.is_dir():
        return {}
    return {p.stem: p.read_text() for p in sorted(schemas_dir.glob("*.sql"))}


def score_pair(pair: dict, schemas: dict[str, str]) -> Verdict:
    predicted = pair.get("predicted")
    if not isinstance(predicted, str) or not predicted.strip():
        return Verdict(False, 0.0, "no_prediction")
    gt = pair.get("ground_truth")
    if not isinstance(gt, str) or not gt.strip():
        return Verdict(False, 0.0, "no_ground_truth")
    try:
        return compare(gt, predicted, schemas.get(pair.get("schema_id")))
    except Exception as e:  # never let one pair abort the run
        return Verdict(False, 0.0, f"error:{type(e).__name__}")


def _init_worker(schemas):
    global _SCHEMAS
    _quiet()
    _SCHEMAS = schemas


def _work(pair):
    return score_pair(pair, _SCHEMAS)


def _workers(requested: int | None) -> int:
    if requested:
        return max(1, requested)
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def _read_pairs(path: Path):
    pairs, bad = [], 0
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                pairs.append(obj)
            else:
                bad += 1
    return pairs, bad


def main(argv=None) -> int:
    _quiet()
    ap = argparse.ArgumentParser(description="Score predicted SQL against ground truth (no execution, no LLMs).")
    ap.add_argument("--schemas", required=True, type=Path, help="directory of <schema_id>.sql DDL files")
    ap.add_argument("--pairs", required=True, type=Path, help="JSONL with id, schema_id, ground_truth, predicted")
    ap.add_argument("--output", required=True, type=Path, help="JSONL results: id, match, score")
    ap.add_argument("--workers", type=int, default=None, help="worker processes (default: available CPUs)")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    schemas = load_schemas(args.schemas)
    pairs, bad_lines = _read_pairs(args.pairs)

    workers = _workers(args.workers)
    if workers > 1 and len(pairs) >= PARALLEL_THRESHOLD:
        methods = __import__("multiprocessing").get_all_start_methods()
        ctx = get_context("fork" if "fork" in methods else "spawn")
        with ctx.Pool(workers, initializer=_init_worker, initargs=(schemas,)) as pool:
            verdicts = list(pool.imap(_work, pairs, chunksize=32))
    else:
        verdicts = [score_pair(p, schemas) for p in pairs]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        for p, v in zip(pairs, verdicts):
            f.write(json.dumps({"id": p.get("id"), "match": v.match, "score": 1.0 if v.match else round(v.score, 4)})
                    + "\n")

    _report(pairs, verdicts, schemas, bad_lines, time.perf_counter() - t0, workers)
    return 0


def _report(pairs, verdicts, schemas, bad_lines, elapsed, workers):
    n = len(pairs)
    matched = sum(v.match for v in verdicts)
    print("SQL equivalence report")
    print("======================")
    print(f"pairs scored     : {n}" + (f"  ({bad_lines} malformed input lines skipped)" if bad_lines else ""))
    print(f"matched          : {matched} ({matched / n:.1%})" if n else "matched          : 0")
    print(f"mean score       : {sum(1.0 if v.match else v.score for v in verdicts) / n:.4f}" if n else "")
    unknown = sorted({str(p.get('schema_id')) for p in pairs if p.get("schema_id") not in schemas})
    if unknown:
        print(f"unknown schemas  : {', '.join(unknown[:10])} (scored without schema constraints)")
    print(f"time             : {elapsed:.2f}s ({n / elapsed:.0f} pairs/s, {workers} worker(s))" if elapsed else "")

    print("\nverdict reasons:")
    for reason, count in Counter(v.reason.split(":")[0] if v.reason.startswith("error") else v.reason
                                 for v in verdicts).most_common():
        print(f"  {reason:32s} {count}")

    by_schema = defaultdict(lambda: [0, 0])
    for p, v in zip(pairs, verdicts):
        s = by_schema[str(p.get("schema_id"))]
        s[0] += 1
        s[1] += v.match
    print("\nmatch rate by schema:")
    for sid, (tot, m) in sorted(by_schema.items()):
        print(f"  {sid:32s} {m:5d}/{tot:<5d} {m / tot:6.1%}")

    labeled = [(p, v) for p, v in zip(pairs, verdicts) if isinstance(p.get("equivalent"), bool)]
    if not labeled:
        return
    tp = sum(1 for p, v in labeled if v.match and p["equivalent"])
    fp = sum(1 for p, v in labeled if v.match and not p["equivalent"])
    fn = sum(1 for p, v in labeled if not v.match and p["equivalent"])
    tn = sum(1 for p, v in labeled if not v.match and not p["equivalent"])
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    print(f"\nagainst labels ({len(labeled)} labeled pairs):")
    print(f"  accuracy  {(tp + tn) / len(labeled):.1%}   precision {prec:.1%}   recall {rec:.1%}   F1 {f1:.3f}")
    print(f"  confusion: TP {tp}  FP {fp}  FN {fn}  TN {tn}")
    for field in ("source", "category"):
        groups = defaultdict(lambda: [0, 0])
        for p, v in labeled:
            if field in p:
                g = groups[str(p[field])]
                g[0] += 1
                g[1] += v.match == p["equivalent"]
        if groups and len(groups) <= 60:
            print(f"\n  accuracy by {field}:")
            for k, (tot, ok) in sorted(groups.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[0])):
                print(f"    {k:34s} {ok:5d}/{tot:<5d} {ok / tot:6.1%}")


if __name__ == "__main__":
    sys.exit(main())
