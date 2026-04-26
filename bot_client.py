"""
Mend Platform bot client for regression mode.
Handles auth (JWT rotation) and MCP tool calls via the local proxy.

Requires the proxy to be running:
    python3 ~/mend-mcp-proxy/proxy.py &
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
import uuid

import httpx

from config import (
    MEND_BASE_URL,
    MEND_EMAIL,
    MEND_ORG_UUID,
    MEND_PROXY_URL,
    MEND_USER_KEY,
)

REFRESH_MARGIN_SECONDS = 60


# ---------------------------------------------------------------------------
# Auth client (mirrors proxy.py auth logic)
# ---------------------------------------------------------------------------

class MendAuthClient:
    def __init__(self, base_url: str = MEND_BASE_URL,
                 email: str = MEND_EMAIL,
                 user_key: str = MEND_USER_KEY):
        self._base_url = base_url
        self._email = email
        self._user_key = user_key
        self._refresh_token: str | None = None
        self._access_token: str | None = None
        self._expires_at: float = 0

    def get_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at - REFRESH_MARGIN_SECONDS:
            return self._access_token
        if not self._refresh_token:
            self._refresh_token = self._login()
        try:
            self._access_token, self._expires_at = self._exchange()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._refresh_token = self._login()
                self._access_token, self._expires_at = self._exchange()
            else:
                raise
        return self._access_token

    def _login(self) -> str:
        url = f"{self._base_url}/api/v3.0/login"
        payload = json.dumps({"email": self._email, "userKey": self._user_key}).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        return data["response"]["refreshToken"]

    def _exchange(self) -> tuple[str, float]:
        url = f"{self._base_url}/api/v3.0/login/accessToken"
        req = urllib.request.Request(
            url, data=b"",
            headers={"Content-Type": "application/json",
                     "wss-refresh-token": self._refresh_token},
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        jwt = data["response"]["jwtToken"]
        ttl_ms = data["response"]["tokenTTL"]
        return jwt, time.time() + (ttl_ms / 1000)


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------

def create_conversation(auth: MendAuthClient) -> str:
    """
    Create a new conversation UUID via the Mend REST API.
    Returns the conversation UUID string.
    """
    token = auth.get_token()
    url = f"{MEND_BASE_URL}/api/v3.0/orgs/{MEND_ORG_UUID}/assistant/conversations"
    req = urllib.request.Request(
        url, data=b"{}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # Response field name may vary — try common variants
    response_body = data.get("response", data)
    for key in ("conversationUuid", "conversation_uuid", "uuid", "id", "conversationId"):
        if key in response_body:
            return response_body[key]

    raise ValueError(f"Could not find conversation UUID in response: {data}")


# ---------------------------------------------------------------------------
# MCP client (calls ask_assistant via proxy)
# ---------------------------------------------------------------------------

def _parse_mcp_response(raw: str) -> dict:
    """Parse a response that may be plain JSON or SSE (data: <json>) format."""
    raw = raw.strip()
    # Try plain JSON first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try SSE: look for data: lines
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return {}


def _extract_tool_result_text(mcp_response: dict) -> str:
    """Extract the text content from an MCP tools/call result."""
    try:
        content = mcp_response.get("result", {}).get("content", [])
        parts = [item["text"] for item in content if item.get("type") == "text"]
        return "\n".join(parts)
    except (AttributeError, TypeError, KeyError):
        return str(mcp_response)


class MendBotClient:
    """
    Minimal MCP client that calls ask_assistant via the local auth proxy.
    Manages MCP session initialization and tool calls.
    """

    def __init__(self, proxy_url: str = MEND_PROXY_URL):
        self._proxy_url = proxy_url
        self._session_id: str | None = None
        self._http = httpx.Client(http1=True, http2=False, timeout=120.0)

    def initialize(self) -> None:
        """Initialize the MCP session with the proxy."""
        msg = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mend-eval-runner", "version": "1.0"},
            },
        }
        resp = self._http.post(
            self._proxy_url,
            json=msg,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")

        # Send initialized notification
        self._http.post(
            self._proxy_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={
                "Content-Type": "application/json",
                **({"mcp-session-id": self._session_id} if self._session_id else {}),
            },
        )

    def ask(self, conversation_uuid: str, question: str, page_url: str | None = None) -> str:
        """
        Call ask_assistant MCP tool and return the bot's response text.
        If the session is not initialized, initializes it first.
        page_url, when provided, is passed to ask_assistant so the bot can
        apply page-aware scoping (e.g. org-level vs app/project-level).
        """
        if self._session_id is None:
            self.initialize()

        call_id = str(uuid.uuid4())
        arguments: dict = {
            "conversation_uuid": conversation_uuid,
            "content": question,
        }
        if page_url is not None:
            arguments["page_url"] = page_url

        msg = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": "ask_assistant",
                "arguments": arguments,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        resp = self._http.post(self._proxy_url, json=msg, headers=headers)
        resp.raise_for_status()
        parsed = _parse_mcp_response(resp.text)
        return _extract_tool_result_text(parsed)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
