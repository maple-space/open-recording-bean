"""Retire this task's disposable artifacts without touching existing leftovers."""
import logging
from pathlib import Path

from .storage import move_to_trash

logger = logging.getLogger(__name__)


def _discard(path, *, empty_directory=False, parent=None):
    try:
        path = Path(path)
        if path.is_symlink() or (parent is not None and path.resolve().parent != Path(parent).resolve()):
            return
        if empty_directory:
            # A UUID directory has one task owner, whose disk work has finished.
            # Never recursively sweep it or any older task's artifacts.
            if not path.is_dir() or next(path.iterdir(), None) is not None:
                return
        elif not path.is_file():
            return
        move_to_trash(path)
    except Exception:
        # Cleanup must not turn a valid archive into a failed job, or mask the
        # original cancellation/integrity error. Leave it recoverable in place.
        logger.warning('无法将任务临时文件移入废纸篓，已保留：%s', path, exc_info=True)


def discard_file(path, *, parent=None):
    _discard(path, parent=parent)


def discard_empty_directory(path):
    _discard(path, empty_directory=True)
