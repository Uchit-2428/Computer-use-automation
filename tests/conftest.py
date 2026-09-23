import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CORELINK_OPERATOR_ID", "teller01")
os.environ.setdefault("CORELINK_OPERATOR_PASSWORD", "demo-pass-123")


def _up(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def mock_app():
    proc = None
    if not _up(8600):
        proc = subprocess.Popen([sys.executable, "-m", "mockapp.server"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if _up(8600):
                break
            time.sleep(0.1)
    yield "http://127.0.0.1:8600"
    if proc:
        proc.terminate()
