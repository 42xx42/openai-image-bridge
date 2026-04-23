# openai-image-bridge

[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-community-2D9CDB?logo=discourse&logoColor=white)](https://linux.do)

[简体中文](README.md) | [English](README.en.md)

`openai-image-bridge` 提供一个标准的 `POST /v1/images/generations` 接口，并把请求转发到上游 `POST /v1/chat/completions` 接口，适配那些“通过聊天模型加图片工具出图”的后端。

这个项目适合下面几类场景：

- 你的客户端只会调用 OpenAI 风格的图片生成接口
- 你的上游实际上是通过 `chat/completions` 返回图片结果
- 你想加一层很薄的兼容桥，而不是直接改客户端

## 它是怎么工作的

1. 接收标准的 OpenAI 兼容出图请求。
2. 把公开模型名映射为上游聊天模型。
3. 调用上游 `chat/completions`。
4. 从上游响应里提取图片数据。
5. 按 OpenAI 风格返回 `b64_json`、`url` 或两者同时返回。

## 当前支持的上游响应结构

目前桥接层会从这些位置提取图片：

- `choices[0].message.images[].image_url.url`
- `choices[0].message.content[]` 中 `type=image_url` 的项目
- `choices[0].message.content[]` 中 `type=output_image` 的项目

图片内容需要是 `data:` URL，并且内部包含 base64 图片字节。

## 快速开始

### 本地 Python

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

默认监听地址是 `http://127.0.0.1:8080`。

## 客户端接入

如果你的客户端支持“OpenAI 兼容接口”，通常这样填就可以：

- Base URL: `http://你的服务器:8080/v1`
- API Key: 这里填你桥接层自己的 token
- 图片模型: 选你暴露给客户端的公开模型名，比如 `gpt-image-2`

如果你想试占位 URL 模式，可以额外暴露一个带后缀的模型名，比如 `gpt-image-2-async`。只有带这个后缀的模型才会立刻返回占位 URL，普通模型仍然保持同步模式。

如果你想试心跳保活模式，也可以额外暴露一个带后缀的模型名，比如 `gpt-image-2-hb`。只有带这个后缀的模型才会使用分块响应周期性发送空白心跳，最后仍然返回完整 JSON。

服务的 `/v1/models` 会自动把所有基础模型展开成完整的后缀变体列表，所以客户端刷新模型后就能直接看到 `基础模型 + -async + -hb` 全套名称。

### Cherry Studio 配置示例

在 Cherry Studio 里新增一个 OpenAI 兼容提供商时，可以这样配：

- 接口地址: `http://你的服务器:8080/v1`
- API Key: 你的桥接层 token
- 模型: `gpt-image-2` 或你自定义的公开模型名

如果你把公开模型映射到了上游的 `gpt-draw-1024x1024`、`gpt-draw-1024x1536` 这类模型，Cherry Studio 只需要知道公开模型名，不需要知道你后端真实走的是 `chat/completions`。

## 请求示例

### 标准 curl

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

### Windows PowerShell

```powershell
curl.exe -sS "http://127.0.0.1:8080/v1/images/generations" `
  -H "Authorization: Bearer your-token" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"gpt-image-2\",\"prompt\":\"一只坐在沙发上的橘猫\",\"response_format\":\"url\"}"
```

示例响应：

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

## 配置说明

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `UPSTREAM_URL` | `http://127.0.0.1:3000/v1/chat/completions` | 上游聊天补全接口 |
| `UPSTREAM_AUTH_HEADER` | 未设置 | 发给上游的固定鉴权头 |
| `UPSTREAM_EXTRA_BODY_JSON` | `{}` | 追加到每个上游请求体的 JSON |
| `DEFAULT_PUBLIC_MODEL` | `gpt-image-2` | 客户端没传 `model` 时使用的默认公开模型 |
| `MODEL_MAP_JSON` | 内置示例映射 | 公开模型到上游模型的映射 |
| `SIZE_MAP_JSON` | 内置示例映射 | 按图片尺寸兜底映射到上游模型 |
| `ALLOW_UNMAPPED_MODEL_PASSTHROUGH` | `false` | 未映射模型是否原样透传 |
| `SYSTEM_PROMPT` | 未设置 | 发给上游聊天调用的可选 system prompt |
| `PROMPT_PREFIX` | 空 | 自动加在用户 prompt 前面 |
| `PROMPT_SUFFIX` | 空 | 自动加在用户 prompt 后面 |
| `FORWARD_USER_FIELD` | `true` | 是否把入参里的 `user` 传给上游 |
| `PERSIST_IMAGES` | `true` | 是否把生成图片落盘 |
| `OUTPUT_DIR` | `./data/generated` | 图片输出目录 |
| `FILE_URL_PATH` | `/generated` | 对外暴露静态图片的路径 |
| `PUBLIC_BASE_URL` | 未设置 | 手动覆盖返回的图片访问前缀 |
| `DEFAULT_RESPONSE_FORMAT` | `b64_json` | 客户端没传时默认返回格式 |
| `ALWAYS_INCLUDE_B64_JSON` | `false` | 请求 `url` 时是否仍然附带 `b64_json` |
| `ALWAYS_INCLUDE_URL` | `true` | 开启落盘时是否总是返回 `url` |
| `ASYNC_PLACEHOLDER_MODEL_SUFFIX` | 空 | 只有带这个后缀的模型名才走实验性的占位 URL 模式 |
| `HEARTBEAT_MODEL_SUFFIX` | 空 | 只有带这个后缀的模型名才走实验性的心跳保活模式 |
| `HEARTBEAT_INTERVAL_SECONDS` | `15` | 心跳模式下两次空白保活块之间的秒数 |
| `CLEANUP_MAX_AGE_SECONDS` | `0` | 自动清理超过这个年龄的图片，`0` 表示关闭 |
| `CLEANUP_SWEEP_INTERVAL_SECONDS` | `3600` | 两次清理扫描之间的最短间隔 |

## 模型映射

`MODEL_MAP_JSON` 可以是简单字符串映射：

```json
{
  "gpt-image-2": "gpt-draw-1024x1024",
  "gpt-image-2-1024x1536": "gpt-draw-1024x1536"
}
```

也可以写成对象结构：

```json
{
  "gpt-image-2": {
    "upstream_model": "gpt-draw-1024x1024",
    "size": "1024x1024"
  }
}
```

当公开模型本身没有直接映射，但请求里带了可识别的 `size` 时，会继续参考 `SIZE_MAP_JSON`。

## 占位 URL 模式

设置 `ASYNC_PLACEHOLDER_MODEL_SUFFIX=-async` 后，只有带这个后缀的模型才会走异步占位。例如：

- `gpt-image-2`：保持原来的同步模式
- `gpt-image-2-async`：立即返回一个 `/generated/job-...` URL，后台继续出图

这个 URL 在任务还没完成时会返回一张 SVG 占位图；后台出图完成后，同一个 URL 会开始返回真实图片。

这个模式适合试验规避长请求超时，但要注意：

- 只适用于 `response_format=url`
- 需要 `PERSIST_IMAGES=true`
- 不支持 `ALWAYS_INCLUDE_B64_JSON=true`
- 某些客户端可能会缓存占位图，因此更适合先做链路测试
- 这本质上仍然是兼容性 hack，不是真正的异步 OpenAI 图片接口

## 心跳保活模式

设置 `HEARTBEAT_MODEL_SUFFIX=-hb` 后，只有带这个后缀的模型才会走心跳保活。例如：

- `gpt-image-2`：保持原来的同步模式
- `gpt-image-2-hb`：立即开始分块响应，周期性发送空白心跳，最后再返回完整 JSON

这个模式的目标是让反向代理或 CDN 在等待上游出图时持续看到响应流量，从而尽量减少长时间无响应导致的超时。

这个模式的限制也要提前知道：

- 它依赖中间层接受 HTTP chunked 响应
- 某些客户端、反代或 WAF 可能会缓冲整个响应，导致心跳失效
- 如果上游在心跳开始后才报错，HTTP 状态码已经发出，最终只能在 JSON 体里返回错误对象
- 这同样是实验性兼容方案，更适合先在你的实际链路里压测

## 部署说明

- 桥接层可以自己托管 `FILE_URL_PATH` 下的生成图片。
- 如果放在反向代理后面，请正确转发 `Host` 和 `X-Forwarded-Proto`。
- 如果不希望把调用方的 token 传给上游，请设置 `UPSTREAM_AUTH_HEADER`。

可参考 [examples/nginx.conf](examples/nginx.conf) 和 [examples/openai-image-bridge.service](examples/openai-image-bridge.service)。

## 限制

- 目前只桥接图片生成，不处理图片编辑或变体。
- 当 `n > 1` 时，必要情况下会通过重复调用上游来实现。
- 这个桥接层不会强制覆盖上游的图片参数，通常由模型别名或上游路由处理。
- 上游必须把图片数据放在聊天响应里返回。

## 开发

运行测试：

```bash
python -m unittest discover -s tests -q
```

## 致谢

- 感谢 [LINUX DO 社区](https://linux.do) 提供的讨论和实践思路，这个项目的桥接方案就是在这些交流基础上整理出来的。
