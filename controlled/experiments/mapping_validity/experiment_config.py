from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROTOTYPE_ROOT = ROOT.parents[1]
INPUT_ROOT = ROOT / 'fixed_inputs'
OUTPUT_ROOT = ROOT / 'results'
WORK_ROOT = PROTOTYPE_ROOT / 'runtime' / 'mapping_validity'
PYTHON_ENV = Path(__import__('sys').executable)
REPEATS = 5
