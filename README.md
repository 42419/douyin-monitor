# 抖音多用户视频监控脚本

定期检查多个抖音账号是否发布了新视频或删除了旧视频，并通过钉钉、Bark、企业微信、Server 酱、Telegram 等一个或多个渠道推送通知。依赖三个常用的第三方库（`requests`、`python-dotenv`、`DingtalkChatbot`），通过 `pip install -r requirements.txt` 安装即可；也可以用 `./deploy.sh install` 一键完成部署。

## 功能特性

- 多用户监控，配置文件支持热加载（运行中修改 `users.conf` 无需重启脚本），并按 `sec_user_id` 自动去重（重复配置会打印警告并跳过，避免同一账号被检查两次）
- **多推送渠道**：钉钉群机器人 / Bark / 企业微信群机器人 / Server 酱 / Telegram，可以通过 `.env` 的 `NOTIFY_CHANNELS` 同时启用多个，某个渠道挂了不影响其它渠道继续推送
- **只读 Web 状态面板**（可选开启）：一张自动刷新的网页，看每个账号的状态、已知视频数、连续失败次数，不需要额外依赖
- 并发检查多个账号（可通过 `MAX_CONCURRENT_USERS` 配置），但整体"发起新请求"的节奏与原串行版本保持一致（默认相邻请求间隔 3~8 秒随机），不会因为改成并发就变相提高访问下游抓取 API 的频率；好处是某个账号请求卡住或超时，不会拖慢排在它后面的其他账号
- 自动识别新发布的视频并逐条推送通知（带视频封面、标题、点赞/评论/分享/收藏数、时长、话题标签、发布时间、观看链接）
- 视频被删除时单独推送通知，并能正确区分"真实删除"和"视频被新内容挤出抓取窗口"两种情况；删除判定带**二次确认**——普通视频需连续 2 轮检测都消失、置顶视频需连续 3 轮都消失才会真正判定为删除并推送通知，避免接口偶发抖动导致的"先误报删除、马上又误报新发布"的假警报
- 置顶视频的置顶状态变化、视频标题变化都会被静默检测并同步记录到本地状态（标题变化会记一条 INFO 日志，不会额外推送通知，只是保证本地保存的标题始终是最新的，避免以后这条视频真被删除时，通知里显示的还是旧标题）
- 已知视频列表数量超过上限时会自动裁剪最旧的记录，且不会裁剪置顶视频（置顶视频通常发布时间最早，若参与裁剪，裁掉后会被误判成"新视频"重新推送一次）
- 接口连续请求失败会触发告警，恢复后自动推送恢复通知，均带冷却时间避免刷屏
- 两级 Cookie 失效检测：API 响应内容长时间无变化 / 长期没有新视频，两种情况都会提醒你去更新 Cookie；同时对"接口返回的视频字段异常（缺少视频 ID）"这类畸形响应做了防御，避免被误判成批量删除
- 每个监控账号的运行状态独立保存为一个 JSON 文件，脚本重启不丢失检测进度
- 日志分文件夹保存，按级别区分用途，并自动轮转、压缩归档（见下文"日志"章节）
- 文件锁防止脚本被重复启动；支持 `Ctrl+C` / `systemctl stop` 优雅退出
- 附带单元测试（`tests/`），核心状态机逻辑（新增/删除确认/滚动出窗口/请求限速）都有测试覆盖

## 环境要求

- Linux（依赖 `fcntl` 做文件锁，暂不支持 Windows）
- Python 3.8+
- 三个第三方库：`requests`（HTTP 请求）、`python-dotenv`（解析 `.env`）、`DingtalkChatbot`（钉钉机器人加签与推送，即使不用钉钉渠道也需要安装，因为 `requirements.txt` 里是固定依赖）
- 并发检查、Web 状态面板用的都是 Python 标准库（`concurrent.futures`、`http.server`），不需要额外安装依赖
- 抓取接口依赖 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 项目提供的服务，需要自行部署该 API 后将 `API_URL` 指向对应地址
- 想用哪个推送渠道，只需要有对应渠道的机器人/Token 即可，不需要额外安装 Python 依赖（各渠道都是用 `requests` 直接调 HTTP API，仅 DingtalkChatbot 库是钉钉专用）

## 文件说明

| 文件/目录 | 作用 |
|---|---|
| `deploy.sh` | 一键部署/运维脚本：安装、更新、卸载、启停、查看状态和日志、重新配置（推荐入口） |
| `douyin_monitor.py` | 主入口脚本（兼容 `python3 douyin_monitor.py` 直接运行） |
| `douyin_monitor/` | 核心逻辑包（模块拆分后的代码） |
| `requirements.txt` | 运行依赖列表，`pip install -r requirements.txt` 安装 |
| `requirements-dev.txt` | 额外的开发/测试依赖（pytest），`pip install -r requirements-dev.txt` 安装 |
| `.env.example` | 环境变量配置示例，部署时复制为 `.env` 并填写（用 `deploy.sh install` 会自动生成，不需要手动复制） |
| `users.conf.example` | 监控用户列表示例，部署时复制为 `users.conf` |
| `tests/` | 单元测试，`pytest -q` 运行 |

核心包 `douyin_monitor/` 内部模块说明：

| 模块 | 作用 |
|---|---|
| `__main__.py` | 支持 `python -m douyin_monitor` 运行 |
| `cli.py` | 命令行入口：主循环、信号处理、PID 锁、状态查看 |
| `config.py` | 配置管理：.env 解析、Config 数据类、路径常量、users.conf 加载 |
| `state.py` | 每用户运行状态管理（JSON 持久化） |
| `monitor.py` | 监控核心逻辑：API 请求、单用户检测、状态快照 |
| `notifiers/` | 推送渠道包：`base.py`（统一接口+公共格式化）、`dingtalk.py`、`bark.py`、`wecom.py`、`serverchan.py`、`telegram.py`、`composite.py`（多渠道广播） |
| `webui.py` | 只读 Web 状态面板（标准库 `http.server` 实现） |
| `pacer.py` | 请求节奏控制器（保证并发场景下整体请求频率不变） |
| `logging_setup.py` | 日志配置：info/debug 双文件轮转 + 终端输出 |
| `utils.py` | 时间工具和通用辅助函数 |

脚本运行时会在工作目录下自动创建以下内容：

```
<工作目录>/
├── .env                # 你的配置（deploy.sh install 会交互式生成，也可以手动创建）
├── users.conf          # 你要监控的用户列表（deploy.sh install 会交互式生成）
├── state/              # 每个用户的运行状态（脚本自动维护，JSON 格式）
├── log/
│   ├── info/            # 精简日志：关键事件 + 每轮汇总
│   └── debug/           # 完整日志：包含每个用户每一轮的详细过程
├── status.json          # 最近一次检测的状态快照（--status 查看的就是这个）
├── monitor.pid           # 防止重复启动的锁文件
├── nohup.log             # 没有 systemd 时，deploy.sh start 用 nohup 启动产生的输出
└── .backup_*/            # deploy.sh update 每次更新前自动备份的旧代码，确认无误后可自行删除
```

## 部署步骤

### 用 deploy.sh 一键部署（推荐）

```bash
chmod +x deploy.sh
sudo ./deploy.sh install
```

会依次交互式引导你完成：选工作目录 → 复制代码 → 建虚拟环境装依赖 → 选择并配置一个或多个推送渠道（钉钉/Bark/企业微信/Server 酱/Telegram）→ 填监控账号列表 → 跑一轮测试 → 询问是否配置 systemd 常驻服务。装完之后终端会提示后续常用命令。

不带命令直接运行 `sudo ./deploy.sh` 会进入交互菜单，逐项列出所有操作，不用记子命令。

日常运维：

```bash
./deploy.sh status    # 查看服务是否在跑 + 各账号监控状态
./deploy.sh logs      # 实时看日志（有 systemd 用 journalctl，没有就 tail 日志文件）
./deploy.sh restart   # 重启
./deploy.sh config    # 新增/修改推送渠道、编辑监控账号列表，改完可选是否自动重启
```

**以后有新版本代码时，更新只需要一条命令**：

```bash
git pull   # 或者重新下载最新代码到当前目录
sudo ./deploy.sh update
```

`update` 会：自动找到之前装在哪（不用重新输入路径）、更新前备份一份旧代码到工作目录下的 `.backup_时间戳/`、同步新代码和依赖、把 `.env.example` 里新增的配置项（比如新支持的推送渠道开关）追加到你的 `.env` 末尾但绝不覆盖已有的值、如果服务原来在跑就自动重启。全程不会碰你的 `.env`、`users.conf`、`state/`、`log/`。

不再需要时卸载：

```bash
sudo ./deploy.sh uninstall
```

会先停止/移除 systemd 服务，询问是否把 `.env`/`users.conf`/`state/` 打包备份到 `/tmp`，再询问是否删除整个工作目录（默认保留，需要你显式确认才删除）。

如果装在非默认路径、且标记文件丢失导致脚本找不到安装位置，所有子命令都可以加 `--dir` 显式指定，例如 `./deploy.sh status --dir /opt/douyin-monitor`。

### 手动部署

不想用脚本、或者想自己控制每一步，也可以手动来：

```bash
# 1. 选一个工作目录，放入脚本和配置文件
sudo mkdir -p /opt/douyin-monitor
sudo cp douyin_monitor.py requirements.txt /opt/douyin-monitor/
sudo cp -r douyin_monitor /opt/douyin-monitor/
sudo cp .env.example /opt/douyin-monitor/.env
sudo cp users.conf.example /opt/douyin-monitor/users.conf

# 2. 建议用虚拟环境安装依赖，避免污染系统 Python
cd /opt/douyin-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 编辑配置
vi .env          # 填入推送渠道 token/secret、API_URL
vi users.conf    # 填入要监控的 sec_user_id|昵称

# 4. 先跑一轮测试，确认配置没问题
python3 douyin_monitor.py --once

# 5. 确认没问题后，常驻运行
python3 douyin_monitor.py
```

默认工作目录是脚本所在目录。如果想换一个工作目录，运行前设置环境变量即可：

```bash
export DOUYIN_MONITOR_HOME=/path/to/your/dir
python3 douyin_monitor.py
```

### 用 systemd 常驻

`deploy.sh install` 会自动询问并生成下面这份 unit 文件，这里贴出来供手动部署或者想了解细节的人参考：

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

> 注意 `ExecStart` 用的是虚拟环境里的 `python3`（`venv/bin/python3`），不是系统的 `/usr/bin/python3`，否则 systemd 启动时会因为找不到 `requests`/`python-dotenv`/`DingtalkChatbot` 而报 `ModuleNotFoundError` 退出。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-monitor
sudo systemctl status douyin-monitor
sudo journalctl -u douyin-monitor -f   # 实时看终端输出
```

没有 systemd 的环境（比如容器、WSL）`deploy.sh start` 会自动退化成 `nohup` 方式常驻，日志写到工作目录下的 `nohup.log`，用 `deploy.sh stop` 发送 `SIGTERM` 优雅停止。



## 配置说明（`.env`）

| 配置项 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `NOTIFY_CHANNELS` | 否 | `dingtalk` | 启用的推送渠道，逗号分隔，可同时启用多个，如 `dingtalk,bark` |
| `DINGTALK_TOKEN` | 启用 dingtalk 渠道时必填 | - | 钉钉自定义机器人 Webhook 的 access_token |
| `DINGTALK_SECRET` | 启用 dingtalk 渠道时必填 | - | 钉钉自定义机器人的加签密钥（安全设置选"加签"） |
| `AT_MOBILES` | 否 | 空 | 钉钉告警时需要 @ 的手机号，多个用逗号分隔 |
| `BARK_SERVER` | 否 | `https://api.day.app` | Bark 服务器地址，自建服务器改成自己的地址 |
| `BARK_DEVICE_KEY` | 启用 bark 渠道时必填 | - | Bark App 里生成的设备 Key |
| `WECOM_WEBHOOK_KEY` | 启用 wecom 渠道时必填 | - | 企业微信群机器人 Webhook 里 `key=` 后面的那一串 |
| `SERVERCHAN_SENDKEY` | 启用 serverchan 渠道时必填 | - | Server 酱 SendKey，可转发到微信 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 启用 telegram 渠道时必填 | - | Telegram Bot 的 token 和目标 chat id |
| `API_URL` | 否 | `http://localhost/api/douyin/web/fetch_user_post_videos` | 抓取抖音视频列表的接口地址 |
| `STALE_THRESHOLD` | 否 | `604800`（7 天，单位秒） | API 响应内容连续多久不变就提醒"疑似 Cookie 过期" |
| `FETCH_COUNT` | 否 | `10` | 单次请求拉取的视频条数。如果某用户经常一次发布很多视频，建议调大，否则可能漏检 |
| `MAX_CONCURRENT_USERS` | 否 | `5` | 并发检查账号数的安全上限。正常情况下真正控制请求节奏的是下面的轮询间隔，这个值只是防止极端情况下同时挂起的请求数失控，一般不需要调整 |
| `POLL_INTERVAL_MIN` | 否 | `15` | 每轮检测之间的最小等待间隔（秒）。密集监控可设小（如 10），佛系监控可设大（如 300） |
| `POLL_INTERVAL_MAX` | 否 | `40` | 每轮检测之间的最大等待间隔（秒）。实际间隔为 [POLL_INTERVAL_MIN, POLL_INTERVAL_MAX] 之间的随机值 |
| `LOG_LEVEL` | 否 | `INFO` | 终端/`journalctl` 实时输出的详细程度，不影响日志文件内容（见下文） |
| `WEB_ENABLED` | 否 | `false` | 是否随主循环一起启动只读 Web 状态面板 |
| `WEB_HOST` | 否 | `127.0.0.1` | 面板监听地址，默认只监听本机；局域网访问改成 `0.0.0.0`（注意安全风险，面板本身不做鉴权） |
| `WEB_PORT` | 否 | `8787` | 面板监听端口 |

`.env` 支持值两边带引号的写法，例如 `DINGTALK_TOKEN="xxx"` 和 `DINGTALK_TOKEN=xxx` 都可以。用 `deploy.sh install` 部署的话，`.env` 会根据你选择的推送渠道交互式生成，不需要手动对照这张表填。

## 配置说明（`users.conf`）

```
# 格式：sec_user_id|昵称
# sec_user_id 是抖音账号的加密用户 ID（可在浏览器开发者工具的接口请求里找到），不是抖音号
# 昵称仅用于通知展示，可以重复，不影响监控逻辑
# 以 # 开头的行和空行会被忽略
# 脚本每轮都会重新读取本文件，修改后无需重启脚本
# 同一个 sec_user_id 如果出现了多行，只有第一行会生效，其余的会在日志里打印警告并跳过

MS4wLjABAAAA_example_sec_user_id_1|示例用户A
MS4wLjABAAAA_example_sec_user_id_2|示例用户B
```

## 命令行参数

```bash
python3 douyin_monitor.py            # 常驻监控（默认行为）
python3 douyin_monitor.py --once     # 只检测一轮后退出，便于调试或配合 cron
python3 douyin_monitor.py --status   # 打印最近一次状态快照（status.json）
```

## 日志

日志分两路保存，分别在两个文件夹里：

```
log/
├── info/
│   └── monitor.log        # 只记录 INFO 及以上：关键事件 + 每轮汇总，日常查看用
└── debug/
    └── monitor.log        # 记录全部级别，包含每个用户每一轮的详细过程，排查问题用
```

`info/monitor.log` 平时看到的内容大致是这样，干净紧凑：

```
[2026-06-24 12:00:00] ✅ 抖音监控服务已启动（PID 12345，推送渠道: dingtalk,bark）
[2026-06-24 12:00:23] 💤 本轮完成：检查 5 个用户，均无变化，等待 23 秒...
[2026-06-24 12:05:31] 💤 本轮完成：检查 5 个用户，新视频 2 条，等待 31 秒...
[2026-06-24 12:08:47] 💤 本轮完成：检查 5 个用户，标题变更 1 条，等待 19 秒...
[2026-06-24 12:10:02] 🚨 用户 XXX 连续失败 5 次
[2026-06-24 12:15:20] 🗑️ 用户 XXX 确认检测到 1 条视频被删除
```

不会出现"检查用户 X""用户 X 无更新"这类逐条噪音，这些细节始终完整保存在 `debug/monitor.log` 里，需要排查问题时直接去翻那个文件，不需要重启脚本或改配置。`debug/monitor.log` 里还会看到类似 `⏳ 用户 XXX 有 N 条视频疑似消失，待后续轮次确认` 这样的记录，这是删除二次确认机制的中间状态，正常现象，不代表出了问题。

`.env` 里的 `LOG_LEVEL` 只影响**终端 / `journalctl` 实时输出**的详细程度（默认 `INFO`；想让终端也实时刷出逐用户细节就设成 `DEBUG`），不影响两个日志文件的内容——文件该记的内容始终都会完整记录。

### 自动轮转与压缩

两个文件夹下的日志各自独立轮转：单个 `monitor.log` 超过 10MB 时自动轮转，归档文件用 gzip 压缩保存为 `monitor.log.1.gz`、`monitor.log.2.gz`、`monitor.log.3.gz`，只保留最近 3 份，超出的自动丢弃最旧的一份。查看归档内容可以用 `zcat monitor.log.1.gz` 或 `zless monitor.log.1.gz`。

由于 `debug/` 文件夹记录的内容明显比 `info/` 多，它会更快攒满 10MB 触发轮转，这是正常现象。

## 关于"漏检"的提醒

抖音接口单次最多返回 `FETCH_COUNT` 条非置顶视频（默认 10）。如果某用户在一次检测间隔内发布的新视频数 ≥ `FETCH_COUNT`，超出部分会被漏检。脚本检测到这种情况会在日志里打印警告，建议适当调大 `.env` 中的 `FETCH_COUNT`。

## 关于删除通知的延迟

为了避免接口偶发抖动（某一轮响应里漏返了几条视频）导致的假删除通知，删除判定不是"这一轮没看到就立刻通知"，而是要连续多轮都没看到才会真正判定为删除：

- 普通视频：连续 2 轮检测都消失才会通知
- 置顶视频：连续 3 轮检测都消失才会通知（更保守，因为置顶视频被删的概率本来就低）

也就是说，一条视频真被删除后，你收到通知的时间会比"删除动作发生"晚 1~2 轮检测间隔（通常几十秒到几分钟，取决于你的轮询间隔），这是有意为之的权衡，用来换取"不会先误报删除、几十秒后又误报新发布"这种更让人困惑的假警报。如果中途视频又重新出现（说明只是接口抖动），计数会自动清零，什么通知都不会发。

## 常见问题

**Q: 脚本提示"监控脚本已在运行，请勿重复启动"，但我确认没有别的进程在跑？**

检查工作目录下的 `monitor.pid` 文件锁是否被异常进程占用，或者上次进程是否被强行杀死后系统还没释放文件锁。可以用 `lsof monitor.pid` 或 `fuser monitor.pid` 看看是否真的有进程持有锁；确认无进程占用后可以直接删除该文件再重启，或者直接 `./deploy.sh restart`。

**Q: 一直收不到任何通知，是不是配置错了？**

先用 `python3 douyin_monitor.py --once`（或 `./deploy.sh status`）跑一轮，看终端输出和 `log/debug/monitor.log` 里有没有报错（比如钉钉返回的 `errcode`、其它渠道返回的 HTTP 状态码/错误信息），大多数问题都能在这里看出来。如果启用了多个渠道，日志里 `[渠道名]` 前缀能看出具体是哪个渠道失败。

**Q: 想监控的用户突然不更新通知了？**

看一下 `status.json`（`python3 douyin_monitor.py --status` 或 `./deploy.sh status`）里对应用户的 `consecutive_fails` 和 `hours_since_update`，结合是否收到过"连续失败"或"Cookie 过期"告警来判断。启用了 Web 面板的话，打开面板页面看会更直观。

**Q: 启动时报 `ModuleNotFoundError: No module named 'requests'`（或 `dotenv`/`dingtalkchatbot`）？**

说明依赖没装，或者装的位置跟运行脚本时用的 Python 不是同一个。先确认是否激活了虚拟环境（`source venv/bin/activate`），再 `pip install -r requirements.txt`。用 `deploy.sh` 部署的话正常不会出现这个问题；如果是手动部署 + systemd，检查 `ExecStart` 是否指向了虚拟环境里的 `python3`（见上文部署步骤里的提示）。

**Q: 钉钉群一直收到签名错误，或者完全没反应？**

检查 `DINGTALK_SECRET` 是不是以 `SEC` 开头——钉钉机器人"加签"方式的密钥都是这个格式，如果填的是别的字符串（比如不小心填了 access_token 或别的密钥），脚本启动时会在日志里打印警告提醒你，但不会阻止启动。去钉钉群里机器人设置页面重新核对一下 access_token 和加签密钥分别填对了没有。

**Q: 已经部署好了，想再加一个推送渠道（比如再加个 Bark）怎么办？**

用 `./deploy.sh config`，选择重新配置推送渠道即可，原 `.env` 会先自动备份成 `.env.bak_时间戳` 再重新生成。也可以手动编辑 `.env`，把 `NOTIFY_CHANNELS` 改成 `dingtalk,bark` 这样的逗号分隔列表，再把对应渠道需要的配置项（参考上文配置说明表）填上，然后 `./deploy.sh restart`。

**Q: 为什么删除视频的通知会晚一段时间才收到，而不是立刻收到？**

这是有意的设计，见上文"关于删除通知的延迟"章节，不是 bug。

**Q: `users.conf` 里配置了同一个账号两次，会怎么样？**

只有第一次出现的那一行会生效，重复的行会在日志里打印警告并跳过，不会导致重复通知或状态文件损坏。建议看到警告后手动清理一下配置文件。

**Q: 按 `Ctrl+C` 或 `systemctl stop`（或 `./deploy.sh stop`）之后，脚本没有立刻退出，等了几秒才停？**

这是正常现象，不是卡死。脚本收到退出信号后会等正在处理中的账号检查完成再退出，最长不会超过单次请求的超时时间（默认 10 秒）。这是为了避免强行中断一个正在进行的检查，导致状态文件写到一半或者通知发到一半——用短暂的等待换取"不留下不一致状态"，是有意的设计取舍。

**Q: `./deploy.sh update` 更新完之后运行不正常，能回滚吗？**

可以。每次 `update` 之前都会把旧的 `douyin_monitor/` 和 `douyin_monitor.py` 备份到工作目录下的 `.backup_时间戳/`，把里面的文件拷回工作目录覆盖新代码，再 `./deploy.sh restart` 就是回滚。确认新版本没问题之后，这些备份目录可以自行删除。

## 开发与测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

单元测试覆盖了删除二次确认状态机、滚动出窗口的静默清理、置顶视频三轮确认、请求限速器 `RequestPacer`、`users.conf` 解析去重、多渠道通知的容错广播等核心逻辑，改动这些模块后建议先跑一遍测试再部署。
