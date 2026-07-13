# 抖音多用户视频监控脚本

定期检查多个抖音账号是否发布了新视频或删除了旧视频，并通过钉钉、Bark、企业微信、Server 酱、Telegram 等一个或多个渠道推送通知。

---

## 目录

- [功能特性](#功能特性)
- [快速开始（Docker）](#快速开始docker)
- [部署方式](#部署方式)
  - [Docker 部署（推荐）](#docker-部署推荐)
  - [deploy.sh 一键部署](#deploysh-一键部署)
  - [手动部署](#手动部署)
- [配置说明](#配置说明)
  - [环境变量 `.env`](#环境变量-env)
  - [监控列表 `users.conf`](#监控列表-usersconf)
- [命令行参数](#命令行参数)
- [日志](#日志)
- [工作原理](#工作原理)
  - [关于漏检](#关于漏检)
  - [关于删除通知的延迟](#关于删除通知的延迟)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)
- [文件结构](#文件结构)

---

## 功能特性

- **多用户监控**：配置文件支持热加载（运行中修改 `users.conf` 无需重启），按 `sec_user_id` 自动去重
- **多推送渠道**：钉钉 / Bark / 企业微信 / Server 酱 / Telegram，通过 `NOTIFY_CHANNELS` 同时启用多个，某个渠道挂了不影响其它渠道
- **Web 状态面板**（可选）：自动刷新的网页，查看每个账号的状态、视频数、更新频率，点击可查看详情
- **并发检查**：多个账号同时检测，单个账号超时不会拖慢其它账号，整体请求节奏与串行一致（3~8 秒/次）
- **新视频通知**：带封面、标题、点赞/评论/分享/收藏数、时长、话题标签、发布时间、观看链接、距上次发布间隔天数
- **删除检测**：区分"真实删除"和"被新视频挤出窗口"，带二次确认（普通视频 2 轮、置顶视频 3 轮），避免接口抖动导致的假警报
- **标题变更同步**：静默检测标题变化并更新本地记录，不推送通知
- **Cookie 过期检测**：API 响应长时间不变 / 长期无新视频，两种情况都会提醒
- **状态持久化**：每个账号的运行状态独立保存为 JSON 文件，重启不丢失
- **日志分级**：info（关键事件）和 debug（完整细节）分文件夹保存，自动轮转压缩
- **健康检查**：`/api/health` 端点，可接入 Uptime Kuma / Prometheus 等监控系统
- **单元测试**：核心状态机逻辑（新增/删除确认/滚动出窗口/请求限速/多渠道广播）都有测试覆盖

---

## 快速开始（Docker）

三步搞定，不需要 clone 代码：

```bash
# 1. 下载配置文件
mkdir -p ~/douyin-monitor && cd ~/douyin-monitor
curl -O https://dmonitor.yunov.top/docker-compose.yml
curl -O https://dmonitor.yunov.top/env.example
curl -O https://dmonitor.yunov.top/users.conf.example
mv env.example .env && mv users.conf.example users.conf

# 2. 编辑配置
vi .env        # 填通知渠道 token（见下文配置说明）
vi users.conf  # 填要监控的抖音账号

# 3. 启动
docker compose up -d
```

常用运维命令：

```bash
docker compose logs -f       # 实时看日志
docker compose ps            # 看容器状态
docker compose restart       # 改完配置后重启
docker compose down          # 停止容器（数据不丢）
docker pull yunfeiii/douyin-monitor:latest && docker compose up -d   # 更新版本
```

---

## 部署方式

### Docker 部署（推荐）

最省心的方式，适合 VPS、NAS、树莓派等 Linux 环境。

详见上方[快速开始](#快速开始docker)。

> `network_mode: host` 仅 Linux 有效，Mac/Windows 需改用 ports 映射，`docker-compose.yml` 里有说明。

### deploy.sh 一键部署

```bash
bash <(curl -sS https://dmonitor.yunov.top/deploy.sh) install
```

脚本会自动从 GitHub 下载最新代码，交互式引导你完成：选工作目录 → 下载代码 → 建虚拟环境装依赖 → 配置推送渠道 → 填监控账号 → 跑测试 → 配置 systemd 常驻。

日常运维：

```bash
./deploy.sh status    # 查看服务状态 + 各账号监控状态
./deploy.sh logs      # 实时看日志
./deploy.sh restart   # 重启
./deploy.sh config    # 修改推送渠道或监控账号列表
```

更新版本：

```bash
sudo ./deploy.sh update
```

`update` 会从 GitHub 拉取最新代码、自动备份旧代码、更新依赖、追加新配置项（不覆盖已有值）、自动重启服务。

> 如果 deploy.sh 脚本本身有更新，重新下载即可：
> ```bash
> bash <(curl -sS https://dmonitor.yunov.top/deploy.sh) update
> ```

卸载：

```bash
sudo ./deploy.sh uninstall
```

不带命令直接运行 `sudo ./deploy.sh` 会进入交互菜单。所有子命令都可以加 `--dir /path` 指定工作目录。

### 手动部署

```bash
# 1. 准备工作目录
sudo mkdir -p /opt/douyin-monitor
sudo cp douyin_monitor.py requirements.txt /opt/douyin-monitor/
sudo cp -r douyin_monitor /opt/douyin-monitor/
sudo cp env.example /opt/douyin-monitor/.env
sudo cp users.conf.example /opt/douyin-monitor/users.conf

# 2. 安装依赖
cd /opt/douyin-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 编辑配置
vi .env
vi users.conf

# 4. 测试
python3 douyin_monitor.py --once

# 5. 常驻运行
python3 douyin_monitor.py
```

工作目录默认是脚本所在目录，可通过 `DOUYIN_MONITOR_HOME` 环境变量覆盖。

#### systemd 常驻

```ini
# /etc/systemd/system/douyin-monitor.service
[Unit]
Description=Douyin multi-user video monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/douyin-monitor
ExecStart=/opt/douyin-monitor/venv/bin/python3 /opt/douyin-monitor/douyin_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-monitor
sudo journalctl -u douyin-monitor -f
```

> `ExecStart` 必须用虚拟环境里的 `python3`，否则会报 `ModuleNotFoundError`。

没有 systemd 的环境（容器、WSL）会自动退化为 `nohup` 方式常驻。

---

## 配置说明

### 环境变量 `.env`

| 配置项 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `NOTIFY_CHANNELS` | 否 | `dingtalk` | 启用的推送渠道，逗号分隔，如 `dingtalk,bark` |
| **钉钉** | | | |
| `DINGTALK_TOKEN` | 启用 dingtalk 时必填 | - | Webhook access_token |
| `DINGTALK_SECRET` | 启用 dingtalk 时必填 | - | 加签密钥（SEC 开头） |
| `AT_MOBILES` | 否 | 空 | 告警时 @ 的手机号，逗号分隔 |
| **Bark** | | | |
| `BARK_SERVER` | 否 | `https://api.day.app` | Bark 服务器地址 |
| `BARK_DEVICE_KEY` | 启用 bark 时必填 | - | 设备 Key |
| **企业微信** | | | |
| `WECOM_WEBHOOK_KEY` | 启用 wecom 时必填 | - | Webhook key |
| **Server 酱** | | | |
| `SERVERCHAN_SENDKEY` | 启用 serverchan 时必填 | - | SendKey |
| **Telegram** | | | |
| `TELEGRAM_BOT_TOKEN` | 启用 telegram 时必填 | - | Bot token |
| `TELEGRAM_CHAT_ID` | 启用 telegram 时必填 | - | 目标 chat id |
| **抓取接口** | | | |
| `API_URL` | 否 | `http://localhost/api/douyin/web/fetch_user_post_videos` | 抓取 API 地址 |
| `FETCH_COUNT` | 否 | `10` | 单次拉取视频条数 |
| **轮询** | | | |
| `POLL_INTERVAL_MIN` | 否 | `15` | 轮询间隔最小值（秒） |
| `POLL_INTERVAL_MAX` | 否 | `40` | 轮询间隔最大值（秒） |
| `MAX_CONCURRENT_USERS` | 否 | `5` | 并发检查上限 |
| `STALE_THRESHOLD` | 否 | `604800`（7 天） | Cookie 过期检测阈值（秒） |
| **Web 面板** | | | |
| `WEB_ENABLED` | 否 | `false` | 是否启用 Web 状态面板 |
| `WEB_HOST` | 否 | `127.0.0.1` | 监听地址（局域网访问改 `0.0.0.0`） |
| `WEB_PORT` | 否 | `8787` | 监听端口 |
| **日志** | | | |
| `LOG_LEVEL` | 否 | `INFO` | 终端输出详细程度（不影响日志文件） |

> `.env` 支持值两边带引号，如 `DINGTALK_TOKEN="xxx"` 和 `DINGTALK_TOKEN=xxx` 都可以。

### 监控列表 `users.conf`

```
# 格式：sec_user_id|昵称
# sec_user_id 是抖音账号的加密用户 ID（浏览器开发者工具 → 接口请求里找）
# 昵称仅用于通知展示，可以重复
# # 开头为注释，空行忽略，运行中修改无需重启

MS4wLjABAAAA_example_sec_user_id_1|示例用户A
MS4wLjABAAAA_example_sec_user_id_2|示例用户B
```

---

## 命令行参数

```bash
python3 douyin_monitor.py            # 常驻监控
python3 douyin_monitor.py --once     # 只检测一轮后退出
python3 douyin_monitor.py --status   # 打印最近一次状态快照
```

---

## 日志

```
log/
├── info/monitor.log     # 关键事件 + 每轮汇总（日常查看）
└── debug/monitor.log    # 完整细节（排查问题）
```

`info/monitor.log` 示例：

```
[2026-07-12 17:14:47] 抖音监控服务已启动（PID 3253790，推送渠道: dingtalk）
[2026-07-12 17:15:22] 本轮完成：检查 3 个用户，均无变化，等待 28 秒...
[2026-07-12 17:20:31] 本轮完成：检查 3 个用户，新视频 2 条，等待 31 秒...
[2026-07-12 17:25:02] 用户 XXX 连续失败 5 次
[2026-07-12 17:30:15] 用户 XXX 确认检测到 1 条视频被删除
```

单个日志文件超过 10MB 自动轮转，gzip 压缩保存最近 3 份（`monitor.log.1.gz` ~ `monitor.log.3.gz`）。

`.env` 中的 `LOG_LEVEL` 只影响终端输出，不影响日志文件内容。

---

## 工作原理

### 关于漏检

抖音接口单次最多返回 `FETCH_COUNT` 条非置顶视频。如果某用户一次发布的视频数超过这个值，超出部分会被漏检。脚本检测到会在日志里警告，建议调大 `FETCH_COUNT`。

### 关于删除通知的延迟

删除判定带二次确认，需要连续多轮检测都消失才会通知：

| 类型 | 确认轮数 |
|------|---------|
| 普通视频 | 连续 2 轮消失 |
| 置顶视频 | 连续 3 轮消失 |

收到通知会比实际删除晚 1~2 轮检测间隔（通常几十秒到几分钟），这是为了避免接口抖动导致的假警报。中途视频重新出现则计数清零，不会发送任何通知。

---

## 常见问题

**容器一直重启/退出？**
先 `docker compose logs` 看日志，大概率是 `.env` 还没配。编辑 `.env` 填好 token 后 `docker compose up -d` 重启。

**Web 面板打不开？**
检查 `.env` 里 `WEB_ENABLED=true`，局域网访问需 `WEB_HOST=0.0.0.0`，改完重启容器。

**收不到通知？**
用 `--once` 跑一轮，看 `log/debug/monitor.log` 里的报错信息。

**"监控脚本已在运行"？**
删除工作目录下的 `monitor.pid` 文件后重试（Docker 部署 `docker compose restart` 即可）。

**钉钉签名错误？**
确认 `DINGTALK_SECRET` 是以 `SEC` 开头的加签密钥，不要填成 access_token。

**删除通知为什么不是立刻收到？**
这是有意的设计，见[关于删除通知的延迟](#关于删除通知的延迟)。

**`users.conf` 配了同一个账号两次？**
只有第一次生效，重复的会在日志里警告并跳过。

**退出时等了几秒才停？**
正常现象，脚本等正在处理中的检查完成再退出，避免留下不一致状态。

---

## 开发与测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## 文件结构

```
├── docker-compose.yml         # Docker 部署配置
├── Dockerfile                 # Docker 镜像构建
├── docker/entrypoint.sh       # 容器启动脚本
├── deploy.sh                  # 一键部署/运维脚本
├── douyin_monitor.py          # 主入口脚本
├── douyin_monitor/            # 核心逻辑包
│   ├── cli.py                 # 命令行入口、主循环
│   ├── config.py              # 配置管理
│   ├── monitor.py             # 监控核心逻辑
│   ├── state.py               # 状态持久化
│   ├── notifiers/             # 推送渠道
│   │   ├── base.py            # 统一接口 + 公共格式化
│   │   ├── dingtalk.py        # 钉钉
│   │   ├── bark.py            # Bark (iOS)
│   │   ├── wecom.py           # 企业微信
│   │   ├── serverchan.py      # Server 酱
│   │   ├── telegram.py        # Telegram
│   │   └── composite.py       # 多渠道广播
│   ├── webui.py               # Web 状态面板
│   ├── pacer.py               # 请求节奏控制
│   ├── logging_setup.py       # 日志配置
│   └── utils.py               # 工具函数
├── tests/                     # 单元测试
├── requirements.txt           # 运行依赖
├── requirements-dev.txt       # 开发/测试依赖
├── env.example                # 环境变量配置模板
└── users.conf.example         # 监控用户列表模板
```
