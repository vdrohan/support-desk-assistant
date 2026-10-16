"""Small-sample statistics for eval reports: Wilson intervals, confusion counts, kappa.

Pure functions, no I/O. Rates over a few dozen items are noisy, so every reported rate
carries its n and a Wilson score interval (which behaves at k=0 and k=n, unlike the
normal approximation). Agreement between human and machine verdicts is reported as a
confusion matrix plus Cohen's kappa.
"""

import math
from collections.abc import Sequence
from typing import Any


def wilson_interval(k: int, n: int, z: float) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials; None when n == 0."""
    if n == 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside 0..n={n}")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo = min(max(centre - half, 0.0), 1.0)
    hi = min(max(centre + half, 0.0), 1.0)
    return round(lo, 3), round(hi, 3)


def rate_ci(k: int, n: int, z: float) -> dict[str, Any]:
    """{"k", "n", "rate", "ci95"}; rate and ci95 are None when n == 0."""
    ci = wilson_interval(k, n, z)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 3) if n else None,
        "ci95": list(ci) if ci is not None else None,
    }


def confusion(pairs: Sequence[tuple[bool, bool]]) -> dict[str, int]:
    """Counts for (human_present, machine_present) pairs, positive = "present".

    fp: machine present, human absent ("false present").
    fn: machine absent, human present ("false absent").
    """
    out = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for human, machine in pairs:
        if human and machine:
            out["tp"] += 1
        elif machine:
            out["fp"] += 1
        elif human:
            out["fn"] += 1
        else:
            out["tn"] += 1
    return out


def cohens_kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two raters on a binary label. None if there are no pairs or
    chance agreement is 1 (both raters gave one label to everything)."""
    n = len(pairs)
    if n == 0:
        return None
    c = confusion(pairs)
    observed = (c["tp"] + c["tn"]) / n
    human_yes = (c["tp"] + c["fn"]) / n
    machine_yes = (c["tp"] + c["fp"]) / n
    chance = human_yes * machine_yes + (1 - human_yes) * (1 - machine_yes)
    if math.isclose(chance, 1.0):
        return None
    return round((observed - chance) / (1 - chance), 3)
