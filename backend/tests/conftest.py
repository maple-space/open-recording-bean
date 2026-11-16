"""Test artifacts are retained; cleanup is an explicit move to macOS Trash."""
import uuid
from pathlib import Path

import pytest

RUNS = Path(__file__).resolve().parents[2] / '.local' / 'test-runs'


@pytest.fixture
def tmp_path():
    path = RUNS / uuid.uuid4().hex
    path.mkdir(parents=True, mode=0o700)
    return path


@pytest.fixture(autouse=True)
def no_models_of_this_computer(monkeypatch):
    """Tests see only the speech models they set up themselves, not the ones ./start.sh downloaded here."""
    monkeypatch.setenv('RECORDING_ASR_DIR', str(RUNS / 'no-models'))
    for name in ('RECORDING_SENSEVOICE_DIR', 'RECORDING_SPEAKER_DIR', 'RECORDING_FUNASR_NANO_DIR'):
        monkeypatch.delenv(name, raising=False)
