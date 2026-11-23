"""Atomic JSON writes and process-scoped file locks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import time
import uuid
from repo_agent.backend.schemas.task_contract import load_json

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def exclusive(path):
    """Nonblocking OS lock, automatically released on process exit."""
    with path.open("a+b") as stream:
        # Windows permits locking beyond EOF. No initialization write: that
        # would race the first holder on a newly created lock file.
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    write_bytes(path, data)


def write_bytes(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers can briefly prevent replacing an open destination.
        # Retry only replacement, preserving the previous complete JSON meanwhile.
        for attempt in range(8):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_json(path):
    # Windows replacement can briefly deny opening the destination as well.
    for attempt in range(8):
        try:
            with path.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.01 * 2 ** attempt)
    if len(data) > 1024 * 1024:
        raise ValueError("Oversized preparation record")
    return load_json(data)


