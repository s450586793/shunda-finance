import subprocess
import sys
from pathlib import Path


def test_close_untrusted_descriptors_closes_fds_above_lowered_soft_limit():
    helper = Path("scripts/system-update-updater-atomic.py").resolve()
    probe = r"""
import errno
import fcntl
import os
import resource
import runpy
import sys

namespace = runpy.run_path(sys.argv[1], run_name="updater_atomic_probe")
close_untrusted_descriptors = namespace["close_untrusted_descriptors"]
original_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
source_descriptor = os.open("/dev/null", os.O_RDONLY)
lock_descriptor = os.open("/dev/null", os.O_RDONLY)
high_descriptor = -1
try:
    high_descriptor = fcntl.fcntl(source_descriptor, fcntl.F_DUPFD, 256)
    os.set_inheritable(high_descriptor, True)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, original_limit[1]))

    close_untrusted_descriptors(lock_descriptor)

    try:
        os.fstat(high_descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    else:
        raise RuntimeError("high inherited descriptor remained open")
    os.fstat(lock_descriptor)
finally:
    resource.setrlimit(resource.RLIMIT_NOFILE, original_limit)
    for descriptor in (source_descriptor, lock_descriptor, high_descriptor):
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
"""

    result = subprocess.run(
        [sys.executable, "-c", probe, str(helper)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
