# 聊天应用

将 nanobot 连接到你喜欢的聊天平台。想自己构建？参见[频道插件指南](./channel-plugin-guide.md)。

| 平台 | 所需信息 |
|------|----------|
| **Telegram** | 来自 @BotFather 的 Bot Token |
| **Discord** | Bot Token + 消息内容 Intent |
| **WhatsApp** | 二维码扫描（`nanobot channels login whatsapp`） |
| **微信（Weixin）** | 二维码扫描（`nanobot channels login weixin`） |
| **飞书（Feishu）** | App ID + App Secret |
| **钉钉（DingTalk）** | App Key + App Secret |
| **Slack** | Bot Token + 应用级 Token |
| **Matrix** | 主服务器 URL + 访问令牌 |
| **邮箱（Email）** | IMAP/SMTP 凭据 |
| **QQ** | App ID + App Secret |
| **企业微信（Wecom）** | Bot ID + Bot Secret |
| **Microsoft Teams** | App ID + App 密码 + 公开 HTTPS 端点 |
| **Mochat** | Claw Token（可自动配置） |
| **Signal** | signal-cli 守护进程 + 电话号码 |

<details>
<summary><b>Telegram</b>（推荐）</summary>

**1. 创建 Bot**
- 打开 Telegram，搜索 `@BotFather`
- 发送 `/newbot`，按提示操作
- 复制 Token

**2. 配置**

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

> 你可以在 Telegram 设置中找到你的**用户 ID**。显示为 `@yourUserId`。
> 复制这个值**不要包含 `@` 符号**并粘贴到配置文件中。


**3. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Mochat (Claw IM)</b></summary>

默认使用 **Socket.IO WebSocket**，HTTP 轮询作为回退。

**1. 让 nanobot 帮你设置 Mochat**

只需向 nanobot 发送这条消息（将 `xxx@xxx` 替换为你的真实邮箱）：

```
Read https://raw.githubusercontent.com/HKUDS/MoChat/refs/heads/main/skills/nanobot/skill.md and register on MoChat. My Email account is xxx@xxx Bind me as your owner and DM me on MoChat.
```

nanobot 会自动注册、配置 `~/.nanobot/config.json` 并连接到 Mochat。

**2. 重启网关**

```bash
nanobot gateway
```

就这样 — nanobot 会处理其余一切！

<br>

<details>
<summary>手动配置（高级）</summary>

如果你更喜欢手动配置，将以下内容添加到 `~/.nanobot/config.json`：

> 请妥善保管 `claw_token`。它只应在请求头 `X-Claw-Token` 中发送给你的 Mochat API 端点。

```json
{
  "channels": {
    "mochat": {
      "enabled": true,
      "base_url": "https://mochat.io",
      "socket_url": "https://mochat.io",
      "socket_path": "/socket.io",
      "claw_token": "claw_xxx",
      "agent_user_id": "6982abcdef",
      "sessions": ["*"],
      "panels": ["*"],
      "reply_delay_mode": "non-mention",
      "reply_delay_ms": 120000
    }
  }
}
```



</details>

</details>

<details>
<summary><b>Discord</b></summary>

**1. 创建 Bot**
- 前往 https://discord.com/developers/applications
- 创建应用程序 → Bot → 添加 Bot
- 复制 Bot Token

**2. 启用 Intents**
- 在 Bot 设置中，启用 **MESSAGE CONTENT INTENT**（消息内容意图）
- （可选）如果你计划使用基于成员数据的允许列表，启用 **SERVER MEMBERS INTENT**（服务器成员意图）

**3. 获取你的用户 ID**
- Discord 设置 → 高级 → 启用 **开发者模式**
- 右键点击你的头像 → **复制用户 ID**

**4. 配置**

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"],
      "allowChannels": [],
      "groupPolicy": "mention",
      "streaming": true
    }
  }
}
```

> `groupPolicy` 控制 Bot 在群聊中的响应方式：
> - `"mention"`（默认）— 仅在被 @提及时响应
> - `"open"` — 响应所有消息
> 私聊中只要发送者在 `allowFrom` 中就会始终响应。
> - 如果将群聊策略设为 open，请创建新线程并将其设为私有线程，然后在其中 @机器人。否则线程本身及其所在的频道都会生成一个 bot 会话。
> `allowChannels` 将 Bot 限制为特定的 Discord 频道 ID。留空（默认）表示在 Bot 可见的每个频道中都响应。示例：`["1234567890", "0987654321"]`。过滤发生在 `allowFrom` 之后，所以两者都必须通过。允许的父频道下的 Discord 线程也被允许；对于论坛频道，允许父论坛频道意味着该论坛的所有线程/帖子都允许。
> `streaming` 默认为 `true`。仅在你明确想要非流式回复时才禁用它。

**5. 邀请 Bot**
- OAuth2 → URL 生成器
- 权限范围：`bot`
- Bot 权限：`Send Messages`（发送消息）、`Read Message History`（读取消息历史）
- 打开生成的邀请链接并将 Bot 添加到你的服务器

**6. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Matrix (Element)</b></summary>

首先安装 Matrix 依赖：

```bash
pip install nanobot-ai[matrix]
```

> [!NOTE]
> Matrix 不支持 Windows。`matrix-nio[e2e]` 依赖于
> `python-olm`，它没有预构建的 Windows wheel 并且在
> `sys_platform == 'win32'` 时被 `matrix` 额外选项跳过。上面的命令在 Windows 上仍然
> 成功但不会安装 `matrix-nio`，所以启用 Matrix 频道会在启动时失败。请使用 macOS、Linux 或 WSL2。

**1. 创建/选择 Matrix 账户**

- 在你的主服务器上创建或复用一个 Matrix 账户（例如 `matrix.org`）
- 确认你可以用 Element 登录

**2. 获取凭据**

你需要：
  - `userId`（例如：`@nanobot:matrix.org`）
  - `password`（密码）

（注意：出于兼容性原因，`accessToken` 和 `deviceId` 仍然受支持，但为了可靠的加密，推荐使用密码登录。如果提供了 `password`，`accessToken` 和 `deviceId` 会被忽略。）

**3. 配置**

```json
{
  "channels": {
    "matrix": {
      "enabled": true,
      "homeserver": "https://matrix.org",
      "userId": "@nanobot:matrix.org",
      "password": "mypasswordhere",
      "e2eeEnabled": true,
      "allowFrom": ["@your_user:matrix.org"],
      "groupPolicy": "open",
      "groupAllowFrom": [],
      "allowRoomMentions": false,
      "maxMediaBytes": 20971520
    }
  }
}
```

> 保持持久的 `matrix-store` 目录 — 加密的会话状态如果跨重启发生变化将会丢失。

| 选项 | 描述 |
|------|------|
| `allowFrom` | 允许交互的用户 ID。留空拒绝所有人；使用 `["*"]` 允许所有人。 |
| `groupPolicy` | `open`（默认）、`mention` 或 `allowlist`。 |
| `groupAllowFrom` | 房间白名单（策略为 `allowlist` 时使用）。 |
| `allowRoomMentions` | 在 mention 模式下接受 `@room`提及。 |
| `e2eeEnabled` | 端到端加密支持（默认 `true`）。设置为 `false` 仅使用纯文本。 |
| `maxMediaBytes` | 最大附件大小（默认 `20MB`）。设置为 `0` 阻止所有媒体。



**4. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>WhatsApp</b></summary>

需要 **Node.js ≥18**。

**1. 绑定设备**

```bash
nanobot channels login whatsapp
# 使用 WhatsApp → 设置 → 关联设备 扫描二维码
```

**2. 配置**

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["+1234567890"]
    }
  }
}
```

**3. 运行**（需要两个终端）

```bash
# 终端 1
nanobot channels login whatsapp

# 终端 2
nanobot gateway
```

> 对于已有安装，WhatsApp bridge 更新不会自动应用。
> 升级 nanobot 后，使用以下命令重建本地 bridge：
> `rm -rf ~/.nanobot/bridge && nanobot channels login whatsapp`

</details>

<details>
<summary><b>飞书（Feishu）</b></summary>

使用 **WebSocket** 长连接 — 无需公网 IP。

**1. 创建飞书 Bot**
- 访问[飞书开放平台](https://open.feishu.cn/app)
- 创建新应用 → 启用 **Bot** 能力
- **权限**：
  - `im:message`（发送消息）和 `im:message.p2p_msg:readonly`（接收消息）
  - **流式回复**（nanobot 默认）：添加 **`cardkit:card:write`**（在飞书开发者控制台中通常标记为 **创建和更新卡片**）。CardKit 实体和流式助手文本都需要此权限。旧版应用可能还没有 — 打开**权限管理**，启用该作用域，然后如果控制台要求则**发布**新版本应用。
  - 如果你**无法**添加 `cardkit:card:write`，请在 `channels.feishu` 下设置 `"streaming": false`（见下文）。Bot 仍然可以工作；回复使用普通的交互卡片，无需逐 token 流式传输。
- **事件**：添加 `im.message.receive_v1`（接收消息）
  - 选择**长连接**模式（需要先运行 nanobot 来建立连接）
- 从"凭证与基础信息"获取 **App ID** 和 **App Secret**
- 发布应用

**2. 配置**

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": ["ou_YOUR_OPEN_ID"],
      "groupPolicy": "mention",
      "reactEmoji": "OnIt",
      "doneEmoji": "DONE",
      "toolHintPrefix": "🔧",
      "streaming": true,
      "domain": "feishu"
    }
  }
}
```

> `streaming` 默认为 `true`。如果你的应用没有 **`cardkit:card:write`** 权限（见上文），请使用 `false`。
> `encryptKey` 和 `verificationToken` 在长连接模式下是可选的。
> `allowFrom`：添加你的 open_id（向 bot 发送消息时可以在 nanobot 日志中找到）。使用 `["*"]` 允许所有用户。
> `groupPolicy`：`"mention"`（默认 — 仅被 @提及时响应）、`"open"`（响应所有群消息）。私聊始终响应。
> `reactEmoji`："处理中"状态的 emoji（默认：`OnIt`）。参见[可用 emoji 列表](https://open.larkoffice.com/document/server-docs/im-v1/message-reaction/emojis-introduce)。
> `doneEmoji`："完成"状态的可选 emoji（如 `DONE`、`OK`、`HEART`）。设置后，Bot 会在移除 `reactEmoji` 后添加此反应。
> `toolHintPrefix`：流式卡片中内联工具提示的前缀（默认：`🔧`）。
> `domain`：`"feishu"`（默认）用于国内（open.feishu.cn），`"lark"` 用于国际版 Lark（open.larksuite.com）。

**3. 运行**

```bash
nanobot gateway
```

> [!TIP]
> 飞书使用 WebSocket 接收消息 — 不需要 webhook 或公网 IP！

</details>

<details>
<summary><b>QQ（QQ 单聊）</b></summary>

使用 **botpy SDK** 和 WebSocket — 无需公网 IP。目前**仅支持私聊**。

**1. 注册并创建 Bot**
- 访问 [QQ 开放平台](https://q.qq.com) → 注册成为开发者（个人或企业）
- 创建新的 Bot 应用
- 进入**开发设置** → 复制 **AppID** 和 **AppSecret**

**2. 设置沙箱测试**
- 在 Bot 管理控制台中，找到**沙箱配置**
- 在**消息列表配置**下，点击**添加成员**并添加你自己的 QQ 号
- 添加后，用手机 QQ 扫描 Bot 二维码 → 打开 Bot 资料 → 点击"发消息"开始聊天

**3. 配置**

> - `allowFrom`：添加你的 openid（向 bot 发送消息时可以在 nanobot 日志中找到）。使用 `["*"]` 开放访问。
> - `msgFormat`：可选。使用 `"plain"`（默认）以获得与旧版 QQ 客户端的最大兼容性，或使用 `"markdown"` 在新版客户端上获得更丰富的格式。
> - 生产环境：在 Bot 控制台提交审核并发布。完整发布流程参见 [QQ Bot 文档](https://bot.q.qq.com/wiki/)。

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "YOUR_APP_ID",
      "secret": "YOUR_APP_SECRET",
      "allowFrom": ["YOUR_OPENID"],
      "msgFormat": "plain"
    }
  }
}
```

**4. 运行**

```bash
nanobot gateway
```

现在从 QQ 向 Bot 发送消息 — 它应该会回应！

</details>

<details>
<summary><b>钉钉（DingTalk）</b></summary>

使用 **Stream 模式** — 无需公网 IP。

**1. 创建钉钉 Bot**
- 访问[钉钉开放平台](https://open-dev.dingtalk.com/)
- 创建新应用 → 添加 **机器人**能力
- **配置**：
  - 开启 **Stream 模式**
- **权限**：添加发送消息所需的必要权限
- 从"凭证"获取 **AppKey**（客户端 ID）和 **AppSecret**（客户端密钥）
- 发布应用

**2. 配置**

```json
{
  "channels": {
    "dingtalk": {
      "enabled": true,
      "clientId": "YOUR_APP_KEY",
      "clientSecret": "YOUR_APP_SECRET",
      "allowFrom": ["YOUR_STAFF_ID"]
    }
  }
}
```

> `allowFrom`：添加你的员工 ID。使用 `["*"]` 允许所有用户。

**3. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Slack</b></summary>

使用 **Socket 模式** — 不需要公开 URL。

**1. 创建 Slack 应用**
- 前往 [Slack API](https://api.slack.com/apps) → **创建新应用** → "从头开始"
- 选择名称并选择你的工作区

**2. 配置应用**
- **Socket 模式**：开启 → 生成具有 `connections:write` 作用域的 **应用级令牌** → 复制它（`xapp-...`）
- **OAuth 与权限**：添加 Bot 作用域：`chat:write`、`reactions:write`、`app_mentions:read`、`files:read`、`files:write`、`channels:history`、`groups:history`、`im:history`、`mpim:history`
- **事件订阅**：开启 → 订阅 Bot 事件：`message.im`、`message.channels`、`app_mention` → 保存更改
- **应用主页**：滚动到**显示标签页** → 启用**消息标签页** → 勾选**"允许用户从消息标签页发送斜杠命令和消息"**
- **安装应用**：点击**安装到工作区** → 授权 → 复制 **Bot 令牌**（`xoxb-...`）

> `files:read` 用于读取用户发给 nanobot 的文件。`files:write` 是 nanobot 发送图片、视频和其他文件上传所必需的。如果之后添加任一作用域，需重新安装 Slack应用到工作区并重启 nanobot 以使用更新的 bot 令牌。

**3. 配置 nanobot**

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "allowFrom": ["YOUR_SLACK_USER_ID"],
      "groupPolicy": "mention"
    }
  }
}
```

**4. 运行**

```bash
nanobot gateway
```

直接私信 Bot 或在频道中 @它 — 它应该会回应！

> [!TIP]
> - `groupPolicy`：`"mention"`（默认 — 仅被 @提及时响应）、`"open"`（响应所有频道消息）或 `"allowlist"`（限制为特定频道）。
> - 私聊策略默认开放。设置 `"dm": {"enabled": false}` 来禁用私聊。

</details>

<details>
<summary><b>邮箱（Email）</b></summary>

给 nanobot 一个专属邮箱账户。它轮询 **IMAP** 接收邮件并通过 **SMTP** 回复 — 就像一个个人邮件助手。

**1. 获取凭据（Gmail 示例）**
- 为你的 Bot 创建一个专用 Gmail 账户（如 `my-nanobot@gmail.com`）
- 启用两步验证 → 创建[应用密码](https://myaccount.google.com/apppasswords)
- IMAP 和 SMTP 都使用此应用密码

**2. 配置**

> - `consentGranted` 必须为 `true` 以允许访问邮箱。这是一个安全门控 — 设为 `false` 可以完全禁用。
> - `allowFrom`：添加你的邮箱地址。使用 `["*"]` 接受来自任何人的邮件。
> - `smtpUseTls` 和 `smtpUseSsl` 默认分别为 `true` / `false`，这对 Gmail（端口 587 + STARTTLS）是正确的。无需显式设置。
> - 如果你只想读/分析邮件而不想发送自动回复，请设置 `"autoReplyEnabled": false`。
> - `allowedAttachmentTypes`：保存匹配这些 MIME 类型的入站附件 — `["*"]` 表示全部，例如 `["application/pdf", "image/*"]`（默认 `[]` = 禁用）。
> - `maxAttachmentSize`：每个附件的最大大小（字节）（默认 `2000000` / 2MB）。
> - `maxAttachmentsPerEmail`：每封邮件最多保存的附件数（默认 `5`）。

```json
{
  "channels": {
    "email": {
      "enabled": true,
      "consentGranted": true,
      "imapHost": "imap.gmail.com",
      "imapPort": 993,
      "imapUsername": "my-nanobot@gmail.com",
      "imapPassword": "your-app-password",
      "smtpHost": "smtp.gmail.com",
      "smtpPort": 587,
      "smtpUsername": "my-nanobot@gmail.com",
      "smtpPassword": "your-app-password",
      "fromAddress": "my-nanobot@gmail.com",
      "allowFrom": ["your-real-email@gmail.com"],
      "allowedAttachmentTypes": ["application/pdf", "image/*"]
    }
  }
}
```


**3. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>微信（Weixin）</b></summary>

通过 ilinkai 个人微信 API 使用 **HTTP 长轮询**和二维码登录。不需要本地微信桌面客户端。

**1. 安装微信支持**

```bash
pip install "nanobot-ai[weixin]"
```

**2. 配置**

```json
{
  "channels": {
    "weixin": {
      "enabled": true,
      "allowFrom": ["YOUR_WECHAT_USER_ID"]
    }
  }
}
```

> - `allowFrom`：添加你在 nanobot 日志中看到的微信账号的发送者 ID。使用 `["*"]` 允许所有用户。
> - `token`：可选。如果省略，将以交互方式登录，nanobot 会为你保存 token。
> - `routeTag`：可选。当你的上游微信部署需要请求路由时，nanobot 会将其作为 `SKRouteTag` 请求头发送。
> - `stateDir`：可选。默认使用 nanobot 运行目录存放微信状态。
> - `pollTimeout`：可选的长轮询超时时间（秒）。

**3. 登录**

```bash
nanobot channels login weixin
```

使用 `--force` 重新认证并忽略任何已保存的 token：

```bash
nanobot channels login weixin --force
```

**4. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>企业微信（Wecom）</b></summary>

> 这里我们使用 [wecom-aibot-sdk-python](https://github.com/chengyongru/wecom_aibot_sdk)（官方 [@wecom/aibot-node-sdk](https://www.npmjs.com/package/@wecom/aibot-node-sdk) 的社区 Python 版本）。
>
> 使用 **WebSocket** 长连接 — 无需公网 IP。

**1. 安装可选依赖**

```bash
pip install nanobot-ai[wecom]
```

**2. 创建企业微信 AI Bot**

前往企业微信管理后台 → 智能机器人 → 创建机器人 → 选择 **API 模式**并选择**长连接**。复制 Bot ID 和密钥。

**3. 配置**

```json
{
  "channels": {
    "wecom": {
      "enabled": true,
      "botId": "your_bot_id",
      "secret": "your_bot_secret",
      "allowFrom": ["your_id"]
    }
  }
}
```

**4. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Microsoft Teams</b>（MVP — 仅私聊）</summary>

> 私聊文本输入/输出，租户感知的 OAuth，会话引用持久化。
> 使用公开 HTTPS webhook — 没有 WebSocket；你需要隧道或反向代理。

**1. 安装可选依赖**

```bash
pip install nanobot-ai[msteams]
```

**2. 创建 Teams / Azure Bot 应用注册**

创建或复用 Microsoft Teams / Azure Bot 应用注册。将 Bot 消息端点设置为以 `/api/messages` 结尾的公开 HTTPS URL。

**3. 配置**

```json
{
  "channels": {
    "msteams": {
      "enabled": true,
      "appId": "YOUR_APP_ID",
      "appPassword": "YOUR_APP_SECRET",
      "tenantId": "YOUR_TENANT_ID",
      "host": "0.0.0.0",
      "port": 3978,
      "path": "/api/messages",
      "allowFrom": ["*"],
      "replyInThread": true,
      "mentionOnlyResponse": "Hi — what can I help with?",
      "validateInboundAuth": true,
      "refTtlDays": 30,
      "pruneWebChatRefs": true,
      "pruneNonPersonalRefs": true,
      "refTouchIntervalS": 300
    }
  }
}
```

> - `replyInThread: true` 当存储的 `activity_id` 可用时，回复触发的 Teams 活动。
> - `mentionOnlyResponse` 控制当用户只发送 Bot mention（`<at>Nanobot</at>`）时 Nanobot 收到什么。设为 `""` 以忽略仅 mention 的消息。
> - `validateInboundAuth: true` 启用入站 Bot Framework bearer-token 验证（签名、发行者、受众、有效期、`serviceUrl`）。这是公开部署的安全默认值。仅在本地开发或严格控制测试时才设为 `false`。
> - `refTtlDays`（默认 `30`）控制存储的会话引用在被清理前可以保留多久。
> - `pruneWebChatRefs`（默认 `true`）丢弃具有 `webchat.botframework.com` 服务 URL 的引用。
> - `pruneNonPersonalRefs`（默认 `true`）丢弃 `conversation_type` 不是 `personal` 的引用。
> - `refTouchIntervalS`（默认 `300`）节流成功的发送刷新活跃引用的 `updated_at` 频率。

**4. 运行**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Signal</b></summary>

使用 HTTP 模式的 **signal-cli** 守护进程 — 通过 SSE 接收消息，通过 JSON-RPC 发送。

**1. 安装 signal-cli**

安装 [signal-cli](https://github.com/AsamK/signal-cli) 并注册电话号码：

```bash
signal-cli -u +1234567890 register
signal-cli -u +1234567890 verify <CODE>
```

启动守护进程：

```bash
signal-cli -a +1234567890 daemon --http localhost:8080
```

**2. 配置**

```json
{
  "channels": {
    "signal": {
      "enabled": true,
      "phoneNumber": "+1234567890",
      "daemonHost": "localhost",
      "daemonPort": 8080,
      "dm": {
        "enabled": true,
        "policy": "open"
      },
      "group": {
        "enabled": true,
        "policy": "open",
        "requireMention": true
      }
    }
  }
}
```

> - `phoneNumber`：你注册的 Signal 电话号码。
> - `daemonHost` / `daemonPort`：signal-cli 守护进程监听地址（默认 `localhost:8080`）。
> - `dm.policy`：`"open"`（任何人都可以私聊）或 `"allowlist"`（仅列出的号码/UUID）。当为 `"allowlist"` 时，未列出的私聊发送者会收到配对码。
> - `dm.allowFrom`：允许的电话号码或 UUID 列表（策略为 `"allowlist"` 时使用）。
> - `group.policy`：`"open"`（所有群组）或 `"allowlist"`（仅列出的群组 ID）。
> - `group.requireMention`：当为 `true`（默认）时，Bot 仅在被 @提及时在群组中响应。
> - `group.allowFrom`：允许的群组 ID 列表（群组策略为 `"allowlist"` 时使用）。
> - `attachmentsDir`：覆盖 signal-cli 存储入站附件的目录。默认 `~/.local/share/signal-cli/attachments`（Linux 默认值）。如果 signal-cli 使用自定义 `XDG_DATA_HOME` 运行或在 macOS/Windows 上，请设置此项。
> - `groupMessageBufferSize`：为上下文保留的最近群消息数量（默认 `20`，必须 > 0）。

**3. 运行**

```bash
nanobot gateway
```

> [!TIP]
> 该频道会在连接断开时以指数退避方式自动重连到 signal-cli 守护进程。
> Bot 回复中的 Markdown 会自动转换为 Signal 文本样式（粗体、斜体、代码等）。

</details>
