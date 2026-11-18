"""Unit tests for client.py: framing, envelope handling and command mapping.

Run from the repository root:
    python -m unittest discover -s client/tests -v

These tests never open a socket: a FakeSocket records what the client would send
and canned server messages are pushed straight into the message handler, so the
whole protocol surface is exercised without timing or port dependencies.
"""
import io
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import client  # noqa: E402


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(bytes(data))

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def unframe(data):
    (length,) = struct.unpack("!I", data[:4])
    return json.loads(data[4:4 + length].decode("utf-8"))


def envelope(kind, payload=None, request_id=None, event_id=None, session="s-test"):
    return {"version": 1, "type": kind, "request_id": request_id, "event_id": event_id,
            "session_id": session, "payload": payload if payload is not None else {}}


def player(user_id, name, ready=False, is_host=False):
    return {"user_id": user_id, "name": name, "ready": ready, "is_host": is_host}


def output(client_instance):
    return client_instance._console._stream.getvalue()


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.client = client.QuizClient("127.0.0.1", 5000, "student1", "Alice",
                                        client.Console(self.stream))
        self.client._sock = FakeSocket()
        self.client._session_id = "s-test"
        self.client._login_ok = True

    def tearDown(self):
        self.stream.close()

    # helpers -------------------------------------------------------------
    def send(self, kind, payload=None):
        return self.client._send(kind, payload if payload is not None else {})

    def deliver(self, message):
        """Mirror the receiver thread: protocol violations close the connection."""
        try:
            self.client._handle_message(message)
        except client.ProtocolViolation as exc:
            self.client._protocol_failure(str(exc))

    def receive(self, kind, payload, request_id=None, event_id=None):
        self.deliver(envelope(kind, payload, request_id, event_id))

    def room_view(self):
        return {"room_code": "AB12CD", "quiz_id": "geography", "title": "Geography",
                "host_user_id": "student1", "player_count": 1, "capacity": 10,
                "state": "LOBBY",
                "players": [player("student1", "Alice", False, True)]}


class FramingTests(unittest.TestCase):
    def test_frame_round_trip(self):
        message = {"version": 1, "type": "X", "payload": {"text": "caf\u00e9"}}
        frame = client.encode_frame(message)
        assembler = client.FrameAssembler()
        self.assertEqual(assembler.feed(frame), [message])

    def test_byte_at_a_time(self):
        assembler = client.FrameAssembler()
        frame = client.encode_frame({"a": 1})
        seen = []
        for index in range(len(frame)):
            seen.extend(assembler.feed(frame[index:index + 1]))
        self.assertEqual(seen, [{"a": 1}])

    def test_coalesced_frames(self):
        assembler = client.FrameAssembler()
        blob = client.encode_frame({"n": 1}) + client.encode_frame({"n": 2}) + b"\x00\x00"
        self.assertEqual(assembler.feed(blob), [{"n": 1}, {"n": 2}])

    def test_split_across_reads(self):
        assembler = client.FrameAssembler()
        frame = client.encode_frame({"n": 3})
        self.assertEqual(assembler.feed(frame[:3]), [])
        self.assertEqual(assembler.feed(frame[3:-2]), [])
        self.assertEqual(assembler.feed(frame[-2:]), [{"n": 3}])

    def test_zero_length_is_rejected(self):
        assembler = client.FrameAssembler()
        with self.assertRaises(client.WireError):
            assembler.feed(b"\x00\x00\x00\x00")

    def test_oversized_length_is_rejected(self):
        assembler = client.FrameAssembler()
        with self.assertRaises(client.WireError):
            assembler.feed(struct.pack("!I", client.MAX_FRAME_BYTES + 1))

    def test_encode_rejects_oversized_body(self):
        with self.assertRaises(client.WireError):
            client.encode_frame({"blob": "x" * client.MAX_FRAME_BYTES})

    def test_decode_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            client.decode_message(b'{"a":1,"a":2}')

    def test_decode_rejects_nan(self):
        with self.assertRaises(ValueError):
            client.decode_message(b'{"a":NaN}')

    def test_decode_rejects_non_object(self):
        with self.assertRaises(ValueError):
            client.decode_message(b'[1,2,3]')

    def test_decode_rejects_invalid_utf8(self):
        with self.assertRaises(ValueError):
            client.decode_message(b'\xff\xfe')


class EnvelopeTests(ClientTestCase):
    def test_login_response_stores_session(self):
        request_id = self.send("LOGIN_REQUEST", {"user_id": "student1", "name": "Alice"})
        self.receive("LOGIN_RESPONSE", {"success": True, "session_id": "s-1"}, request_id)
        self.assertEqual(self.client._session_id, "s-1")
        self.assertTrue(self.client._login_done.is_set())
        self.assertIn("Logged in", output(self.client))

    def test_error_response_prints_code_and_hint(self):
        request_id = self.send("START_QUIZ_REQUEST", {})
        self.receive("ERROR_RESPONSE", {"code": "NOT_ENOUGH_PLAYERS", "message": "..."},
                     request_id)
        text = output(self.client)
        self.assertIn("NOT_ENOUGH_PLAYERS", text)
        self.assertIn("at least two players", text)

    def test_response_without_pending_request_is_ignored(self):
        self.receive("LOGIN_RESPONSE", {"success": True, "session_id": "s-1"}, "req-999")
        self.assertIn("unknown or repeated request", output(self.client))

    def test_unknown_event_closes_connection(self):
        self.receive("SOMETHING_ELSE_EVENT", {"room_code": "AB12CD"}, event_id="e-1")
        self.assertTrue(self.client._closed.is_set())
        self.assertIn("incompatible server message", output(self.client))

    def test_unknown_response_type_is_rejected(self):
        request_id = self.send("LIST_ROOMS_REQUEST", {})
        self.deliver(envelope("MYSTERY_RESPONSE", {}, request_id))
        self.assertTrue(self.client._closed.is_set())

    def test_unsupported_version_is_rejected(self):
        message = envelope("LOGIN_RESPONSE", {"session_id": "s"})
        message["version"] = 2
        self.deliver(message)
        self.assertIn("unsupported protocol version", output(self.client))

    def test_request_envelope_carries_session(self):
        self.send("LIST_QUIZZES_REQUEST", {})
        sent = unframe(self.client._sock.sent[-1])
        self.assertEqual(sent["session_id"], "s-test")
        self.assertIsNone(sent["event_id"])
        self.assertEqual(sent["version"], 1)
        self.assertTrue(sent["request_id"].startswith("req-"))

    def test_request_ids_are_unique(self):
        first = self.send("LIST_ROOMS_REQUEST", {})
        second = self.send("LIST_ROOMS_REQUEST", {})
        self.assertNotEqual(first, second)


class RoomTests(ClientTestCase):
    def test_create_room_stores_view(self):
        request_id = self.send("CREATE_ROOM_REQUEST", {"quiz_id": "geography"})
        self.receive("CREATE_ROOM_RESPONSE", {"success": True, "room": self.room_view()},
                     request_id)
        self.assertEqual(self.client._room["room_code"], "AB12CD")
        self.assertIn("Created room AB12CD", output(self.client))
        self.assertIn("Alice (student1)", output(self.client))

    def test_leave_room_clears_state(self):
        request_id = self.send("CREATE_ROOM_REQUEST", {"quiz_id": "geography"})
        self.receive("CREATE_ROOM_RESPONSE", {"success": True, "room": self.room_view()},
                     request_id)
        request_id = self.send("LEAVE_ROOM_REQUEST", {})
        self.receive("LEAVE_ROOM_RESPONSE", {"success": True, "room_code": "AB12CD"},
                     request_id)
        self.assertIsNone(self.client._room)
        self.assertIsNone(self.client._question)

    def test_player_status_updates_membership(self):
        request_id = self.send("CREATE_ROOM_REQUEST", {"quiz_id": "geography"})
        self.receive("CREATE_ROOM_RESPONSE", {"success": True, "room": self.room_view()},
                     request_id)
        self.receive("PLAYER_STATUS_EVENT",
                     {"room_code": "AB12CD", "host_user_id": "student2",
                      "players": [player("student1", "Alice", True, False),
                                  player("student2", "Bob", True, True)]},
                     event_id="e-1")
        room = self.client._room
        self.assertEqual(room["host_user_id"], "student2")
        self.assertEqual(room["player_count"], 2)
        self.assertIn("Bob (student2)", output(self.client))

    def test_lowercase_room_code_is_normalised(self):
        self.client._dispatch_command("/join", ["ab12cd"])
        sent = unframe(self.client._sock.sent[-1])
        self.assertEqual(sent["type"], "JOIN_ROOM_REQUEST")
        self.assertEqual(sent["payload"]["room_code"], "AB12CD")

    def test_malformed_room_code_is_not_sent(self):
        self.client._dispatch_command("/join", ["abc"])
        self.assertEqual(self.client._sock.sent, [])
        self.assertIn("six letters or digits", output(self.client))


class QuestionTests(ClientTestCase):
    def start_quiz(self):
        request_id = self.send("CREATE_ROOM_REQUEST", {"quiz_id": "geography"})
        self.receive("CREATE_ROOM_RESPONSE", {"success": True, "room": self.room_view()},
                     request_id)
        self.receive("QUIZ_STARTED_EVENT",
                     {"room_code": "AB12CD", "quiz_id": "geography", "title": "Geography",
                      "total_questions": 10}, event_id="e-1")

    def question_event(self, number=1, question_id="GK001"):
        return {"room_code": "AB12CD", "question_id": question_id, "number": number,
                "total_questions": 10, "text": "Which ocean is the largest?",
                "options": {"A": "Indian", "B": "Arctic", "C": "Atlantic", "D": "Pacific"},
                "time_limit": 60, "points": 100}

    def test_question_event_renders_options(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(), event_id="e-2")
        text = output(self.client)
        for letter in "ABCD":
            self.assertIn(f"{letter})", text)
        self.assertIn("Question 1/10", text)
        self.assertEqual(self.client._question["question_id"], "GK001")

    def test_answer_uses_open_question_id(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(), event_id="e-2")
        self.client._dispatch_command("/answer", ["b"])
        sent = unframe(self.client._sock.sent[-1])
        self.assertEqual(sent["type"], "ANSWER_REQUEST")
        self.assertEqual(sent["payload"], {"question_id": "GK001", "answer": "B"})

    def test_answer_without_question_is_local(self):
        self.client._dispatch_command("/answer", ["A"])
        self.assertEqual(self.client._sock.sent, [])
        self.assertIn("No question is open", output(self.client))

    def test_invalid_answer_letter_is_local(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(), event_id="e-2")
        before = len(self.client._sock.sent)
        self.client._dispatch_command("/answer", ["E"])
        self.assertEqual(len(self.client._sock.sent), before)
        self.assertIn("Answers are A, B, C or D", output(self.client))

    def test_result_clears_open_question(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(), event_id="e-2")
        self.receive("QUESTION_RESULT_EVENT",
                     {"room_code": "AB12CD", "question_id": "GK001", "correct_answer": "D",
                      "your_answer": "B", "points_awarded": 0,
                      "leaderboard": [{"rank": 1, "user_id": "student2", "name": "Bob",
                                       "score": 100, "active": True}]},
                     event_id="e-3")
        self.assertIsNone(self.client._question)
        text = output(self.client)
        self.assertIn("Correct answer: D", text)
        self.assertIn("0 points (wrong)", text)

    def test_missing_answer_is_reported(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(), event_id="e-2")
        self.receive("QUESTION_RESULT_EVENT",
                     {"room_code": "AB12CD", "question_id": "GK001", "correct_answer": "D",
                      "your_answer": None, "points_awarded": 0, "leaderboard": []},
                     event_id="e-3")
        self.assertIn("no answer", output(self.client))

    def test_finished_event_prints_winner(self):
        self.start_quiz()
        winner = {"rank": 1, "user_id": "student1", "name": "Alice", "score": 1000,
                  "active": True}
        self.receive("QUIZ_FINISHED_EVENT",
                     {"room_code": "AB12CD", "quiz_id": "geography",
                      "final_leaderboard": [winner], "winner": winner}, event_id="e-4")
        text = output(self.client)
        self.assertIn("Winner: Alice (student1) with 1000 points", text)
        self.assertIn("Final leaderboard", text)

    def test_prompt_shows_room_and_question(self):
        self.start_quiz()
        self.receive("QUESTION_EVENT", self.question_event(number=2), event_id="e-2")
        self.assertEqual(self.client._build_prompt(), "[AB12CD QUESTION_OPEN | Q2/10] > ")

    def test_leaderboard_marks_players_who_left(self):
        request_id = self.send("LEADERBOARD_REQUEST", {})
        self.receive("LEADERBOARD_RESPONSE",
                     {"room_code": "AB12CD",
                      "leaderboard": [{"rank": 1, "user_id": "student1", "name": "Alice",
                                       "score": 300, "active": False}]},
                     request_id)
        self.assertIn("[left]", output(self.client))


class ArgumentTests(unittest.TestCase):
    def test_valid_arguments(self):
        args = client.parse_args(["--server", "127.0.0.1", "--port", "5000",
                                  "--id", "student_1", "--name", "Alice"])
        self.assertEqual(args.port, 5000)
        self.assertEqual(args.user_id, "student_1")

    def test_bad_port(self):
        with self.assertRaises(SystemExit):
            client.parse_args(["--server", "h", "--port", "70000", "--id", "a", "--name", "A"])

    def test_bad_user_id(self):
        with self.assertRaises(SystemExit):
            client.parse_args(["--server", "h", "--port", "1", "--id", "bad id", "--name", "A"])

    def test_blank_name(self):
        with self.assertRaises(SystemExit):
            client.parse_args(["--server", "h", "--port", "1", "--id", "a", "--name", "   "])

    def test_name_too_long(self):
        with self.assertRaises(SystemExit):
            client.parse_args(["--server", "h", "--port", "1", "--id", "a", "--name", "x" * 33])


class TableTests(unittest.TestCase):
    def test_padding_uses_display_width(self):
        # A CJK character occupies two terminal columns, so padding with len()
        # would misalign the following column.
        self.assertEqual(client._display_width(client._pad("\u5f20\u4e09", 8)), 8)
        self.assertEqual(client._display_width(client._pad("Alice", 8)), 8)
        self.assertEqual(client._pad("\u5f20\u4e09", 5), "\u5f20\u4e09 ")

    def test_table_aligns_columns(self):
        rows = client._table(("user", "score"), [("\u5f20\u4e09", "100"), ("Bob", "20")])
        self.assertTrue(rows[2].startswith("\u5f20\u4e09  100"))
        self.assertTrue(rows[3].startswith("Bob   20"))

    def test_no_prompt_when_output_is_redirected(self):
        stream = io.StringIO()
        console = client.Console(stream)
        console.show_prompt("> ")
        self.assertEqual(stream.getvalue(), "")
        stream.close()


if __name__ == "__main__":
    unittest.main()
