# 部署指南

## Docker

> [!TIP]
> `-v ~/.nanobot:/home/nanobot/.nanobot` 标志会将你的本地配置目录挂载到容器中，这样你的配置和工作区在容器重启后持久保存。
> 容器以非 root 用户 `nanobot`（UID 1000）运行，并从 `/home/nanobot/.nanobot` 读取配置。始终将主机配置目录挂载到 `/home/nanobot/.nanobot`，而不是 `/root/.nanobot`。
> 如果你遇到**权限拒绝**错误，请先在主机上修复所有权：`sudo chown -R 1000:1000 ~/.nanobot`，或者传递 `--user $(id -u):$(id -g)` 以匹配你的主机 UID。Podman 用户可以改用 `--userns=keep-id`。
>
> [!IMPORTANT]
> 目前官方 Docker 用法意味着使用本仓库包含的 `Dockerfile` 进行构建。第三方命名空间下的 Docker Hub 镜像不由 HKUDS/nanobot 维护或验证；除非你信任发布者，否则不要在其中挂载 API 密钥或 Bot Token。
>
> [!IMPORTANT]
> 网关和 WebSocket 频道在 `config.json` 中默认为 `host: "127.0.0.1"`（在 `nanobot/config/schema.py` 中设置）。Docker `-p` 端口转发无法到达容器的环回接口，因此要使主机或 LAN 能够访问暴露的端口，必须在启动容器前将两者都设为 `0.0.0.0`（写入 `~/.nanobot/config.json`）：
>
> ```json
> {
>   "gateway":  { "host": "0.0.0.0" },
>   "channels": { "websocket": { "host": "0.0.0.0" } }
> }
> ```
>
> 当 `host` 为 `0.0.0.0` 时，除非还在 WebSocket 频道上配置了 `token` 或 `tokenIssueSecret`，否则网关会拒绝启动 — 详情参见 [`webui/README.md`](../webui/README.md)。

### Docker Compose

```bash
docker compose run --rm nanobot-cli onboard   # 首次设置
vim ~/.nanobot/config.json                     # 添加 API 密钥
docker compose up -d nanobot-gateway           # 启动网关
```

```bash
docker compose run --rm nanobot-cli agent -m "你好！"   # 运行 CLI
docker compose logs -f nanobot-gateway                   # 查看日志
docker compose down                                      # 停止
```

### Docker

```bash
# 构建镜像
docker build -t nanobot .

# 初始化配置（仅需首次）
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot onboard

# 在主机上编辑配置以添加 API 密钥
vim ~/.nanobot/config.json

# 运行网关（连接到启用的频道，如 Telegram/Discord/Mochat）。
# 镜像 docker-compose.yml 中声明的安全限制和端口映射：
#   - `--cap-drop ALL --cap-add SYS_ADMIN` + unconfined apparmor/seccomp 在
#     `tools.exec.sandbox: "bwrap"` 启用时是必需的（bwrap 需要 CAP_SYS_ADMIN 用于
#     用户命名空间）。没有它们，`bwrap` 会因 `clone3: Operation not permitted` 退出。
#   - `-p 8765:8765` 暴露 WebSocket 频道 / WebUI 以及网关健康检查
#     端口 18790。
docker run \
  --cap-drop ALL --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  -v ~/.nanobot:/home/nanobot/.nanobot \
  -p 18790:18790 -p 8765:8765 \
  nanobot gateway

# 或者运行单个命令
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot agent -m "你好！"
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot status
```

## Linux 服务（Systemd）

将网关作为 systemd 用户服务运行，使其自动启动并在故障时重启。

**1. 找到 nanobot 二进制路径：**

```bash
which nanobot   # 例如 /home/user/.local/bin/nanobot
```

**2. 在 `~/.config/systemd/user/nanobot-gateway.service` 创建服务文件**（如需要替换 `ExecStart` 路径）：

```ini
[Unit]
Description=Nanobot Gateway
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/nanobot gateway
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
```

**3. 启用并启动：**

```bash
systemctl --user daemon-reload
systemctl --user enable --now nanobot-gateway
```

**常见操作：**

```bash
systemctl --user status nanobot-gateway        # 检查状态
systemctl --user restart nanobot-gateway       # 配置更改后重启
journalctl --user -u nanobot-gateway -f        # 跟踪日志
```

如果你编辑了 `.service` 文件本身，请在重启前运行 `systemctl --user daemon-reload`。

> **注意：** 用户服务仅在你登录时运行。要在登出后保持网关运行，请启用 lingering：
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

当你希望 `nanobot gateway` 在登录后保持在线而无需打开终端时，使用 LaunchAgent。

**1. 获取绝对 `nanobot` 路径：**

```bash
which nanobot   # 例如 /Users/youruser/.local/bin/nanobot
```

在 plist 中使用精确路径。它会保留你安装方式中的 Python 环境。

**2. 创建 `~/Library/LaunchAgents/ai.nanobot.gateway.plist`：**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.nanobot.gateway</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/youruser/.local/bin/nanobot</string>
    <string>gateway</string>
    <string>--workspace</string>
    <string>/Users/youruser/.nanobot/workspace</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/youruser/.nanobot/workspace</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/youruser/.nanobot/logs/gateway.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/youruser/.nanobot/logs/gateway.error.log</string>
</dict>
</plist>
```

**3. 加载并启动：**

```bash
mkdir -p ~/Library/LaunchAgents ~/.nanobot/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.nanobot.gateway.plist
launchctl enable gui/$(id -u)/ai.nanobot.gateway
launchctl kickstart -k gui/$(id -u)/ai.nanobot.gateway
```

**常见操作：**

```bash
launchctl list | grep ai.nanobot.gateway
launchctl kickstart -k gui/$(id -u)/ai.nanobot.gateway   # 重启
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.nanobot.gateway.plist
```

编辑 plist 后，需再次运行 `launchctl bootout ...` 和 `launchctl bootstrap ...`。

> **注意：** 如果启动失败并提示"address already in use"，请先停止手动启动的 `nanobot gateway` 进程。
