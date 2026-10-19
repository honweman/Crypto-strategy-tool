"""Backward-looking rank dependence and conservative complete-link deduplication."""
from __future__ import annotations

import math
import statistics
from itertools import combinations

MIN_SAMPLES = 90
WINDOW = 180
VERSION = "daily-spearman-v1"


def spearman(left, right):
    if len(left) != len(right) or len(left) < 3:
        return None
    if any(max(values)-min(values) <= 1e-12*max(1, max(abs(x) for x in values)) for values in (left, right)):
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        output = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and values[order[end]] == values[order[start]]:
                end += 1
            for index in order[start:end]:
                output[index] = (start + end - 1) / 2
            start = end
        return output

    a, b = ranks(left), ranks(right)
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = sum((x-ma)**2 for x in a), sum((x-mb)**2 for x in b)
    if not va or not vb:
        return None
    return max(-1.0, min(1.0, sum((x-ma)*(y-mb) for x, y in zip(a, b)) / math.sqrt(va*vb)))


def correlation_report(rows, groups, cutoff):
    # One closed UTC day per row, strictly before the currently evaluated candle.
    rows = sorted((row for row in rows if row["at"] < cutoff), key=lambda row: row["at"])[-WINDOW:]
    if len({row["at"] for row in rows}) != len(rows):
        raise ValueError("duplicate_correlation_day")
    pairs = []
    for left, right in combinations(sorted(groups), 2):
        if groups[left] != groups[right]:
            continue
        sample = [(r["at"], r.get(left), r.get(right)) for r in rows]
        sample = [(at, a, b) for at, a, b in sample
                  if all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (a, b))]
        a, b = [x[1] for x in sample], [x[2] for x in sample]
        n, half = len(sample), len(sample)//2
        contiguous = all(abs(y[0]-x[0]-86400) < .01 for x, y in zip(sample, sample[1:]))
        rho, early, late = spearman(a, b), spearman(a[:half], b[:half]), spearman(a[half:], b[half:])
        status = "insufficient" if n < MIN_SAMPLES else "gapped" if not contiguous else "constant" if None in (rho, early, late) else "eligible"
        pairs.append({"left": left, "right": right, "n": n, "rho": rho, "early_rho": early,
                      "late_rho": late, "status": status})
    return {"version": VERSION, "method": "Spearman / complete-link", "window": WINDOW,
            "min_samples": MIN_SAMPLES, "days": len(rows), "start": rows[0]["at"] if rows else None,
            "end": rows[-1]["at"] if rows else None, "cutoff": cutoff, "pairs": pairs}


def stable_pair(pair, threshold, orientation=None):
    if pair.get("status") != "eligible" or pair.get("n", 0) < MIN_SAMPLES:
        return False
    values = [pair.get(key) for key in ("rho", "early_rho", "late_rho")]
    if any(v is None or not math.isfinite(v) for v in values):
        return False
    direction = orientation if orientation is not None else (1 if values[0] > 0 else -1)
    rho, early, late = [v * direction for v in values]
    return rho >= threshold and min(early, late) >= max(.75, threshold-.1)


def complete_link(keys, pairs, threshold, orientations=None):
    lookup = {frozenset((p["left"], p["right"])): p for p in pairs}
    clusters = [[key] for key in sorted(keys)]
    while True:
        candidates = []
        for i, j in combinations(range(len(clusters)), 2):
            strengths = []
            for a in clusters[i]:
                for b in clusters[j]:
                    pair = lookup.get(frozenset((a, b)), {})
                    direction = orientations[a]*orientations[b] if orientations else None
                    if not stable_pair(pair, threshold, direction):
                        strengths = []
                        break
                    strengths.append(abs(pair["rho"]))
                if not strengths:
                    break
            if strengths:
                candidates.append((min(strengths), -i, -j))
        if not candidates:
            break
        _, i, j = max(candidates)
        i, j = -i, -j
        clusters[i] = sorted(clusters[i] + clusters[j])
        clusters.pop(j)
    return clusters


def merge_checks(checks, pairs, threshold, structural, eligible):
    """Fixed semantic families first; empirical links may not bridge untested pairs."""
    by_key = {c["key"]: c for c in checks}
    groups = [sorted(set(family) & by_key.keys()) for family in structural]
    groups = [g for g in groups if g]
    used = {key for group in groups for key in group}
    groups.extend([key] for key in by_key if key not in used)
    empirical = complete_link([key for key in by_key if key in eligible], pairs, threshold, eligible)
    # A whole structural cluster must be contained in one empirical cluster before merging.
    for cluster in empirical:
        covered = [g for g in groups if set(g) <= set(cluster)]
        if len(covered) > 1:
            groups = [g for g in groups if g not in covered] + [sorted(k for g in covered for k in g)]
    result = []
    for keys in groups:
        budget = max(by_key[k]["max_points"] for k in keys)
        points = max(by_key[k]["points"] for k in keys)
        reason = "structural" if any(set(keys) <= set(f) for f in structural) else "empirical" if len(keys) > 1 else "single"
        result.append({"keys": keys, "reason": reason, "points": points, "max_points": budget,
                       "removed_budget": sum(by_key[k]["max_points"] for k in keys)-budget})
    return result
