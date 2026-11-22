import csv
import json
import sys
from collections import Counter
from pathlib import Path

from benchmark_olist_queries import REFERENCE, load_orders

sys.path.insert(0, str(REFERENCE.parents[2]))
from dcm_scheduler import Premise, Scheduler, State, equal_field

FIELDS = ("price", "payment", "items", "payments", "scope")
def nonnegative(field):
    return lambda s: State.VALID if s[field] >= 0 else State.INVALID

def present(field):
    return lambda s: State.VALID if s[field] > 0 else State.UNKNOWN

specs = [
    Premise("price", "1", ("price",), nonnegative("price"), True),
    Premise("payment", "1", ("payment",), nonnegative("payment"), True),
    Premise("items", "1", ("items",), present("items"), True),
    Premise("payments", "1", ("payments",), present("payments"), True),
    Premise("scope", "1", ("scope",), equal_field("scope", "gross"), True),
]
def versions(state):
    return {k: repr(state[k]) for k in FIELDS}

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the controlled Olist business cases")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    orders = [r for r in load_orders(args.archive)
              if r[1] >= 0 and r[2] >= 0 and r[3] > 0 and r[4] > 0][:2500]
    assert len(orders) == 2500
    counts = {g: Counter() for g in ("G0", "G1", "G2")}
    fields = ["order_id", "category", "expected", "method", "actual", "allow", "checks", "hits"]
    with (args.out / "olist_cases.csv").open( "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for key, price, payment, items, payments in orders:
            base = dict(price=price, payment=payment, items=items, payments=payments, scope="gross")
            for category, changes, expected in (
                ("annotation", {"note": "edited"}, "VALID"),
                ("legal_price", {"price": price + 1}, "VALID"),
                ("wrong_scope", {"scope": "net"}, "INVALID"),
                ("missing_scope", {"scope": None}, "UNKNOWN"),
            ):
                after = dict(base, **changes)
                reference_answer = after["price"]
                cached_answer = base["price"] if versions(base)["price"] == versions(after)["price"] else after["price"]
                assert cached_answer == reference_answer
                unchanged = cached_answer == base["price"]
                for group in counts:
                    if group == "G0":
                        actual, allow, checks, hits = None, True, 0, 0
                    else:
                        s = Scheduler(incremental=group == "G2")
                        if group == "G2":
                            assert s.evaluate(specs, base, versions(base))["deliver"]
                        result = s.evaluate(specs, after, versions(after))
                        actual, allow = result["business_state"], result["deliver"]
                        checks, hits = result["premise_checks"], result["evidence_hits"]
                        assert actual == expected and allow == (expected == "VALID")
                    c = counts[group]
                    c["cases"] += 1
                    c["correct_" + expected] += actual == expected
                    c["deliver_" + expected] += allow
                    c["correct_delivery"] += allow == (expected == "VALID")
                    c["identified_unchanged_invalid"] += unchanged and actual == "INVALID"
                    c["checks"] += checks
                    c["hits"] += hits
                    writer.writerow(dict(order_id=key, category=category, expected=expected, method=group,
                                         actual=actual, allow=allow, checks=checks, hits=hits))
    (args.out / "olist_case_summary.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    print(json.dumps(counts))

if __name__ == "__main__":
    main()
