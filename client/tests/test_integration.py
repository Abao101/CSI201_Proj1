"""Pair test: the real client process against the real server process.

Run from the repository root:
    python -m unittest discover -s client/tests -v

The test starts server.py on a free loopback port, drives two client.py
processes through a complete two-player quiz (including a wrong answer and both
error paths that need no setup) and then checks the exit status.  It is the
client half of the acceptance list in PROTOCOL.md section 10; the server's own
suite lives in server/tests.
"""
import json
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = str(ROOT / "client.py")
SERVER = str(ROOT / "server.py")
QUESTIONS = str(ROOT / "questions_50.json")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Output:
    """Collects one process's stdout and waits for regular expressions in it."""

    def __init__(self, name, process, timeout=30):
        self.name = name
        self.process = process
        self.timeout = timeout
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.buffer = ""
        self.eof = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.process.stdout:
                with self.cond:
                    self.buffer += line
                    self.cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.eof = True
                self.cond.notify_all()

    def mark(self):
        with self.lock:
            return len(self.buffer)

    def wait(self, pattern, start=0, timeout=None):
        regex = re.compile(pattern, re.S)
        deadline = time.time() + (timeout or self.timeout)
        with self.cond:
            while True:
                found = regex.search(self.buffer, start)
                if found:
                    return found
                if self.eof:
                    raise AssertionError(
                        f"{self.name}: stream closed while waiting for {pattern!r}\n"
                        f"--- output ---\n{self.buffer}")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise AssertionError(
                        f"{self.name}: timeout waiting for {pattern!r}\n"
                        f"--- output ---\n{self.buffer}")
                self.cond.wait(min(remaining, 0.2))

    def text(self):
        with self.lock:
            return self.buffer


class ClientProcess:
    def __init__(self, display_name, user_id, port):
        self.process = subprocess.Popen(
            [sys.executable, CLIENT, "--server", "127.0.0.1", "--port", str(port),
             "--id", user_id, "--name", display_name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT))
        self.output = Output(user_id, self.process)

    def send(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)
        try:
            self.process.stdout.close()
        except (OSError, ValueError):
            pass


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = subprocess.Popen(
            [sys.executable, SERVER, "--host", "127.0.0.1", "--port", str(cls.port),
             "--questions", QUESTIONS],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT))
        cls.server_output = Output("server", cls.server)
        cls.server_output.wait(r"SERVER_READY", timeout=20)

    @classmethod
    def tearDownClass(cls):
        if cls.server.poll() is None:
            cls.server.kill()
            cls.server.wait(10)
        try:
            cls.server.stdout.close()
        except (OSError, ValueError):
            pass

    def test_duplicate_user_is_rejected(self):
        first = ClientProcess("Alice", "student1", self.port)
        second = None
        try:
            first.output.wait(r"Logged in\.")
            # Started only now, otherwise the two logins race and either may win.
            second = ClientProcess("Alice again", "student1", self.port)
            second.output.wait(r"\[error\] DUPLICATE_USER")
            self.assertEqual(second.process.wait(20), 1)
            self.assertIn("already connected", second.output.text())
            first.send("/quit")
            self.assertEqual(first.process.wait(20), 0)
        finally:
            first.stop()
            if second is not None:
                second.stop()

    def test_two_player_match(self):
        bank = json.loads(Path(QUESTIONS).read_text(encoding="utf-8"))
        answers = {question["question_id"]: question["answer"]
                   for quiz in bank["quizzes"] for question in quiz["questions"]}

        alice = ClientProcess("Alice", "student1", self.port)
        bob = None
        try:
            alice.output.wait(r"Logged in\.")

            position = alice.output.mark()
            alice.send("/quizzes")
            alice.output.wait(r"geography\s+Geography", start=position)

            position = alice.output.mark()
            alice.send("/create geography")
            room = alice.output.wait(r"Created room ([A-Z0-9]{6})", start=position).group(1)

            bob = ClientProcess("Bob", "student2", self.port)
            bob.output.wait(r"Logged in\.")

            position = bob.output.mark()
            bob.send("/rooms")
            bob.output.wait(rf"Open rooms:[\s\S]*{room}", start=position)

            position = bob.output.mark()
            bob.send("/join " + room.lower())          # the client must upper-case
            bob.output.wait(rf"Joined room {room}\.", start=position)

            position = alice.output.mark()
            alice.send("/start")                        # nobody is ready yet
            alice.output.wait(r"\[error\] PLAYERS_NOT_READY", start=position)

            for client in (alice, bob):
                position = client.output.mark()
                client.send("/ready")
                client.output.wait(r"You are ready\.", start=position)

            started_a = alice.output.mark()
            started_b = bob.output.mark()
            alice.send("/start")
            alice.output.wait(rf"Quiz \"Geography\" started: 10 questions", start=started_a)
            bob.output.wait(rf"Quiz \"Geography\" started: 10 questions", start=started_b)

            for number in range(1, 11):
                question_a = alice.output.wait(r"Question %d/10\s+(\S+)" % number,
                                               start=started_a, timeout=60)
                question_b = bob.output.wait(r"Question %d/10\s+(\S+)" % number,
                                             start=started_b, timeout=60)
                question_id = question_a.group(1)
                self.assertEqual(question_id, question_b.group(1))
                # The event that publishes a question must not carry its answer.
                self.assertNotIn("Correct answer", alice.output.text()[question_a.start():])

                correct = answers[question_id]
                wrong = "A" if correct != "A" else "B"
                started_a = alice.output.mark()
                started_b = bob.output.mark()
                alice.send("/answer " + correct.lower())
                bob.send("/answer " + wrong)
                alice.output.wait(rf"Answer for {question_id} recorded", start=started_a)
                bob.output.wait(rf"Answer for {question_id} recorded", start=started_b)
                alice.output.wait(rf"Correct answer: {correct}", start=started_a, timeout=30)
                bob.output.wait(rf"Correct answer: {correct}", start=started_b, timeout=30)
                alice.output.wait(rf"You answered {correct}: 100 points \(correct\)",
                                  start=started_a, timeout=30)
                bob.output.wait(rf"You answered {wrong}: 0 points \(wrong\)",
                                start=started_b, timeout=30)

            alice.output.wait(r"Winner: Alice \(student1\) with 1000 points", start=started_a)
            alice.output.wait(r"Final leaderboard", start=started_a)
            bob.output.wait(r"Bob \(student2\)", start=started_b)

            position = alice.output.mark()
            alice.send("/score")
            alice.output.wait(r"Alice \(student1\)\s+1000", start=position)

            position = alice.output.mark()
            alice.send("/answer B")
            alice.output.wait(r"No question is open right now\.", start=position)

            position = alice.output.mark()
            alice.send("/start")
            alice.output.wait(r"\[error\] INVALID_STATE", start=position)

            for client in (alice, bob):
                position = client.output.mark()
                client.send("/leave")
                client.output.wait(rf"You left room {room}\.", start=position)

            alice.send("/quit")
            bob.send("/quit")
            self.assertEqual(alice.process.wait(20), 0)
            self.assertEqual(bob.process.wait(20), 0)
            self.assertIn("Logged out", alice.output.text())
        finally:
            alice.stop()
            if bob is not None:
                bob.stop()


if __name__ == "__main__":
    unittest.main()
