"""Durable replacement: an interrupted write cannot truncate the previous snapshot."""
import os
import tempfile
import time
from pathlib import Path


def write_text(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers and file scanners can temporarily deny rename/delete
        # sharing. Keep the completed temp file and retry only that replacement.
        for delay in (0.05, 0.1, 0.2, 0.4, None):
            try:
                os.replace(temp, path)
                break
            except OSError as exc:
                if delay is None or getattr(exc, 'winerror', None) not in (5, 32, 33):
                    raise
                time.sleep(delay)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
