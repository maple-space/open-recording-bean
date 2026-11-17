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


@pytest.fixture(autouse=True)
def retain_retired_task_artifacts(monkeypatch, tmp_path):
    """Exercise task cleanup without filling the user's real macOS Trash."""
    from backend import task_artifacts

    def move(path):
        retained = tmp_path / 'retired-task-artifacts'
        retained.mkdir(exist_ok=True)
        Path(path).rename(retained / f'{uuid.uuid4().hex}-{Path(path).name}')

    monkeypatch.setattr(task_artifacts, 'move_to_trash', move)
