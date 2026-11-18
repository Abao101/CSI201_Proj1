import copy
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol import FrameError, encode_frame, pop_frame
from question_bank import load_questions
from server import Connection, QuizServer
import selectors

ROOT = Path(__file__).resolve().parents[1]


def bank():
    return {'schema_version': 1, 'quizzes': [dict(quiz_id='test', title='Test', description='',
        questions=[dict(question_id=f'Q{i}', text='Question?', options=dict(A='a', B='b', C='c', D='d'),
                        answer='A', time_limit=5, points=100) for i in range(5)])]}


class Peer:
    def __init__(self, address):
        self.sock = socket.create_connection(address, timeout=3)
        self.sock.settimeout(4)
        self.session = None
        self.counter = 0
        self.buffer = bytearray()
        self.events = []

    def receive(self):
        while True:
            message = pop_frame(self.buffer)
            if message is not None:
                return message
            data = self.sock.recv(65536)
            if not data:
                raise EOFError('Server closed connection')
            self.buffer.extend(data)

    def request(self, command, payload=None, **overrides):
        self.counter += 1
        message = dict(version=1, type=command + '_REQUEST', request_id=f'req-{self.counter}',
                       event_id=None, session_id=self.session, payload={} if payload is None else payload)
        message.update(overrides)
        self.sock.sendall(encode_frame(message))
        while True:
            response = self.receive()
            if response['event_id'] is not None:
                self.events.append(response)
                continue
            if response['type'] == 'LOGIN_RESPONSE':
                self.session = response['payload']['session_id']
            return response

    def event(self, kind):
        for i, message in enumerate(self.events):
            if message['type'] == kind:
                return self.events.pop(i)
        while True:
            message = self.receive()
            if message['type'] == kind:
                return message
            self.events.append(message)


class ValidationTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'questions.json'
            path.write_text(json.dumps(value), encoding='utf-8')
            return load_questions(path)

    def test_teacher_bank(self):
        self.assertEqual(sum(len(q['questions']) for q in load_questions(ROOT.parent / 'questions_50.json')), 50)

    def test_extra_members_and_schema(self):
        value = bank()
        value['future'] = True
        self.assertEqual(len(self.load(value)), 1)
        for field, invalid in [('time_limit', True), ('points', 0), ('answer', 'AB'),
                               ('options', dict(A='a', B='b', C='c', D='d', E='e'))]:
            value = bank()
            value['quizzes'][0]['questions'][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(value)

    def test_global_duplicate_question_id(self):
        value = bank()
        second = copy.deepcopy(value['quizzes'][0])
        second['quiz_id'] = 'second'
        value['quizzes'].append(second)
        with self.assertRaises(ValueError):
            self.load(value)

    def test_frame_fragmentation_coalescing(self):
        frame = encode_frame({'text': '中文'})
        buffer = bytearray()
        for byte in frame[:-1]:
            buffer.append(byte)
            self.assertIsNone(pop_frame(buffer))
        buffer.extend(frame[-1:] + frame)
        self.assertEqual(pop_frame(buffer), {'text': '中文'})
        self.assertEqual(pop_frame(buffer), {'text': '中文'})
        self.assertFalse(buffer)

    def test_bad_frames(self):
        for data in [struct.pack('!I', 0), struct.pack('!I', 65537)]:
            with self.assertRaises(FrameError):
                pop_frame(bytearray(data))
        for body in [b'[]', b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'{']:
            with self.subTest(body=body), self.assertRaises(FrameError):
                pop_frame(bytearray(struct.pack('!I', len(body)) + body))

    def test_startup_rejects_missing_bank_and_bad_port(self):
        for args in [('--port', '5000', '--questions', 'missing-bank.json'),
                     ('--port', '0', '--questions', 'questions_50.json')]:
            run = subprocess.run([sys.executable, str(ROOT / 'server.py'), '--host', '127.0.0.1', *args],
                                 cwd=ROOT, capture_output=True, timeout=5)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn(b'SERVER_READY', run.stdout)


class ResourceTests(unittest.TestCase):
    def test_connection_timeouts_and_queue_limit(self):
        # No event-loop thread: a fake monotonic clock exercises exact boundaries.
        now = [0.0]
        server = QuizServer('127.0.0.1', 0, bank()['quizzes'], clock=lambda: now[0])
        remotes = []
        try:
            for mode in ('login', 'partial', 'send', 'overflow'):
                remote = socket.create_connection(server.listener.getsockname())
                remotes.append(remote)
                sock, _ = server.listener.accept()
                sock.setblocking(False)
                c = Connection(sock, 0)
                server.connections[sock] = c
                server.selector.register(sock, selectors.EVENT_READ, c)
                if mode != 'login':
                    c.session = 'test'
                if mode == 'partial':
                    c.partial_since = 0
                if mode == 'send':
                    c.outgoing.extend(b'test')
                    c.progress = 0
                if mode == 'overflow':
                    c.outgoing.extend(b'x' * (1024 * 1024))
                    server.queue(c, 'TEST', {})
                    self.assertTrue(c.failed)
            now[0] = 30
            server.tick()
            self.assertFalse(server.connections)
        finally:
            for remote in remotes:
                remote.close()
            server.close()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.server = QuizServer('127.0.0.1', 0, bank()['quizzes'])
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.peers = []

    def tearDown(self):
        for peer in self.peers:
            peer.sock.close()
        self.server.running = False
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def peer(self, uid=None):
        peer = Peer(self.server.listener.getsockname())
        self.peers.append(peer)
        if uid:
            self.assertEqual(peer.request('LOGIN', dict(user_id=uid, name=uid))['type'], 'LOGIN_RESPONSE')
        return peer

    def room(self, peers):
        response = peers[0].request('CREATE_ROOM', dict(quiz_id='test'))
        code = response['payload']['room']['room_code']
        for peer in peers[1:]:
            self.assertEqual(peer.request('JOIN_ROOM', dict(room_code=code))['type'], 'JOIN_ROOM_RESPONSE')
        for peer in peers:
            peer.request('READY', dict(ready=True))
        peers[0].request('START_QUIZ')
        return code

    def test_complete_game_and_confidentiality(self):
        a, b = self.peer('a'), self.peer('b')
        listing = a.request('LIST_QUIZZES')['payload']
        self.assertNotIn('questions', listing['quizzes'][0])
        self.room([a, b])
        for index in range(5):
            for peer in (a, b):
                question = peer.event('QUESTION_EVENT')['payload']
                self.assertNotIn('answer', question)
                self.assertEqual(question['question_id'], f'Q{index}')
            a.request('ANSWER', dict(question_id=f'Q{index}', answer='A'))
            duplicate = a.request('ANSWER', dict(question_id=f'Q{index}', answer='B'))
            self.assertEqual(duplicate['payload']['code'], 'DUPLICATE_ANSWER')
            self.assertEqual(a.request('LEADERBOARD')['payload']['leaderboard'][0]['score'], index * 100)
            b.request('ANSWER', dict(question_id=f'Q{index}', answer='B'))
            for peer in (a, b):
                result = peer.event('QUESTION_RESULT_EVENT')['payload']
                self.assertEqual(result['leaderboard'][0]['score'], (index + 1) * 100)
                self.assertEqual(result['correct_answer'], 'A')
        final = a.event('QUIZ_FINISHED_EVENT')['payload']
        self.assertEqual(final['winner']['user_id'], 'a')
        self.assertEqual(final['winner']['score'], 500)
        self.assertEqual(a.request('START_QUIZ')['payload']['code'], 'INVALID_STATE')

    def test_ten_clients_two_rooms(self):
        peers = [self.peer(f'u{i}') for i in range(10)]
        codes = [self.room(peers[:5]), self.room(peers[5:])]
        for i, peer in enumerate(peers):
            event = peer.event('QUESTION_EVENT')
            self.assertEqual(event['payload']['room_code'], codes[i // 5])
        for peer in peers[:5]:
            peer.request('ANSWER', dict(question_id='Q0', answer='A'))
        self.assertEqual(peers[0].event('QUESTION_RESULT_EVENT')['payload']['leaderboard'][0]['score'], 100)
        self.assertEqual(peers[5].request('LEADERBOARD')['payload']['leaderboard'][0]['score'], 0)
        self.assertTrue(all(e['payload']['room_code'] == codes[1] for e in peers[5].events))

    def test_deadline_missing_and_late_answer(self):
        a, b = self.peer('a'), self.peer('b')
        self.room([a, b])
        a.event('QUESTION_EVENT')
        a.sock.settimeout(7)
        result = a.event('QUESTION_RESULT_EVENT')['payload']
        self.assertEqual(result['points_awarded'], 0)
        self.assertIsNone(result['your_answer'])
        self.assertEqual(a.request('ANSWER', dict(question_id='Q0', answer='A'))['payload']['code'], 'QUESTION_CLOSED')

    def test_identity_errors_and_disconnect_isolation(self):
        a = self.peer('a')
        b = self.peer()
        self.assertEqual(b.request('LOGIN', dict(user_id='a', name='other'))['payload']['code'], 'DUPLICATE_USER')
        self.assertEqual(a.request('LIST_QUIZZES', session_id='wrong')['payload']['code'], 'INVALID_SESSION')
        self.assertEqual(a.request('LIST_QUIZZES', request_id='req-1')['payload']['code'], 'DUPLICATE_REQUEST')
        b.sock.sendall(struct.pack('!I', 0))
        self.assertEqual(b.sock.recv(1), b'')
        self.assertEqual(a.request('LIST_QUIZZES')['type'], 'LIST_QUIZZES_RESPONSE')

    def test_host_transfer_and_retained_answer(self):
        a, b = self.peer('a'), self.peer('b')
        self.room([a, b])
        a.event('QUESTION_EVENT')
        b.event('QUESTION_EVENT')
        a.request('ANSWER', dict(question_id='Q0', answer='A'))
        a.request('LEAVE_ROOM')
        players = b.request('PLAYER_LIST')['payload']['players']
        self.assertEqual(players, [dict(user_id='b', name='b', ready=True, is_host=True)])
        b.request('ANSWER', dict(question_id='Q0', answer='B'))
        ranking = b.event('QUESTION_RESULT_EVENT')['payload']['leaderboard']
        self.assertEqual(ranking[0]['score'], 100)
        self.assertFalse(ranking[0]['active'])

    def test_room_start_rules_and_logout(self):
        a, b = self.peer('a'), self.peer('b')
        code = a.request('CREATE_ROOM', dict(quiz_id='test'))['payload']['room']['room_code']
        self.assertEqual(a.request('START_QUIZ')['payload']['code'], 'NOT_ENOUGH_PLAYERS')
        b.request('JOIN_ROOM', dict(room_code=code))
        self.assertEqual(b.request('START_QUIZ')['payload']['code'], 'NOT_ROOM_HOST')
        self.assertEqual(a.request('START_QUIZ')['payload']['code'], 'PLAYERS_NOT_READY')
        a.request('LOGOUT')
        self.assertEqual(a.sock.recv(1), b'')
        self.assertTrue(b.request('PLAYER_LIST')['payload']['players'][0]['is_host'])
        b.request('LEAVE_ROOM')
        self.assertEqual(b.request('LIST_ROOMS')['payload']['rooms'], [])
        self.peer('a')

    def test_envelope_errors_keep_connection(self):
        a = self.peer('a')
        for changes, expected in [({'version': True}, 'INVALID_MESSAGE'),
                                  ({'version': 2}, 'UNSUPPORTED_VERSION'),
                                  ({'type': 'BOGUS_REQUEST'}, 'UNKNOWN_COMMAND'),
                                  ({'payload': []}, 'INVALID_MESSAGE')]:
            self.assertEqual(a.request('LIST_ROOMS', **changes)['payload']['code'], expected)
        self.assertEqual(a.request('LIST_QUIZZES')['type'], 'LIST_QUIZZES_RESPONSE')

    def test_network_fragmentation_and_coalescing(self):
        a = self.peer()
        login = dict(version=1, type='LOGIN_REQUEST', request_id='login', event_id=None,
                     session_id=None, payload=dict(user_id='fragment', name='中文'))
        for byte in encode_frame(login):
            a.sock.sendall(bytes([byte]))
        response = a.receive()
        a.session = response['session_id']
        request = dict(version=1, type='LIST_QUIZZES_REQUEST', request_id='one', event_id=None,
                       session_id=a.session, payload={})
        a.sock.sendall(encode_frame(request) + encode_frame(dict(request, request_id='two')))
        self.assertEqual([a.receive()['request_id'], a.receive()['request_id']], ['one', 'two'])


if __name__ == '__main__':
    unittest.main()
