import argparse
import csv
import hashlib
import io
import json
import statistics
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

REFERENCE = Path(__file__).resolve().parents[1] / 'original_experiment_reruns_v1/calculation/rows_10000'
sys.path.insert(0, str(REFERENCE))
from fabric.maintenance import maintain
import config
import build_fixtures


def load_orders(path):
    with ZipFile(path) as archive:
        def rows(name):
            with archive.open(f"olist_{name}_dataset.csv") as stream:
                yield from csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))

        orders = sorted((row["order_purchase_timestamp"], row["order_id"]) for row in rows("orders"))
        items, payments = defaultdict(Decimal), defaultdict(Decimal)
        item_counts, payment_counts = defaultdict(int), defaultdict(int)
        for row in rows("order_items"):
            items[row["order_id"]] += Decimal(row["price"])
            item_counts[row["order_id"]] += 1
        for row in rows("order_payments"):
            payments[row["order_id"]] += Decimal(row["payment_value"])
            payment_counts[row["order_id"]] += 1
    if len(orders) < 10000 or len({key for _, key in orders}) != len(orders):
        raise ValueError("Expected at least 10,000 unique orders")
    return [(key, items[key], payments[key], item_counts[key], payment_counts[key]) for _, key in orders]


def build_events(orders, data_dir):

    source = Path(build_fixtures.__file__).read_text(encoding="utf-8")
    original = "base=[{'x':str(Decimal(i)/10),'y':str(i)} for i in range(ROWS)]"
    if source.count(original) != 1:
        raise ValueError("Original fixture generator changed")
    source = source.replace(original, "base=[{'x':str(price),'y':str(payment)} for _, price, payment, _, _ in order_data]")

    boundary = "base[0]['x']=None; base[1]['x']=''; base[2]['x']='1.005'; base[3]['x']='1.015'"
    if source.count(boundary) != 1:
        raise ValueError("Original boundary fixture changed")
    source = source.replace(boundary, "")
    config.ROWS = len(orders)
    config.DATA_ROOT = data_dir
    namespace = {"__name__": "olist_fixtures", "order_data": orders}
    exec(compile(source, build_fixtures.__file__, "exec"), namespace)
    return namespace["build"]()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, default=Path("olist_results"))
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--sizes", nargs="+", default=["60", "10000", "all"])
    args = parser.parse_args()
    orders = load_orders(args.archive)
    args.out.mkdir(parents=True, exist_ok=False)
    result = []
    trials = []
    scope = []
    for label in args.sizes:
        count = len(orders) if label == "all" else int(label)
        selected = orders[:count]
        folder = args.out / str(count)
        folder.mkdir()
        scope.append(dict(orders=count, item_rows=sum(row[3] for row in selected),
                          payment_rows=sum(row[4] for row in selected),
                          orders_without_items=sum(row[3] == 0 for row in selected),
                          orders_without_payments=sum(row[4] == 0 for row in selected)))
        events, queries = build_events(selected, folder / "fixtures")
        for repeat in range(args.repeats):
            order = ["A", "B", "C"]
            order = order[repeat % 3:] + order[:repeat % 3]
            for method in order:
                records, _ = maintain(method, events, folder / f"catalog_{repeat}_{method}.sqlite", repeat=repeat)
                row = dict(orders=count, method=method, repeat=repeat,
                           checks=sum(r["checks"] for r in records),
                           query_execs=sum(r["reexecutions"] for r in records),
                           query_reuses=sum(r["reused"] for r in records),
                           maintenance_ms=sum(r["total_seconds"] for r in records)*1000)
                if row["query_execs"] + row["query_reuses"] != len(queries)*(len(events)-1):
                    raise AssertionError(row)
                trials.append(row)
                with (args.out / "trials.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row)+"\n")
                print(row, flush=True)
            by_method = {row["method"]: row for row in trials[-3:]}
            for key in ("query_execs", "query_reuses"):
                if by_method["B"][key] != by_method["C"][key]:
                    raise AssertionError("B and C differ in " + key)
        for method in ("A", "B", "C"):
            runs = [r for r in trials if r["orders"] == count and r["method"] == method]
            result.append(dict(orders=count, method=method, **{
                key: statistics.median(r[key] for r in runs)
                for key in ("checks", "query_execs", "query_reuses", "maintenance_ms")
            }))
        with (args.out / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(result[0]))
            writer.writeheader()
            writer.writerows(result)
        (args.out / "manifest.json").write_text(json.dumps({
            "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
            "repeats": args.repeats, "scope": scope,
            "sampling": "nested chronological prefixes, order ID breaks timestamp ties",
            "values": "sum of item price and sum of payment value per order, empty sums are zero",
            "checks": "computational equivalence checks",
            "events": [e["name"] for e in events[1:]], "queries": len(queries)
        }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
