# 聊天内命令

这些命令可在聊天频道和交互式 agent 会话中使用：

| 命令 | 描述 |
|------|------|
| `/new` | 停止当前任务并开始新对话 |
| `/stop` | 停止当前任务 |
| `/restart` | 重启 Bot |
| `/status` | 显示 Bot 状态 |
| `/model` | 显示当前模型和可用的模型预设 |
| `/model <preset>` | 为未来的回合切换运行时模型预设 |
| `/dream` | 立即运行 Dream 记忆整合 |
| `/dream-log` | 显示最新的 Dream 记忆变更 |
| `/dream-log <sha>` | 显示特定的 Dream 记忆变更 |
| `/dream-restore` | 列出最近的 Dream 记忆版本 |
| `/dream-restore <sha>` | 将记忆恢复到特定变更之前的状态 |
| `/pairing` | 列出待处理的配对请求 |
| `/pairing approve <code>` | 批准配对码 |
| `/pairing deny <code>` | 拒绝待处理的配对请求 |
| `/pairing revoke <user_id>` | 撤销当前频道上先前批准的用户 |
| `/pairing revoke <channel> <user_id>` | 撤销特定频道上先前批准的用户 |
| `/help` | 显示可用的聊天内命令 |

## 配对（Pairing）

当有人向 Bot 发送私聊但不在允许列表中时 — 无论是新用户还是现有用户在新频道上 — nanobot 会自动回复一个**配对码**（如 `ABCD-EFGH`），该码在 10 分钟后过期。要授予他们访问权限：

```text
/pairing approve ABCD-EFGH
```

要查看谁在等待，使用 `/pairing`。稍后要移除某人，使用 `/pairing revoke <user_id>` — 你可以在 `/pairing list` 输出中找到用户 ID。

完整的设置指南参见[配置：配对](./configuration.md#pairing)。

## 模型预设

使用 `/model` 检查当前运行时模型：

```text
/model
```

响应会显示当前模型、当前预设和可用的预设名称。`default` 始终可用，代表来自 `agents.defaults.*` 的模型设置。

要切换未来回合的预设：

```text
/model fast
/model deep
/model default
```

预设名称来自顶层 `modelPresets` 配置。切换仅影响运行时：不会重写 `config.json`，进行中的回合保持使用其开始时的模型。设置详情参见[配置：模型预设](./configuration.md#model-presets)。

## 定期任务

网关每 30 分钟唤醒一次并检查你工作区（`~/.nanobot/workspace/HEARTBEAT.md`）中的 `HEARTBEAT.md` 文件。如果有任务，agent 会执行任务并将结果投递到你最近活跃的聊天频道。

**设置：** 编辑 `~/.nanobot/workspace/HEARTBEAT.md`（由 `nanobot onboard` 自动创建）：

```markdown
## 定期任务

- [ ] 查看天气预报并发送摘要
- [ ] 扫描收件箱查找紧急邮件
```

Agent 也可以自己管理此文件 — 让它"添加定期任务"，它会为你更新 `HEARTBEAT.md`。

> **注意：** 网关必须正在运行（`nanobot gateway`），并且你必须至少与 Bot 聊过一次，这样它才知道要投递到哪个频道。
