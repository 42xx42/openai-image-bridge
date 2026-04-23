from __future__ import annotations

import argparse
import sys

from .config import AppConfig, ConfigError
from .server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openai-image-bridge",
        description=(
            "Expose /v1/images/generations by relaying requests to an "
            "OpenAI-compatible /v1/chat/completions upstream that returns images."
        ),
    )
    parser.add_argument("--host", help="Bind host. Overrides HOST.")
    parser.add_argument("--port", type=int, help="Bind port. Overrides PORT.")
    parser.add_argument(
        "--upstream-url",
        help="Upstream chat completions URL. Overrides UPSTREAM_URL.",
    )
    parser.add_argument(
        "--public-base-url",
        help="Base URL used for generated file links. Overrides PUBLIC_BASE_URL.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory used to persist generated images. Overrides OUTPUT_DIR.",
    )
    parser.add_argument(
        "--dump-config",
        action="store_true",
        help="Print the resolved configuration and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    overrides = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "upstream_url": args.upstream_url,
            "public_base_url": args.public_base_url,
            "output_dir": args.output_dir,
        }.items()
        if value is not None
    }

    try:
        config = AppConfig.from_env(overrides=overrides)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    if args.dump_config:
        print(
            {
                "host": config.host,
                "port": config.port,
                "upstream_url": config.upstream_url,
                "default_public_model": config.default_public_model,
                "persist_images": config.persist_images,
                "output_dir": str(config.output_dir),
                "file_url_path": config.file_url_path,
                "public_base_url": config.public_base_url,
                "default_response_format": config.default_response_format,
                "model_map": {
                    key: {
                        "upstream_model": value.upstream_model,
                        "size": value.size,
                    }
                    for key, value in config.model_map.items()
                },
                "size_map": config.size_map,
            }
        )
        return 0

    try:
        serve(config)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
