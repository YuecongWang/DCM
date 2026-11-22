import argparse
import csv
from pathlib import Path

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    args = ap.parse_args()
    out = args.out
    if out.exists():
        ap.error("use a new output path")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    source = args.summary
    with source.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    x = [int(r["premises"]) for r in rows]
    fig, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(7, 6))
    for key, label in [("json_cache", "JSON cache"), ("tuple_cache", "Tuple cache"), ("dcm", "DCM")]:
        top.plot(x, [float(r[key + "_followup_128_ms_median"]) for r in rows], marker="o", label=label)
    top.set(xscale="log", yscale="log", ylabel="128-event time (ms)")
    top.legend()
    bottom.plot(x, [-float(r["dcm_vs_tuple_saving_pct_median"]) for r in rows], marker="o")
    bottom.set(xlabel="Number of business premises", ylabel="DCM vs. tuple overhead (%)")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=180)

if __name__ == "__main__":
    main()
