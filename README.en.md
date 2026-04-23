# openai-image-bridge

[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-community-2D9CDB?logo=discourse&logoColor=white)](https://linux.do)

[简体中文](README.md) | [English](README.en.md)

`openai-image-bridge` exposes `POST /v1/images/generations` and relays each request to an upstream `POST /v1/chat/completions` endpoint that returns generated images inside the chat response payload.

This is useful when:

- your client only understands the OpenAI image endpoint
- your upstream actually generates images through a chat model plus image tool
- you want a thin compatibility layer instead of patching the client

## What It Does

1. Accepts a standard OpenAI-compatible image generation request.
2. Maps the public image model to an upstream chat model.
3. Calls the upstream `chat/completions` endpoint.
4. Extracts image data from the upstream response.
5. Returns a standard image generation response with `b64_json`, `url`, or both.

## Supported Upstream Response Shapes

The bridge currently extracts images from:

- `choices[0].message.images[].image_url.url`
- `choices[0].message.content[]` items with `type=image_url`
- `choices[0].message.content[]` items with `type=output_image`

The image payload must be a `data:` URL containing base64 image bytes.

## Quick Start

### Local Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
python -m openai_image_bridge
```

### Docker

```bash
cp .env.example .env
docker compose up --build -d
```

## Example Request

```bash
curl http://127.0.0.1:8080/v1/images/generations \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "A cinematic poster of a red apple on a white background",
    "n": 1,
    "response_format": "url"
  }'
```

Example response:

```json
{
  "created": 1777000000,
  "data": [
    {
      "url": "http://127.0.0.1:8080/generated/1777000000-abc123.png",
      "revised_prompt": "A cinematic poster of a red apple on a white background"
    }
  ],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 45,
    "total_tokens": 168
  }
}
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8080` | Bind port |
| `UPSTREAM_URL` | `http://127.0.0.1:3000/v1/chat/completions` | Upstream chat completions endpoint |
| `UPSTREAM_AUTH_HEADER` | unset | Static auth header for the upstream |
| `UPSTREAM_EXTRA_BODY_JSON` | `{}` | JSON merged into every upstream request |
| `DEFAULT_PUBLIC_MODEL` | `gpt-image-2` | Fallback model when the incoming request omits `model` |
| `MODEL_MAP_JSON` | built-in example map | Public model to upstream model map |
| `SIZE_MAP_JSON` | built-in example map | Fallback size to upstream model map |
| `ALLOW_UNMAPPED_MODEL_PASSTHROUGH` | `false` | Forward unknown public models unchanged |
| `SYSTEM_PROMPT` | unset | Optional system message for upstream chat calls |
| `PROMPT_PREFIX` | empty | Prepended to the incoming prompt |
| `PROMPT_SUFFIX` | empty | Appended to the incoming prompt |
| `FORWARD_USER_FIELD` | `true` | Forward the incoming `user` field to the upstream |
| `PERSIST_IMAGES` | `true` | Save generated images to disk |
| `OUTPUT_DIR` | `./data/generated` | Output directory for persisted images |
| `FILE_URL_PATH` | `/generated` | Public path used to serve persisted files |
| `PUBLIC_BASE_URL` | unset | Override the generated file base URL |
| `DEFAULT_RESPONSE_FORMAT` | `b64_json` | Default response format when the client omits it |
| `ALWAYS_INCLUDE_B64_JSON` | `false` | Always include `b64_json` even when `url` is requested |
| `ALWAYS_INCLUDE_URL` | `true` | Always include a `url` when persistence is enabled |
| `CLEANUP_MAX_AGE_SECONDS` | `0` | Delete files older than this age, disabled when `0` |
| `CLEANUP_SWEEP_INTERVAL_SECONDS` | `3600` | Minimum delay between cleanup sweeps |

## Model Mapping

`MODEL_MAP_JSON` accepts either a simple string map:

```json
{
  "gpt-image-2": "gpt-draw-1024x1024",
  "gpt-image-2-1024x1536": "gpt-draw-1024x1536"
}
```

or object entries:

```json
{
  "gpt-image-2": {
    "upstream_model": "gpt-draw-1024x1024",
    "size": "1024x1024"
  }
}
```

`SIZE_MAP_JSON` is used when the public model does not have a direct mapping but the request includes a recognized `size`.

## Deployment Notes

- The bridge can serve generated files itself at `FILE_URL_PATH`.
- If you deploy behind a reverse proxy, forward `Host` and `X-Forwarded-Proto`.
- If your upstream should not receive the caller's token, set `UPSTREAM_AUTH_HEADER`.

See [examples/nginx.conf](examples/nginx.conf) and [examples/openai-image-bridge.service](examples/openai-image-bridge.service).

## Limitations

- This project only bridges image generation, not edits or variations.
- `n > 1` is implemented by repeated upstream calls when needed.
- The bridge does not force upstream image parameters; model aliases or upstream-side routing usually handle that.
- The upstream must return image data inside the chat response.

## Development

Run tests with:

```bash
python -m unittest discover -s tests -q
```

## Acknowledgements

- Thanks to the [LINUX DO community](https://linux.do) for the discussion and experimentation that inspired this project.
