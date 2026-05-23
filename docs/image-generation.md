# 图片生成

nanobot 可以通过 `generate_image` 工具生成和编辑图片。在 WebUI 中，用户可以从编辑器启用**图片生成**，选择宽高比，并在同一聊天中不断迭代生成的图片。

此功能默认禁用。在 `~/.nanobot/config.json` 中启用它，配置支持的图片 provider，然后重启网关。

## 快速设置

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "${OPENROUTER_API_KEY}"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "openrouter",
      "model": "openai/gpt-5.4-image-2"
    }
  }
}
```

参见 [Provider 说明](#provider-notes) 了解 AIHubMix、MiniMax、Gemini、Ollama 和 StepFun 的配置示例。

> [!TIP]
> 优先使用环境变量存放 API 密钥。nanobot 会在启动时从环境变量解析 `${VAR_NAME}` 值。

## WebUI 使用方法

在 WebUI 编辑器中：

1. 点击 **图片生成**。
2. 选择宽高比：`Auto`、`1:1`、`3:4`、`9:16`、`4:3` 或 `16:9`。
3. 描述图片或你想要的编辑效果。
4. 编辑现有图片时附上参考图片。

生成的图片作为助手媒体渲染在聊天中。"让它暖一些"、"换个背景"、"试试 16:9 版本"等后续提示可以复用最近生成的制品。

WebUI 对用户隐藏了 provider 存储细节。Agent 内部看到保存的制品路径，并将其作为 `reference_images` 传回 `generate_image` 以进行迭代编辑。

## 配置参考

| 选项 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `tools.imageGeneration.enabled` | boolean | `false` | 注册 `generate_image` 工具 |
| `tools.imageGeneration.provider` | string | `"openrouter"` | 图片 provider 名称。支持值：`openrouter`、`aihubmix`、`minimax`、`gemini`、`ollama`、`stepfun` |
| `tools.imageGeneration.model` | string | `"openai/gpt-5.4-image-2"` | Provider 模型名称 |
| `tools.imageGeneration.defaultAspectRatio` | string | `"1:1"` | 提示/工具调用未指定时的默认比例 |
| `tools.imageGeneration.defaultImageSize` | string | `"1K"` | 默认大小提示，例如 `1K`、`2K`、`4K` 或 `1024x1024` |
| `tools.imageGeneration.maxImagesPerTurn` | number | `4` | 单次工具调用接受的最多 `count`。有效范围：`1` 到 `8` |
| `tools.imageGeneration.saveDir` | string | `"generated"` | nanobot 媒体目录下用于存放生成制品的相对目录 |

Provider 设置复用正常的 provider 配置字段：

| 选项 | 描述 |
|------|------|
| `providers.<name>.apiKey` | Provider API 密钥。优先使用 `${ENV_VAR}` |
| `providers.<name>.apiBase` | 可选的自定义基础 URL |
| `providers.<name>.extraHeaders` | 合并到 provider 请求中的请求头 |
| `providers.<name>.extraBody` | 合并到 provider 请求体中的额外 JSON 字段 |

camelCase 和 snake_case 配置键都可接受，但文档使用 camelCase 以匹配 `config.json`。

## Provider 说明

### OpenRouter

OpenRouter 使用聊天补全风格的图片响应。配置如下：

```json
{
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "openrouter",
      "model": "openai/gpt-5.4-image-2"
    }
  }
}
```

使用支持图片生成和图片编辑的模型以实现参考图片编辑。

### AIHubMix

AIHubMix `gpt-image-2-free` 通过 AIHubMix 的统一 predictions API 受支持。内部 nanobot 调用：

```text
/v1/models/openai/gpt-image-2-free/predictions
```

配置如下：

```json
{
  "providers": {
    "aihubmix": {
      "apiKey": "${AIHUBMIX_API_KEY}",
      "extraBody": {
        "quality": "low"
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "aihubmix",
      "model": "gpt-image-2-free"
    }
  }
}
```

`quality: low` 是可选的。它可以使免费图片模型更快且不太可能超时，但对于正确性来说不是必需的。

### MiniMax

MiniMax `image-01` 支持文生图和参考图片（主体参考）编辑。支持的比例为 `1:1`、`16:9`、`4:3`、`3:2`、`2:3`、`3:4`、`9:16` 和 `21:9`。

```json
{
  "providers": {
    "minimax": {
      "apiKey": "${MINIMAX_API_KEY}"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "minimax",
      "model": "image-01",
      "defaultAspectRatio": "1:1"
    }
  }
}
```

### Gemini

nanobot 通过 Google Generative Language API 支持两个 Gemini 图片生成模型系列：

| 模型 | 端点 | 参考图片 |
|------|------|----------|
| `imagen-4.0-generate-001` | `:predict` | 此集成不支持 |
| `gemini-2.5-flash-image` | `:generateContent` | 支持 |

对于参考图片编辑，请使用 Gemini Flash 图片模型：

```json
{
  "providers": {
    "gemini": {
      "apiKey": "${GEMINI_API_KEY}"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "gemini",
      "model": "gemini-2.5-flash-image"
    }
  }
}
```

Imagen 4 支持 `1:1`、`9:16`、`16:9`、`3:4` 和 `4:3` 比例。不支持的比例将被忽略，模型使用其默认值。`defaultImageSize` 设置对 Gemini 模型无效；大小仅由 `defaultAspectRatio` 控制。传给 Imagen 模型的参考图片将被忽略（记录警告日志）。

### Ollama

Ollama 的实验性原生图片生成 API 适用于本地服务器和 ollama.com 托管模型。本地访问 `http://localhost:11434/api` 不需要 API 密钥；仅在目标为 `https://ollama.com/api` 时才设置 `providers.ollama.apiKey`。

```json
{
  "providers": {
    "ollama": {
      "apiBase": "http://localhost:11434/api"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "ollama",
      "model": "x/z-image-turbo",
      "defaultAspectRatio": "16:9",
      "defaultImageSize": "2K"
    }
  }
}
```

Ollama 将 `defaultAspectRatio` 和 `defaultImageSize` 映射到原生的 `width` 和 `height` 值。此集成不支持参考图片。

### StepFun

StepFun（阶跃星辰）`step-image-edit-2` 支持文生图生成。`step-1x-medium` 变体还额外支持**风格参考**图片编辑，其中参考图片指导输出的视觉风格。

支持的比例：`1:1`、`16:9`、`9:16`、`3:4`、`4:3`。大小指定为 `WIDTHxHEIGHT`（如 `1024x1024`、`1280x800`、`800x1280`）。

```json
{
  "providers": {
    "stepfun": {
      "apiKey": "${STEPFUN_API_KEY}"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "stepfun",
      "model": "step-image-edit-2"
    }
  }
}
```

> [!NOTE]
> StepFun provider 复用现有的 `providers.stepfun` 配置块（与 StepFun 的 LLM API 使用的相同）。只需设置一次 `providers.stepfun.apiKey`，它将在文本和图片生成之间共享。
>
> 当使用 `step-image-edit-2` 时，`reference_images` 会被忽略（该模型不支持风格参考）。切换到 `step-1x-medium` 以使用参考图片引导的生成。

#### StepPlan（订阅）

StepPlan 是 StepFun 的订阅层级，使用不同的 API 基础 URL。图片生成端点路径相同 — 只需覆盖 `apiBase`：

```json
{
  "providers": {
    "stepfun": {
      "apiKey": "${STEPFUN_API_KEY}",
      "apiBase": "https://api.stepfun.com/step_plan/v1"
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "provider": "stepfun",
      "model": "step-image-edit-2"
    }
  }
}
```

`apiBase` 优先于注册表默认值，因此配置了 StepPlan 基础 URL 后，图片请求会发送到 `https://api.stepfun.com/step_plan/v1/images/generations` — 与 LLM 调用使用的路径前缀相同。API 密钥与标准 StepFun provider 共享。

## 制品

生成的图片存储在当前 nanobot 实例的媒体目录下：

```text
~/.nanobot/media/generated/YYYY-MM-DD/img_<id>.<ext>
~/.nanobot/media/generated/YYYY-MM-DD/img_<id>.json
```

对于非默认配置位置，媒体目录相对于活动配置文件的目录。

JSON 侧车文件存储：

| 字段 | 含义 |
|------|------|
| `id` | 短的生成图片 ID，如 `img_ab12cd34ef56` |
| `path` | 用于后续编辑的本地图片路径 |
| `mime` | 检测到的图片 MIME 类型 |
| `prompt` | 用于生成的提示词 |
| `model` | Provider 模型 |
| `provider` | Provider 名称 |
| `source_images` | 编辑时使用的参考图片路径 |
| `created_at` | 创建时间戳 |

不要将 base64 图片负载粘贴到聊天中。Agent 应该保持本地制品路径为内部信息，除非用户明确要求调试详情。

## 提示词编写

好的图片提示词包括：

- 主体和场景。
- 构图、相机或布局。
- 风格、情绪、灯光和色调。
- 必须出现在图片中的确切文本，加引号。
- 约束条件如"保持相同角色"或"保留 Logo"。

示例：

```text
nanobot 的极简应用图标：友好的机器人头像，圆角正方形，柔和蓝白色调，干净的矢量风格，无文字
```

对于编辑，描述应该改变什么和必须固定什么：

```text
使用参考图片。保持相同的机器人和构图，将色调改为暖橙色，并添加微妙的日出背景。
```

## 故障排除

| 症状 | 检查项 |
|------|--------|
| `generate_image` 不可用 | 将 `tools.imageGeneration.enabled` 设为 `true` 并重启网关 |
| 缺少 API 密钥错误 | 配置 `providers.<provider>.apiKey`；如果使用 `${VAR_NAME}`，确认环境变量对网关进程可见 |
| `不支持的图片生成 provider` | 使用 `openrouter`、`aihubmix`、`minimax`、`gemini`、`ollama` 或 `stepfun` |
| AIHubMsg 提示 `Incorrect model ID` | 使用 `model: "gpt-image-2-free"`；nanobot 会在内部将其展开为所需的 `openai/gpt-image-2-free` 模型路径 |
| 生成超时 | 尝试较小/默认图片大小，设置 AIHubMix `extraBody.quality` 为 `"low"`，或稍后重试 |
| 参考图片被拒绝 | 参考图片路径必须在工作区或 nanobot 媒体目录内，并且必须是有效的图片文件 |

