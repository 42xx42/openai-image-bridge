from __future__ import annotations

import base64
import http.client
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
        self.response_delay_seconds = 0.0


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
        if self.server.response_delay_seconds > 0:
            time.sleep(self.server.response_delay_seconds)

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
    def _get_json(self, url: str) -> dict[str, object]:
        with request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

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

    def test_async_placeholder_model_returns_placeholder_then_final_image(self) -> None:
        upstream_server = UpstreamStubServer(("127.0.0.1", 0))
        upstream_server.response_delay_seconds = 0.25
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
                default_response_format="url",
                async_placeholder_model_suffix="-async",
            )
            bridge_server = create_server(bridge_config)
            bridge_thread = ServerThread(bridge_server).start()

            try:
                bridge_port = bridge_server.server_address[1]
                response = self._json_request(
                    f"http://127.0.0.1:{bridge_port}/v1/images/generations",
                    {
                        "model": "gpt-image-2-async",
                        "prompt": "Draw a red apple",
                        "response_format": "url",
                    },
                    headers={"Authorization": "Bearer test-key"},
                )

                self.assertEqual(len(response["data"]), 1)
                placeholder_url = response["data"][0]["url"]

                with request.urlopen(placeholder_url, timeout=30) as placeholder_response:
                    placeholder_body = placeholder_response.read().decode("utf-8")
                    self.assertEqual(
                        placeholder_response.headers["X-OpenAI-Image-Status"],
                        "pending",
                    )
                self.assertIn("Generating image", placeholder_body)

                final_headers = None
                final_body = b""
                for _ in range(40):
                    with request.urlopen(placeholder_url, timeout=30) as final_response:
                        final_headers = final_response.headers
                        final_body = final_response.read()
                    if final_headers["X-OpenAI-Image-Status"] == "completed":
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(final_headers)
                self.assertEqual(final_headers["X-OpenAI-Image-Status"], "completed")
                self.assertEqual(final_body, PNG_BYTES)
                self.assertEqual(len(upstream_server.requests), 1)
                self.assertEqual(
                    upstream_server.requests[0]["payload"]["model"],
                    "gpt-draw-1024x1024",
                )
            finally:
                bridge_thread.stop()

        upstream_thread.stop()

    def test_models_endpoint_lists_suffix_variants_for_all_models(self) -> None:
        bridge_config = AppConfig(
            host="127.0.0.1",
            port=0,
            upstream_url="http://127.0.0.1:65535/v1/chat/completions",
            model_map={
                "gpt-image-2": ModelMapping(
                    public_model="gpt-image-2",
                    upstream_model="gpt-draw-1024x1024",
                ),
                "gpt-image-2-1024x1536": ModelMapping(
                    public_model="gpt-image-2-1024x1536",
                    upstream_model="gpt-draw-1024x1536",
                ),
            },
            async_placeholder_model_suffix="-async",
            heartbeat_model_suffix="-hb",
        )
        bridge_server = create_server(bridge_config)
        bridge_thread = ServerThread(bridge_server).start()

        try:
            bridge_port = bridge_server.server_address[1]
            response = self._get_json(f"http://127.0.0.1:{bridge_port}/v1/models")
            model_ids = [item["id"] for item in response["data"]]
            self.assertEqual(
                model_ids,
                [
                    "gpt-image-2",
                    "gpt-image-2-async",
                    "gpt-image-2-hb",
                    "gpt-image-2-1024x1536",
                    "gpt-image-2-1024x1536-async",
                    "gpt-image-2-1024x1536-hb",
                ],
            )
        finally:
            bridge_thread.stop()

    def test_heartbeat_model_streams_whitespace_before_final_json(self) -> None:
        upstream_server = UpstreamStubServer(("127.0.0.1", 0))
        upstream_server.response_delay_seconds = 0.25
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
                always_include_url=False,
                default_response_format="b64_json",
                heartbeat_model_suffix="-hb",
                heartbeat_interval_seconds=0.05,
            )
            bridge_server = create_server(bridge_config)
            bridge_thread = ServerThread(bridge_server).start()

            try:
                bridge_port = bridge_server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", bridge_port, timeout=30)
                started = time.monotonic()
                conn.request(
                    "POST",
                    "/v1/images/generations",
                    body=json.dumps(
                        {
                            "model": "gpt-image-2-hb",
                            "prompt": "Draw a red apple",
                        }
                    ),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-key",
                    },
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.getheader("X-OpenAI-Image-Mode"),
                    "heartbeat-stream",
                )
                self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")

                first_bytes = response.read(2)
                first_byte_elapsed = time.monotonic() - started
                self.assertEqual(first_bytes, b" \n")
                self.assertLess(first_byte_elapsed, 0.2)

                full_body = first_bytes + response.read()
                parsed = json.loads(full_body.decode("utf-8"))
                self.assertEqual(parsed["data"][0]["b64_json"], PNG_B64)
                self.assertEqual(len(upstream_server.requests), 1)
            finally:
                conn.close()
                bridge_thread.stop()

        upstream_thread.stop()


if __name__ == "__main__":
    unittest.main()
