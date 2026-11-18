"""Shared TCP framing. No client or server state belongs in this module."""
import json
import struct

MAX_FRAME = 65536


class FrameError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-finite JSON number')


def decode_json(data):
    return json.loads(data.decode('utf-8'), object_pairs_hook=_object,
                      parse_constant=_constant)


def encode_frame(message):
    body = json.dumps(message, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')
    if not 1 <= len(body) <= MAX_FRAME:
        raise FrameError('Payload size outside 1..65536')
    return struct.pack('!I', len(body)) + body


def pop_frame(buffer):
    """Consume one complete frame; retain incomplete bytes in caller's buffer."""
    if len(buffer) < 4:
        return None
    size = struct.unpack('!I', buffer[:4])[0]
    if not 1 <= size <= MAX_FRAME:
        raise FrameError('Invalid frame length')
    if len(buffer) < size + 4:
        return None
    body = bytes(buffer[4:4 + size])
    del buffer[:4 + size]
    try:
        message = decode_json(body)
        if not isinstance(message, dict):
            raise ValueError('Expected JSON object')
        return message
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FrameError('Invalid JSON payload') from exc
