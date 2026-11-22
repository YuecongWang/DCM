from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENT_ROOT
RESULT_ROOT = EXPERIMENT_ROOT / 'results'
ROWS = 60
REPEATS = 15
SHARED_QUERIES = 12

DELAY_GRID_MS = [(0, 0), (0.2, 0), (0, 0.2)]
