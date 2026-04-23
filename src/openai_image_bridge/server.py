from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse, request

from .config import AppConfig, ConfigError


@dataclass
class GeneratedImage:
    raw_bytes: bytes
    b64_json: str
    mime_type: str
    revised_prompt: str | None = None

    @property
    def extension(self) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(self.mime_type, "bin")


@dataclass
class BridgeState:
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    last_cleanup_monotonic: float = 0.0


class UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", {}).get("message", "upstream error"))
        self.status_code = status_code
        self.payload = payload


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        config: AppConfig,
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.bridge_config = config
        self.bridge_state = BridgeState()


def decode_data_url(data_url: str) -> GeneratedImage:
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("unsupported image payload")
    header, payload = data_url.split(",", 1)
    mime_type = "image/png"
    if ";" in header:
        mime_type = header[5:].split(";", 1)[0] or mime_type
    try:
        raw = base64.b64decode(payload, validate=True)
    except binascii.Error as exc:
        raise ValueError("invalid base64 image payload") from exc
    return GeneratedImage(raw_bytes=raw, b64_json=payload, mime_type=mime_type)


def extract_generated_images(upstream_payload: dict[str, Any]) -> list[GeneratedImage]:
    choices = upstream_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return []

    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []

    generated: list[GeneratedImage] = []
    revised_prompt = message.get("revised_prompt")

    raw_images = message.get("images")
    if isinstance(raw_images, list):
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            data_url = item.get("image_url", {}).get("url")
            if isinstance(data_url, str):
                generated_image = decode_data_url(data_url)
                generated_image.revised_prompt = item.get("revised_prompt") or revised_prompt
                generated.append(generated_image)

    content_items = message.get("content")
    if isinstance(content_items, list):
        for item in content_items:
            if not isinstance(item, dict):
                continue
            data_url: str | None = None
            if item.get("type") == "image_url":
                data_url = item.get("image_url", {}).get("url")
            elif item.get("type") == "output_image":
                data_url = item.get("image_url")
            if isinstance(data_url, str):
                generated_image = decode_data_url(data_url)
                generated_image.revised_prompt = item.get("revised_prompt") or revised_prompt
                generated.append(generated_image)

    return generated


def aggregate_usage(current: dict[str, int], new_usage: dict[str, Any]) -> dict[str, int]:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = new_usage.get(key)
        if isinstance(value, int):
            current[key] = current.get(key, 0) + value
    return current


def create_server(config: AppConfig) -> BridgeHTTPServer:
    return BridgeHTTPServer((config.host, config.port), ImageBridgeHandler, config)


def serve(config: AppConfig) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    server = create_server(config)
    print(
        f"openai-image-bridge listening on http://{server.server_address[0]}:{server.server_address[1]}"
    )
    server.serve_forever()


class ImageBridgeHandler(BaseHTTPRequestHandler):
    server_version = "openai-image-bridge/0.1.0"

    @property
    def config(self) -> AppConfig:
        return self.server.bridge_config  # type: ignore[attr-defined]

    @property
    def state(self) -> BridgeState:
        return self.server.bridge_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            "%s - - [%s] %s"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=2592000")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON request body") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _effective_base_url(self) -> str:
        if self.config.public_base_url:
            return self.config.public_base_url.rstrip("/")
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        if not host:
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"{proto}://{host}".rstrip("/")

    def _build_file_url(self, filename: str) -> str:
        return f"{self._effective_base_url()}{self.config.file_url_path}/{filename}"

    def _maybe_cleanup_files(self) -> None:
        if not self.config.persist_images or self.config.cleanup_max_age_seconds <= 0:
            return

        now_monotonic = time.monotonic()
        with self.state.cleanup_lock:
            if (
                now_monotonic - self.state.last_cleanup_monotonic
                < self.config.cleanup_sweep_interval_seconds
            ):
                return
            self.state.last_cleanup_monotonic = now_monotonic

            cutoff = time.time() - self.config.cleanup_max_age_seconds
            for path in self.config.output_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except FileNotFoundError:
                    continue

    def _persist_image(self, image: GeneratedImage) -> str:
        if not self.config.persist_images:
            raise ValueError("image persistence is disabled")
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_cleanup_files()
        filename = f"{int(time.time())}-{uuid.uuid4().hex}.{image.extension}"
        output_path = self.config.output_dir / filename
        output_path.write_bytes(image.raw_bytes)
        return filename

    def _serve_generated_file(self) -> None:
        prefix = self.config.file_url_path + "/"
        if not self.path.startswith(prefix):
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"message": "not found", "type": "invalid_request_error"}},
            )
            return

        relative_name = parse.unquote(self.path[len(prefix) :])
        if "/" in relative_name or "\\" in relative_name or not relative_name:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "message": "invalid file path",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        target_path = (self.config.output_dir / relative_name).resolve()
        try:
            target_path.relative_to(self.config.output_dir.resolve())
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "message": "invalid file path",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        if not target_path.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"message": "file not found", "type": "invalid_request_error"}},
            )
            return

        content_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
        self._send_bytes(HTTPStatus.OK, target_path.read_bytes(), content_type)

    def _resolve_response_format(self, requested: Any) -> str:
        if requested is None or str(requested).strip() == "":
            return self.config.default_response_format
        response_format = str(requested).strip().lower()
        if response_format not in {"b64_json", "url"}:
            raise ValueError("response_format must be either 'b64_json' or 'url'")
        return response_format

    def _compose_prompt(self, prompt: str) -> str:
        parts = []
        if self.config.prompt_prefix:
            parts.append(self.config.prompt_prefix)
        parts.append(prompt)
        if self.config.prompt_suffix:
            parts.append(self.config.prompt_suffix)
        return "\n".join(part for part in parts if part).strip()

    def _build_upstream_payload(
        self,
        prompt: str,
        upstream_model: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if self.config.system_prompt:
            messages.append({"role": "system", "content": self.config.system_prompt})
        messages.append({"role": "user", "content": self._compose_prompt(prompt)})

        payload: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            "stream": False,
        }
        if self.config.forward_user_field and isinstance(request_payload.get("user"), str):
            payload["user"] = request_payload["user"]
        payload.update(self.config.upstream_extra_body)
        return payload

    def _call_upstream(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.config.upstream_auth_header:
            headers["Authorization"] = self.config.upstream_auth_header
        elif self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]

        upstream_req = request.Request(
            self.config.upstream_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(
                upstream_req, timeout=self.config.upstream_timeout_seconds
            ) as upstream_resp:
                return json.loads(upstream_resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {
                    "error": {
                        "message": exc.reason or "upstream error",
                        "type": "upstream_error",
                    }
                }
            raise UpstreamHTTPError(exc.code, payload) from exc

    def _build_image_object(
        self,
        image: GeneratedImage,
        response_format: str,
        original_prompt: str,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {}

        should_include_b64 = response_format == "b64_json" or self.config.always_include_b64_json
        if should_include_b64:
            item["b64_json"] = image.b64_json

        should_include_url = response_format == "url" or self.config.always_include_url
        if should_include_url:
            if not self.config.persist_images:
                if response_format == "url":
                    raise ValueError(
                        "response_format=url requires PERSIST_IMAGES=true"
                    )
            else:
                filename = self._persist_image(image)
                item["url"] = self._build_file_url(filename)

        item["revised_prompt"] = image.revised_prompt or original_prompt
        return item

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path.startswith(self.config.file_url_path + "/"):
            self._serve_generated_file()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"message": "not found", "type": "invalid_request_error"}},
        )

    def do_HEAD(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path.startswith(self.config.file_url_path + "/"):
            self._serve_generated_file()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"message": "not found", "type": "invalid_request_error"}},
        )

    def do_POST(self) -> None:
        if self.path != "/v1/images/generations":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"message": "not found", "type": "invalid_request_error"}},
            )
            return

        try:
            payload = self._read_json()
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("prompt is required")

            image_count = int(payload.get("n", 1))
            if image_count < 1:
                raise ValueError("n must be >= 1")

            requested_model = str(
                payload.get("model") or self.config.default_public_model
            )
            requested_size = payload.get("size")
            if requested_size is not None and not isinstance(requested_size, str):
                raise ValueError("size must be a string when provided")

            resolved_model = self.config.resolve_model(requested_model, requested_size)
            response_format = self._resolve_response_format(payload.get("response_format"))

            collected_images: list[GeneratedImage] = []
            aggregated_usage: dict[str, int] = {}

            while len(collected_images) < image_count:
                upstream_payload = self._build_upstream_payload(
                    prompt=prompt.strip(),
                    upstream_model=resolved_model.upstream_model,
                    request_payload=payload,
                )
                upstream_response = self._call_upstream(upstream_payload)
                new_images = extract_generated_images(upstream_response)
                if not new_images:
                    raise ValueError("upstream returned no images")
                collected_images.extend(new_images)
                usage = upstream_response.get("usage")
                if isinstance(usage, dict):
                    aggregate_usage(aggregated_usage, usage)

            data = [
                self._build_image_object(image, response_format, prompt.strip())
                for image in collected_images[:image_count]
            ]
            response_payload: dict[str, Any] = {
                "created": int(time.time()),
                "data": data,
            }
            if aggregated_usage:
                response_payload["usage"] = aggregated_usage
            self._send_json(HTTPStatus.OK, response_payload)
        except ConfigError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
            )
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
            )
        except UpstreamHTTPError as exc:
            self._send_json(exc.status_code, exc.payload)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"message": str(exc), "type": "server_error"}},
            )
