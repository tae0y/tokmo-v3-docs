"""Small sorted nonblocking flock set for governed provider operations."""

import fcntl
import os
from pathlib import Path


class OperationLeaseV1:
    def __init__(self, descriptors, operations):
        self._descriptors = descriptors
        self.operations = operations

    def close(self):
        while self._descriptors:
            descriptor = self._descriptors.pop()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def __enter__(self): return self
    def __exit__(self, *unused): self.close()


def acquire_operation_lease(root: Path, operations) -> OperationLeaseV1 | None:
    operations = tuple(sorted(set(operations)))
    if not operations or any(not isinstance(value, str) or not value for value in operations):
        raise ValueError("operations")
    directory = Path(root) / "ops" / "operation-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptors = []
    try:
        for operation in operations:
            if not operation.replace("_", "").isalnum():
                raise ValueError("operation")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
            descriptor = os.open(directory / f"{operation}.lock", flags, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise
            descriptors.append(descriptor)
    except BlockingIOError:
        OperationLeaseV1(descriptors, operations).close()
        return None
    except BaseException:
        OperationLeaseV1(descriptors, operations).close()
        raise
    return OperationLeaseV1(descriptors, operations)
