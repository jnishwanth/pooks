"""Deployment unit contract assertions.

deploy/ holds the systemd units shipped with pooks. A socket-activated service
must not declare WantedBy=multi-user.target, which would defeat on-demand startup
and cause systemd to launch it eagerly on boot.
"""

from __future__ import annotations

import configparser
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"


def _read_unit(filename: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read(DEPLOY_DIR / filename)
    return cp


def test_pooks_daemon_unit_is_wanted_by_multi_user_target() -> None:
    cp = _read_unit("pooks.service")
    assert cp.get("Install", "WantedBy") == "multi-user.target"


def test_pooks_web_socket_is_wanted_by_sockets_target() -> None:
    cp = _read_unit("pooks-web.socket")
    assert cp.get("Install", "WantedBy") == "sockets.target"


def test_pooks_web_service_is_socket_activated_not_multi_user() -> None:
    cp = _read_unit("pooks-web.service")
    # pooks-web is triggered strictly on-demand by pooks-web.socket.
    # Having WantedBy=multi-user.target starts the process on boot/rebuild,
    # consuming resident memory before any HTTP request arrives.
    assert not cp.has_section("Install") or "multi-user.target" not in cp.get(
        "Install", "WantedBy", fallback=""
    )
    assert "pooks-web.socket" in cp.get("Unit", "Requires", fallback="")
