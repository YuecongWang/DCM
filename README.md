# DCM

Research code for **Dual-Channel Maintenance for Data Logical Views**.

DCM maintains query results and versioned business-premise evidence, then uses both to determine whether a result may be delivered.

## Installation

Python 3.10 or later.

```bash
pip install -r requirements.txt
```

For analysis and plotting:

```bash
pip install -r requirements-analysis.txt
```

## Quick start

Run the scheduler's built-in verification:

```bash
python dcm_scheduler.py
```

## Experiments

- `controlled/` — controlled computational and business-premise cases.
- `olist/` — computational maintenance using Olist orders and payments.
- `ted_multiform/` — maintenance over seven TED form types.
- `cache_128_events_balanced/` — cache scheduling overhead.
- `expansion_v2/` — dependency audits and scale experiments.

Olist and TED inputs must be obtained separately. Some experiments require dataset paths to be configured in their scripts. Generated datasets, runtime files, and result directories are excluded by `.gitignore`.

## Data sources

- [Olist Brazilian E-Commerce Dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
- [OP-TED RDF mapping repository](https://github.com/OP-TED/ted-rdf-mapping), commit `2004fef480fe38706a5f15dc8db89806d005077d`.
