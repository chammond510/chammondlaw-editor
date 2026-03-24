#!/usr/bin/env python3
import json
import os
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HOST = os.environ.get("WORD_ADDIN_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("WORD_ADDIN_BRIDGE_PORT", "8765"))
CODEX_BIN = os.environ.get("WORD_ADDIN_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("WORD_ADDIN_CODEX_MODEL", "gpt-5.4")
CODEX_REASONING = os.environ.get("WORD_ADDIN_CODEX_REASONING", "medium")
CODEX_CWD = os.environ.get("WORD_ADDIN_CODEX_CWD", "/tmp")
JOB_TIMEOUT_SECONDS = int(os.environ.get("WORD_ADDIN_CODEX_TIMEOUT", "480"))
TLS_CERT_FILE = os.environ.get("WORD_ADDIN_BRIDGE_CERT_FILE", "")
TLS_KEY_FILE = os.environ.get("WORD_ADDIN_BRIDGE_KEY_FILE", "")

ALLOWED_ORIGIN_HOSTS = {
    "localhost",
    "127.0.0.1",
    "editor.chammondlaw.com",
}


def _allow_origin(origin: str) -> str:
    parsed = urlparse(origin or "")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    if host in ALLOWED_ORIGIN_HOSTS or host.endswith(".onrender.com"):
        return origin
    return ""


def _read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def _write_json(handler, status_code, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status_code)
    origin = _allow_origin(handler.headers.get("Origin", ""))
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    if handler.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
        handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _chat_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answer": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string"},
                        "title": {"type": "string"},
                        "citation": {"type": "string"},
                        "document_id": {"type": ["integer", "null"]},
                    },
                    "required": ["kind", "title", "citation", "document_id"],
                },
            },
        },
        "required": ["answer", "citations"],
    }


def _suggest_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selection_summary": {"type": "string"},
            "draft_gap": {"type": "string"},
            "authorities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string"},
                        "title": {"type": "string"},
                        "citation": {"type": "string"},
                        "document_id": {"type": ["integer", "null"]},
                        "precedential_status": {"type": "string"},
                        "validity_status": {"type": "string"},
                        "relevance": {"type": "string"},
                        "suggested_use": {"type": "string"},
                        "pinpoint": {"type": "string"},
                    },
                    "required": [
                        "kind",
                        "title",
                        "citation",
                        "document_id",
                        "precedential_status",
                        "validity_status",
                        "relevance",
                        "suggested_use",
                        "pinpoint",
                    ],
                },
            },
            "search_notes": {"type": "string"},
            "next_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "selection_summary",
            "draft_gap",
            "authorities",
            "search_notes",
            "next_questions",
        ],
    }


def _chat_prompt(payload):
    transcript = payload.get("transcript") or []
    transcript_text = "\n".join(
        f"{item.get('role', 'user')}: {str(item.get('content') or '').strip()}"
        for item in transcript[-8:]
        if str(item.get("content") or "").strip()
    )
    return "\n\n".join(
        block
        for block in [
            "You are Hammond Law's Word-side immigration research assistant.",
            "Use the biaedge_mcp tools first for cases, validity, statutes, regulations, and policy.",
            "Do not use shell commands or inspect local files. Do not browse the web unless the user explicitly asks.",
            "Answer directly for a practicing immigration attorney drafting inside Microsoft Word.",
            "Keep the work bounded and interactive. Use the minimum number of BIA Edge lookups needed and prefer 1 to 3 core citations.",
            "Return valid JSON that matches the provided schema.",
            f"Document title: {str(payload.get('document_title') or '').strip()}",
            f"Document type slug: {str(payload.get('document_type_slug') or '').strip()}",
            ("Recent transcript:\n" + transcript_text) if transcript_text else "",
            ("Selected text:\n" + str(payload.get("selected_text") or "").strip()) if str(payload.get("selected_text") or "").strip() else "",
            ("Document excerpt:\n" + str(payload.get("document_excerpt") or "").strip()) if str(payload.get("document_excerpt") or "").strip() else "",
            "Attorney question:\n" + str(payload.get("message") or "").strip(),
        ]
        if block
    )


def _suggest_prompt(payload):
    return "\n\n".join(
        block
        for block in [
            "You are Hammond Law's Word-side authority suggestion assistant.",
            "Use the biaedge_mcp tools first and verify final authorities through BIA Edge tools.",
            "Do not use shell commands or inspect local files. Do not browse the web unless the user explicitly asks.",
            "Prefer precedential cases, statutes, regulations, and policy.",
            "Keep the search bounded and interactive. Return no more than 5 authorities and stop once you have the best controlling support.",
            "Return valid JSON that matches the provided schema.",
            f"Document title: {str(payload.get('document_title') or '').strip()}",
            f"Document type slug: {str(payload.get('document_type_slug') or '').strip()}",
            "Selected text:\n" + str(payload.get("selected_text") or "").strip(),
            ("Focus note:\n" + str(payload.get("focus_note") or "").strip()) if str(payload.get("focus_note") or "").strip() else "",
            ("Document excerpt:\n" + str(payload.get("document_excerpt") or "").strip()) if str(payload.get("document_excerpt") or "").strip() else "",
        ]
        if block
    )


def _run_codex(prompt_text, schema):
    with tempfile.TemporaryDirectory(prefix="word-addin-codex-") as tmpdir:
        schema_path = Path(tmpdir) / "schema.json"
        output_path = Path(tmpdir) / "output.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "-C",
            CODEX_CWD,
            "--sandbox",
            "read-only",
            "--model",
            CODEX_MODEL,
            "-c",
            f"model_reasoning_effort={json.dumps(CODEX_REASONING)}",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            timeout=JOB_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "codex exec failed").strip()
            raise RuntimeError(detail[:4000])
        if not output_path.exists():
            raise RuntimeError("Codex did not produce an output file.")
        raw_output = output_path.read_text(encoding="utf-8").strip()
        if not raw_output:
            raise RuntimeError("Codex returned an empty response.")
        result = json.loads(raw_output)
        result["model"] = CODEX_MODEL
        return result


def _health_payload():
    login_status = subprocess.run(
        [CODEX_BIN, "login", "status"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    mcp_status = subprocess.run(
        [CODEX_BIN, "mcp", "list"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    login_text = (login_status.stdout or login_status.stderr or "").strip()
    mcp_text = (mcp_status.stdout or mcp_status.stderr or "").strip()
    return {
        "ok": login_status.returncode == 0 and "Logged in" in login_text and "biaedge_mcp" in mcp_text,
        "login_status": login_text,
        "mcp_summary": mcp_text,
        "model": CODEX_MODEL,
        "reasoning": CODEX_REASONING,
        "scheme": "https" if TLS_CERT_FILE and TLS_KEY_FILE else "http",
        "tls_enabled": bool(TLS_CERT_FILE and TLS_KEY_FILE),
    }


@dataclass
class BridgeJob:
    job_id: str
    mode: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict | None = None
    error: str = ""


class JobStore:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def create(self, mode):
        job = BridgeJob(job_id=str(uuid.uuid4()), mode=mode, status="in_progress")
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def update_result(self, job_id, *, result=None, error=""):
        with self._lock:
            job = self._jobs[job_id]
            job.updated_at = time.time()
            if error:
                job.status = "failed"
                job.error = error
            else:
                job.status = "completed"
                job.result = result or {}
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)


JOBS = JobStore()


def _spawn_job(mode, payload):
    job = JOBS.create(mode)

    def worker():
        try:
            if mode == "chat":
                result = _run_codex(_chat_prompt(payload), _chat_schema())
            else:
                result = _run_codex(_suggest_prompt(payload), _suggest_schema())
            JOBS.update_result(job.job_id, result=result)
        except Exception as exc:
            JOBS.update_result(job.job_id, error=str(exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return job


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "WordAddinCodexBridge/1.0"

    def do_OPTIONS(self):
        _write_json(self, 204, {})

    def do_GET(self):
        if self.path == "/health":
            _write_json(self, 200, _health_payload())
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = JOBS.get(job_id)
            if not job:
                _write_json(self, 404, {"error": "Job not found."})
                return
            payload = {
                "job_id": job.job_id,
                "mode": job.mode,
                "status": job.status,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
            if job.result is not None:
                payload["result"] = job.result
            if job.error:
                payload["error"] = job.error
            _write_json(self, 200, payload)
            return
        _write_json(self, 404, {"error": "Not found."})

    def do_POST(self):
        if self.path not in {"/v1/chat", "/v1/suggest"}:
            _write_json(self, 404, {"error": "Not found."})
            return
        try:
            payload = _read_json(self)
        except Exception as exc:
            _write_json(self, 400, {"error": f"Invalid JSON payload: {exc}"})
            return
        mode = "chat" if self.path.endswith("/chat") else "suggest"
        if mode == "chat" and not str(payload.get("message") or "").strip():
            _write_json(self, 400, {"error": "message is required."})
            return
        if mode == "suggest" and not str(payload.get("selected_text") or "").strip():
            _write_json(self, 400, {"error": "selected_text is required."})
            return
        job = _spawn_job(mode, payload)
        _write_json(self, 202, {"job_id": job.job_id, "status": job.status})


def main():
    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    scheme = "http"
    if TLS_CERT_FILE and TLS_KEY_FILE:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"Word add-in Codex bridge listening on {scheme}://{HOST}:{PORT}")
    print(f"Using Codex model {CODEX_MODEL} from cwd {CODEX_CWD}")
    server.serve_forever()


if __name__ == "__main__":
    main()
