# 配置

配置文件：`~/.nanobot/config.json`

> [!NOTE]
> 如果你的配置文件比当前架构旧，你可以在不覆盖现有值的情况下刷新它：
> 运行 `nanobot onboard`，然后在询问是否覆盖配置时回答 `N`。
> nanobot 会合并缺失的默认字段并保留你的当前设置。

## 用于密钥的环境变量

除了将密钥直接存储在 `config.json` 中，你还可以使用 `${VAR_NAME}` 引用，这些引用在启动时从环境变量解析：

```json
{
  "channels": {
    "telegram": { "token": "${TELEGRAM_TOKEN}" },
    "email": {
      "imapPassword": "${IMAP_PASSWORD}",
      "smtpPassword": "${SMTP_PASSWORD}"
    }
  },
  "providers": {
    "groq": { "apiKey": "${GROQ_API_KEY}" }
  }
}
```

`config.json` 中的任何字符串值都可以使用 `${VAR_NAME}`。解析在启动时仅运行一次，仅在内存中 — 解析后的值永远不会写回磁盘，因此通过 `nanobot onboard` 或 WebUI 编辑配置会保留占位符。

如果引用的变量未设置，nanobot 在启动时会以 `ValueError: Environment variable 'NAME' referenced in config is not set` 快速失败。

### 更多示例

**MCP 服务器** — stdio `env` 和 HTTP `headers` 都支持：

```json
{
  "tools": {
    "mcpServers": {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" }
      },
      "remote": {
        "url": "https://example.com/mcp/",
        "headers": { "Authorization": "Bearer ${REMOTE_MCP_TOKEN}" }
      }
    }
  }
}
```

**网络搜索 Provider：**

```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "apiKey": "${BRAVE_API_KEY}"
      }
    }
  }
}
```

### 启动时加载变量

选择适合你部署的方式 — nanobot 只在启动时读取 `os.environ`，所以任何填充进程环境的机制都可以工作。

**systemd** — 在服务单元中使用 `EnvironmentFile=` 从只有部署用户可读的文件加载变量：

```ini
# /etc/systemd/system/nanobot.service（摘录）
[Service]
EnvironmentFile=/home/youruser/nanobot_secrets.env
User=nanobot
ExecStart=...
```

```bash
# /home/youruser/nanobot_secrets.env（权限 600，所有者为 youruser）
TELEGRAM_TOKEN=your-token-here
IMAP_PASSWORD=your-password-here
```

**Docker** — 将 env 文件传递给本地构建的镜像（每行一个 `KEY=VALUE`），或使用 `-e KEY=value`：

```bash
docker run --rm --env-file=./nanobot.env \
  -v ~/.nanobot:/home/nanobot/.nanobot \
  nanobot agent -m "Hello"
```

**direnv** — 在工作目录中放置 `.envrc` 并运行 `direnv allow`：

```bash
# .envrc（由 direnv 自动加载）
export TELEGRAM_TOKEN=your-token-here
export ANTHROPIC_API_KEY=...
```

**密钥管理器（1Password、Bitwarden、pass）** — 包装进程使密钥仅作为环境变量存在于运行期间，永不落盘：

```bash
# 1Password — .env.tpl 中的引用格式为 `op://Vault/Item/field`
op run --env-file=.env.tpl -- nanobot agent

# pass (passwordstore.org)
ANTHROPIC_API_KEY="$(pass show api/anthropic)" nanobot agent

# Bitwarden
ANTHROPIC_API_KEY="$(bw get password api/anthropic)" nanobot agent
```

## Providers（模型提供商）

> [!TIP]
> - **语音转写**：语音消息（Telegram、WhatsApp）会自动使用 Whisper 转写。默认使用 Groq（免费层）。在 `channels` 下设置 `"transcriptionProvider": "openai"` 可改用 OpenAI Whisper，并可选设置 `"transcriptionLanguage": "en"`（或其他 ISO-639-1 代码）以获得更准确的转写。API 密钥从匹配的 provider 配置中获取。
> - **MiniMax 编程计划**：nanobot 社区的专属折扣链接：[海外](https://platform.minimax.io/subscribe/coding-plan?code=9txpdXw04g&source=link) · [中国大陆](https://platform.minimaxi.com/subscribe/token-plan?code=GILTJpMTqZ&source=link)
> - **MiniMax（中国大陆）**：如果你的 API 密钥来自 MiniMax 中国大陆平台（minimaxi.com），请在 minimax provider 配置中设置 `"apiBase": "https://api.minimaxi.com/v1"`。
> - **MiniMax 思考模式**：当你需要 `reasoningEffort` / 思考模式时，请使用 `providers.minimaxAnthropic`。MiniMax 通过其 Anthropic 兼容端点暴露此能力，所以 nanobot 将其作为单独的 provider 而不是在通用的 OpenAI 兼容 `minimax` 端点上猜测 MiniMax 特定的思考参数。它使用相同的 `MINIMAX_API_KEY`。默认 Anthropic 兼容基础 URL：`https://api.minimax.io/anthropic`；中国大陆使用 `https://api.minimaxi.com/anthropic`。
> - **火山引擎 / BytePlus 编程计划**：使用专用 providers `volcengineCodingPlan` 或 `byteplusCodingPlan`，而非按量付费的 `volcengine` / `byteplus` providers。
> - **智谱编程计划**：如果你使用的是智谱的编程计划，请在 zhipu provider 配置中设置 `"apiBase": "https://open.bigmodel.cn/api/coding/paas/v4"`。
> - **阿里云百炼**：如果你使用阿里云百炼的 OpenAI 兼容端点，请在 dashscope provider 配置中设置 `"apiBase": "https://dashscope.aliyuncs.com/compatible-mode/v1"`。
> - **阶跃星辰（Step Fun）（中国大陆）**：如果你的 API 密钥来自阶跃星辰的中国大陆平台（stepfun.com），请在 stepfun provider 配置中设置 `"apiBase": "https://api.stepfun.com/v1"`。
> - **小米 MiMo 思考模式**：MiMo 模型（如 `mimo-v2.5-pro`）默认启用思考。使用 `agents.defaults.reasoningEffort: "none"` 禁用它，或使用 `"low"` / `"medium"` / `"high"` 保持开启。省略此字段则保留每个模型的 provider 默认值。

| Provider | 用途 | 获取 API 密钥 |
|----------|------|--------------|
| `custom` | 任何 OpenAI 兼容端点 | — |
| `openrouter` | LLM（推荐，可访问所有模型） | [openrouter.ai](https://openrouter.ai) |
| `huggingface` | LLM（Hugging Face 推理 Provider） | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| `skywork` | LLM（Skywork / APIFree API 网关） | [apifree.ai](https://www.apifree.ai) |
| `volcengine` | LLM（火山引擎，按量付费） | [编程计划](https://www.volcengine.com/activity/codingplan?utm_campaign=nanobot&utm_content=nanobot&utm_medium=devrel&utm_source=OWO&utm_term=nanobot) · [volcengine.com](https://www.volcengine.com) |
| `byteplus` | LLM（火山引擎国际版，按量付费） | [编程计划](https://www.byteplus.com/en/activity/codingplan?utm_campaign=nanobot&utm_content=nanobot&utm_medium=devrel&utm_source=OWO&utm_term=nanobot) · [byteplus.com](https://www.byteplus.com) |
| `anthropic` | LLM（Claude 直连） | [console.anthropic.com](https://console.anthropic.com) |
| `azure_openai` | LLM（Azure OpenAI） | [portal.azure.com](https://portal.azure.com) |
| `bedrock` | LLM（AWS Bedrock Converse，Claude/Nova/Llama 等） | [aws.amazon.com/bedrock](https://aws.amazon.com/bedrock/) |
| `openai` | LLM + 语音转写（Whisper） | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM（DeepSeek 直连） | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + 语音转写（Whisper，默认） | [console.groq.com](https://console.groq.com) |
| `minimax` | LLM（MiniMax 直连） | [platform.minimaxi.com](https://platform.minimaxi.com) |
| `minimax_anthropic` | LLM（MiniMax Anthropic 兼容端点，思考模式） | [platform.minimaxi.com](https://platform.minimaxi.com) |
| `gemini` | LLM（Gemini 直连） | [aistudio.google.com](https://aistudio.google.com) |
| `aihubmix` | LLM（API 网关，可访问所有模型） | [aihubmix.com](https://aihubmix.com) |
| `siliconflow` | LLM（SiliconFlow/硅基流动） | [siliconflow.cn](https://siliconflow.cn) |
| `novita` | LLM（Novita AI OpenAI 兼容网关） | [novita.ai](https://novita.ai) |
| `dashscope` | LLM（通义千问 Qwen） | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |
| `moonshot` | LLM（月之暗面 Moonshot/Kimi） | [platform.moonshot.cn](https://platform.moonshot.cn) |
| `zhipu` | LLM（智谱 GLM） | [open.bigmodel.cn](https://open.bigmodel.cn) |
| `mimo` | LLM（MiMo） | [platform.xiaomimimo.com](https://platform.xiaomimimo.com) |
| `longcat` | LLM（LongCat） | [longcat.chat](https://longcat.chat/platform/docs/zh/) |
| `ant_ling` | LLM（蚂蚁百灵 Ant Ling） | [developer.ant-ling.com](https://developer.ant-ling.com/en/docs/api-reference/openai/) |
| `ollama` | LLM（本地，Ollama） | — |
| `lm_studio` | LLM（本地，LM Studio） | — |
| `atomic_chat` | LLM（本地，[Atomic Chat](https://atomic.chat/)） | — |
| `mistral` | LLM | [docs.mistral.ai](https://docs.mistral.ai/) |
| `stepfun` | LLM（阶跃星辰 Step Fun） | [platform.stepfun.com](https://platform.stepfun.com) |
| `ovms` | LLM（本地，OpenVINO Model Server） | [docs.openvino.ai](https://docs.openvino.ai/2026/model-server/ovms_docs_llm_quickstart.html) |
| `vllm` | LLM（本地，任何 OpenAI 兼容服务器） | — |
| `openai_codex` | LLM（Codex，OAuth） | `nanobot provider login openai-codex` |
| `github_copilot` | LLM（GitHub Copilot，OAuth） | `nanobot provider login github-copilot` |
| `qianfan` | LLM（百度千帆） | [cloud.baidu.com](https://cloud.baidu.com/doc/qianfan/s/Hmh4suq26) |

<details>
<summary><b>Skywork / APIFree</b></summary>

Skywork 使用 APIFree 的 OpenAI 兼容 Agent API 端点。配置一次 provider，
然后使用 Skywork 模型 ID 如 `skywork-ai/skyclaw-v1`。

```json
{
  "providers": {
    "skywork": {
      "apiKey": "${SKYWORK_API_KEY}",
      "apiBase": "https://api.apifree.ai/agent/v1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "skywork",
      "model": "skywork-ai/skyclaw-v1",
      "maxTokens": 32768,
      "contextWindowTokens": 131072
    }
  }
}
```

如果你的环境将凭据命名为 `APIFREE_API_KEY`，你也可以在 `apiKey` 中引用 `${APIFREE_API_KEY}`。

</details>


<details>
<summary><b>AWS Bedrock (Converse API)</b></summary>

Bedrock 使用原生 `bedrock-runtime` Converse API，所以它可以调用 Bedrock 模型 ID 如 Claude Opus 4.7、Claude Sonnet、Amazon Nova、Meta Llama、Mistral、Qwen 以及其他支持 Converse 的模型。它支持正常聊天、流式传输、工具调用、工具结果、Token 使用量和 Bedrock 错误元数据。

此 provider 用于 Bedrock 原生 Converse API，而不是 Bedrock 的 OpenAI 兼容 `/openai/v1` 端点。对于 OpenAI 兼容的 Bedrock 模型，如果你确实想要该 API 表面，仍可以使用 `custom`。

**1. 配置凭据**

使用正常的 AWS 凭据链（`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`、AWS 配置文件或 IAM 角色）。IAM 身份需要：

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream"
  ],
  "Resource": "*"
}
```

你也可以设置 `providers.bedrock.apiKey` 为 Bedrock API 密钥；nanobot 会将其作为 `AWS_BEARER_TOKEN_BEDROCK` 导出给 AWS SDK。

凭据选项：

- **AWS CLI/默认配置文件**：留空 `apiKey` 和 `profile`，然后运行 `aws configure` 或提供 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`。
- **命名的 AWS 配置文件**：将 `profile` 设置为来自 `~/.aws/config` 或 `~/.aws/credentials` 的配置文件名。
- **IAM 角色**：在 EC2/ECS/Lambda 上，留空 `apiKey` 和 `profile` 并附加具有 Bedrock 权限的角色。
- **Bedrock API 密钥**：设置 `apiKey` 或 `AWS_BEARER_TOKEN_BEDROCK`；`profile` 可保持为 `null`。

**2. 最小配置**

对于非 Anthropic 模型如 Amazon Nova：

```json
{
  "providers": {
    "bedrock": {
      "region": "us-east-1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "bedrock",
      "model": "bedrock/amazon.nova-lite-v1:0",
      "reasoningEffort": null
    }
  }
}
```

使用 Bedrock API 密钥：

```json
{
  "providers": {
    "bedrock": {
      "region": "us-east-1",
      "apiKey": "${AWS_BEARER_TOKEN_BEDROCK}"
    }
  },
  "agents": {
    "defaults": {
      "provider": "bedrock",
      "model": "bedrock/amazon.nova-lite-v1:0",
      "reasoningEffort": null
    }
  }
}
```

使用命名的 AWS 配置文件：

```json
{
  "providers": {
    "bedrock": {
      "region": "us-east-1",
      "profile": "my-bedrock-profile"
    }
  },
  "agents": {
    "defaults": {
      "provider": "bedrock",
      "model": "bedrock/amazon.nova-lite-v1:0"
    }
  }
}
```

**3. Claude Opus 4.7 示例**

```json
{
  "providers": {
    "bedrock": {
      "region": "us-east-1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "bedrock",
      "model": "bedrock/global.anthropic.claude-opus-4-7",
      "reasoningEffort": "medium",
      "maxTokens": 8192
    }
  }
}
```

对于区域路由，使用 Bedrock 的推理 ID 之一，例如 `bedrock/us.anthropic.claude-opus-4-7`、`bedrock/eu.anthropic.claude-opus-4-7` 或 `bedrock/jp.anthropic.claude-opus-4-7`。

Claude Opus 4.7 不接受 `temperature`、`top_p` 或 `top_k`；nanobot 会为此模型自动省略 `temperature`。如果 `reasoningEffort` 设为 `low`、`medium`、`high`、`max` 或 `adaptive`，nanobot 会发送 Bedrock 的自适应思考参数。

Bedrock 上的 Anthropic 模型可能还需要 Anthropic 用例注册，并受 Anthropic 支持的国家/地区限制。如果 Claude 因不受支持的国家/地区而出现 `ValidationException`，尝试使用非 Anthropic Bedrock 模型（如 Amazon Nova）来验证 provider 设置是否正确。

**4. 模型 ID**

在 nanobot 配置中使用带 `bedrock/` 前缀的 Bedrock 模型 ID 或推理配置文件 ID。nanobot 在调用 AWS 前会移除此前缀。

示例：

- `bedrock/amazon.nova-micro-v1:0`
- `bedrock/amazon.nova-lite-v1:0`
- `bedrock/global.anthropic.claude-opus-4-7`
- `bedrock/us.anthropic.claude-opus-4-7`
- `bedrock/openai.gpt-oss-20b-1:0`
- `bedrock/meta.llama...`
- `bedrock/mistral...`

查看 Bedrock 控制台获取确切的模型 ID 和区域可用性。某些模型需要跨区域推理配置文件 ID 如 `us.*`、`eu.*` 或 `global.*`。

**5. 高级模型字段**

可以通过 `extraBody` 提供特定于模型的字段；nanobot 会将其合并到 Converse `additionalModelRequestFields` 中：

```json
{
  "providers": {
    "bedrock": {
      "region": "us-east-1",
      "extraBody": {
        "thinking": {
          "type": "adaptive",
          "effort": "medium",
          "display": "summarized"
        }
      }
    }
  }
}
```

仅对自定义 Bedrock Runtime 端点 URL 使用 `apiBase`（如 VPC 端点或代理）。正常 AWS 区域不需要。

当前范围：nanobot 传递 `messages`、`system`、`inferenceConfig`、`toolConfig` 和 `additionalModelRequestFields`。Bedrock Prompt Management、Guardrails、`serviceTier` 和其他顶层 Converse 选项目前还不是一等配置字段。

**6. 快速检查**

```bash
# 对于 AWS 凭据链使用：
aws sts get-caller-identity

# 对于 API 密钥使用：
export AWS_BEARER_TOKEN_BEDROCK="your-bedrock-api-key"
export AWS_REGION="us-east-1"
```

然后运行：

```bash
nanobot agent -m "用一句话回复。"
```

</details>


<details>
<summary><b>OpenAI Codex (OAuth)</b></summary>

Codex 使用 OAuth 而非 API 密钥。需要 ChatGPT Plus 或 Pro 账户。
不需要在 `config.json` 中添加 `providers.openaiCodex` 块；`nanobot provider login` 将 OAuth 会话存储在配置之外。

**1. 登录：**
```bash
nanobot provider login openai-codex
```

**2. 设置模型**（合并到 `~/.nanobot/config.json`）：
```json
{
  "agents": {
    "defaults": {
      "model": "openai-codex/gpt-5.1-codex"
    }
  }
}
```

**3. 对话：**
```bash
nanobot agent -m "你好！"

# 本地定位特定工作区/配置
nanobot agent -c ~/.nanobot-telegram/config.json -m "你好！"

# 在该配置基础上进行一次性工作区覆盖
nanobot agent -c ~/.nanobot-telegram/config.json -w /tmp/nanobot-telegram-test -m "你好！"
```

> Docker 用户：使用 `docker run -it` 进行交互式 OAuth 登录。

</details>


<details>
<summary><b>GitHub Copilot (OAuth)</b></summary>

GitHub Copilot 使用 OAuth 而非 API 密钥。需要有[配置了计划的 GitHub 账户](https://github.com/features/copilot/plans)。
不需要在 `config.json` 中添加 `providers.githubCopilot` 块；`nanobot provider login` 将 OAuth 会话存储在配置之外。

**1. 登录：**
```bash
nanobot provider login github-copilot
```

**2. 设置模型**（合并到 `~/.nanobot/config.json`）：
```json
{
  "agents": {
    "defaults": {
      "model": "github-copilot/gpt-4.1"
    }
  }
}
```

**3. 对话：**
```bash
nanobot agent -m "你好！"

# 本地定位特定工作区/配置
nanobot agent -c ~/.nanobot-telegram/config.json -m "你好！"

# 在该配置基础上进行一次性工作区覆盖
nanobot agent -c ~/.nanobot-telegram/config.json -w /tmp/nanobot-telegram-test -m "你好！"
```

> Docker 用户：使用 `docker run -it` 进行交互式 OAuth 登录。

</details>


<details>
<summary><b>LongCat (OpenAI 兼容)</b></summary>

LongCat 可通过 nanobot 内置的 OpenAI 兼容 provider 流程使用。
默认 API 基础 URL 已经指向 `https://api.longcat.chat/openai/v1`，所以你
通常只需要设置 `apiKey`。

```json
{
  "providers": {
    "longcat": {
      "apiKey": "${LONGCAT_API_KEY}"
    }
  },
  "agents": {
    "defaults": {
      "provider": "longcat",
      "model": "LongCat-Flash-Chat"
    }
  }
}
```

官方模型名称包括 `LongCat-Flash-Chat`、`LongCat-Flash-Thinking`、
`LongCat-Flash-Thinking-2601` 和 `LongCat-Flash-Lite`。

</details>

<details>
<summary><b>蚂蚁百灵 (OpenAI 兼容)</b></summary>

蚂蚁百灵可通过 nanobot 内置的 OpenAI 兼容 provider 流程使用。
默认 API 基础 URL 指向 `https://api.ant-ling.com/v1`，所以你通常
只需要设置 `apiKey`。

```json
{
  "providers": {
    "antLing": {
      "apiKey": "${ANT_LING_API_KEY}"
    }
  },
  "agents": {
    "defaults": {
      "provider": "ant_ling",
      "model": "Ling-2.6-flash"
    }
  }
}
```

官方 OpenAI 兼容模型名称包括 `Ling-2.6-1T`、
`Ling-2.6-flash`、`Ling-2.5-1T`、`Ling-1T`、`Ring-2.5-1T` 和 `Ring-1T`。

</details>

<details>
<summary><b>自定义 Provider（任何 OpenAI 兼容 API）</b></summary>

直接连接任何 OpenAI 兼容端点 — llama.cpp、Together AI、Fireworks、Azure OpenAI 或任何自托管服务器。模型名称原样传递。

```json
{
  "providers": {
    "custom": {
      "apiKey": "your-api-key",
      "apiBase": "https://api.your-provider.com/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "your-model-name"
    }
  }
}
```

> 对于不需要认证的本地服务器，将 `apiKey` 设为 `null`。
>
> `custom` 是暴露 OpenAI 兼容**聊天补全**API 的 provider 的正确选择。它**不会**强制第三方端点到 OpenAI/Azure 的 **Responses API**。
>
> 如果你的代理或网关专门兼容 Responses-API，请改用 `azure_openai` provider 形状并将 `apiBase` 指向该端点：
>
> ```json
> {
>   "providers": {
>     "azure_openai": {
>       "apiKey": "your-api-key",
>       "apiBase": "https://api.your-provider.com",
>       "defaultModel": "your-model-name"
>     }
>   },
>   "agents": {
>     "defaults": {
>       "provider": "azure_openai",
>       "model": "your-model-name"
>     }
>   }
> }
> ```
>
> 简而言之：**兼容聊天补全的端点 → `custom`**；**兼容 Responses 的端点 → `azure_openai`**。

某些 OpenAI 兼容网关暴露请求体扩展如 vLLM 引导解码或局部采样控制。将它们放在 `extraBody` 下；nanobot 会在 provider 默认值之后将其合并到聊天补全请求体中：

```json
{
  "providers": {
    "custom": {
      "apiKey": "your-api-key",
      "apiBase": "https://api.your-provider.com/v1",
      "extraBody": {
        "repetition_penalty": 1.15,
        "chat_template_kwargs": {
          "enable_thinking": false
        }
      }
    }
  }
}
```

</details>

<a id="local-providers"></a>
<a id="ollama-local"></a>
<details>
<summary><b>Ollama (本地)</b></summary>

使用 Ollama 运行本地模型，然后添加到配置：

**1. 启动 Ollama**（示例）：
```bash
ollama run llama3.2
```

**2. 添加到配置**（部分 — 合并到 `~/.nanobot/config.json`）：
```json
{
  "providers": {
    "ollama": {
      "apiBase": "http://localhost:11434"
    }
  },
  "agents": {
    "defaults": {
      "provider": "ollama",
      "model": "llama3.2"
    }
  }
}
```

> 当 `providers.ollama.apiBase` 已配置时，`provider: "auto"` 也有效，但设置 `"provider": "ollama"` 是最清晰的选择。

</details>

<details>
<summary><b>LM Studio (本地)</b></summary>

[LM Studio](https://lmstudio.ai/) 提供了一个用于运行 LLM 的本地 OpenAI 兼容服务器。通过 LM Studio UI 下载模型，然后启动本地服务器。

**1. 启动 LM Studio 服务器：**
- 启动 LM Studio
- 进入"本地服务器"标签页
- 加载模型（如 Llama、Mistral、Qwen）
- 点击"启动服务器"（默认端口：1234）

**2. 添加到配置**（部分 — 合并到 `~/.nanobot/config.json`）：
```json
{
  "providers": {
    "lm_studio": {
      "apiKey": null,
      "apiBase": "http://localhost:1234/v1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "lm_studio",
      "model": "local-model"
    }
  }
}
```

> **注意：** 由于 LM Studio 在本地运行且不需要认证，请将 `apiKey` 设置为 `null`。模型名称应与 LM Studio UI 中显示的一致。
> 当 `providers.lm_studio.apiBase` 已配置时，`provider: "auto"` 也有效，但设置 `"provider": "lm_studio"` 是最清晰的选择。

</details>

<a id="atomic-chat-local"></a>
<details>
<summary><b>Atomic Chat (本地)</b></summary>

[Atomic Chat](https://atomic.chat/) 是一个本地优先的桌面应用，暴露了 **OpenAI 兼容**的 HTTP API（默认 `http://localhost:1337/v1`）。当你想在自已的机器上运行 nanobot 连接模型而非托管的 API provider 时使用它。

**1. 启动 Atomic Chat**

- 在你的机器上安装 [Atomic Chat](https://atomic.chat/)
- 打开 Atomic Chat，下载模型并保持应用运行。本地 API 默认启用。
- 复制本地 API 暴露的模型 ID。例如，`Qwen 3 32B` 的模型 ID 可能是 `qwen3-32b`。

**2. 添加到配置**（部分 — 合并到 `~/.nanobot/config.json`）：

```json
{
  "providers": {
    "atomic_chat": {
      "apiKey": null,
      "apiBase": "http://localhost:1337/v1"
    }
  },
  "agents": {
    "defaults": {
      "provider": "atomic_chat",
      "model": "qwen3-32b"
    }
  }
}
```

> **注意：** 用 Atomic Chat 的模型 ID 替换 `qwen3-32b`。如果你的 Atomic Chat 服务器不需要密钥，请将 `apiKey` 设为 `null`。如果需要，请设置 `apiKey`（或 `ATOMIC_CHAT_API_KEY` 环境变量）为 Atomic Chat 期望的值。
> 当 `providers.atomic_chat.apiBase` 已配置时，`provider: "auto"` 也有效，但设置 `"provider": "atomic_chat"` 是最清晰的选择。

</details>

<details>
<summary><b>OpenVINO Model Server (本地 / OpenAI 兼容)</b></summary>

使用 [OpenVINO Model Server](https://docs.openvino.ai/2026/model-server/ovms_docs_llm_quickstart.html) 在 Intel GPU 上本地运行 LLM。OVMS 在 `/v3` 暴露 OpenAI 兼容 API。

> 需要 Docker 和具有驱动访问权限的 Intel GPU（`/dev/dri`）。

**1. 拉取模型**（示例）：

```bash
mkdir -p ov/models && cd ov

docker run -d \
  --rm \
  --user $(id -u):$(id -g) \
  -v $(pwd)/models:/models \
  openvino/model_server:latest-gpu \
  --pull \
  --model_name openai/gpt-oss-20b \
  --model_repository_path /models \
  --source_model OpenVINO/gpt-oss-20b-int4-ov \
  --task text_generation \
  --tool_parser gptoss \
  --reasoning_parser gptoss \
  --enable_prefix_caching true \
  --target_device GPU
```

> 这会下载模型权重。等待容器完成后再继续。

**2. 启动服务器**（示例）：

```bash
docker run -d \
  --rm \
  --name ovms \
  --user $(id -u):$(id -g) \
  -p 8000:8000 \
  -v $(pwd)/models:/models \
  --device /dev/dri \
  --group-add=$(stat -c "%g" /dev/dri/render* | head -n 1) \
  openvino/model_server:latest-gpu \
  --rest_port 8000 \
  --model_name openai/gpt-oss-20b \
  --model_repository_path /models \
  --source_model OpenVINO/gpt-oss-20b-int4-ov \
  --task text_generation \
  --tool_parser gptoss \
  --reasoning_parser gptoss \
  --enable_prefix_caching true \
  --target_device GPU
```

**3. 添加到配置**（部分 — 合并到 `~/.nanobot/config.json`）：

```json
{
  "providers": {
    "ovms": {
      "apiBase": "http://localhost:8000/v3"
    }
  },
  "agents": {
    "defaults": {
      "provider": "ovms",
      "model": "openai/gpt-oss-20b"
    }
  }
}
```

> OVMS 是本地服务器 — 不需要 API 密钥。支持工具调用（`--tool_parser gptoss`）、推理（`--reasoning_parser gptoss`）和流式传输。
> 更多详情参见[官方 OVMS 文档](https://docs.openvino.ai/2026/model-server/ovms_docs_llm_quickstart.html)。
</details>

<a id="vllm-local-openai-compatible"></a>
<details>
<summary><b>vLLM (本地 / OpenAI 兼容)</b></summary>

使用 vLLM 或任何 OpenAI 兼容服务器运行你自己的模型，然后添加到配置：

**1. 启动服务器**（示例）：
```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

**2. 添加到配置**（部分 — 合并到 `~/.nanobot/config.json`）：

*Provider（本地服务器无需设置 API 密钥）：*
```json
{
  "providers": {
    "vllm": {
      "apiKey": null,
      "apiBase": "http://localhost:8000/v1"
    }
  }
}
```

*模型：*
```json
{
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

</details>

<details>
<summary><b>添加新 Provider（开发者指南）</b></summary>

nanobot 使用 **Provider Registry**（`nanobot/providers/registry.py`）作为唯一真实来源。
添加新 provider 只需 **2 步** — 无需触碰 if-elif 链。

**步骤 1.** 向 `nanobot/providers/registry.py` 中的 `PROVIDERS` 添加 `ProviderSpec` 条目：

```python
ProviderSpec(
    name="myprovider",                   # 配置字段名
    keywords=("myprovider", "mymodel"),  # 用于自动匹配的模型名称关键词
    env_key="MYPROVIDER_API_KEY",        # 环境变量名
    display_name="My Provider",          # 显示在 `nanobot status` 中
    default_api_base="https://api.myprovider.com/v1",  # OpenAI 兼容端点
)
```

**步骤 2.** 向 `nanobot/config/schema.py` 中的 `ProvidersConfig` 添加字段：

```python
class ProvidersConfig(BaseModel):
    ...
    myprovider: ProviderConfig = ProviderConfig()
```

就这样！环境变量、模型路由、配置匹配和 `nanobot status` 显示都会自动工作。

**常用 `ProviderSpec` 选项：**

| 字段 | 描述 | 示例 |
|------|------|------|
| `default_api_base` | OpenAI 兼容基础 URL | `"https://api.deepseek.com"` |
| `env_extras` | 要设置的额外环境变量 | `(("ZHIPUAI_API_KEY", "{api_key}"),)` |
| `model_overrides` | 每模型参数覆盖 | `(("kimi-k2.5", {"temperature": 1.0}), ("kimi-k2.6", {"temperature": 1.0}),)` |
| `is_gateway` | 可以路由任何模型（如 OpenRouter） | `True` |
| `detect_by_key_prefix` | 通过 API 密钥前缀检测网关 | `"sk-or-"` |
| `detect_by_base_keyword` | 通过 API 基础 URL 检测网关 | `"openrouter"` |
| `strip_model_prefix` | 发送到网关前剥离 provider 前缀 | `True`（用于 AiHubMix） |
| `supports_max_completion_tokens` | 使用 `max_completion_tokens` 而非 `max_tokens`；用于拒绝同时设置两者的 provider（如火山引擎） | `True` |

</details>

## 模型预设

模型预设允许你命名完整的模型配置并在运行时通过 `/model <preset>` 切换。

现有配置无需更改。如果你没有设置 `modelPresets` 或 `agents.defaults.modelPreset`，nanobot 会继续完全像以前一样使用 `agents.defaults.*`。

```json
{
  "agents": {
    "defaults": {
      "model": "openai/gpt-4.1",
      "provider": "openai",
      "maxTokens": 8192,
      "contextWindowTokens": 128000,
      "temperature": 0.1,
      "modelPreset": "fast",
      "fallbackModels": ["deep"]
    }
  },
  "modelPresets": {
    "fast": {
      "model": "openai/gpt-4.1-mini",
      "provider": "openai",
      "maxTokens": 4096,
      "contextWindowTokens": 128000,
      "temperature": 0.2,
      "reasoningEffort": "low"
    },
    "deep": {
      "model": "anthropic/claude-opus-4-5",
      "provider": "anthropic",
      "maxTokens": 8192,
      "contextWindowTokens": 200000,
      "reasoningEffort": "high"
    }
  }
}
```

`modelPresets` 是顶层对象。其下的键（`fast`、`deep`、`coding` 等）是用户定义的预设名称。每个预设支持：

| 字段 | 描述 |
|------|------|
| `model` | 该预设使用的模型名称。 |
| `provider` | Provider 名称，或 `"auto"` 以使用 provider 自动检测。 |
| `maxTokens` | 最大完成/输出 token 数。 |
| `contextWindowTokens` | 用于提示构建和整合决策的上下文窗口大小。 |
| `temperature` | 采样温度。 |
| `reasoningEffort` | 可选的推理/思考设置。各 provider 支持情况不同。 |

`default` 是保留名称，始终表示由 `agents.defaults.*` 构建的隐式预设；不要定义 `modelPresets.default`。使用 `/model default` 切换回 `agents.defaults.*`。

### 模型回退

`agents.defaults.fallbackModels` 定义了活动模型配置的有序故障转移链。主模型仍由 `agents.defaults.modelPreset` 选择（或当没有激活预设时使用隐式默认配置）。

每个回退候选可以是：

- 来自 `modelPresets` 的预设名称，如 `"deep"`。预设的完整模型、provider、生成和上下文窗口配置都会被使用。
- 至少包含 `provider` 和 `model`的内联回退对象。可选的 `maxTokens`、`contextWindowTokens` 和 `temperature` 字段在省略时继承自主配置。`reasoningEffort` 不会继承；省略则关闭该回退的推理，或为支持推理的模型显式设置。

```json
{
  "agents": {
    "defaults": {
      "modelPreset": "fast",
      "fallbackModels": [
        "deep",
        {
          "provider": "deepseek",
          "model": "deepseek-v4-pro",
          "maxTokens": 4096,
          "contextWindowTokens": 262144
        }
      ]
    }
  }
}
```

字符串条目是预设名称，不是原始模型名。如果你想使用尚未成为预设的模型，请使用内联对象形式。

回退仅在主 provider 在任何答案文本被流式传输之前返回可重试的模型/provider 错误时运行。典型回退情况包括超时、连接错误、5xx 服务器错误、429 速率限制、过载以及配额/余额耗尽。它不会针对畸形请求、认证/权限错误、内容过滤/拒绝或上下文长度/消息格式错误运行。

如果回退候选使用较小的 `contextWindowTokens` 值，nanobot 会使用活动链中最小的窗口构建上下文，以便每个候选都能接收相同的提示。

设置 `agents.defaults.modelPreset` 以使用命名预设开始：

```json
{
  "agents": {
    "defaults": {
      "modelPreset": "fast"
    }
  }
}
```

当 `modelPreset` 为 `null` 或省略时，启动时使用来自 `agents.defaults.*` 的隐式 `default` 预设。通过 `/model <preset>` 进行的运行时更改不会写回 `config.json`；它们影响未来的回合，直到进程重启或另一个模型/配置更改替换它们。

## 频道设置

应用于所有频道的全局设置。在 `~/.nanobot/config.json` 的 `channels` 部分下配置：

```json
{
  "channels": {
    "sendProgress": true,
    "sendToolHints": false,
    "sendMaxRetries": 3,
    "transcriptionProvider": "groq",
    "transcriptionLanguage": null,
    "telegram": { ... }
  }
}
```

| 设置 | 默认值 | 描述 |
|------|--------|------|
| `sendProgress` | `true` | 将 Agent 文本进度流式传输到频道 |
| `sendToolHints` | `false` | 流式传输工具调用提示（如 `read_file("…")`） |
| `showReasoning` | `true` | 允许频道展示模型推理/思考内容（DeepSeek-R1 的 `reasoning_content`、Anthropic 的 `thinking_blocks`、内联 `think` 标签）。推理作为带有 `_reasoning_delta` / `_reasoning_end` 标记的专用流传输 — 频道通过重写 `send_reasoning_delta` / `send_reasoning_end` 来渲染原地更新。即使设为 `true`，没有这些重写的频道也会保持静默空操作。目前在 CLI 和 WebSocket/WebUI 上可用（斜体闪烁头部，流结束后自动折叠）；Telegram / Slack / Discord / 飞书 / 微信 / Matrix 在气泡 UI 适配之前保持基类空操作。与 `sendProgress` 独立。 |
| `sendMaxRetries` | `3` | 每条出站消息的最大投递尝试次数，包括初始发送（配置范围 0-10，最少实际尝试 1 次） |
| `transcriptionProvider` | `"groq"` | 语音转写后端：`"groq"`（免费层，默认）或 `"openai"`。API 密钥从匹配的 provider 配置中自动解析。 |
| `transcriptionLanguage` | `null` | 音频转写的可选 ISO-639-1 语言提示，如 `"en"`、`"ko"`、`"ja"`。 |

`sendProgress` 和 `sendToolHints` 也可以在每个频道上覆盖。全局值作为不设置自己值的频道的默认值：

```json
{
  "channels": {
    "sendProgress": true,
    "sendToolHints": false,
    "telegram": {
      "enabled": true,
      "sendProgress": false
    },
    "websocket": {
      "enabled": true,
      "sendToolHints": true
    }
  }
}
```

### 重试行为

重试机制有意设计得很简单。

当频道 `send()` 抛出异常时，nanobot 会在频道管理器层重试。默认情况下，`channels.sendMaxRetries` 为 `3`，该计数包括初始发送。

- **第 1 次尝试**：立即发送
- **第 2 次尝试**：`1s` 后重试
- **第 3 次尝试**：`2s` 后重试
- **更高的重试预算**：退避继续为 `1s`、`2s`、`4s`，然后保持在 `4s` 封顶
- **瞬态故障**：网络抖动和临时 API 限制通常在下一次尝试时恢复
- **永久性故障**：无效 token、撤销的访问或被禁用的频道会耗尽重试预算并干净利落地失败

> [!NOTE]
> 这种设计是故意的：频道实现应该在投递失败时抛出异常，频道管理器拥有共享的重试策略。
>
> 某些频道可能仍在内部应用小的 API 特定重试。例如，Telegram 在最终失败暴露给管理器之前会分别重试超时和洪水控制错误。
>
> 如果某个频道完全不可达，nanobot 无法通过同一频道通知用户。注意日志中的 `Failed to send to {channel} after N attempts` 以发现持续的投递故障。

## 网络工具

nanobot 包含基本的网络访问工具。包括通过 API 搜索和以 Markdown 格式抓取任意网页。它们默认启用，可以在 `~/.nanobot/config.json` 的 `tools.web` 下配置。

如果你想禁用它们（这会从发送给 LLM 的工具列表中移除 `web_search` 和 `web_fetch`），请将 `tools.web.enable` 设为 `false`：

```json
{
  "tools": {
    "web": {
      "enable": false
    }
  }
}
```

如果你想允许信任的私有地址范围如 Tailscale / CGNAT 地址，你可以通过 `tools.ssrfWhitelist` 明确地将它们从 SSRF 阻断中豁免：

```json
{
  "tools": {
    "ssrfWhitelist": ["100.64.0.0/10"]
  }
}
```

> [!TIP]
> 使用 `tools.web` 中的 `proxy` 通过代理路由所有网络请求（搜索 + 抓取）：
> ```json
> { "tools": { "web": { "proxy": "http://127.0.0.1:7890" } } }
> ```

### `tools.web`

| 选项 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `enable` | boolean | `true` | 启用或禁用所有内置网络工具（`web_search` + `web_fetch`） |
| `proxy` | string 或 null | `null` | 所有网络请求的代理，例如 `http://127.0.0.1:7890` |
| `userAgent` | string 或 null | `null` | 所有网络请求的 User-Agent 请求头。如果为 null，将使用浏览器 UA |

### 网络搜索

nanobot 支持多个网络搜索 provider。在 `~/.nanobot/config.json` 的 `tools.web.search` 下配置。

默认情况下，网络搜索使用 `duckduckgo`，开箱即用且无需 API 密钥。

| Provider | 配置字段 | 环境变量回退 | 免费 |
|----------|----------|-------------|------|
| `brave` | `apiKey` | `BRAVE_API_KEY` | 否 |
| `tavily` | `apiKey` | `TAVILY_API_KEY` | 否 |
| `jina` | `apiKey` | `JINA_API_KEY` | 免费层（1000 万 token） |
| `kagi` | `apiKey` | `KAGI_API_KEY` | 否 |
| `olostep` | `apiKey` | `OLOSTEP_API_KEY` | 否 |
| `searxng` | `baseUrl` | `SEARXNG_BASE_URL` | 是（自托管） |
| `duckduckgo`（默认） | — | — | 是 |

**Brave：**
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "apiKey": "${BRAVE_API_KEY}"
      }
    }
  }
}
```

**Tavily：**
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "tavily",
        "apiKey": "${TAVILY_API_KEY}"
      }
    }
  }
}
```

**Jina**（带 1000 万 token 的免费层）：
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "jina",
        "apiKey": "${JINA_API_KEY}"
      }
    }
  }
}
```

**Kagi：**
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "kagi",
        "apiKey": "${KAGI_API_KEY}"
      }
    }
  }
}
```

**Olostep：**
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "olostep",
        "apiKey": "${OLOSTEP_API_KEY}"
      }
    }
  }
}
```

你也可以在环境中设置 `OLOSTEP_API_KEY` 而不是将其存储在配置中。

**SearXNG**（自托管，无需 API 密钥）：
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "searxng",
        "baseUrl": "https://searx.example"
      }
    }
  }
}
```

**DuckDuckGo**（零配置）：
```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "duckduckgo"
      }
    }
  }
}
```

#### `tools.web.search`

| 选项 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `provider` | string | `"duckduckgo"` | 搜索后端：`brave`、`tavily`、`jina`、`searxng`、`duckduckgo` |
| `apiKey` | string | `""` | Brave 或 Tavily 的 API 密钥 |
| `baseUrl` | string | `""` | SearXNG 的基础 URL |
| `maxResults` | integer | `5` | 每次搜索的结果数（1–10） |

### 网页抓取

> [!TIP]
> 如果你在 JS 工作量证明或 Cloudflare 验证码方面遇到问题，请设置随机用户代理并禁用 Jina Reader：
> ```json
> { "tools": { "web": { "userAgent": "Not-A-Browser", "fetch": { "useJinaReader": false } } } }
> ```

nanobot 默认使用第三方 API [Jina Reader](https://jina.ai/reader/) 将任意页面转换为 Markdown 格式以便 LLM 消化，如果前者失败则会基于 [readability-lxml](https://github.com/buriy/python-readability) 使用本地回退。

如果你想始终使用本地转换，可以强制执行：

```json
{
  "tools": {
    "web": {
      "fetch": {
        "useJinaReader": false
      }
    }
  }
}
```

#### `tools.web.fetch`

| 选项 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `useJinaReader` | boolean | `true` | 如果为 true，优先使用 Jina Reader 而非本地转换 |

## 图片生成

图片生成在 `tools.imageGeneration` 下配置，使用来自 `providers.openrouter` 或 `providers.aihubmix` 的 provider 凭据。

参见[图片生成](./image-generation.md)了解 WebUI 使用方法、provider 示例、制品存储和故障排除。

## MCP (Model Context Protocol)

> [!TIP]
> 配置格式与 Claude Desktop / Cursor 兼容。你可以直接从任何 MCP 服务器的 README 复制 MCP 服务器配置。

nanobot 支持 [MCP](https://modelcontextprotocol.io/) — 连接外部工具服务器并将它们用作原生 Agent 工具。

将 MCP 服务器添加到你的 `config.json`：

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
      },
      "my-remote-mcp": {
        "url": "https://example.com/mcp/",
        "headers": {
          "Authorization": "Bearer xxxxx"
        }
      }
    }
  }
}
```

支持两种传输模式：

| 模式 | 配置 | 示例 |
|------|------|------|
| **Stdio** | `command` + `args` | 通过 `npx` / `uvx` 的本地进程 |
| **HTTP** | `url` + `headers`（可选） | 远程端点（`https://mcp.example.com/sse`） |

使用 `toolTimeout` 覆盖慢速服务器的默认每次调用 30 秒超时：

```json
{
  "tools": {
    "mcpServers": {
      "my-slow-server": {
        "url": "https://example.com/mcp/",
        "toolTimeout": 120
      }
    }
  }
}
```

使用 `enabledTools` 仅注册 MCP 服务器的工具子集：

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
        "enabledTools": ["read_file", "mcp_filesystem_write_file"]
      }
    }
  }
}
```

`enabledTools` 接受原始 MCP 工具名称（如 `read_file`）或包装后的 nanobot 工具名称（如 `mcp_filesystem_write_file`）。

- 省略 `enabledTools` 或设为 `["*"]` 以注册所有工具。
- 将 `enabledTools` 设为 `[]` 以不注册该服务器的任何工具。
- 将 `enabledTools` 设为非空名称列表以仅注册该子集。

MCP 工具会在启动时自动发现和注册。LLM 可以与内置工具一起使用它们 — 无需额外配置。



## 安全性

> [!TIP]
> 对于生产部署，请在配置中设置 `"restrictToWorkspace": true` 和 `"tools.exec.sandbox": "bwrap"` 以将 Agent 沙箱化。

关于 API 密钥、token 和其他密钥，参见[用于密钥的环境变量](#environment-variables-for-secrets) — 避免将它们直接存储在 `config.json` 中。

| 选项 | 默认值 | 描述 |
|------|--------|------|
| `tools.restrictToWorkspace` | `false` | 当为 `true` 时，将**所有** Agent 工具（shell、文件读写/编辑、列表）限制在工作区目录下。防止路径遍历和越权访问。 |
| `tools.exec.sandbox` | `""` | Shell 命令的沙箱后端。设置为 `"bwrap"` 以将 exec 调用包装在 [bubblewrap](https://github.com/containers/bubblewrap) 沙箱中 — 进程只能看到工作区（读写）和媒体目录（只读）；配置文件和 API 密钥被隐藏。会为文件工具自动启用 `restrictToWorkspace`。**仅限 Linux** — 需要安装 `bwrap`（`apt install bubblewrap`；Docker 镜像中已预装）。macOS 或 Windows 上不可用（bwrap 依赖 Linux 内核命名空间）。 |
| `tools.exec.enable` | `true` | 当为 `false` 时，不会注册 shell `exec` 工具。用它来完全禁用 shell 命令执行。 |
| `tools.exec.pathAppend` | `""` | 运行 shell 命令时要追加到 `PATH` 的额外目录（如 `/usr/sbin` 用于 `ufw`）。 |
| `channels.*.allowFrom` | 省略 | 每个频道的访问控制。省略以使用纯配对模式；设置 `["*"]` 允许所有人；或列出特定用户 ID。详情参见[配对（Pairing）](#pairing)。 |

**Docker 安全性**：官方 Docker 镜像以非 root 用户（`nanobot`，UID 1000）运行并预装了 bubblewrap。使用 `docker-compose.yml` 时，容器除 `SYS_ADMIN`（bwrap 的命名空间隔离所需）外放弃所有 Linux 能力。

## 配对（Pairing）

配对让用户通过简单的代码交换即可获得 Bot 访问权限 — 无需编辑配置。这对新用户和从新频道连接的现有用户（如已在 Telegram 批准的用户现在设置 Discord）都有效。

### 工作原理

1. 用户在任何频道（Telegram、Discord、Slack 等）上向 Bot 发送私聊，他们在该频道上尚未被批准。
2. Bot 回复一个配对码（如 `ABCD-EFGH`）并告诉他们转发给你。
3. 你批准该代码：

```text
/pairing approve ABCD-EFGH
```

4. 该用户现在可以正常与 Bot 聊天。

配对仅在**私聊**中有效 — 群聊中未批准的用户会被静默忽略。

### 纯配对模式

默认情况下，如果你不设置 `allowFrom`，任何尚未被批准的人在私聊 Bot 时都会收到配对码。这意味着你可以完全跳过 `allowFrom` 并通过配对所有访问权限进行管理：

```json
{
  "channels": {
    "telegram": {
      "enabled": true
    }
  }
}
```

如果你更倾向于允许所有人而无需批准：

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "allowFrom": ["*"]
    }
  }
}
```

### 访问管理

| 命令 | 功能 |
|------|------|
| `/pairing` | 显示所有待处理的配对请求 |
| `/pairing approve <code>` | 批准请求 — 发送者现在可以聊天 |
| `/pairing deny <code>` | 拒绝待处理的请求 |
| `/pairing revoke <user_id>` | 从当前频道移除先前批准的用户 |
| `/pairing revoke <channel> <user_id>` | 从特定频道移除用户 |

你可以在 `/pairing list` 输出中找到用户 ID。

从终端：

```bash
nanobot agent -m "/pairing list"
nanobot agent -m "/pairing approve ABCD-EFGH"
```

## 子 Agent 并发

默认情况下，nanobot 一次只允许一个生成的子 Agent 运行。当达到限制时，`spawn` 工具会返回错误，以便 Agent 决定等待或重新安排工作。这保护本地 LLM 服务器免于同时加载多个 KV 缓存。如果你的 provider 可以处理更多并行工作，可以提高限制：

```json
{
  "agents": {
    "defaults": {
      "maxConcurrentSubagents": 2
    }
  }
}
```

| 选项 | 默认值 | 描述 |
|------|--------|------|
| `agents.defaults.maxConcurrentSubagents` | `1` | 可同时运行的生成子 Agent 的最大数量。超出此限制的 spawn 尝试会返回错误。 |

## 自动压缩

当用户空闲时间超过配置的阈值时，nanobot 会**主动**将会话上下文中较旧的部分压缩为摘要，同时保留最近的合法存活消息。这减少了 token 成本和首 token 延迟 — 当用户回来时，模型接收到紧凑的摘要、最近的活动上下文和新鲜输入，而不是重新处理一个长过期上下文及其失效的 KV 缓存。

```json
{
  "agents": {
    "defaults": {
      "idleCompactAfterMinutes": 15
    }
  }
}
```

| 选项 | 默认值 | 描述 |
|------|--------|------|
| `agents.defaults.idleCompactAfterMinutes` | `0`（禁用） | 自动压缩开始前的空闲分钟数。设为 `0` 以禁用。推荐：`15` — 接近典型的 LLM KV 缓存过期窗口，这样用户回来前过期会话会被压缩。 |

`sessionTtlMinutes` 作为遗留别名仍被接受以向后兼容，但 `idleCompactAfterMinutes` 是今后推荐的配置键。

工作原理：
1. **空闲检测**：每个空闲时钟周期（约 1 s），检查所有会话是否过期。
2. **后台压缩**：过期会话通过 LLM 总结较旧的活动前缀，并保留最近的合法后缀（目前 8 条消息）。
3. **摘要注入**：当用户回来时，摘要作为运行时上下文注入（一次性，不持久化），同时附带保留的最近后缀。
4. **重启安全恢复**：摘要也被镜像到会话元数据中，以便进程重启后仍能恢复。

> [!NOTE]
> 心理模型："总结较旧的上下文，保留最新的活跃回合，**并用紧凑形式覆盖会话文件。** 它不是完整的 `session.clear()`，但它是一个写入 — 不是软光标移动。
>
> 具体来说，自动压缩会原地重写 `sessions/<key>.jsonl`：较旧的消息（包括它们的结构化 `tool_calls` / `tool_call_id` / `reasoning_content`）被替换为仅保留的最近后缀（目前 8 条消息），而被归档的前缀仅作为纯文本摘要追加到 `memory/history.jsonl`（如果 LLM 摘要失败则为 `[RAW] ...` 展平转储）。这些回合的原始 JSON 无法再从会话文件中恢复。
>
> 这与**由 token 驱动的软整合**不同，后者在提示超过上下文预算时触发：该路径仅推进内部的 `last_consolidated` 光标并保持会话文件不变，原始工具调用轨迹仍保存在磁盘上并可重放或审计。如果你依赖该轨迹进行调试或审计，请将 `idleCompactAfterMinutes` 保持在默认值 `0` 并让仅 token 驱动的路径运行。

## 时区

时间即上下文。上下文应当精确。

默认情况下，nanobot 使用 `UTC` 作为运行时时上下文。如果你希望 Agent 以你的本地时间思考，请将 `agents.defaults.timezone` 设置为有效的 [IANA 时区名称](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)：

```json
{
  "agents": {
    "defaults": {
      "timezone": "Asia/Shanghai"
    }
  }
}
```

这会影响显示给模型的运行时时间字符串，如运行时上下文和心跳提示。当 cron 表达式省略 `tz` 时，它也成为 cron 调度的默认时区，以及 ISO 日期时间没有明确偏移时的一次性 `at` 时间的默认时区。

常见示例：`UTC`、`America/New_York`、`America/Los_Angeles`、`Europe/London`、`Europe/Berlin`、`Asia/Tokyo`、`Asia/Shanghai`、`Asia/Singapore`、`Australia/Sydney`。

> 还需要其他时区？浏览完整的 [IANA 时区数据库](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)。

## 统一会话

默认情况下，每个频道 × 聊天 ID 组合都有自己的会话。如果你跨多个频道（如 Telegram + Discord + CLI）使用 nanobot 并希望它们共享相同的对话，请启用 `unifiedSession`：

```json
{
  "agents": {
    "defaults": {
      "unifiedSession": true
    }
  }
}
```

启用后，所有传入消息 — 无论从哪个频道到达 — 都被路由到单个共享会话。从 Telegram 切换到 Discord（或任何其他频道）可以无缝继续相同的对话。

| 行为 | `false`（默认） | `true` |
|------|-------------------|--------|
| 会话键 | `channel:chat_id` | `unified:default` |
| 跨频道连续性 | 否 | 是 |
| `/new` 清除 | 当前频道会话 | 共享会话 |
| `/stop` 查找任务 | 按频道会话 | 按共享会话 |
| 已有的 `session_key_override`（如 Telegram 线程） | 受尊重 | 仍然受尊重 — 不会被覆盖 |

> 这是为单用户、多设备设计的。它**默认关闭** — 现有用户行为零变化。

## 禁用的技能

nanobot 附带内置技能，你的工作区也可以在 `skills/` 下定义自定义技能。如果想对 Agent 隐藏特定技能，请将 `agents.defaults.disabledSkills` 设置为技能目录名称列表：

```json
{
  "agents": {
    "defaults": {
      "disabledSkills": ["github", "weather"]
    }
  }
}
```

被禁用的技能会被排除在主 Agent 的技能摘要、常驻技能注入和子 Agent 技能摘要之外。这对于某些捆绑技能对你的部署不必要或不应暴露给最终用户的情况很有用。

| 选项 | 默认值 | 描述 |
|------|--------|------|
| `agents.defaults.disabledSkills` | `[]` | 要排除加载的技能目录名称列表。适用于内置技能和工作区技能。 |

## 工具提示最大长度

工具提示是 Agent 调用工具时显示的简短进度消息（如 `$ cd …/project && npm test`）。默认情况下，它们在 40 个字符处截断，这可能使长命令难以阅读。

设置 `agents.defaults.toolHintMaxLength` 来控制截断阈值：

```json
{
  "agents": {
    "defaults": {
      "toolHintMaxLength": 120
    }
  }
}
```

| 选项 | 默认值 | 描述 |
|------|--------|------|
| `agents.defaults.toolHintMaxLength` | `40` | 工具提示显示的最大字符数。范围：20–500。较高的值显示更多命令或路径内容；较低的值使提示保持紧凑。 |
