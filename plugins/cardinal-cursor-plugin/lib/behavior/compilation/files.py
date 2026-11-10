"""Private atomic receipt files shared by authoring and execution transport."""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the same calls on msvcrt.locking
    from . import winfcntl as fcntl


def replace(source, destination, windows=None, sleep=time.sleep) -> None:
    """os.replace. On Windows it raises PermissionError while another
    process has the destination open (a concurrent writer or reader), so it
    is retried there for about two seconds."""
    if not (os.name == 'nt' if windows is None else windows):
        os.replace(source, destination)
        return
    for attempt in range(40):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 39:
                raise
            sleep(0.05)


def private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        # newline: the bytes on disk are the text's own, on Windows too (they are hashed and compared).
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def private_json(path: Path, value: dict) -> None:
    private_text(path, json.dumps(value, ensure_ascii=False, indent=2))


@contextmanager
def program_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
