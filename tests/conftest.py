"""Shared pytest configuration.

pytest-homeassistant-custom-component blocks socket creation to mirror Home Assistant CI.
That works on Linux, where asyncio's self-pipe is an (allowed) AF_UNIX socketpair, but on
Windows socket.socketpair() is AF_INET, so every event loop fails to construct. On Windows
dev machines, turn disable_socket into a no-op; socket_allow_hosts(["127.0.0.1"]) still
applies, so tests remain unable to reach the network. Linux CI keeps the full guard.
"""

import sys

import pytest_socket

if sys.platform == "win32":

    def _disable_socket_noop(allow_unix_socket: bool = False) -> None:
        return None

    pytest_socket.disable_socket = _disable_socket_noop
