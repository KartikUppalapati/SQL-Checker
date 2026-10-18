"""Decide whether a predicted query is equivalent to the ground truth."""
import re
from dataclasses import dataclass
from functools import lru_cache

from . import canonical
from .canonical import Context, Unsupported
from .schema import load_catalog
from .validate import Invalid, parse_tree


@dataclass
class Verdict:
    match: bool
    score: float
    reason: str = ""


_CONTEXTS: dict[str, Context] = {}


def context_for(schema_ddl: str | None) -> Context:
    key = schema_ddl or ""
    if key not in _CONTEXTS:
        _CONTEXTS[key] = Context(load_catalog(schema_ddl) if schema_ddl else None)
    return _CONTEXTS[key]


@lru_cache(maxsize=4096)
def _analyze_gt(gt: str, schema_ddl: str | None):
    """Ground truth analysis is shared by every pair with the same target."""
    ctx = context_for(schema_ddl)
    try:
        ast = parse_tree(gt)
    except Invalid:
        return None, None, None
    try:
        tree = canonical.prepare(gt, ctx)
        ordered = canonical.has_outer_order(tree)
        return ast, canonical.canonicalize_tree(tree, ctx, ordered), ordered
    except (Invalid, Unsupported, RecursionError):
        return ast, None, None
    except Exception:
        return ast, None, None


def invalid_reason(sql: str, schema_ddl: str | None) -> str | None:
    """Why the harness rejects sql as not a valid PostgreSQL query, or None if it accepts it
    (including when it cannot canonicalize it). The same gate compare() applies."""
    try:
        parse_tree(sql)
        ctx = context_for(schema_ddl)
        canonical.canonicalize_tree(canonical.prepare(sql, ctx), ctx, True)
    except Invalid as e:
        return str(e)
    except Exception:
        return None
    return None


def compare(ground_truth: str, predicted: str, schema_ddl: str | None) -> Verdict:
    # 1. the prediction must be exactly one PostgreSQL query
    try:
        pred_ast = parse_tree(predicted)
    except Invalid as e:
        return Verdict(False, 0.0, f"invalid:{e}")

    gt_ast, gt_canon, gt_ordered = _analyze_gt(ground_truth, schema_ddl)
    if gt_ast is None:
        return Verdict(False, 0.0, "ground_truth_unparseable")

    # 2. identical PostgreSQL parse trees (formatting, keyword case, unquoted identifier case)
    if pred_ast == gt_ast:
        return Verdict(True, 1.0, "identical_parse_tree")

    # 3. canonical forms
    ctx = context_for(schema_ddl)
    try:
        tree = canonical.prepare(predicted, ctx)
    except Invalid as e:
        return Verdict(False, 0.0, f"invalid:{e}")
    except Unsupported:
        return Verdict(False, _text_similarity(ground_truth, predicted), "unsupported")
    if gt_canon is None:
        return Verdict(False, _text_similarity(ground_truth, predicted), "ground_truth_unsupported")
    try:
        pred_canon = canonical.canonicalize_tree(tree, ctx, gt_ordered)
    except Invalid as e:
        return Verdict(False, 0.0, f"invalid:{e}")
    except Exception:
        return Verdict(False, _text_similarity(ground_truth, predicted), "unsupported")

    if pred_canon == gt_canon:
        return Verdict(True, 1.0, "canonical_match")
    return Verdict(False, _canon_similarity(gt_canon, pred_canon), "not_equivalent")


# ----------------------------------------------------------------------------- partial credit

_TOK = re.compile(r"[A-Za-z_#'][\w#.'%-]*")


def _tokens(s: str) -> set[str]:
    return set(_TOK.findall(s))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _canon_similarity(gt: str, pred: str) -> float:
    """Partial credit for a valid but non-equivalent prediction: overlap of canonical tokens
    (tables, columns, operators, literals), capped below 1.0."""
    return round(0.95 * _jaccard(_tokens(gt), _tokens(pred)), 4)


def _text_similarity(gt: str, pred: str) -> float:
    return round(0.5 * _jaccard(_tokens(gt.lower()), _tokens(pred.lower())), 4)
