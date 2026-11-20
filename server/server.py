"""Single-owner, nonblocking TCP quiz server implementing PROTOCOL.md."""
import argparse
from collections import deque
from dataclasses import dataclass, field
import logging
import re
import secrets
import selectors
import socket
import string
import time

from protocol import FrameError, encode_frame, pop_frame
from question_bank import IDENTIFIER, load_questions, visible

REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
ROOM_CODE = re.compile(r'[A-Z0-9]{6}\Z')
COMMANDS = set(('LOGIN LIST_QUIZZES LIST_ROOMS CREATE_ROOM JOIN_ROOM PLAYER_LIST '
                'READY START_QUIZ ANSWER LEADERBOARD LEAVE_ROOM LOGOUT').split())


class RequestError(Exception):
    def __init__(self, code):
        self.code = code


def require(condition, code):
    if not condition:
        raise RequestError(code)


@dataclass(eq=False)
class Connection:
    sock: socket.socket
    created: float
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    partial_since: float | None = None
    progress: float = 0
    session: str | None = None
    user: str | None = None
    name: str = ''
    room: object = None
    ready: bool = False
    closing: bool = False
    failed: bool = False
    ids: deque = field(default_factory=deque)
    id_set: set = field(default_factory=set)


@dataclass
class Room:
    code: str
    quiz: dict
    host: str
    players: dict = field(default_factory=dict)
    state: str = 'LOBBY'
    index: int = -1
    deadline: float = 0
    next_question: float = 0
    answers: dict = field(default_factory=dict)
    contestants: dict = field(default_factory=dict)


class QuizServer:
    """All mutable state is owned by serve_forever's thread; no network locks."""
    def __init__(self, host, port, quizzes, clock=time.monotonic):
        self.clock = clock
        self.quizzes = {q['quiz_id']: q for q in quizzes}
        self.selector = selectors.DefaultSelector()
        self.connections, self.users, self.rooms = {}, {}, {}
        self.used_codes = set()
        self.event_counter = 0
        self.running = True
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((host, port))
            self.listener.listen(128)
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ, None)
        except BaseException:
            self.listener.close()
            self.selector.close()
            raise

    def queue(self, client, kind, payload, request_id=None, event_id=None):
        if client.failed or client.sock not in self.connections:
            return
        try:
            frame = encode_frame(dict(version=1, type=kind, request_id=request_id,
                                      event_id=event_id, session_id=client.session, payload=payload))
        except (ValueError, UnicodeError):
            client.failed = True
            return
        if len(client.outgoing) + len(frame) > 1024 * 1024:
            client.failed = True
            return
        if not client.outgoing:
            client.progress = self.clock()
        client.outgoing.extend(frame)
        self.selector.modify(client.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, client)

    def event(self, room, kind, payload, recipient=None):
        self.event_counter += 1
        for client in ([recipient] if recipient else list(room.players.values())):
            self.queue(client, kind, dict(room_code=room.code, **payload),
                       event_id=f'evt-{self.event_counter}')

    def players(self, room):
        return [dict(user_id=c.user, name=c.name, ready=c.ready, is_host=c.user == room.host)
                for c in room.players.values()]

    def view(self, room, full=False):
        result = dict(room_code=room.code, quiz_id=room.quiz['quiz_id'], title=room.quiz['title'],
                      host_user_id=room.host, player_count=len(room.players), capacity=10,
                      state=room.state)
        if full:
            result['players'] = self.players(room)
        return result

    def ranking(self, room):
        entries = room.contestants if room.state != 'LOBBY' else {
            c.user: dict(name=c.name, score=0) for c in room.players.values()}
        ordered = sorted(entries.items(), key=lambda item: (-item[1]['score'], item[0]))
        return [dict(rank=i, user_id=uid, name=data['name'], score=data['score'],
                     active=uid in room.players) for i, (uid, data) in enumerate(ordered, 1)]

    def status(self, room):
        self.event(room, 'PLAYER_STATUS_EVENT', dict(players=self.players(room), host_user_id=room.host))

    def leave(self, client):
        room = client.room
        if room is None:
            return
        client.room, client.ready = None, False
        room.players.pop(client.user, None)
        if not room.players:
            self.rooms.pop(room.code, None)
            return
        if room.host == client.user:
            room.host = next(iter(room.players))
        self.status(room)
        if room.state == 'QUESTION_OPEN' and all(uid in room.answers for uid in room.players):
            self.close_question(room)

    def disconnect(self, client):
        self.leave(client)
        if self.users.get(client.user) is client:
            del self.users[client.user]
        self.connections.pop(client.sock, None)
        try:
            self.selector.unregister(client.sock)
        except KeyError:
            pass
        client.sock.close()

    def open_question(self, room):
        room.index += 1
        room.state, room.answers = 'QUESTION_OPEN', {}
        question = room.quiz['questions'][room.index]
        room.deadline = self.clock() + question['time_limit']
        payload = {key: question[key] for key in ('question_id', 'text', 'options', 'time_limit', 'points')}
        payload.update(number=room.index + 1, total_questions=len(room.quiz['questions']))
        self.event(room, 'QUESTION_EVENT', payload)

    def close_question(self, room):
        if room.state != 'QUESTION_OPEN':
            return
        room.state = 'QUESTION_CLOSED'
        question = room.quiz['questions'][room.index]
        awards = {uid: question['points'] if answer == question['answer'] else 0
                  for uid, answer in room.answers.items()}
        for uid, points in awards.items():
            room.contestants[uid]['score'] += points
        ranking = self.ranking(room)
        for client in list(room.players.values()):
            self.event(room, 'QUESTION_RESULT_EVENT', dict(question_id=question['question_id'],
                       correct_answer=question['answer'], your_answer=room.answers.get(client.user),
                       points_awarded=awards.get(client.user, 0), leaderboard=ranking), client)
        if room.index + 1 == len(room.quiz['questions']):
            room.state = 'FINISHED'
            self.event(room, 'QUIZ_FINISHED_EVENT', dict(quiz_id=room.quiz['quiz_id'],
                       final_leaderboard=ranking, winner=ranking[0]))
        else:
            room.next_question = self.clock() + 2

    def handle(self, client, message):
        request_id = message.get('request_id')
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            request_id = None
        try:
            require(set(('version', 'type', 'request_id', 'event_id', 'session_id', 'payload')) <= message.keys()
                    and type(message['version']) is int and isinstance(message['type'], str)
                    and request_id is not None and message['event_id'] is None
                    and (message['session_id'] is None or isinstance(message['session_id'], str))
                    and isinstance(message['payload'], dict), 'INVALID_MESSAGE')
            require(message['version'] == 1, 'UNSUPPORTED_VERSION')
            kind = message['type']
            command = kind.removesuffix('_REQUEST')
            require(kind.endswith('_REQUEST') and command in COMMANDS, 'UNKNOWN_COMMAND')
            require(request_id not in client.id_set, 'DUPLICATE_REQUEST')
            client.ids.append(request_id)
            client.id_set.add(request_id)
            if len(client.ids) > 1024:
                client.id_set.remove(client.ids.popleft())
            if command == 'LOGIN':
                require(client.session is None, 'ALREADY_AUTHENTICATED')
                require(message['session_id'] is None, 'INVALID_SESSION')
            else:
                require(client.session is not None, 'NOT_AUTHENTICATED')
                require(message['session_id'] == client.session, 'INVALID_SESSION')
            self.dispatch(client, command, message['payload'], request_id)
        except RequestError as exc:
            self.queue(client, 'ERROR_RESPONSE', dict(code=exc.code,
                       message=exc.code.replace('_', ' ').capitalize() + '.'), request_id)
        except Exception:
            # Do not log request payloads, session IDs, or hidden answers.
            logging.error('Unexpected request failure; closing affected connection')
            self.queue(client, 'ERROR_RESPONSE', dict(code='SERVER_ERROR', message='Internal server error.'), request_id)
            client.closing = True

    def dispatch(self, c, command, p, rid):
        def reply(payload):
            self.queue(c, command + '_RESPONSE', payload, rid)

        def text_field(key, pattern=None):
            value = p.get(key)
            require(isinstance(value, str) and (pattern is None or pattern.fullmatch(value)), 'INVALID_MESSAGE')
            return value

        if command == 'LOGIN':
            uid = text_field('user_id', IDENTIFIER)
            name = text_field('name')
            require(visible(name, 1, 32), 'INVALID_MESSAGE')
            require(uid not in self.users, 'DUPLICATE_USER')
            c.user, c.name, c.session = uid, name, secrets.token_hex(16)
            self.users[uid] = c
            reply(dict(success=True, session_id=c.session))
        elif command == 'LIST_QUIZZES':
            reply(dict(quizzes=[dict(quiz_id=q['quiz_id'], title=q['title'], description=q['description'],
                                    question_count=len(q['questions'])) for q in self.quizzes.values()]))
        elif command == 'LIST_ROOMS':
            reply(dict(rooms=[self.view(r) for r in self.rooms.values()
                             if r.state == 'LOBBY' and len(r.players) < 10]))
        elif command in ('CREATE_ROOM', 'JOIN_ROOM'):
            value = text_field('quiz_id' if command == 'CREATE_ROOM' else 'room_code',
                               IDENTIFIER if command == 'CREATE_ROOM' else ROOM_CODE)
            require(c.room is None, 'ALREADY_IN_ROOM')
            if command == 'CREATE_ROOM':
                require(value in self.quizzes, 'QUIZ_NOT_FOUND')
                while True:
                    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                    if code not in self.used_codes:
                        break
                self.used_codes.add(code)
                room = Room(code, self.quizzes[value], c.user)
                self.rooms[code] = room
            else:
                require(value in self.rooms, 'ROOM_NOT_FOUND')
                room = self.rooms[value]
                require(room.state == 'LOBBY', 'INVALID_STATE')
                require(len(room.players) < 10, 'ROOM_FULL')
            c.room, c.ready = room, False
            room.players[c.user] = c
            reply(dict(success=True, room=self.view(room, True)))
            self.status(room)
        elif command == 'LOGOUT':
            reply(dict(success=True))
            self.leave(c)
            c.closing = True
        else:
            if command == 'READY':
                require(type(p.get('ready')) is bool, 'INVALID_MESSAGE')
            if command == 'ANSWER':
                text_field('question_id')
                text_field('answer')
            require(c.room is not None, 'NOT_IN_ROOM')
            room = c.room
            if command == 'PLAYER_LIST':
                reply(dict(players=self.players(room)))
            elif command == 'LEADERBOARD':
                reply(dict(room_code=room.code, leaderboard=self.ranking(room)))
            elif command == 'LEAVE_ROOM':
                reply(dict(success=True, room_code=room.code))
                self.leave(c)
            elif command == 'READY':
                require(room.state == 'LOBBY', 'INVALID_STATE')
                changed = c.ready != p['ready']
                c.ready = p['ready']
                reply(dict(success=True, ready=c.ready))
                if changed:
                    self.status(room)
            elif command == 'START_QUIZ':
                require(room.state == 'LOBBY', 'INVALID_STATE')
                require(room.host == c.user, 'NOT_ROOM_HOST')
                require(len(room.players) >= 2, 'NOT_ENOUGH_PLAYERS')
                require(all(player.ready for player in room.players.values()), 'PLAYERS_NOT_READY')
                room.contestants = {uid: dict(name=player.name, score=0) for uid, player in room.players.items()}
                reply(dict(success=True, room_code=room.code))
                self.event(room, 'QUIZ_STARTED_EVENT', dict(quiz_id=room.quiz['quiz_id'],
                           title=room.quiz['title'], total_questions=len(room.quiz['questions'])))
                self.open_question(room)
            elif command == 'ANSWER':
                require(room.state == 'QUESTION_OPEN' and self.clock() < room.deadline, 'QUESTION_CLOSED')
                require(p['question_id'] not in {q['question_id'] for q in room.quiz['questions'][:room.index]},
                        'QUESTION_CLOSED')
                question = room.quiz['questions'][room.index]
                require(p['question_id'] == question['question_id'] and p['answer'] in list('ABCD'), 'INVALID_ANSWER')
                require(c.user not in room.answers, 'DUPLICATE_ANSWER')
                room.answers[c.user] = p['answer']
                reply(dict(accepted=True, question_id=p['question_id']))
                if all(uid in room.answers for uid in room.players):
                    self.close_question(room)

    def tick(self):
        now = self.clock()
        for c in list(self.connections.values()):
            if (c.failed or (c.session is None and now - c.created >= 30)
                    or (c.partial_since is not None and now - c.partial_since >= 30)
                    or (c.outgoing and now - c.progress >= 30)
                    or (c.closing and not c.outgoing)):
                self.disconnect(c)
        for room in list(self.rooms.values()):
            if room.state == 'QUESTION_OPEN' and now >= room.deadline:
                self.close_question(room)
            elif room.state == 'QUESTION_CLOSED' and now >= room.next_question:
                self.open_question(room)

    def read(self, c):
        data = c.sock.recv(65536)
        if not data:
            self.disconnect(c)
            return
        if not c.incoming:
            c.partial_since = self.clock()
        c.incoming.extend(data)
        # Bound work per selector cycle; buffered frames resume on the next cycle.
        self.drain(c)

    def drain(self, c):
        for _ in range(32):
            if c.closing or c.failed:
                return
            message = pop_frame(c.incoming)
            if message is None:
                return
            c.partial_since = self.clock() if c.incoming else None
            self.handle(c, message)

    def serve_forever(self):
        try:
            while self.running:
                self.tick()
                for c in list(self.connections.values()):
                    if c.incoming and not c.closing:
                        try:
                            self.drain(c)
                        except FrameError:
                            self.disconnect(c)
                for key, mask in self.selector.select(0.02):
                    if key.data is None:
                        try:
                            sock, _ = self.listener.accept()
                        except OSError as exc:
                            logging.warning('Accept failed: %s', exc)
                            continue
                        sock.setblocking(False)
                        c = Connection(sock, self.clock())
                        self.connections[sock] = c
                        self.selector.register(sock, selectors.EVENT_READ, c)
                        continue
                    c = key.data
                    if c.sock not in self.connections:
                        continue
                    try:
                        if mask & selectors.EVENT_READ and not c.closing and len(c.incoming) < 65540:
                            self.read(c)
                        if mask & selectors.EVENT_WRITE and c.sock in self.connections:
                            sent = c.sock.send(c.outgoing)
                            if sent:
                                del c.outgoing[:sent]
                                c.progress = self.clock()
                            if not c.outgoing:
                                self.selector.modify(c.sock, selectors.EVENT_READ, c)
                    except BlockingIOError:
                        pass
                    except (OSError, FrameError):
                        self.disconnect(c)
        finally:
            self.close()

    def close(self):
        for c in list(self.connections.values()):
            c.sock.close()
        self.connections.clear()
        self.users.clear()
        self.rooms.clear()
        self.listener.close()
        self.selector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--questions', required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    try:
        quizzes = load_questions(args.questions)
        server = QuizServer(args.host, args.port, quizzes)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Startup failed: {exc}\n')
    print('SERVER_READY', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
