"""The one door to a program dectl starts on this machine.

Every subprocess goes through `run`, so a timeout, error translation or test stub attaches
here once rather than at each call site. The caller resolves the binary with `shutil.which`
first, so a missing program is refused with a message naming it before anything starts.
"""

import subprocess


def run(argv: list[str], capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `argv` and return what it did, never raising on a non-zero status.

    `capture=False` hands the program this terminal, as an editor needs. A captured run
    returns stdout and stderr as text, so the caller can report the program's own message."""
    return subprocess.run(argv, capture_output=capture, text=True, check=False)
