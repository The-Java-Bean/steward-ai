# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Fail if a Python source file doesn't start with the SPDX license header."""

from __future__ import annotations

import subprocess
import sys

HEADER = "# SPDX-License-Identifier: Apache-2.0\n"


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    missing = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            if f.readline() != HEADER:
                missing.append(path)
    for path in missing:
        print(f"{path}: first line must be {HEADER.strip()!r}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
