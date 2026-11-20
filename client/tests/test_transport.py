"""Real socket checks for bounded sends and idle receive behavior."""
import io
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import client


class TransportTests(unittest.TestCase):
    def test_nonreading_peer_cannot_block_sender_forever(self):
        writer, reader = socket.socketpair()
        writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        writer.settimeout(0.1)
        stream = io.StringIO()
        app = client.QuizClient('unused', 1, 'test', 'Test', client.Console(stream))
        app._sock = writer
        started = time.monotonic()
        try:
            for _ in range(512):
                if app._send('TEST_REQUEST', {'data': 'x' * 60000}) is None:
                    break
            self.assertTrue(app._closed.is_set())
            self.assertLess(time.monotonic() - started, 5)
        finally:
            app.abort()
            reader.close()

    def test_idle_read_timeout_keeps_connection(self):
        local, remote = socket.socketpair()
        local.settimeout(0.03)
        app = client.QuizClient('unused', 1, 'test', 'Test', client.Console(io.StringIO()))
        app._sock = local
        thread = threading.Thread(target=app._read_loop)
        thread.start()
        try:
            time.sleep(0.12)
            self.assertFalse(app._closed.is_set())
            self.assertTrue(thread.is_alive())
        finally:
            app.abort()
            remote.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
