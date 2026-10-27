"""Offline test guard loaded before LiteLLM imports."""
import socket
import threading
import types
import sys

_state = types.SimpleNamespace(blocked_attempts=0, lock=threading.Lock())
_original_socket = socket.socket


def _deny(*_args, **_kwargs):
    with _state.lock:
        _state.blocked_attempts += 1
    raise OSError("offline test guard denied socket/DNS operation")


class _OfflineSocket(_original_socket):
    def connect(self, *args, **kwargs):
        return _deny(*args, **kwargs)

    def connect_ex(self, *args, **kwargs):
        return _deny(*args, **kwargs)

    def sendto(self, *args, **kwargs):
        return _deny(*args, **kwargs)


socket.socket = _OfflineSocket
socket.create_connection = _deny
socket.getaddrinfo = _deny
sys.modules["failure_aware_offline_state"] = _state
