"""Windows/Unix compatibility layer for fcntl.flock using msvcrt on Windows."""
import os

try:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_SH = fcntl.LOCK_SH
    LOCK_NB = fcntl.LOCK_NB
    LOCK_UN = fcntl.LOCK_UN
    flock = fcntl.flock
except ImportError:
    import errno
    import msvcrt

    LOCK_EX = 1
    LOCK_SH = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def flock(file, op):
        fd = file.fileno()
        current_pos = file.tell()
        file.seek(0)
        try:
            if op & LOCK_UN:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError as e:
                    # Windows LK_UNLCK raises EACCES/PermissionError if the range is not locked
                    # by the current process. Unix flock(LOCK_UN) is a silent no-op.
                    if e.errno not in (errno.EACCES, errno.EDEADLK):
                        raise
            elif op & LOCK_NB:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLK):
                raise BlockingIOError(str(e)) from e
            raise
        finally:
            try:
                file.seek(current_pos)
            except OSError:
                pass
