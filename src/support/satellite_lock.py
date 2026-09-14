from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


class SatelliteOutputBusyError(OSError):
    pass


# Refuses overlapping writers and releases the lock when this operation finishes.
@contextmanager
def satellite_output_lock(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / ".satellite.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise SatelliteOutputBusyError(
            f"Satellite output is already in use: {directory}. "
            "Another updated process holds the lock; do not delete lock files or start another run."
        ) from exc
    try:
        yield
    finally:
        lock.release()
