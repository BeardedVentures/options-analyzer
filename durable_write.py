#!/usr/bin/env python3
"""durable_write.py - one implementation of "write this file without losing it".

Every persistent record in VEGA is maintained by the same three-step move: read the whole file,
change something, write the whole file back. That is fine for one writer and unsafe for two, and
VEGA runs several processes per cycle -- the engine scan, vega_candidates, the paper desk mark
loop -- plus whatever the operator has open.

What that cost, concretely:

  * data/data_quality_log.json was destroyed five times between 2026-08-13 and 2026-08-25. The
    last wipe took 886 KB of per-ticker chain readings down to 10 KB. The file is gitignored and
    unbacked, so none of it came back.
  * logs/scan_log.json corrupted on 2026-07-08; the wreckage is still on disk as
    scan_log.json.corrupt.bak.

Two distinct failures, and fixing either alone is not enough:

  1. A SHARED temp path. Two writers open the same ".tmp" with "w"; the shorter truncates and
     writes while the longer still holds a handle at a high offset, and its flush lands past the
     end. The result parses as a complete JSON document with a longer one's tail attached --
     "Extra data: line N column 2".
  2. os.replace REFUSES to run on Windows while any other process holds the destination open,
     even for reading. Per-process temp paths fix the corruption but not this; a five-writer
     stress run on 2026-08-25 still lost about a fifth of its records to "[WinError 5] Access is
     denied" after five retries each.

Retrying cannot fix read-modify-write, because the read is already stale by the time the write
is refused. Only one writer at a time can. So: take a lock, write through a per-process temp
path, and retry the replace. The stress test that lost records now lands 200/200.

Use `exclusive(path)` around any read-modify-write, and `atomic_write_text` for the write itself.
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# A crashed holder must never wedge a subsystem forever. Longer than any single write takes,
# far shorter than a cycle.
LOCK_STALE_SECONDS = 30
DEFAULT_TIMEOUT = 10.0
REPLACE_ATTEMPTS = 5

PathLike = Union[str, "os.PathLike[str]", Path]


@contextlib.contextmanager
def exclusive(path: PathLike, timeout: float = DEFAULT_TIMEOUT):
    """Serialise a read-modify-write on `path` across processes.

    O_CREAT|O_EXCL is the portable mutex: the create either wins or raises, with no window
    between the check and the claim. A lock older than LOCK_STALE_SECONDS is broken rather than
    waited on, so a killed scan cannot silence instrumentation for every run that follows.
    """
    lock = "%s.lock" % path
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > LOCK_STALE_SECONDS:
                    logger.warning("[durable_write] breaking a stale lock on %s", path)
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError("%s locked for over %.0fs" % (path, timeout))
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:                                  # pragma: no cover - defensive
            pass
        try:
            os.unlink(lock)
        except OSError:                                  # pragma: no cover - defensive
            pass


def replace_with_retry(src: PathLike, dst: PathLike, attempts: int = REPLACE_ATTEMPTS) -> None:
    """os.replace, retried. See the module docstring for why a bare os.replace is not enough."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Replace `path` with `text`, or leave it exactly as it was.

    The temp path carries the PID even though callers should also hold `exclusive`. Belt and
    braces on purpose: a lock only protects the writers that take it, and anything that ever
    writes here without one still cannot clobber another process's temp file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".%d.tmp" % os.getpid())
    try:
        tmp.write_text(text, encoding=encoding)
        replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:                                  # pragma: no cover - defensive
            pass
