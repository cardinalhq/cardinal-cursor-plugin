import hashlib
import json
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def filehash(path):
    return digest(Path(path).read_bytes())


class Journal:
    """A durable reservation precedes every expensive operation; no implicit retries."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def call(self, key, payload, fn):
        slot = self.root / digest(key)
        slot.mkdir()  # exclusive across processes and restart attempts
        write(slot / 'request.json', {'key': key, 'payload': payload})
        try:
            result = fn()
        except Exception as exc:
            write(slot / 'error.json', {'type': type(exc).__name__, 'message': str(exc)})
            raise
        write(slot / 'result.json', result)
        return result
