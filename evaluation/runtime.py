"""Load a complete local strategy checkout, defaulting to frozen reference code."""
import importlib
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get("EVALUATION_STRATEGY_ROOT",
                          Path(__file__).parent / "reference")).resolve()
if not all((ROOT / "optiver_bot" / name).is_file() for name in ("models.py", "risk.py")):
    raise ValueError(f"Strategy root must contain optiver_bot/models.py and risk.py: {ROOT}")
sys.path.insert(0, str(ROOT))
from optiver_bot.models import DesiredOrder, Snapshot, StrategyConfig
from optiver_bot.risk import capacity
for name in ("optiver_bot.models", "optiver_bot.risk"):
    if not Path(sys.modules[name].__file__).resolve().is_relative_to(ROOT):
        raise RuntimeError("A different optiver_bot was already imported; start a fresh Python process")


def strategy(module):
    return importlib.import_module(module).decide
