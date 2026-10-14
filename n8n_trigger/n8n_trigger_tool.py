"""
title: n8n-Workflow auslösen
author: completax
version: 0.1.0
description: Löst freigegebene n8n-Workflows über ihren Webhook aus und gibt das Ergebnis zurück. Nur Workflows aus der Freigabeliste sind erlaubt, jeder Aufruf trägt einen Idempotenz-Schlüssel.
requirements: httpx
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_WORKFLOW_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RETRYABLE_STATUS = {502, 503, 504}

# ---------------------------------------------------------------------------
# Kern ohne Open-WebUI-Abhängigkeit
# ---------------------------------------------------------------------------


def parse_allowlist(text: str) -> dict[str, str]:
    """Freigabeliste: eine Zeile je Workflow im Format 'name = Beschreibung' oder nur 'name'."""
    allowed: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, description = line.partition("=")
        name = name.strip().lower()
        if _WORKFLOW_NAME.match(name):
            allowed[name] = description.strip()
    return allowed


def validate_workflow(name: str, allowed: dict[str, str]) -> str:
    cleaned = (name or "").strip().lower()
    if not _WORKFLOW_NAME.match(cleaned):
        raise ValueError("Ungültiger Workflow-Name. Erlaubt sind Kleinbuchstaben, Ziffern, Bindestrich und Unterstrich.")
    if cleaned not in allowed:
        if not allowed:
            raise PermissionError("Es sind keine Workflows freigegeben. ALLOWED_WORKFLOWS in den Tool-Einstellungen pflegen.")
        raise PermissionError(f"Workflow '{cleaned}' ist nicht freigegeben. Freigegeben: {', '.join(sorted(allowed))}")
    return cleaned


def parse_parameters(parameters_json: str, max_chars: int) -> dict:
    text = (parameters_json or "").strip() or "{}"
    if len(text) > max_chars:
        raise ValueError(f"Parameter zu groß ({len(text)} Zeichen, erlaubt {max_chars}).")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"parameters_json ist kein gültiges JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("parameters_json muss ein JSON-Objekt sein, z. B. {\"mandant\": \"M-100\"}.")
    return parsed


def build_url(base_url: str, path_prefix: str, workflow: str, test_mode: bool) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise ValueError("N8N_BASE_URL muss mit http:// oder https:// beginnen.")
    prefix = (path_prefix or "webhook").strip("/")
    if test_mode:
        prefix = f"{prefix}-test"
    return f"{base}/{prefix}/{workflow}"


def summarize_response(status: int, content_type: str, body: str, max_chars: int) -> dict:
    """Antwort für das Modell aufbereiten: JSON wenn möglich, sonst Text, immer begrenzt."""
    summary: dict[str, Any] = {"http_status": status}
    text = body or ""
    if "json" in (content_type or "").lower() or text[:1] in "{[":
        try:
            parsed = json.loads(text)
            serialized = json.dumps(parsed, ensure_ascii=False)
            if len(serialized) > max_chars:
                summary["result"] = serialized[:max_chars]
                summary["truncated"] = True
            else:
                summary["result"] = parsed
            return summary
        except json.JSONDecodeError:
            pass
    summary["result"] = text[:max_chars]
    summary["truncated"] = len(text) > max_chars
    return summary


async def call_webhook(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout_seconds: float,
    max_retries: int,
    client_factory: Any = None,
) -> tuple[int, str, str]:
    """POST mit begrenzten Wiederholungen bei 502/503/504 oder Verbindungsfehlern.

    Wiederholt wird nur mit demselben Idempotenz-Schlüssel, damit n8n Duplikate erkennen kann.
    Gibt (Status, Content-Type, Body) zurück.
    """
    import httpx

    factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False))
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            async with factory() as client:
                response = await client.post(url, json=payload, headers=headers)
            if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            return response.status_code, response.headers.get("content-type", ""), response.text
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            last_error = exc
            if attempt < max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
    raise RuntimeError(f"n8n nicht erreichbar ({type(last_error).__name__ if last_error else 'unbekannt'}).")


# ---------------------------------------------------------------------------
# Open-WebUI-Tool
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        N8N_BASE_URL: str = Field(
            default="http://host.docker.internal:5678",
            description="Basis-URL von n8n aus Sicht des Open-WebUI-Containers.",
        )
        WEBHOOK_PATH_PREFIX: str = Field(default="webhook", description="Pfadpräfix der Webhooks, Standard 'webhook'.")
        AUTH_HEADER_NAME: str = Field(default="X-Webhook-Token", description="Header für die Header-Auth des Webhook-Nodes.")
        AUTH_TOKEN: str = Field(default="", description="Geheimer Wert für den Header. Leer = kein Header (nur für lokale Tests).")
        ALLOWED_WORKFLOWS: str = Field(
            default="",
            description="Freigabeliste, eine Zeile je Workflow: name = Beschreibung. Nur diese Namen darf das Modell aufrufen.",
        )
        TIMEOUT_SECONDS: int = Field(default=60, description="Zeitlimit je Aufruf.")
        MAX_RETRIES: int = Field(default=0, description="Wiederholungen bei 502/503/504 oder Verbindungsfehler. 0 = keine.")
        MAX_PARAMETER_CHARS: int = Field(default=20_000, description="Obergrenze für parameters_json.")
        MAX_RESPONSE_CHARS: int = Field(default=20_000, description="Obergrenze für die Antwort an das Modell.")
        TEST_MODE: bool = Field(default=False, description="True = 'webhook-test'-Pfad von n8n (Workflow im Editor auf 'Listen').")

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def list_workflows(self, __user__: Optional[dict] = None) -> str:
        """
        Listet die freigegebenen n8n-Workflows mit Beschreibung. Nutze dieses Tool, bevor du einen Workflow auslöst, und immer wenn der Nutzer nach n8n, Workflows, Webhooks oder Automatisierungen in n8n fragt. Nicht verwechseln mit geplanten Chat-Automatisierungen von Open WebUI.

        :return: JSON mit Namen und Beschreibungen der freigegebenen Workflows.
        """
        allowed = parse_allowlist(self.valves.ALLOWED_WORKFLOWS)
        return json.dumps(
            {
                "status": "success",
                "workflows": [{"name": name, "description": desc} for name, desc in sorted(allowed.items())],
                "count": len(allowed),
                "hint": "" if allowed else "Keine Workflows freigegeben. Admin muss ALLOWED_WORKFLOWS in den Tool-Einstellungen pflegen.",
            },
            ensure_ascii=False,
        )

    async def run_workflow(
        self,
        workflow: str,
        parameters_json: str = "{}",
        __user__: Optional[dict] = None,
        __event_emitter__: Any = None,
        __chat_id__: Optional[str] = None,
    ) -> str:
        """
        Löst einen freigegebenen n8n-Workflow aus und gibt dessen Antwort zurück. Nutze dieses Tool nur für Workflows aus list_workflows. Übergib Parameter als JSON-Objekt. Löse einen Workflow nicht mehrfach aus, wenn der erste Aufruf eine Antwort geliefert hat.

        :param workflow: Name des Workflows, wie in list_workflows angegeben.
        :param parameters_json: Parameter als JSON-Objekt, z. B. {"mandant": "M-100", "jahr": 2025}. Leer = keine Parameter.
        :return: JSON mit HTTP-Status, Antwort des Workflows und dem verwendeten Idempotenz-Schlüssel.
        """
        started = time.monotonic()
        if not __user__ or not __user__.get("id"):
            return self._fail("Kein Nutzerkontext vorhanden. Abgebrochen.")
        try:
            allowed = parse_allowlist(self.valves.ALLOWED_WORKFLOWS)
            name = validate_workflow(workflow, allowed)
            parameters = parse_parameters(parameters_json, self.valves.MAX_PARAMETER_CHARS)
            url = build_url(self.valves.N8N_BASE_URL, self.valves.WEBHOOK_PATH_PREFIX, name, self.valves.TEST_MODE)
        except (ValueError, PermissionError) as exc:
            return self._fail(str(exc))

        idempotency_key = str(uuid.uuid4())
        payload = {
            "workflow": name,
            "parameters": parameters,
            "idempotency_key": idempotency_key,
            "requested_by": {"user_id": __user__["id"], "chat_id": __chat_id__ or ""},
            "source": "open-webui",
        }
        headers = {
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
            "X-Request-Source": "open-webui",
        }
        if self.valves.AUTH_TOKEN:
            headers[self.valves.AUTH_HEADER_NAME.strip() or "X-Webhook-Token"] = self.valves.AUTH_TOKEN

        await self._status(__event_emitter__, f"Starte Workflow {name} …")
        try:
            status, content_type, body = await call_webhook(
                url, payload, headers, float(self.valves.TIMEOUT_SECONDS), int(self.valves.MAX_RETRIES)
            )
        except Exception as exc:
            log.warning("n8n_trigger: workflow=%s failed (%s)", name, type(exc).__name__)
            await self._status(__event_emitter__, "Workflow nicht erreichbar.", done=True)
            return self._fail(str(exc) if isinstance(exc, RuntimeError) else f"Aufruf fehlgeschlagen: {type(exc).__name__}")

        seconds = round(time.monotonic() - started, 1)
        log.info("n8n_trigger: workflow=%s status=%s seconds=%s", name, status, seconds)
        summary = summarize_response(status, content_type, body, self.valves.MAX_RESPONSE_CHARS)
        ok = 200 <= status < 300
        await self._status(__event_emitter__, "Workflow fertig." if ok else f"Workflow antwortete mit {status}.", done=True)
        return json.dumps(
            {
                "status": "success" if ok else "error",
                "workflow": name,
                "idempotency_key": idempotency_key,
                "seconds": seconds,
                **summary,
                "instruction_for_assistant": (
                    "Ergebnis zusammenfassen. Bei Fehlerstatus den Nutzer informieren und nicht automatisch erneut auslösen."
                ),
            },
            ensure_ascii=False,
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _fail(message: str) -> str:
        return json.dumps({"status": "error", "message": message}, ensure_ascii=False)

    @staticmethod
    async def _status(emitter: Any, description: str, done: bool = False) -> None:
        if not emitter:
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception:
            pass
