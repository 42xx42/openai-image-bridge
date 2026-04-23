from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from openai_image_bridge.config import AppConfig, ModelMapping
from openai_image_bridge.server import create_server


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2uoAAAAASUVORK5CYII="
PNG_BYTES = base64.b64decode(PNG_B64)


class ServerThread:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def start(self) -> "ServerThread":
        self.thread.start()
        time.sleep(0.05)
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class UpstreamStubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, UpstreamStubHandler)
        self.requests: list[dict[str, object]] = []


class UpstreamStubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
                "payload": payload,
            }
        )

        response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-draw-1024x1024",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{PNG_B64}"
                                },
                            }
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        }
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeTests(unittest.TestCase):
    def _json_request(
        self,
        url: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_bridges_image_generation_and_serves_persisted_file(self) -> None:
        upstream_server = UpstreamStubServer(("127.0.0.1", 0))
        upstream_thread = ServerThread(upstream_server).start()

        with tempfile.TemporaryDirectory() as temp_dir:
            bridge_config = AppConfig(
                host="127.0.0.1",
                port=0,
                upstream_url=(
                    f"http://127.0.0.1:{upstream_server.server_address[1]}"
                    "/v1/chat/completions"
                ),
                model_map={
                    "gpt-image-2": ModelMapping(
                        public_model="gpt-image-2",
                        upstream_model="gpt-draw-1024x1024",
                    )
                },
                persist_images=True,
                output_dir=Path(temp_dir).resolve(),
                always_include_url=True,
                default_response_format="b64_json",
            )
            bridge_server = create_server(bridge_config)
            bridge_thread = ServerThread(bridge_server).start()

            try:
                bridge_port = bridge_server.server_address[1]
                response = self._json_request(
                    f"http://127.0.0.1:{bridge_port}/v1/images/generations",
                    {
                        "model": "gpt-image-2",
                        "prompt": "Draw a red apple",
                    },
                    headers={"Authorization": "Bearer test-key"},
                )

                self.assertIn("data", response)
                self.assertEqual(len(response["data"]), 1)

                item = response["data"][0]
                self.assertEqual(item["b64_json"], PNG_B64)
                self.assertIn("url", item)
                self.assertEqual(response["usage"]["total_tokens"], 30)

                with request.urlopen(item["url"], timeout=30) as file_response:
                    file_bytes = file_response.read()
                self.assertEqual(file_bytes, PNG_BYTES)

                self.assertEqual(len(upstream_server.requests), 1)
                upstream_request = upstream_server.requests[0]
                self.assertEqual(
                    upstream_request["payload"]["model"],
                    "gpt-draw-1024x1024",
                )
                self.assertEqual(
                    upstream_request["headers"]["Authorization"],
                    "Bearer test-key",
                )
            finally:
                bridge_thread.stop()

        upstream_thread.stop()

    def test_response_format_url_requires_persistence(self) -> None:
        upstream_server = UpstreamStubServer(("127.0.0.1", 0))
        upstream_thread = ServerThread(upstream_server).start()

        bridge_config = AppConfig(
            host="127.0.0.1",
            port=0,
            upstream_url=(
                f"http://127.0.0.1:{upstream_server.server_address[1]}"
                "/v1/chat/completions"
            ),
            model_map={
                "gpt-image-2": ModelMapping(
                    public_model="gpt-image-2",
                    upstream_model="gpt-draw-1024x1024",
                )
            },
            persist_images=False,
            always_include_url=False,
        )
        bridge_server = create_server(bridge_config)
        bridge_thread = ServerThread(bridge_server).start()

        try:
            bridge_port = bridge_server.server_address[1]
            with self.assertRaises(error.HTTPError) as raised:
                self._json_request(
                    f"http://127.0.0.1:{bridge_port}/v1/images/generations",
                    {
                        "model": "gpt-image-2",
                        "prompt": "Draw a red apple",
                        "response_format": "url",
                    },
                )
            self.assertEqual(raised.exception.code, 400)
            error_body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertIn("PERSIST_IMAGES", error_body["error"]["message"])
        finally:
            bridge_thread.stop()
            upstream_thread.stop()


if __name__ == "__main__":
    unittest.main()
