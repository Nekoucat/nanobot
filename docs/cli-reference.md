# CLI 参考手册

| 命令 | 描述 |
|------|------|
| `nanobot onboard` | 在 `~/.nanobot/` 下初始化配置和工作区 |
| `nanobot onboard --wizard` | 启动交互式引导配置向导 |
| `nanobot onboard -c <config> -w <workspace>` | 初始化或刷新特定实例配置和工作区 |
| `nanobot agent -m "..."` | 与 Agent 对话 |
| `nanobot agent -w <workspace>` | 对特定工作区进行对话 |
| `nanobot agent -w <workspace> -c <config>` | 对特定工作区/配置进行对话 |
| `nanobot agent` | 交互式聊天模式 |
| `nanobot agent --no-markdown` | 显示纯文本回复 |
| `nanobot agent --logs` | 聊天期间显示运行时日志 |
| `nanobot serve` | 启动 OpenAI 兼容 API |
| `nanobot gateway` | 启动网关 |
| `nanobot status` | 显示状态 |
| `nanobot provider login openai-codex` | Provider OAuth 登录 |
| `nanobot channels login <channel>` | 交互式验证频道身份 |
| `nanobot channels status` | 显示频道状态 |

交互模式退出方式：`exit`、`quit`、`/exit`、`/quit`、`:q` 或 `Ctrl+D`。
