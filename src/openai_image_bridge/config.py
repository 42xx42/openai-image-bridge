from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class ModelMapping:
    public_model: str
    upstream_model: str
    size: str | None = None


@dataclass(frozen=True)
class ResolvedModel:
    public_model: str
    upstream_model: str
    size: str | None = None


DEFAULT_MODEL_MAP = {
    "gpt-image-2": "gpt-draw-1024x1024",
    "gpt-image-2-1024x1024": "gpt-draw-1024x1024",
    "gpt-image-2-1024x1536": "gpt-draw-1024x1536",
    "gpt-image-2-1536x1024": "gpt-draw-1536x1024",
}

DEFAULT_SIZE_MAP = {
    "1024x1024": "gpt-draw-1024x1024",
    "1024x1536": "gpt-draw-1024x1536",
    "1536x1024": "gpt-draw-1536x1024",
}


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean value: {value}")


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"invalid integer value: {value}") from exc


def _parse_json_object(value: str | None, default: dict[str, Any]) -> dict[str, Any]:
    if value is None or value.strip() == "":
        return dict(default)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON object: {value}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("expected a JSON object")
    return parsed


def _parse_model_map(raw: dict[str, Any]) -> dict[str, ModelMapping]:
    mappings: dict[str, ModelMapping] = {}
    for public_model, value in raw.items():
        if isinstance(value, str):
            mappings[public_model] = ModelMapping(
                public_model=public_model,
                upstream_model=value,
            )
            continue
        if not isinstance(value, dict):
            raise ConfigError(
                "MODEL_MAP entries must be strings or JSON objects"
            )
        upstream_model = value.get("upstream_model") or value.get("model")
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise ConfigError(
                f"MODEL_MAP entry for {public_model!r} is missing upstream_model"
            )
        size = value.get("size")
        if size is not None and not isinstance(size, str):
            raise ConfigError(
                f"MODEL_MAP entry for {public_model!r} has a non-string size"
            )
        mappings[public_model] = ModelMapping(
            public_model=public_model,
            upstream_model=upstream_model.strip(),
            size=size.strip() if isinstance(size, str) and size.strip() else None,
        )
    return mappings


def _normalize_file_url_path(value: str | None) -> str:
    path = (value or "/generated").strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    return path or "/generated"


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    upstream_url: str = "http://127.0.0.1:3000/v1/chat/completions"
    upstream_timeout_seconds: int = 240
    upstream_auth_header: str | None = None
    upstream_extra_body: dict[str, Any] = field(default_factory=dict)
    default_public_model: str = "gpt-image-2"
    model_map: dict[str, ModelMapping] = field(default_factory=dict)
    size_map: dict[str, str] = field(default_factory=dict)
    allow_unmapped_model_passthrough: bool = False
    system_prompt: str | None = None
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    forward_user_field: bool = True
    persist_images: bool = True
    output_dir: Path = field(default_factory=lambda: Path("data/generated").resolve())
    file_url_path: str = "/generated"
    public_base_url: str | None = None
    default_response_format: str = "b64_json"
    always_include_b64_json: bool = False
    always_include_url: bool = True
    cleanup_max_age_seconds: int = 0
    cleanup_sweep_interval_seconds: int = 3600

    @classmethod
    def from_env(cls, overrides: dict[str, Any] | None = None) -> "AppConfig":
        overrides = overrides or {}
        env = os.environ

        raw_model_map = _parse_json_object(
            str(overrides.get("model_map_json"))
            if overrides.get("model_map_json") is not None
            else env.get("MODEL_MAP_JSON"),
            DEFAULT_MODEL_MAP,
        )
        raw_size_map = _parse_json_object(
            str(overrides.get("size_map_json"))
            if overrides.get("size_map_json") is not None
            else env.get("SIZE_MAP_JSON"),
            DEFAULT_SIZE_MAP,
        )
        model_map = _parse_model_map(raw_model_map)
        size_map = {
            str(size): str(upstream_model)
            for size, upstream_model in raw_size_map.items()
        }

        default_response_format = (
            str(
                overrides.get("default_response_format")
                if overrides.get("default_response_format") is not None
                else env.get("DEFAULT_RESPONSE_FORMAT", "b64_json")
            )
            .strip()
            .lower()
        )
        if default_response_format not in {"b64_json", "url"}:
            raise ConfigError(
                "DEFAULT_RESPONSE_FORMAT must be either 'b64_json' or 'url'"
            )

        output_dir_value = overrides.get("output_dir")
        if output_dir_value is None:
            output_dir_value = env.get("OUTPUT_DIR", "data/generated")

        config = cls(
            host=str(overrides.get("host", env.get("HOST", "0.0.0.0"))),
            port=int(overrides.get("port", _parse_int(env.get("PORT"), 8080))),
            upstream_url=str(
                overrides.get(
                    "upstream_url",
                    env.get(
                        "UPSTREAM_URL",
                        "http://127.0.0.1:3000/v1/chat/completions",
                    ),
                )
            ),
            upstream_timeout_seconds=int(
                overrides.get(
                    "upstream_timeout_seconds",
                    _parse_int(env.get("UPSTREAM_TIMEOUT_SECONDS"), 240),
                )
            ),
            upstream_auth_header=(
                str(overrides["upstream_auth_header"])
                if overrides.get("upstream_auth_header") is not None
                else env.get("UPSTREAM_AUTH_HEADER") or None
            ),
            upstream_extra_body=_parse_json_object(
                str(overrides.get("upstream_extra_body_json"))
                if overrides.get("upstream_extra_body_json") is not None
                else env.get("UPSTREAM_EXTRA_BODY_JSON"),
                {},
            ),
            default_public_model=str(
                overrides.get(
                    "default_public_model",
                    env.get("DEFAULT_PUBLIC_MODEL", "gpt-image-2"),
                )
            ),
            model_map=model_map,
            size_map=size_map,
            allow_unmapped_model_passthrough=_parse_bool(
                str(overrides.get("allow_unmapped_model_passthrough"))
                if overrides.get("allow_unmapped_model_passthrough") is not None
                else env.get("ALLOW_UNMAPPED_MODEL_PASSTHROUGH"),
                False,
            ),
            system_prompt=(
                str(overrides["system_prompt"])
                if overrides.get("system_prompt") is not None
                else env.get("SYSTEM_PROMPT") or None
            ),
            prompt_prefix=str(
                overrides.get("prompt_prefix", env.get("PROMPT_PREFIX", ""))
            ),
            prompt_suffix=str(
                overrides.get("prompt_suffix", env.get("PROMPT_SUFFIX", ""))
            ),
            forward_user_field=_parse_bool(
                str(overrides.get("forward_user_field"))
                if overrides.get("forward_user_field") is not None
                else env.get("FORWARD_USER_FIELD"),
                True,
            ),
            persist_images=_parse_bool(
                str(overrides.get("persist_images"))
                if overrides.get("persist_images") is not None
                else env.get("PERSIST_IMAGES"),
                True,
            ),
            output_dir=Path(str(output_dir_value)).resolve(),
            file_url_path=_normalize_file_url_path(
                str(overrides.get("file_url_path"))
                if overrides.get("file_url_path") is not None
                else env.get("FILE_URL_PATH")
            ),
            public_base_url=(
                str(overrides["public_base_url"]).rstrip("/")
                if overrides.get("public_base_url") is not None
                else (env.get("PUBLIC_BASE_URL") or "").rstrip("/") or None
            ),
            default_response_format=default_response_format,
            always_include_b64_json=_parse_bool(
                str(overrides.get("always_include_b64_json"))
                if overrides.get("always_include_b64_json") is not None
                else env.get("ALWAYS_INCLUDE_B64_JSON"),
                False,
            ),
            always_include_url=_parse_bool(
                str(overrides.get("always_include_url"))
                if overrides.get("always_include_url") is not None
                else env.get("ALWAYS_INCLUDE_URL"),
                True,
            ),
            cleanup_max_age_seconds=int(
                overrides.get(
                    "cleanup_max_age_seconds",
                    _parse_int(env.get("CLEANUP_MAX_AGE_SECONDS"), 0),
                )
            ),
            cleanup_sweep_interval_seconds=int(
                overrides.get(
                    "cleanup_sweep_interval_seconds",
                    _parse_int(env.get("CLEANUP_SWEEP_INTERVAL_SECONDS"), 3600),
                )
            ),
        )

        if not config.upstream_url:
            raise ConfigError("UPSTREAM_URL must not be empty")
        if config.port < 0 or config.port > 65535:
            raise ConfigError("PORT must be between 0 and 65535")
        if config.cleanup_max_age_seconds < 0:
            raise ConfigError("CLEANUP_MAX_AGE_SECONDS must be >= 0")
        if config.cleanup_sweep_interval_seconds <= 0:
            raise ConfigError("CLEANUP_SWEEP_INTERVAL_SECONDS must be > 0")
        return config

    def resolve_model(self, requested_model: str, requested_size: str | None) -> ResolvedModel:
        if requested_model in self.model_map:
            mapping = self.model_map[requested_model]
            return ResolvedModel(
                public_model=requested_model,
                upstream_model=mapping.upstream_model,
                size=mapping.size or requested_size,
            )
        if requested_size and requested_size in self.size_map:
            return ResolvedModel(
                public_model=requested_model,
                upstream_model=self.size_map[requested_size],
                size=requested_size,
            )
        if self.allow_unmapped_model_passthrough:
            return ResolvedModel(
                public_model=requested_model,
                upstream_model=requested_model,
                size=requested_size,
            )
        raise ConfigError(
            f"no upstream mapping found for model={requested_model!r}, size={requested_size!r}"
        )
