import socket

# Imported eagerly so its module classes (e.g. SSLSocket, which subclasses
# socket.socket at import time) are built before the sandbox patch below
# replaces socket.socket; lazy first import inside a test would fail on the
# patched name instead of on a forbidden network call.
import ssl  # noqa: F401

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Sandbox broad enough for the whole fixture suite: any socket creation,
    connection, name resolution, or server bind fails loudly."""

    def blocked(*args, **kwargs):
        raise AssertionError("network forbidden in fixture tests")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_server", blocked)
    monkeypatch.setattr(socket, "socketpair", blocked)
