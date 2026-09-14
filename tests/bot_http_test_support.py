import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _chat_response(*, text="", finish_reason="stop", usage=(3,2), tool_call=False,
                   tool_name=None, tool_arguments=None, call_id="call_1"):
    message={"role":"assistant","content":text}
    if tool_call or tool_name:
        message["tool_calls"]=[{"id":call_id,"type":"function","function":{
            "name":tool_name or "runtime_status","arguments":json.dumps(tool_arguments or {})}}]
        finish_reason="tool_calls"
    return json.dumps({"choices":[{"message":message,"finish_reason":finish_reason}],
                       "usage":{"prompt_tokens":usage[0],"completion_tokens":usage[1]}}).encode()

@contextmanager
def _loopback_server(responses: list[dict]):
    requests: list[dict] = []
    scripted = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except Exception:
                payload = None
            requests.append({
                "path": self.path,
                "payload": payload,
                "has_authorization": bool(self.headers.get("Authorization")),
            })
            response = scripted.pop(0) if scripted else {
                "status": 500,
                "body": b'{"error":{"message":"unscripted"}}',
                "content_type": "application/json",
            }
            started = response.get("started")
            if started is not None:
                started.set()
            delay = float(response.get("delay", 0))
            if delay:
                time.sleep(delay)
            body = response.get("body", b"")
            if callable(body):
                body = body(payload)
            if isinstance(body, str):
                body = body.encode()
            try:
                self.send_response(int(response.get("status", 200)))
                self.send_header("Content-Type", response.get("content_type", "application/json"))
                for name, value in response.get("headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
