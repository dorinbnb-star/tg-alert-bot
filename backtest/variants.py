"""The compared variants. Every one calls the live `detect_entry`; none re-implements a rule.

a   v1 exactly as live (R:R >= 1.8, TP = first opposing 1H pivot)
b   in-memory copy of the rules with minimum_rr_for_enter = 2.0
c1  (main) the same entries as v1; TP moved to the second opposing 1H pivot (`shadow_target`)
c2  (secondary) `opposing_target` patched with `shadow_target` inside this process only, so the R:R filter
    is already applied against the second pivot at detection time
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402

DETECTED_VARIANTS = ("a", "b", "c2")
TRADED_VARIANTS = ("a", "b", "c1", "c2")
MAIN_VARIANTS = ("a", "b", "c1")
LABELS = {
    "a": "a. v1 exact (R:R>=1.8, TP pivot 1)",
    "b": "b. R:R minim 2.0",
    "c1": "c1. TP = pivot 2 (aceleasi intrari ca v1)",
    "c2": "c2. R:R calculat fata de pivot 2 (secundar)",
}


def variant_rules(variant: str, rules: dict) -> dict:
    if variant == "b":
        return dict(rules, minimum_rr_for_enter=2.0)
    return rules


@contextmanager
def second_pivot_target():
    original = em.opposing_target
    em.opposing_target = em.shadow_target
    try:
        yield
    finally:
        em.opposing_target = original


def detect(variant: str, market: dict, rules: dict, config: dict, trace: dict | None = None) -> em.EntrySignal | None:
    chosen = variant_rules(variant, rules)
    if variant == "c2":
        with second_pivot_target():
            return em.detect_entry(market["context"], market["structure"], market["trigger"], chosen, config, trace)
    return em.detect_entry(market["context"], market["structure"], market["trigger"], chosen, config, trace)


def c1_target(signal: em.EntrySignal, market: dict, rules: dict) -> float | None:
    all_pivots = em.pivots(market["structure"], int(rules["reference_pivot_closed_candles_each_side"]))
    return em.shadow_target(all_pivots, signal.direction, signal.entry, signal.confirmation_start_ms)
