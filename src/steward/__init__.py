# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""steward-ai: a self-hosted AI steward for the files on your server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("steward-ai")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"
