"""`fcntl.flock` for Windows, on `msvcrt.locking`.

Imported as `fcntl` where the real module does not exist, so the lock code
keeps the calls it makes on POSIX. The lock is one byte at offset 0 of the
lock file. A Windows region lock is always exclusive, so LOCK_SH behaves as
LOCK_EX; like flock, it conflicts between two opens of the file even in one
process, and the system drops it when the holder exits.

Only what cardinal_core uses: a non-blocking lock (each caller runs its own
bounded wait) and unlock. A held byte (EACCES from msvcrt.locking) is raised
as BlockingIOError, the class flock raises and callers catch; any other
error reaches the caller unchanged.
"""

from __future__ import annotations

import errno
import os

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8


def held(error: OSError) -> bool:
    """Whether a failed msvcrt.locking means the byte is held: EACCES from
    the C runtime, EDEADLOCK when it gives up. Wine's runtime reports a held
    byte with errno 0 (seen in the Wine CI job); that counts only under
    Wine, where ntdll exports wine_get_version."""
    if error.errno in (errno.EACCES, errno.EDEADLK):
        return True
    if error.errno != 0:
        return False
    try:
        import ctypes
        return hasattr(ctypes.windll.ntdll, "wine_get_version")
    except (AttributeError, OSError):
        return False


def flock(fd: int, operation: int) -> None:
    import msvcrt  # Windows only; imported here so this module imports anywhere

    if not operation & (LOCK_UN | LOCK_NB):
        raise NotImplementedError("a blocking lock: pass LOCK_NB and wait in the caller")
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK if operation & LOCK_UN else msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if operation & LOCK_UN or not held(error):
            raise
        raise BlockingIOError(errno.EWOULDBLOCK, "the lock is held") from error  # what flock raises
