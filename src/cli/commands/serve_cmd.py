"""``arbor serve`` — remote experiment control console."""

from __future__ import annotations

import time
import webbrowser
from pathlib import Path

import typer

from ...webui.auth import ConsoleAuth
from ...webui.console_server import ControlConsoleServer
from ...webui.manager import RunManager


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def serve_command(
    workspace_root: Path = typer.Option(
        Path("."),
        "--workspace-root",
        help="Root directory whose projects and Arbor sessions may be managed.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Use 0.0.0.0 for remote browser access.",
    ),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Console port."),
    session_ttl: int = typer.Option(
        8 * 60 * 60,
        "--session-ttl",
        min=60,
        help="Anonymous browser session lifetime in seconds (default: 8 hours).",
    ),
    secure_cookie: bool = typer.Option(
        False,
        "--secure-cookie/--no-secure-cookie",
        help="Mark browser-session cookies HTTPS-only (enable behind an HTTPS reverse proxy).",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the console locally after it starts.",
    ),
) -> None:
    """Run the no-login Web console for experiments and session history."""

    try:
        manager = RunManager(workspace_root)
        auth = ConsoleAuth(ttl_seconds=session_ttl, secure_cookie=secure_cookie)
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    server = ControlConsoleServer(manager, auth, host=host, port=port)
    if not server.start():
        typer.secho(
            f"error: could not bind the control console to {host}:{port}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    local_url = server.url
    typer.secho(f"\nArbor experiment console → {local_url}", fg=typer.colors.CYAN, bold=True)
    typer.secho("  access: no account login", fg=typer.colors.YELLOW)
    typer.secho(f"  workspace: {manager.workspace_root}", dim=True)
    if host not in _LOOPBACK_HOSTS:
        typer.secho(
            f"  remote URL: http://<server-ip>:{server.port} (allow this port in your firewall)",
            fg=typer.colors.YELLOW,
        )
        if not secure_cookie:
            typer.secho(
                "  security: anyone with the URL can control experiments. Do not expose "
                "plain HTTP; use an HTTPS reverse proxy with an access policy, then "
                "restart with --secure-cookie.",
                fg=typer.colors.YELLOW,
            )
    typer.secho("  Ctrl-C stops the console; running experiments continue independently.\n", dim=True)

    if open_browser and host in _LOOPBACK_HOSTS:
        try:
            webbrowser.open(local_url)
        except Exception:  # pragma: no cover - headless / no browser available
            pass

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        typer.echo("\nstopping experiment console…")
    finally:
        server.stop()
