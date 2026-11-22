import sys
from experiment_config import PROTOTYPE_ROOT

if not (PROTOTYPE_ROOT / 'fabric' / 'engine.py').is_file():
    raise RuntimeError('Existing prototype unavailable; this experiment cannot replace it.')
sys.path.insert(0, str(PROTOTYPE_ROOT))
