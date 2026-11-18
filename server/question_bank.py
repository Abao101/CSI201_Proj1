"""Validate the entire compulsory question-bank schema before opening sockets."""
import re
from pathlib import Path
from protocol import decode_json

IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')
MAX_FILE_BYTES = 1024 * 1024


def visible(value, minimum, maximum):
    return (isinstance(value, str) and minimum <= len(value) <= maximum
            and all(c.isprintable() for c in value)
            and (minimum == 0 or bool(value.strip())))


def load_questions(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if not 1 <= len(raw) <= MAX_FILE_BYTES:
        raise ValueError('Question bank must contain 1..1048576 bytes')
    try:
        bank = decode_json(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('Question bank must be valid UTF-8 JSON') from exc

    def require(condition, location):
        if not condition:
            raise ValueError('Invalid question bank: ' + location)

    require(isinstance(bank, dict), 'top-level object')
    require(type(bank.get('schema_version')) is int and bank['schema_version'] == 1,
            'schema_version must be integer 1')
    quizzes = bank.get('quizzes')
    require(isinstance(quizzes, list) and 1 <= len(quizzes) <= 20, 'quizzes count')
    quiz_ids, question_ids = set(), set()
    for i, quiz in enumerate(quizzes):
        loc = f'quizzes[{i}]'
        require(isinstance(quiz, dict), loc)
        qid = quiz.get('quiz_id')
        require(isinstance(qid, str) and IDENTIFIER.fullmatch(qid) and qid not in quiz_ids,
                loc + '.quiz_id (format or duplicate)')
        quiz_ids.add(qid)
        for field, low, high in [('title', 1, 100), ('description', 0, 300)]:
            require(visible(quiz.get(field), low, high), loc + '.' + field)
        questions = quiz.get('questions')
        require(isinstance(questions, list) and 5 <= len(questions) <= 50, loc + '.questions')
        for j, question in enumerate(questions):
            here = f'{loc}.questions[{j}]'
            require(isinstance(question, dict), here)
            qid = question.get('question_id')
            require(isinstance(qid, str) and IDENTIFIER.fullmatch(qid)
                    and qid not in question_ids, here + '.question_id (format or duplicate)')
            question_ids.add(qid)
            require(visible(question.get('text'), 1, 500), here + '.text')
            options = question.get('options')
            require(isinstance(options, dict) and set(options) == set('ABCD'), here + '.options')
            for value in options.values():
                require(isinstance(value, str) and 1 <= len(value) <= 200, here + '.options value')
            require(isinstance(question.get('answer'), str) and question['answer'] in list('ABCD'),
                    here + '.answer')
            for field, low, high in [('time_limit', 5, 60), ('points', 1, 1000)]:
                value = question.get(field)
                require(type(value) is int and low <= value <= high, here + '.' + field)
    return quizzes
