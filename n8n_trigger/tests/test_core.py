"""Tests für n8n_trigger mit einem lokalen Mini-Webserver statt echtem n8n. Alle Inhalte erfunden.

Ausführen:
    python n8n_trigger/tests/test_core.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import n8n_trigger_tool as t  # noqa: E402

TOKEN = "test-token-nur-fuer-tests"
STATE = {"calls": [], "fail_first": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # still
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE["calls"].append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.headers.get("X-Webhook-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        if STATE["fail_first"] > 0:
            STATE["fail_first"] -= 1
            self.send_response(503)
            self.end_headers()
            return
        if self.path.endswith("/text"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(("x" * 50).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"echo": body["parameters"], "key": body["idempotency_key"]}).encode())


def start_server() -> str:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


BASE = start_server()


def make_tool(**overrides) -> t.Tools:
    tool = t.Tools()
    values = {
        "N8N_BASE_URL": BASE,
        "AUTH_TOKEN": TOKEN,
        "ALLOWED_WORKFLOWS": "echo = Gibt Parameter zurück\ntext = Antwortet mit Text\n# kommentar\nUngültig Name = x",
        **overrides,
    }
    tool.valves = t.Tools.Valves(**values)
    return tool


USER = {"id": "user-1"}


def run(coro):
    return json.loads(asyncio.run(coro))


def test_allowlist_and_validation():
    allowed = t.parse_allowlist("a = eins\nb\n# c = drei\nBad Name = x\nd-2_x = vier")
    assert allowed == {"a": "eins", "b": "", "d-2_x": "vier"}
    assert t.validate_workflow("A", allowed) == "a"
    for bad in ("", "../x", "a b", "x" * 70):
        try:
            t.validate_workflow(bad, allowed)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    try:
        t.validate_workflow("zzz", allowed)
    except PermissionError as exc:
        assert "nicht freigegeben" in str(exc)
    else:
        raise AssertionError("expected PermissionError")
    try:
        t.validate_workflow("a", {})
    except PermissionError as exc:
        assert "keine Workflows" in str(exc)


def test_parameters_and_url():
    assert t.parse_parameters("", 100) == {}
    assert t.parse_parameters('{"a": 1}', 100) == {"a": 1}
    for bad in ("[1]", "nicht json", "x" * 200):
        try:
            t.parse_parameters(bad, 100)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert t.build_url("http://n8n:5678/", "webhook", "echo", False) == "http://n8n:5678/webhook/echo"
    assert t.build_url("http://n8n:5678", "/webhook/", "echo", True) == "http://n8n:5678/webhook-test/echo"
    try:
        t.build_url("n8n:5678", "webhook", "echo", False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_list_workflows():
    result = run(make_tool().list_workflows(__user__=USER))
    assert [w["name"] for w in result["workflows"]] == ["echo", "text"]
    assert result["workflows"][0]["description"] == "Gibt Parameter zurück"
    empty = run(make_tool(ALLOWED_WORKFLOWS="").list_workflows(__user__=USER))
    assert empty["count"] == 0 and "Keine Workflows" in empty["hint"]


def test_run_workflow_success_with_headers_and_idempotency():
    STATE["calls"].clear()
    result = run(make_tool().run_workflow("echo", '{"mandant": "M-100"}', __user__=USER, __chat_id__="chat-9"))
    assert result["status"] == "success" and result["http_status"] == 200
    assert result["result"]["echo"] == {"mandant": "M-100"}
    assert result["result"]["key"] == result["idempotency_key"]
    call = STATE["calls"][-1]
    assert call["path"] == "/webhook/echo"
    assert call["headers"]["X-Idempotency-Key"] == result["idempotency_key"]
    assert call["body"]["requested_by"] == {"user_id": "user-1", "chat_id": "chat-9"}
    assert "email" not in json.dumps(call["body"]), "nur die Nutzer-ID geht an n8n"


def test_run_workflow_denied_without_allowlist_or_user():
    result = run(make_tool().run_workflow("other", "{}", __user__=USER))
    assert result["status"] == "error" and "nicht freigegeben" in result["message"]
    result = run(make_tool().run_workflow("echo", "{}", __user__=None))
    assert result["status"] == "error" and "Nutzerkontext" in result["message"]
    result = run(make_tool().run_workflow("echo", "[1,2]", __user__=USER))
    assert result["status"] == "error" and "JSON-Objekt" in result["message"]


def test_wrong_token_is_reported_not_retried():
    STATE["calls"].clear()
    result = run(make_tool(AUTH_TOKEN="falsch", MAX_RETRIES=2).run_workflow("echo", "{}", __user__=USER))
    assert result["status"] == "error" and result["http_status"] == 401
    assert len(STATE["calls"]) == 1


def test_retry_on_503_keeps_same_idempotency_key():
    STATE["calls"].clear()
    STATE["fail_first"] = 1
    result = run(make_tool(MAX_RETRIES=1).run_workflow("echo", "{}", __user__=USER))
    assert result["status"] == "success"
    keys = {c["headers"]["X-Idempotency-Key"] for c in STATE["calls"]}
    assert len(STATE["calls"]) == 2 and len(keys) == 1

    STATE["calls"].clear()
    STATE["fail_first"] = 1
    result = run(make_tool(MAX_RETRIES=0).run_workflow("echo", "{}", __user__=USER))
    assert result["status"] == "error" and result["http_status"] == 503 and len(STATE["calls"]) == 1


def test_text_response_is_truncated():
    result = run(make_tool(MAX_RESPONSE_CHARS=10).run_workflow("text", "{}", __user__=USER))
    assert result["result"] == "x" * 10 and result["truncated"] is True


def test_unreachable_server():
    result = run(make_tool(N8N_BASE_URL="http://127.0.0.1:9", TIMEOUT_SECONDS=2).run_workflow("echo", "{}", __user__=USER))
    assert result["status"] == "error" and "nicht erreichbar" in result["message"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
