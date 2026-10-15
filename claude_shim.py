"""Локальный OpenAI-совместимый /v1/chat/completions поверх `claude -p`.

Позволяет hh-applicant-tool генерировать письма через подписку Claude
без API-ключа. Слушает только 127.0.0.1.

Запуск: python3 claude_shim.py [--port 8765] [--model sonnet]
"""

import argparse
import json
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Пустая папка, чтобы claude не подхватывал CLAUDE.md и прочий контекст
WORKDIR = Path(__file__).parent / ".shim-cwd"
TIMEOUT = 180


def content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    # Формат с частями: берём только текст, картинки не поддерживаются
    return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")


def run_claude(messages: list[dict], model: str) -> str:
    system = "\n\n".join(
        content_to_text(m["content"]) for m in messages if m["role"] == "system"
    )
    prompt = "\n\n".join(
        content_to_text(m["content"]) for m in messages if m["role"] != "system"
    )
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--output-format", "json",
    ]
    if system:
        cmd += ["--system-prompt", system]
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        cwd=WORKDIR, timeout=TIMEOUT,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr or proc.stdout}"[:500])
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result") or data)[:500])
    return data["result"]


class Handler(BaseHTTPRequestHandler):
    model = "sonnet"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = run_claude(req["messages"], self.model)
        except Exception as ex:
            self.log_message("error: %s", ex)
            self._send(500, {"error": {"message": str(ex)}})
            return
        self._send(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="sonnet")
    args = parser.parse_args()
    WORKDIR.mkdir(exist_ok=True)
    Handler.model = args.model
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"claude shim: http://127.0.0.1:{args.port}/v1/chat/completions (model={args.model})")
    server.serve_forever()


if __name__ == "__main__":
    main()
