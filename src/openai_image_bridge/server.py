from __future__ import annotations

import base64
import binascii
import html
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
    protocol_version = "HTTP/1.1"

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

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        for header_name, header_value in (extra_headers or {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        content_type: str,
        *,
        cache_control: str = "public, max-age=2592000",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Access-Control-Allow-Origin", "*")
        for header_name, header_value in (extra_headers or {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _start_chunked_json_response(
        self,
        status: int,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Connection", "close")
        for header_name, header_value in (extra_headers or {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        if self.command == "HEAD":
            return
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _finish_chunked_response(self) -> None:
        if self.command == "HEAD":
            return
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

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

    def _request_path(self) -> str:
        return parse.urlsplit(self.path).path

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

    def _async_job_meta_path(self, job_name: str):
        return self.config.output_dir / f"{job_name}.meta.json"

    def _async_job_data_path(self, job_name: str):
        return self.config.output_dir / f"{job_name}.data"

    def _write_json_atomic(self, target_path, payload: dict[str, Any]) -> None:
        temp_path = target_path.with_name(f"{target_path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(target_path)

    def _read_json_file(self, target_path) -> dict[str, Any] | None:
        if not target_path.is_file():
            return None
        return json.loads(target_path.read_text(encoding="utf-8"))

    def _reserve_async_job(self) -> str:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_cleanup_files()
        job_name = f"job-{int(time.time())}-{uuid.uuid4().hex}"
        self._write_json_atomic(
            self._async_job_meta_path(job_name),
            {
                "status": "pending",
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            },
        )
        return job_name

    def _complete_async_job(self, job_name: str, image: GeneratedImage) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        data_path = self._async_job_data_path(job_name)
        data_path.write_bytes(image.raw_bytes)
        self._write_json_atomic(
            self._async_job_meta_path(job_name),
            {
                "status": "completed",
                "mime_type": image.mime_type,
                "data_file": data_path.name,
                "revised_prompt": image.revised_prompt,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            },
        )

    def _fail_async_job(self, job_name: str, error_message: str) -> None:
        self._write_json_atomic(
            self._async_job_meta_path(job_name),
            {
                "status": "failed",
                "error_message": error_message,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            },
        )

    def _status_svg(self, title: str, detail: str, accent: str) -> bytes:
        safe_title = html.escape(title)
        safe_detail = html.escape(detail[:160])
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-label="{safe_title}">
<rect width="1024" height="1024" fill="#0f172a"/>
<rect x="72" y="72" width="880" height="880" rx="40" fill="#111827" stroke="{accent}" stroke-width="8"/>
<circle cx="160" cy="160" r="28" fill="{accent}"/>
<text x="120" y="310" fill="#f8fafc" font-size="64" font-family="Arial, sans-serif">{safe_title}</text>
<text x="120" y="390" fill="#94a3b8" font-size="32" font-family="Arial, sans-serif">{safe_detail}</text>
</svg>"""
        return svg.encode("utf-8")

    def _serve_generated_file(self) -> None:
        prefix = self.config.file_url_path + "/"
        request_path = self._request_path()
        if not request_path.startswith(prefix):
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"message": "not found", "type": "invalid_request_error"}},
            )
            return

        relative_name = parse.unquote(request_path[len(prefix) :])
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

        content_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
        if target_path.is_file():
            self._send_bytes(HTTPStatus.OK, target_path.read_bytes(), content_type)
            return

        job_meta = self._read_json_file(self._async_job_meta_path(relative_name))
        if job_meta is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"message": "file not found", "type": "invalid_request_error"}},
            )
            return

        job_status = str(job_meta.get("status") or "pending")
        status_headers = {"X-OpenAI-Image-Status": job_status}
        if job_status == "completed":
            data_file = job_meta.get("data_file")
            mime_type = job_meta.get("mime_type")
            if not isinstance(data_file, str) or not isinstance(mime_type, str):
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": {
                            "message": "invalid async job metadata",
                            "type": "server_error",
                        }
                    },
                )
                return
            data_path = (self.config.output_dir / data_file).resolve()
            try:
                data_path.relative_to(self.config.output_dir.resolve())
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
            if not data_path.is_file():
                self._send_bytes(
                    HTTPStatus.OK,
                    self._status_svg(
                        "Image unavailable",
                        "The generated image file is missing.",
                        "#ef4444",
                    ),
                    "image/svg+xml; charset=utf-8",
                    cache_control="no-store, max-age=0",
                    extra_headers={"X-OpenAI-Image-Status": "failed"},
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                data_path.read_bytes(),
                mime_type,
                extra_headers=status_headers,
            )
            return

        if job_status == "failed":
            detail = str(job_meta.get("error_message") or "The upstream image request failed.")
            self._send_bytes(
                HTTPStatus.OK,
                self._status_svg("Image failed", detail, "#ef4444"),
                "image/svg+xml; charset=utf-8",
                cache_control="no-store, max-age=0",
                extra_headers=status_headers,
            )
            return

        self._send_bytes(
            HTTPStatus.OK,
            self._status_svg(
                "Generating image",
                "Refresh this URL in a few seconds.",
                "#38bdf8",
            ),
            "image/svg+xml; charset=utf-8",
            cache_control="no-store, max-age=0",
            extra_headers={**status_headers, "Retry-After": "3"},
        )

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

    def _call_upstream(
        self,
        payload: dict[str, Any],
        forwarded_auth_header: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.config.upstream_auth_header:
            headers["Authorization"] = self.config.upstream_auth_header
        elif forwarded_auth_header:
            headers["Authorization"] = forwarded_auth_header

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

    def _should_use_async_placeholder(
        self,
        resolved_model,
        response_format: str,
    ) -> bool:
        return bool(
            resolved_model.async_placeholder
            and response_format == "url"
            and self.config.persist_images
        )

    def _should_use_heartbeat_stream(self, resolved_model) -> bool:
        return bool(resolved_model.heartbeat_stream)

    def _run_async_generation(
        self,
        prompt: str,
        resolved_model,
        request_payload: dict[str, Any],
        job_names: list[str],
        forwarded_auth_header: str | None,
    ) -> None:
        remaining_jobs = list(job_names)
        try:
            while remaining_jobs:
                upstream_payload = self._build_upstream_payload(
                    prompt=prompt,
                    upstream_model=resolved_model.upstream_model,
                    request_payload=request_payload,
                )
                upstream_response = self._call_upstream(
                    upstream_payload,
                    forwarded_auth_header=forwarded_auth_header,
                )
                new_images = extract_generated_images(upstream_response)
                if not new_images:
                    raise ValueError("upstream returned no images")
                for image in new_images:
                    if not remaining_jobs:
                        break
                    self._complete_async_job(remaining_jobs.pop(0), image)
        except Exception as exc:
            for job_name in remaining_jobs:
                self._fail_async_job(job_name, str(exc))

    def _generate_response_payload(
        self,
        prompt: str,
        image_count: int,
        resolved_model,
        request_payload: dict[str, Any],
        response_format: str,
        forwarded_auth_header: str | None,
    ) -> dict[str, Any]:
        collected_images: list[GeneratedImage] = []
        aggregated_usage: dict[str, int] = {}

        while len(collected_images) < image_count:
            upstream_payload = self._build_upstream_payload(
                prompt=prompt,
                upstream_model=resolved_model.upstream_model,
                request_payload=request_payload,
            )
            upstream_response = self._call_upstream(
                upstream_payload,
                forwarded_auth_header=forwarded_auth_header,
            )
            new_images = extract_generated_images(upstream_response)
            if not new_images:
                raise ValueError("upstream returned no images")
            collected_images.extend(new_images)
            usage = upstream_response.get("usage")
            if isinstance(usage, dict):
                aggregate_usage(aggregated_usage, usage)

        data = [
            self._build_image_object(image, response_format, prompt)
            for image in collected_images[:image_count]
        ]
        response_payload: dict[str, Any] = {
            "created": int(time.time()),
            "data": data,
        }
        if aggregated_usage:
            response_payload["usage"] = aggregated_usage
        return response_payload

    def _stream_heartbeat_response(
        self,
        prompt: str,
        image_count: int,
        resolved_model,
        request_payload: dict[str, Any],
        response_format: str,
        forwarded_auth_header: str | None,
    ) -> None:
        result: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["payload"] = self._generate_response_payload(
                    prompt=prompt,
                    image_count=image_count,
                    resolved_model=resolved_model,
                    request_payload=request_payload,
                    response_format=response_format,
                    forwarded_auth_header=forwarded_auth_header,
                )
            except UpstreamHTTPError as exc:
                payload = dict(exc.payload)
                error_payload = payload.get("error")
                if isinstance(error_payload, dict):
                    error_payload.setdefault("upstream_status", exc.status_code)
                result["payload"] = payload
            except ConfigError as exc:
                result["payload"] = {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                    }
                }
            except ValueError as exc:
                result["payload"] = {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                    }
                }
            except Exception as exc:
                result["payload"] = {
                    "error": {"message": str(exc), "type": "server_error"}
                }
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()

        self._start_chunked_json_response(
            HTTPStatus.OK,
            extra_headers={"X-OpenAI-Image-Mode": "heartbeat-stream"},
        )
        self._write_chunk(b" \n")
        while not done.wait(self.config.heartbeat_interval_seconds):
            self._write_chunk(b" \n")
        final_body = json.dumps(
            result.get(
                "payload",
                {"error": {"message": "heartbeat worker produced no payload", "type": "server_error"}},
            ),
            ensure_ascii=False,
        ).encode("utf-8")
        self._write_chunk(final_body)
        self._finish_chunked_response()

    def _models_response_payload(self) -> dict[str, Any]:
        created_at = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": created_at,
                    "owned_by": "openai-image-bridge",
                }
                for model_id in self.config.list_public_model_ids()
            ],
        }

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
        request_path = self._request_path()
        if request_path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if request_path == "/v1/models":
            self._send_json(HTTPStatus.OK, self._models_response_payload())
            return
        if request_path.startswith(self.config.file_url_path + "/"):
            self._serve_generated_file()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"message": "not found", "type": "invalid_request_error"}},
        )

    def do_HEAD(self) -> None:
        request_path = self._request_path()
        if request_path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if request_path == "/v1/models":
            self._send_json(HTTPStatus.OK, self._models_response_payload())
            return
        if request_path.startswith(self.config.file_url_path + "/"):
            self._serve_generated_file()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": {"message": "not found", "type": "invalid_request_error"}},
        )

    def do_POST(self) -> None:
        if self._request_path() != "/v1/images/generations":
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
            forwarded_auth_header = self.headers.get("Authorization")
            normalized_prompt = prompt.strip()

            if self._should_use_async_placeholder(resolved_model, response_format):
                job_names = [self._reserve_async_job() for _ in range(image_count)]
                worker = threading.Thread(
                    target=self._run_async_generation,
                    args=(
                        normalized_prompt,
                        resolved_model,
                        payload,
                        job_names,
                        forwarded_auth_header,
                    ),
                    daemon=True,
                )
                worker.start()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "created": int(time.time()),
                        "data": [
                            {
                                "url": self._build_file_url(job_name),
                                "revised_prompt": normalized_prompt,
                            }
                            for job_name in job_names
                        ],
                    },
                    extra_headers={"X-OpenAI-Image-Mode": "async-placeholder"},
                )
                return

            if self._should_use_heartbeat_stream(resolved_model):
                self._stream_heartbeat_response(
                    prompt=normalized_prompt,
                    image_count=image_count,
                    resolved_model=resolved_model,
                    request_payload=payload,
                    response_format=response_format,
                    forwarded_auth_header=forwarded_auth_header,
                )
                return

            response_payload = self._generate_response_payload(
                prompt=normalized_prompt,
                image_count=image_count,
                resolved_model=resolved_model,
                request_payload=payload,
                response_format=response_format,
                forwarded_auth_header=forwarded_auth_header,
            )
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
