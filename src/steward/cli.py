# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `steward` command line."""

from __future__ import annotations

import typer

import steward
from steward.core.loader import ModuleLoadError, discover, load_modules

app = typer.Typer(
    name="steward",
    help="steward-ai: a self-hosted AI steward for the files on your server.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the steward-ai version."""
    typer.echo(steward.__version__)


@app.command()
def modules() -> None:
    """List installed modules and what each one registers.

    Until module configuration exists, every installed module is loaded with its
    default config.
    """
    try:
        loaded = load_modules(dict.fromkeys(discover()))
    except ModuleLoadError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not loaded.modules:
        typer.echo("No modules installed.")
        return
    for m in loaded.modules:
        mod = m.module
        requires = ", ".join(mod.requires) or "-"
        typer.echo(f"{mod.name} {mod.version} ({mod.kind}; requires: {requires})")
        typer.echo(f"  provided by: {m.provider or 'unknown'}")
        for reg in loaded.registrations.by_module(mod.name):
            typer.echo(f"  {reg.point}: {reg.name}")
