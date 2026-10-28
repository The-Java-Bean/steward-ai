# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Helpers for the web API tests."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from steward.core import setup
from steward.core.runtime import Runtime

USERNAME = "owner"
PASSWORD = "correct horse battery"
SETUP_TOKEN = "test-setup-token-0123456789"


def form_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token field on the page"
    return match.group(1)


def finish_setup(runtime: Runtime) -> None:
    with runtime.dbs.state.write() as conn:
        setup.advance(conn, "done", actor="test")


def make_owner(runtime: Runtime, *, finished: bool = True) -> None:
    runtime.auth.create_owner(USERNAME, PASSWORD)
    if finished:
        finish_setup(runtime)


def log_in(client: TestClient, password: str = PASSWORD) -> str:
    """Log in through the form; returns the session's CSRF token."""
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": USERNAME, "password": password, "csrf_token": form_token(page.text)},
    )
    assert response.status_code == 303, response.text
    return csrf_of(client)


def csrf_of(client: TestClient) -> str:
    return form_token(client.get("/ui/settings").text)
