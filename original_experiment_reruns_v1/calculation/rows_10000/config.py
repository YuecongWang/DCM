from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / 'data'
RESULT_ROOT = ROOT / 'results'
RUNTIME_ROOT = ROOT / 'runtime'
PROJECT_ROOT = ROOT
DOCUMENT_ROOT = ROOT / 'external_inputs'
REPEATS = 7
ROWS = 10000
SHARED_QUERIES = 12

DELAY_GRID_MS = [(0, 0)]
IMPORT_PROJECT_EXCEL = False
EXCEL_ROW_LIMIT_PER_FILE = 100
API_HOST = '127.0.0.1'
API_PORT = 8765
TEST_WORK_ROOT = ROOT / 'runtime' / 'fabric_tests'

