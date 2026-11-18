#!/usr/bin/env python3
"""CSI201 Project 1 quiz client.

The client is a thin terminal for the server described in PROTOCOL.md: it owns
no game rules.  It authenticates, turns slash-commands into requests, prints the
events the server pushes, and keeps only the local state needed to build the next
request (session id, current room, the currently open question id).

Usage:
    python3 client.py --server 127.0.0.1 --port 5000 --id student1 --name Alice

The wire format is the one from PROTOCOL.md section 2: a four byte unsigned
big-endian length followed by that many bytes of UTF-8 encoded JSON object.
Framing is implemented here rather than imported so that this file can be copied
and run on its own inside the course Docker image.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import socket
import struct
import sys
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 65536
CONNECT_TIMEOUT_SECONDS = 10.0
LOGIN_TIMEOUT_SECONDS = 35.0
LOGOUT_TIMEOUT_SECONDS = 5.0

USER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
ROOM_CODE_PATTERN = re.compile(r"[A-Z0-9]{6}\Z")
ANSWER_CHOICES = ("A", "B", "C", "D")

# The protocol says client logic may depend only on the error code, never on the
# server's English message.  These hints are extra coaching, not control flow.
ERROR_HINTS = {
    "DUPLICATE_USER": "That user id is already connected; start again with another --id.",
    "QUIZ_NOT_FOUND": "Unknown quiz id; run /quizzes to see the catalogue.",
    "ROOM_NOT_FOUND": "Unknown room code; run /rooms to see open rooms.",
    "ROOM_FULL": "That room already holds ten players.",
    "ALREADY_IN_ROOM": "Leave your current room with /leave first.",
    "NOT_IN_ROOM": "Create or join a room first.",
    "INVALID_STATE": "The room is not in a state that allows this command.",
    "NOT_ROOM_HOST": "Only the host can start the quiz.",
    "NOT_ENOUGH_PLAYERS": "A quiz needs at least two players.",
    "PLAYERS_NOT_READY": "Every player in the room must be ready.",
    "QUESTION_CLOSED": "That question is already closed.",
    "DUPLICATE_ANSWER": "You have already answered this question.",
    "INVALID_ANSWER": "Answer the current question with A, B, C or D.",
    "DUPLICATE_REQUEST": "Request id reused; the server ignored the duplicate.",
    "NOT_AUTHENTICATED": "Log in again.",
    "INVALID_SESSION": "The session no longer matches; reconnect.",
    "UNSUPPORTED_VERSION": "The server speaks a different protocol version.",
    "SERVER_ERROR": "The server hit an internal error; the connection will close.",
}


class ProtocolViolation(Exception):
    """The peer sent a syntactically valid frame the client cannot interpret."""


class WireError(Exception):
    """A frame could not be encoded or decoded."""


# --------------------------------------------------------------------------- #
# Wire format: 4 byte big endian length + UTF-8 JSON object
# --------------------------------------------------------------------------- #

def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def encode_frame(message: Dict[str, Any]) -> bytes:
    """Serialise one message; raises WireError when it cannot be framed."""
    try:
        body = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WireError(f"cannot encode message: {exc}") from exc
    if not 1 <= len(body) <= MAX_FRAME_BYTES:
        raise WireError(f"frame length {len(body)} is outside 1..{MAX_FRAME_BYTES}")
    return struct.pack("!I", len(body)) + body


def decode_message(body: bytes) -> Dict[str, Any]:
    """Decode one payload; rejects duplicate keys, NaN and non-object JSON."""
    text = body.decode("utf-8")
    value = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                       parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("top level JSON value is not an object")
    return value


class FrameAssembler:
    """Reassembles messages from an arbitrary sequence of recv results.

    TCP splits and coalesces writes, so a frame may arrive one byte at a time or
    several frames may arrive in a single read; both cases are handled by keeping
    the leftover bytes in the buffer.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        self._buffer.extend(data)
        frames: List[Dict[str, Any]] = []
        while True:
            if len(self._buffer) < 4:
                break
            (length,) = struct.unpack("!I", bytes(self._buffer[:4]))
            if not 1 <= length <= MAX_FRAME_BYTES:
                raise WireError(f"invalid frame length {length}")
            if len(self._buffer) < 4 + length:
                break
            body = bytes(self._buffer[4:4 + length])
            del self._buffer[:4 + length]
            frames.append(decode_message(body))
        return frames


# --------------------------------------------------------------------------- #
# Field extraction helpers; every server value is treated as untrusted
# --------------------------------------------------------------------------- #

def _field(payload: Dict[str, Any], key: str, expected: type) -> Any:
    if key not in payload:
        raise ProtocolViolation(f"payload is missing {key!r}")
    value = payload[key]
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolViolation(f"payload.{key} must be an integer")
    elif not isinstance(value, expected):
        raise ProtocolViolation(f"payload.{key} must be a {expected.__name__}")
    return value


def _as_player(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("player entry is not an object")
    return {
        "user_id": _field(value, "user_id", str),
        "name": _field(value, "name", str),
        "ready": _field(value, "ready", bool),
        "is_host": _field(value, "is_host", bool),
    }


def _as_ranking(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("leaderboard entry is not an object")
    return {
        "rank": _field(value, "rank", int),
        "user_id": _field(value, "user_id", str),
        "name": _field(value, "name", str),
        "score": _field(value, "score", int),
        "active": _field(value, "active", bool),
    }


def _as_quiz_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("quiz summary is not an object")
    return {
        "quiz_id": _field(value, "quiz_id", str),
        "title": _field(value, "title", str),
        "description": _field(value, "description", str),
        "question_count": _field(value, "question_count", int),
    }


def _as_room_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("room summary is not an object")
    return {
        "room_code": _field(value, "room_code", str),
        "quiz_id": _field(value, "quiz_id", str),
        "title": _field(value, "title", str),
        "host_user_id": _field(value, "host_user_id", str),
        "player_count": _field(value, "player_count", int),
        "capacity": _field(value, "capacity", int),
        "state": _field(value, "state", str),
    }


def _as_room_view(value: Any) -> Dict[str, Any]:
    room = _as_room_summary(value)
    raw_players = value.get("players")
    if not isinstance(raw_players, list):
        raise ProtocolViolation("room.players must be a list")
    room["players"] = [_as_player(player) for player in raw_players]
    return room


def _as_list(payload: Dict[str, Any], key: str) -> List[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ProtocolViolation(f"payload.{key} must be a list")
    return value


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _table(headers: Tuple[str, ...], rows: List[Tuple[str, ...]]) -> List[str]:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(cell))
    lines = ["  ".join(_pad(header, widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


class Console:
    """Line oriented, thread safe output.

    The receiver thread prints while the operator is typing, so in an interactive
    terminal the pending prompt is erased before each block and redrawn by the
    command loop.  When stdout is redirected the prompt is not drawn at all, which
    keeps piped logs clean for the demonstration transcript.
    """

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        try:
            self._interactive = bool(self._stream.isatty())
        except (AttributeError, ValueError):
            self._interactive = False
        self._shown = ""
        self._prompt_visible = False

    @property
    def interactive(self) -> bool:
        return self._interactive

    def show_prompt(self, prompt: str) -> None:
        if not self._interactive:
            return
        with self._lock:
            if self._prompt_visible and prompt == self._shown:
                return
            if self._prompt_visible:
                self._write("\r" + " " * (_display_width(self._shown) + 1) + "\r")
            self._shown = prompt
            self._write(prompt)
            self._prompt_visible = True

    def prompt_submitted(self) -> None:
        with self._lock:
            self._prompt_visible = False

    def print(self, text: str = "") -> None:
        self.print_lines([text])

    def print_lines(self, lines: List[str]) -> None:
        with self._lock:
            if self._interactive and self._prompt_visible:
                self._write("\r" + " " * (_display_width(self._shown) + 1) + "\r")
                self._prompt_visible = False
            for line in lines:
                self._write(line + "\n")

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            try:
                self._stream.write(text.encode(encoding, "replace").decode(encoding))
                self._stream.flush()
            except (OSError, ValueError):
                pass
        except (BrokenPipeError, ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #

class QuizClient:
    """Connects, logs in, sends requests, renders responses and events."""

    def __init__(self, host: str, port: int, user_id: str, name: str,
                 console: Optional[Console] = None, stdin: Any = None) -> None:
        self._host = host
        self._port = port
        self._user_id = user_id
        self._name = name
        self._console = console if console is not None else Console()
        self._stdin = stdin if stdin is not None else sys.stdin

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._input_queue: "queue.Queue[Optional[str]]" = queue.Queue()

        self._request_counter = 0
        self._pending: Dict[str, str] = {}

        self._session_id: Optional[str] = None
        self._login_done = threading.Event()
        self._login_ok = False
        self._logout_done = threading.Event()
        self._closed = threading.Event()
        self._closing = False
        self._disconnect_reported = False

        self._room: Optional[Dict[str, Any]] = None
        self._question: Optional[Dict[str, Any]] = None

        self._response_handlers = {
            "LOGIN_RESPONSE": self._on_login_response,
            "LIST_QUIZZES_RESPONSE": self._on_list_quizzes_response,
            "LIST_ROOMS_RESPONSE": self._on_list_rooms_response,
            "CREATE_ROOM_RESPONSE": self._on_create_room_response,
            "JOIN_ROOM_RESPONSE": self._on_join_room_response,
            "PLAYER_LIST_RESPONSE": self._on_player_list_response,
            "READY_RESPONSE": self._on_ready_response,
            "START_QUIZ_RESPONSE": self._on_start_quiz_response,
            "ANSWER_RESPONSE": self._on_answer_response,
            "LEADERBOARD_RESPONSE": self._on_leaderboard_response,
            "LEAVE_ROOM_RESPONSE": self._on_leave_room_response,
            "LOGOUT_RESPONSE": self._on_logout_response,
        }
        self._event_handlers = {
            "PLAYER_STATUS_EVENT": self._on_player_status_event,
            "QUIZ_STARTED_EVENT": self._on_quiz_started_event,
            "QUESTION_EVENT": self._on_question_event,
            "QUESTION_RESULT_EVENT": self._on_question_result_event,
            "QUIZ_FINISHED_EVENT": self._on_quiz_finished_event,
        }

    # -- lifecycle ---------------------------------------------------------- #

    def run(self) -> int:
        try:
            self._sock = socket.create_connection((self._host, self._port),
                                                  timeout=CONNECT_TIMEOUT_SECONDS)
        except OSError as exc:
            self._console.print(f"Cannot connect to {self._host}:{self._port}: {exc}")
            return 1
        self._sock.settimeout(None)
        self._console.print(f"Connected to {self._host}:{self._port}.")
        self._console.print(f"Logging in as {self._name} ({self._user_id})...")

        threading.Thread(target=self._read_loop, name="quiz-client-receiver",
                         daemon=True).start()
        self._send("LOGIN_REQUEST", {"user_id": self._user_id, "name": self._name})

        if not self._login_done.wait(LOGIN_TIMEOUT_SECONDS):
            self._console.print("Login timed out; closing the connection.")
            self._close()
            return 1
        if not self._login_ok:
            self._close()
            return 1

        self._console.print("Type /help to see the available commands.")
        threading.Thread(target=self._input_loop, name="quiz-client-stdin",
                         daemon=True).start()
        return self._command_loop()

    def abort(self) -> None:
        """Close the socket without logging out (Ctrl+C behaviour)."""
        self._closing = True
        self._close()

    # -- sockets ------------------------------------------------------------ #

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        assembler = FrameAssembler()
        try:
            while True:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    frames = assembler.feed(chunk)
                except (WireError, ValueError) as exc:
                    self._protocol_failure(f"malformed frame from server: {exc}")
                    return
                for frame in frames:
                    try:
                        self._handle_message(frame)
                    except ProtocolViolation as exc:
                        self._protocol_failure(str(exc))
                        return
                    except Exception as exc:  # noqa: BLE001 - keep the receiver alive
                        self._protocol_failure(
                            f"unexpected server message ({exc.__class__.__name__}: {exc})")
                        return
        finally:
            self._on_disconnected()

    def _send(self, message_type: str, payload: Dict[str, Any]) -> Optional[str]:
        sock = self._sock
        if self._closed.is_set() or sock is None:
            self._console.print("Not connected to the server.")
            return None
        with self._state_lock:
            self._request_counter += 1
            request_id = f"req-{self._request_counter}"
            self._pending[request_id] = message_type
            session_id = self._session_id
        envelope = {
            "version": PROTOCOL_VERSION,
            "type": message_type,
            "request_id": request_id,
            "event_id": None,
            "session_id": session_id,
            "payload": payload,
        }
        try:
            frame = encode_frame(envelope)
        except WireError as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            self._console.print(f"Cannot encode the request: {exc}")
            return None
        try:
            with self._send_lock:
                sock.sendall(frame)
        except OSError:
            with self._state_lock:
                self._pending.pop(request_id, None)
            self._console.print("Failed to send the request: the connection is gone.")
            self._on_disconnected()
            return None
        return request_id

    def _close(self) -> None:
        self._closing = True
        self._closed.set()
        self._shutdown_socket()

    def _shutdown_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _on_disconnected(self) -> None:
        self._closed.set()
        self._login_done.set()
        if not self._disconnect_reported:
            self._disconnect_reported = True
            if not self._closing:
                self._console.print("The server closed the connection.")
        self._shutdown_socket()

    def _protocol_failure(self, reason: str) -> None:
        self._console.print(f"[protocol] incompatible server message: {reason}")
        self._console.print("[protocol] closing the connection.")
        self._disconnect_reported = True
        self._close()

    # -- message dispatch --------------------------------------------------- #

    def _handle_message(self, message: Dict[str, Any]) -> None:
        version = message.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
            raise ProtocolViolation(f"unsupported protocol version {version!r}")
        message_type = message.get("type")
        if not isinstance(message_type, str):
            raise ProtocolViolation("message without a type")
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolViolation("payload is not a JSON object")

        if message_type == "ERROR_RESPONSE" or message_type.endswith("_RESPONSE"):
            self._handle_response(message, payload)
        elif message_type in self._event_handlers:
            event_id = message.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise ProtocolViolation(f"{message_type} has no event_id")
            self._event_handlers[message_type](payload, message)
        else:
            raise ProtocolViolation(f"unknown message type {message_type!r}")

    def _handle_response(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            self._console.print(f"[warn] {message['type']} without a request id")
            return
        with self._state_lock:
            expected = self._pending.pop(request_id, None)
        if expected is None:
            self._console.print(f"[warn] response for unknown or repeated request {request_id}")
            return
        message_type = message["type"]
        if message_type == "ERROR_RESPONSE":
            self._handle_error(payload, expected)
            return
        if message_type != expected.replace("_REQUEST", "_RESPONSE"):
            raise ProtocolViolation(
                f"response {message_type} does not match request {expected}")
        handler = self._response_handlers.get(message_type)
        if handler is None:
            raise ProtocolViolation(f"unknown response type {message_type}")
        handler(payload)

    def _handle_error(self, payload: Dict[str, Any], request_type: str) -> None:
        code = payload.get("code")
        if not isinstance(code, str) or not code:
            code = "SERVER_ERROR"
        detail = payload.get("message")
        detail = detail if isinstance(detail, str) and detail else ""
        self._console.print(f"[error] {code}: {detail}".rstrip(": "))
        hint = ERROR_HINTS.get(code)
        if hint:
            self._console.print(f"        {hint}")
        if request_type == "LOGIN_REQUEST":
            self._login_ok = False
            self._login_done.set()
        elif request_type == "LOGOUT_REQUEST":
            self._logout_done.set()

    # -- room helpers ------------------------------------------------------- #

    def _store_room(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_room = payload.get("room")
        room = _as_room_view(raw_room)
        with self._state_lock:
            self._room = room
            self._question = None
        return room

    def _print_room(self, room: Dict[str, Any]) -> None:
        self._console.print(
            f"Room {room['room_code']} | quiz {room['quiz_id']} ({room['title']}) | "
            f"host {room['host_user_id']} | {room['player_count']}/{room['capacity']} | "
            f"{room['state']}")
        self._print_players(room["players"], room["host_user_id"])

    def _print_players(self, players: List[Dict[str, Any]], host_user_id: str) -> None:
        rows: List[Tuple[str, ...]] = []
        for player in players:
            mark = "@" if player["user_id"] == host_user_id else " "
            rows.append((mark, f"{player['name']} ({player['user_id']})",
                         "ready" if player["ready"] else "-"))
        self._console.print_lines(_table((" ", "player", "ready"), rows))

    def _print_leaderboard(self, entries: List[Dict[str, Any]], heading: str) -> None:
        if not entries:
            self._console.print(f"{heading}: nobody has played yet.")
            return
        rows: List[Tuple[str, ...]] = []
        for entry in entries:
            player = f"{entry['name']} ({entry['user_id']})"
            if not entry["active"]:
                player += " [left]"
            rows.append((str(entry["rank"]), player, str(entry["score"])))
        self._console.print_lines([f"{heading}:"] + _table(("#", "player", "score"), rows))

    def _room_code(self) -> Optional[str]:
        with self._state_lock:
            return self._room["room_code"] if self._room else None

    # -- response handlers -------------------------------------------------- #

    def _on_login_response(self, payload: Dict[str, Any]) -> None:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolViolation("LOGIN_RESPONSE is missing session_id")
        with self._state_lock:
            self._session_id = session_id
            self._login_ok = True
        self._console.print(f"Logged in. Session {session_id}.")
        self._login_done.set()

    def _on_list_quizzes_response(self, payload: Dict[str, Any]) -> None:
        quizzes = [_as_quiz_summary(item) for item in _as_list(payload, "quizzes")]
        if not quizzes:
            self._console.print("The server offers no quizzes.")
            return
        rows = [(quiz["quiz_id"], quiz["title"], str(quiz["question_count"]))
                for quiz in quizzes]
        self._console.print_lines(["Quizzes:"] + _table(("quiz_id", "title", "questions"), rows))
        for quiz in quizzes:
            if quiz["description"]:
                self._console.print(f"  {quiz['quiz_id']}: {quiz['description']}")

    def _on_list_rooms_response(self, payload: Dict[str, Any]) -> None:
        rooms = [_as_room_summary(item) for item in _as_list(payload, "rooms")]
        if not rooms:
            self._console.print("No open rooms. Create one with /create <quiz_id>.")
            return
        rows = [(room["room_code"], room["quiz_id"], room["host_user_id"],
                 f"{room['player_count']}/{room['capacity']}", room["state"])
                for room in rooms]
        self._console.print_lines(
            ["Open rooms:"] + _table(("room", "quiz", "host", "players", "state"), rows))

    def _on_create_room_response(self, payload: Dict[str, Any]) -> None:
        room = self._store_room(payload)
        self._console.print(f"Created room {room['room_code']} and joined as host.")
        self._print_room(room)
        self._console.print("Share the room code, then /ready. Start with /start when everyone is ready.")

    def _on_join_room_response(self, payload: Dict[str, Any]) -> None:
        room = self._store_room(payload)
        self._console.print(f"Joined room {room['room_code']}.")
        self._print_room(room)
        self._console.print("Wait for the host to start. Use /ready when you are ready.")

    def _on_player_list_response(self, payload: Dict[str, Any]) -> None:
        players = [_as_player(item) for item in _as_list(payload, "players")]
        with self._state_lock:
            host = self._room["host_user_id"] if self._room else ""
            if self._room is not None:
                self._room["players"] = players
                self._room["player_count"] = len(players)
        self._print_players(players, host)

    def _on_ready_response(self, payload: Dict[str, Any]) -> None:
        ready = payload.get("ready")
        if not isinstance(ready, bool):
            raise ProtocolViolation("READY_RESPONSE is missing ready")
        self._console.print("You are ready." if ready else "You are no longer ready.")

    def _on_start_quiz_response(self, payload: Dict[str, Any]) -> None:
        room_code = _field(payload, "room_code", str)
        self._console.print(f"Start accepted for room {room_code}. The questions follow.")

    def _on_answer_response(self, payload: Dict[str, Any]) -> None:
        question_id = _field(payload, "question_id", str)
        with self._state_lock:
            question = self._question
        suffix = ""
        if question is not None and question["question_id"] == question_id:
            remaining = question["deadline"] - time.monotonic()
            suffix = f", about {max(0.0, remaining):.0f}s left"
        self._console.print(f"Answer for {question_id} recorded{suffix}. "
                            "It is scored when the question closes.")

    def _on_leaderboard_response(self, payload: Dict[str, Any]) -> None:
        room_code = _field(payload, "room_code", str)
        entries = [_as_ranking(item) for item in _as_list(payload, "leaderboard")]
        self._print_leaderboard(entries, f"Leaderboard for {room_code}")

    def _on_leave_room_response(self, payload: Dict[str, Any]) -> None:
        room_code = _field(payload, "room_code", str)
        with self._state_lock:
            self._room = None
            self._question = None
        self._console.print(f"You left room {room_code}.")

    def _on_logout_response(self, payload: Dict[str, Any]) -> None:
        self._console.print("Logged out.")
        self._logout_done.set()

    # -- event handlers ----------------------------------------------------- #

    def _on_player_status_event(self, payload: Dict[str, Any],
                                message: Dict[str, Any]) -> None:
        self._require_room_code(payload)
        host_user_id = _field(payload, "host_user_id", str)
        players = [_as_player(item) for item in _as_list(payload, "players")]
        with self._state_lock:
            if self._room is not None and self._room["room_code"] == payload["room_code"]:
                self._room["players"] = players
                self._room["player_count"] = len(players)
                self._room["host_user_id"] = host_user_id
            else:
                self._room = {
                    "room_code": payload["room_code"], "quiz_id": "?", "title": "?",
                    "host_user_id": host_user_id, "player_count": len(players),
                    "capacity": 0, "state": "LOBBY", "players": players,
                }
        self._console.print(f"[{payload['room_code']}] room members:")
        self._print_players(players, host_user_id)

    def _on_quiz_started_event(self, payload: Dict[str, Any],
                               message: Dict[str, Any]) -> None:
        room_code = self._require_room_code(payload)
        quiz_id = _field(payload, "quiz_id", str)
        title = _field(payload, "title", str)
        total = _field(payload, "total_questions", int)
        with self._state_lock:
            self._question = None
            if self._room is not None and self._room["room_code"] == room_code:
                self._room["state"] = "QUESTION_OPEN"
                self._room["quiz_id"] = quiz_id
                self._room["title"] = title
        self._console.print(f"[{room_code}] Quiz \"{title}\" started: {total} questions.")

    def _on_question_event(self, payload: Dict[str, Any],
                           message: Dict[str, Any]) -> None:
        room_code = self._require_room_code(payload)
        question_id = _field(payload, "question_id", str)
        number = _field(payload, "number", int)
        total = _field(payload, "total_questions", int)
        text = _field(payload, "text", str)
        time_limit = _field(payload, "time_limit", int)
        points = _field(payload, "points", int)
        raw_options = payload.get("options")
        if not isinstance(raw_options, dict):
            raise ProtocolViolation("QUESTION_EVENT.options is not an object")
        options: Dict[str, str] = {}
        for letter in ANSWER_CHOICES:
            value = raw_options.get(letter)
            if not isinstance(value, str):
                raise ProtocolViolation(f"QUESTION_EVENT.options.{letter} is missing")
            options[letter] = value
        with self._state_lock:
            self._question = {
                "question_id": question_id,
                "number": number,
                "total": total,
                "points": points,
                "deadline": time.monotonic() + time_limit,
            }
            if self._room is not None and self._room["room_code"] == room_code:
                self._room["state"] = "QUESTION_OPEN"
        lines = [f"[{room_code}] Question {number}/{total}  {question_id}  "
                 f"({points} points, {time_limit}s)"]
        lines.append(f"  {text}")
        for letter in ANSWER_CHOICES:
            lines.append(f"    {letter}) {options[letter]}")
        lines.append("  Answer with /answer <A|B|C|D>")
        self._console.print_lines(lines)

    def _on_question_result_event(self, payload: Dict[str, Any],
                                  message: Dict[str, Any]) -> None:
        room_code = self._require_room_code(payload)
        question_id = _field(payload, "question_id", str)
        correct = _field(payload, "correct_answer", str)
        your_answer = payload.get("your_answer")
        if your_answer is not None and not isinstance(your_answer, str):
            raise ProtocolViolation("QUESTION_RESULT_EVENT.your_answer must be a string or null")
        points = _field(payload, "points_awarded", int)
        entries = [_as_ranking(item) for item in _as_list(payload, "leaderboard")]
        with self._state_lock:
            if self._question is not None and self._question["question_id"] == question_id:
                self._question = None
        verdict = "correct" if your_answer == correct else "wrong"
        if your_answer is None:
            verdict = "no answer"
        self._console.print(f"[{room_code}] {question_id} closed. Correct answer: {correct}.")
        self._console.print(f"  You answered {your_answer or '-'}: {points} points ({verdict}).")
        self._print_leaderboard(entries, f"Standings after {question_id}")

    def _on_quiz_finished_event(self, payload: Dict[str, Any],
                                message: Dict[str, Any]) -> None:
        room_code = self._require_room_code(payload)
        quiz_id = _field(payload, "quiz_id", str)
        entries = [_as_ranking(item) for item in _as_list(payload, "final_leaderboard")]
        raw_winner = payload.get("winner")
        winner = _as_ranking(raw_winner)
        with self._state_lock:
            self._question = None
            if self._room is not None and self._room["room_code"] == room_code:
                self._room["state"] = "FINISHED"
        self._console.print(f"[{room_code}] Quiz {quiz_id} finished. "
                            f"Winner: {winner['name']} ({winner['user_id']}) "
                            f"with {winner['score']} points.")
        self._print_leaderboard(entries, "Final leaderboard")

    def _require_room_code(self, payload: Dict[str, Any]) -> str:
        room_code = _field(payload, "room_code", str)
        if not ROOM_CODE_PATTERN.match(room_code):
            raise ProtocolViolation(f"invalid room code {room_code!r} in an event")
        return room_code

    # -- command loop ------------------------------------------------------- #

    def _input_loop(self) -> None:
        try:
            for line in self._stdin:
                self._input_queue.put(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._input_queue.put(None)

    def _command_loop(self) -> int:
        while True:
            if self._closed.is_set():
                break
            self._console.show_prompt(self._build_prompt())
            try:
                line = self._input_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            self._console.prompt_submitted()
            if line is None:          # stdin closed
                break
            text = line.strip()
            if not text:
                continue
            if not text.startswith("/"):
                self._console.print("Commands start with '/'. Type /help for the list.")
                continue
            parts = text.split()
            command, arguments = parts[0].lower(), parts[1:]
            if command == "/quit":
                self._quit()
                return 0
            self._dispatch_command(command, arguments)
            if self._closed.is_set():
                break
        self._close()
        return 0

    def _build_prompt(self) -> str:
        with self._state_lock:
            room = self._room
            question = self._question
        parts: List[str] = []
        if room is not None:
            state = room["state"]
            parts.append(f"{room['room_code']} {state}".strip())
        if question is not None:
            parts.append(f"Q{question['number']}/{question['total']}")
        if not parts:
            return "> "
        return "[" + " | ".join(parts) + "] > "

    def _dispatch_command(self, command: str, arguments: List[str]) -> None:
        if command == "/help":
            self._print_help()
        elif command == "/quizzes":
            self._send("LIST_QUIZZES_REQUEST", {})
        elif command == "/rooms":
            self._send("LIST_ROOMS_REQUEST", {})
        elif command == "/create":
            if len(arguments) != 1:
                self._console.print("Usage: /create <quiz_id>   (see /quizzes)")
                return
            self._send("CREATE_ROOM_REQUEST", {"quiz_id": arguments[0]})
        elif command == "/join":
            if len(arguments) != 1:
                self._console.print("Usage: /join <room_code>   (see /rooms)")
                return
            room_code = arguments[0].upper()
            if not ROOM_CODE_PATTERN.match(room_code):
                self._console.print("A room code is six letters or digits, for example AB12CD.")
                return
            self._send("JOIN_ROOM_REQUEST", {"room_code": room_code})
        elif command == "/players":
            self._send("PLAYER_LIST_REQUEST", {})
        elif command in ("/ready", "/unready"):
            self._send("READY_REQUEST", {"ready": command == "/ready"})
        elif command == "/start":
            self._send("START_QUIZ_REQUEST", {})
        elif command == "/answer":
            self._answer(arguments)
        elif command == "/score":
            self._send("LEADERBOARD_REQUEST", {})
        elif command == "/leave":
            self._send("LEAVE_ROOM_REQUEST", {})
        else:
            self._console.print(f"Unknown command {command!r}. Type /help for the list.")

    def _answer(self, arguments: List[str]) -> None:
        if len(arguments) != 1:
            self._console.print("Usage: /answer <A|B|C|D>")
            return
        choice = arguments[0].upper()
        if choice not in ANSWER_CHOICES:
            self._console.print("Answers are A, B, C or D.")
            return
        with self._state_lock:
            question = self._question
        if question is None:
            self._console.print("No question is open right now.")
            return
        self._send("ANSWER_REQUEST", {"question_id": question["question_id"], "answer": choice})

    def _quit(self) -> None:
        self._console.print("Logging out...")
        self._send("LOGOUT_REQUEST", {})
        if not self._logout_done.wait(LOGOUT_TIMEOUT_SECONDS):
            self._closed.wait(1.0)
        self._close()

    def _print_help(self) -> None:
        self._console.print_lines([
            "Commands (all of them go to the server unless marked local):",
            "  /help                 local  show this help",
            "  /quizzes                     list the quiz catalogue",
            "  /rooms                       list open lobby rooms",
            "  /create <quiz_id>            create a room and become its host",
            "  /join <room_code>            join an open room",
            "  /players                     list the players in your room",
            "  /ready, /unready             set your readiness",
            "  /start                       host: start once two or more players are ready",
            "  /answer <A|B|C|D>            answer the open question",
            "  /score                       show the current leaderboard",
            "  /leave                       leave your room",
            "  /quit                        log out and close the connection",
        ])


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _valid_name(name: str) -> bool:
    return (1 <= len(name) <= 32
            and all(char.isprintable() for char in name)
            and bool(name.strip()))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="client.py", description="Networked quiz tournament client.")
    parser.add_argument("--server", required=True, help="server IP address or host name")
    parser.add_argument("--port", required=True, type=int, help="server TCP port")
    parser.add_argument("--id", required=True, dest="user_id",
                        help="unique user id: 1-32 of A-Z a-z 0-9 _ -")
    parser.add_argument("--name", required=True,
                        help="display name: 1-32 printable characters, not all spaces")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not USER_ID_PATTERN.match(args.user_id):
        parser.error("--id must be 1-32 ASCII letters, digits, '_' or '-'")
    if not _valid_name(args.name):
        parser.error("--name must be 1-32 printable characters with a non-space character")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    console = Console()
    client = QuizClient(args.server, args.port, args.user_id, args.name, console)
    try:
        return client.run()
    except KeyboardInterrupt:
        client.abort()
        console.print("Connection closed.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
