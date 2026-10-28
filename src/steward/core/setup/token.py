# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The one-time setup token.

While no owner account exists, `steward api` makes a random token and prints it in its
log. Only someone who can read the log (the person running the server) can open the
setup wizard and claim the instance. The token lives only in memory: a restart prints a
new one, and it stops working once the owner exists.
"""

from __future__ import annotations

import hmac
import secrets

TOKEN_BYTES = 24


class SetupToken:
    def __init__(self, value: str | None = None) -> None:
        self.value = value or secrets.token_urlsafe(TOKEN_BYTES)

    def check(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(self.value.encode(), presented.strip().encode())
