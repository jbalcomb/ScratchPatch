"""The HTTP helper against a real local socket server."""

import json
import socket
import threading

import pytest

from taskdeck.shared import http


def serve_once(raw_response: bytes):
    """Accept one connection, read the request, reply with raw bytes. Returns the URL."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, body = data.partition(b"\r\n\r\n")
            length = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
            while len(body) < length:
                body += conn.recv(4096)
            conn.sendall(raw_response)
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/v1/chat/completions"


def ok(body: dict, interim: bytes = b"") -> bytes:
    payload = json.dumps(body).encode()
    return interim + (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                      b"Content-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)


def test_plain_json_response():
    url = serve_once(ok({"answer": 42}))
    assert http.post_json(url, {"q": 1}, timeout=5) == {"answer": 42}


def test_103_early_hints_is_skipped():
    # What Google's front end can send before the real reply.
    hints = b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
    url = serve_once(ok({"answer": 42}, interim=hints))
    assert http.post_json(url, {"q": 1}, timeout=5) == {"answer": 42}


def test_several_interim_responses_are_skipped():
    interim = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 102 Processing\r\n\r\nHTTP/1.1 103 Early Hints\r\nLink: <x>\r\n\r\n"
    url = serve_once(ok({"answer": 42}, interim=interim))
    assert http.post_json(url, {"q": 1}, timeout=5) == {"answer": 42}


def test_http_error_carries_status_and_message():
    body = b'{"error": {"message": "API key not valid"}}'
    url = serve_once(b"HTTP/1.1 400 Bad Request\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + body)
    with pytest.raises(http.HttpError) as exc:
        http.post_json(url, {}, timeout=5)
    assert exc.value.status == 400 and "API key not valid" in str(exc.value)


def test_unreachable_has_no_status():
    with pytest.raises(http.HttpError) as exc:
        http.post_json("http://127.0.0.1:1/x", {}, timeout=2)
    assert exc.value.status is None
