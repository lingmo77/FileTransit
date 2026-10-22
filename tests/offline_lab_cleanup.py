"""Remove only the three fixed raw-engine fixtures in the acceptance container."""
import os
from pathlib import Path
import shutil

assert os.environ.get("FT_ARIA2_BT_PORT") == "16882", "acceptance container only"
root = Path("/downloads/tasks").resolve()
for identifier in ("d" * 32, "e" * 32, "f" * 32):
    directory = root / identifier
    assert directory.resolve().parent == root and not directory.is_symlink()
    if directory.exists():
        shutil.rmtree(directory)
print("fixed raw-engine acceptance fixtures removed")
