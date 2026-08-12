import atexit
import os
import shutil
import tempfile
from pathlib import Path


RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="music_partner_tests_"))
os.environ["MUSIC_PARTNER_LOG_FILE"] = str(RUNTIME_DIR / "test.log")
os.environ["MUSIC_PARTNER_STATE_FILE"] = str(RUNTIME_DIR / "state.json")


@atexit.register
def _cleanup_runtime_dir():
    shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
