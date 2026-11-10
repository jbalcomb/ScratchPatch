"""An MCP server (stdio) that lets an AI agent send to your devices through the gateway.

It is just another sender: it signs with its own credentials file, the gateway stamps
`origin_agent` from that sender's token, screening runs as for anything else, and each device's
policy decides what happens. Nothing the agent writes can choose an action or a label.

    python -m courier mcp --creds ~/.config/courier/claude.sender.json

Messages are newline-delimited JSON-RPC 2.0 on stdin/stdout; logs go to stderr.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import IO, Callable

from courier import client

log = logging.getLogger("courier.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CHANNEL = re.compile(r"^[a-z0-9-]{1,48}$")
MAX_TEXT = 3000  # the broker's envelope limit leaves about this much

SEND_DESCRIPTION = (
    "Send a short text to the user's own devices (computers and phones) through Courier. "
    "What each device does with it is set by that device's policy, not by the message: "
    "'clip' usually puts single-line text on the clipboard, 'agent-inbox' usually shows a notification. "
    "Every message is marked as coming from an AI agent and screened; secrets are refused or held back. "
    "Use it when the user asks you to send, copy or notify something to their devices."
)


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _result(text: str, *, error: bool = False, structured: dict | None = None) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}], "isError": error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


class Server:
    def __init__(self, creds: client.Credentials, channels: list[str] | tuple[str, ...] = (),
                 send: Callable = client.send):
        self.creds = creds
        self.channels = sorted(set(channels))
        self._send = send

    def tools(self) -> list[dict]:
        channel: dict = {"type": "string", "description": "Which channel to send on."}
        if self.channels:
            channel["enum"] = self.channels
        return [{
            "name": "send_to_devices",
            "title": "Send to my devices",
            "description": SEND_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": channel,
                    "text": {"type": "string",
                             "description": f"The message (plain text, up to {MAX_TEXT} characters)."},
                    "ttl_seconds": {"type": "integer", "minimum": 30, "maximum": 86400,
                                    "description": "Discard it if not delivered within this many seconds "
                                                   "(default 600)."},
                },
                "required": ["channel", "text"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                            "openWorldHint": True},
        }]

    def call(self, name: str, arguments: dict) -> dict:
        if name != "send_to_devices":
            raise RpcError(-32602, f"unknown tool {name!r}")
        channel, text = arguments.get("channel"), arguments.get("text")
        ttl = arguments.get("ttl_seconds", 600)
        if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
            return _result("Not sent: channel must be lowercase letters, digits and dashes.", error=True)
        if self.channels and channel not in self.channels:
            return _result(f"Not sent: this sender may use {', '.join(self.channels)}.", error=True)
        if not isinstance(text, str) or not text.strip():
            return _result("Not sent: text is empty.", error=True)
        if len(text) > MAX_TEXT:
            return _result(f"Not sent: text is {len(text)} characters; the limit is {MAX_TEXT}.", error=True)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 30 <= ttl <= 86400:
            return _result("Not sent: ttl_seconds must be a whole number from 30 to 86400.", error=True)
        try:
            reply = self._send(self.creds, channel, text, ttl_s=ttl)
        except client.SendFailed as failure:
            log.info("send to %s failed: %s", channel, failure)
            return _result(f"Not sent: {failure}.", error=True)
        labels = [l for l in reply.get("labels", []) if not l.startswith("origin_")]
        note = f" Screening labels: {', '.join(labels)}." if labels else ""
        log.info("sent %s to %s", reply.get("id"), channel)
        # Nothing comes back from the devices: the agent learns only that the gateway admitted it.
        return _result(f"Sent to '{channel}' (id {reply.get('id')}).{note} Each device's own policy decides "
                       "what happens with it.",
                       structured={"id": reply.get("id"), "channel": channel, "labels": reply.get("labels", [])})

    def handle(self, message: dict) -> dict | None:
        """One JSON-RPC message in; the response out (None for notifications)."""
        method, msg_id = message.get("method"), message.get("id")
        is_request = "id" in message
        try:
            if not isinstance(method, str):
                raise RpcError(-32600, "invalid request")
            result = self._dispatch(method, message.get("params") or {})
        except RpcError as error:
            if not is_request:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": error.code, "message": str(error)}}
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "courier", "title": "Courier", "version": "0.1.0"},
                "instructions": "Use send_to_devices only when the user asks to send something to their devices.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "arguments must be an object")
            return self.call(params.get("name"), arguments)
        if method.startswith("notifications/"):
            return {}
        raise RpcError(-32601, f"method not found: {method}")


def serve(server: Server, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(message, dict):
                response = server.handle(message)
            else:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
