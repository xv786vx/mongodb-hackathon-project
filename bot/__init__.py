"""Discord bot. Until `pip install -e agents/suspect` is the norm, make mystery_core importable from the repo."""
import sys
from pathlib import Path

_core = Path(__file__).resolve().parent.parent / "agents" / "suspect" / "src"
if _core.is_dir() and str(_core) not in sys.path:
    sys.path.insert(0, str(_core))
