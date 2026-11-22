from __future__ import annotations
import argparse
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


class State(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Premise:
    key: str
    revision: str
    dependencies: tuple[str, ...]
    evaluator: Callable[[dict], State]

    dependencies_complete: bool = False


@dataclass(frozen=True)
class Evidence:
    signature: tuple
    state: State


class Scheduler:







    def __init__(self, incremental: bool):
        self.incremental = incremental
        self.cache: dict[str, Evidence] = {}

    def evaluate(self, premises: list[Premise], snapshot: dict, versions: dict):
        if len({p.key for p in premises}) != len(premises):
            raise ValueError("Duplicate premise IDs")
        active = {p.key for p in premises}
        self.cache = {k: v for k, v in self.cache.items() if k in active}
        states = {}
        checks = hits = 0
        for p in premises:
            deps = tuple(sorted(set(p.dependencies)))
            version_known = all(isinstance(versions.get(d), str) and versions[d]
                                for d in deps)
            signature = (p.revision, p.dependencies_complete,
                         tuple((d, versions.get(d)) for d in deps))
            old = self.cache.get(p.key)
            reusable = (p.dependencies_complete and bool(p.revision)
                        and version_known)
            if (self.incremental and reusable and old is not None
                    and old.signature == signature and old.state != State.UNKNOWN):
                state = old.state
                hits += 1
            else:
                if not version_known:
                    state = State.UNKNOWN
                else:
                    state = p.evaluator(snapshot)
                    checks += 1
                    if not isinstance(state, State):
                        raise TypeError("Evaluator must return State")
                self.cache[p.key] = Evidence(signature, state)
            states[p.key] = state.value
        values = set(states.values())

        business = (State.INVALID if State.INVALID.value in values else
                    State.UNKNOWN if not values or State.UNKNOWN.value in values
                    else State.VALID)
        return {"premise_states": states, "business_state": business.value,
                "deliver": business == State.VALID,
                "premise_checks": checks, "evidence_hits": hits}


def equal_field(field, required):
    def evaluate(snapshot):
        value = snapshot.get(field)
        return (State.UNKNOWN if value is None else
                State.VALID if value == required else State.INVALID)
    return evaluate


def smoke():
    full, incremental = Scheduler(False), Scheduler(True)
    premises = [
        Premise("unit", "rule-unit-v1", ("unit",), equal_field("unit", "EUR"), True),
        Premise("scope", "rule-scope-v1", ("scope",), equal_field("scope", "payable"), True),
    ]

    cases = [
        ("initial", "EUR", "payable", "u1", "s1", "VALID", 2),
        ("unrelated_change", "EUR", "payable", "u1", "s1", "VALID", 0),
        ("scope_changed_answer_unchanged", "EUR", "paid", "u1", "s2", "INVALID", 1),
        ("recovery", "EUR", "payable", "u1", "s3", "VALID", 1),
        ("missing_version", "EUR", "payable", "u1", None, "UNKNOWN", 0),
        ("version_restored", "EUR", "payable", "u1", "s4", "VALID", 1),
    ]
    rows = []
    for name, unit, scope, uv, sv, expected, checks in cases:
        snapshot = {"unit": unit, "scope": scope, "amount": 100}
        versions = {"unit": uv, "scope": sv}
        a = full.evaluate(premises, snapshot, versions)
        b = incremental.evaluate(premises, snapshot, versions)
        assert a["premise_states"] == b["premise_states"], name
        assert a["business_state"] == b["business_state"] == expected, name
        assert a["deliver"] == b["deliver"] == (expected == "VALID"), name
        assert b["premise_checks"] == checks, name
        rows.append({"case": name, "query_answer": 100, "expected_state": expected,
                     "full": a, "D": b, "passed": True})

    revised = [Premise("unit", "rule-unit-v2", ("unit",), equal_field("unit", "USD"), True)]
    for runner in (full, incremental):
        r = runner.evaluate(revised, {"unit": "EUR"}, {"unit": "u1"})
        assert r["business_state"] == "INVALID" and r["premise_checks"] == 1

    incomplete = [Premise("unit", "r1", ("unit",), equal_field("unit", "EUR"), False)]
    runner = Scheduler(True)
    runner.evaluate(incomplete, {"unit": "EUR"}, {"unit": "u1"})
    r = runner.evaluate(incomplete, {"unit": "USD"}, {"unit": "u1"})
    assert r["business_state"] == "INVALID" and r["evidence_hits"] == 0

    runner = Scheduler(True)
    complete = [Premise("unit", "r1", ("unit",), equal_field("unit", "EUR"), True)]
    r = runner.evaluate(complete, {}, {"unit": "u1"})
    assert r["business_state"] == "UNKNOWN" and not r["deliver"]
    r = runner.evaluate(complete, {}, {"unit": "u1"})
    assert r["premise_checks"] == 1 and r["evidence_hits"] == 0
    assert runner.evaluate([], {}, {})["business_state"] == "UNKNOWN"
    return {"kind": "synthetic_unit_verification", "new_implementation": True,
            "ontoripple_executed": False, "real_procurement_data": False,
            "passed": True, "cases": rows,
            "extra_checks": ["rule_revision", "incomplete_dependencies", "unknown_not_reused", "empty_contract"],
            "limitations": ["Trusted complete dependency declarations and snapshot versions required.",
                            "No query reuse algorithm, RML engine, or SHACL adapter implemented here.",
                            "No reproduction claim for experiments 29/30; no benchmark performance claim."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/dcm_scheduler"))
    args = parser.parse_args()
    result = smoke()
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / "summary.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in result["cases"]:
        print(row["case"], row["D"]["business_state"], "checks=", row["D"]["premise_checks"])
    print("PASS: 6 sequence cases + 4 boundary checks")
    print("UNIT VERIFICATION ONLY; not the OntoRipple comparison benchmark")
    print("RESULT:", destination.resolve())
