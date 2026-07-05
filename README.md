# 抖音多用户视频监控脚本

定期检查多个抖音账号是否发布了新视频或删除了旧视频，并通过钉钉群机器人推送通知。依赖三个常用的第三方库（`requests`、`python-dotenv`、`DingtalkChatbot`），通过 `pip install -r requirements.txt` 安装即可。

## 功能特性

- 多用户监控，配置文件支持热加载（运行中修改 `users.conf` 无需重启脚本），并按 `sec_user_id` 自动去重（重复配置会打印警告并跳过，避免同一账号被检查两次）
- 并发检查多个账号（可通过 `MAX_CONCURRENT_USERS` 配置），但整体"发起新请求"的节奏与原串行版本保持一致（默认相邻请求间隔 3~8 秒随机），不会因为改成并发就变相提高访问下游抓取 API 的频率；好处是某个账号请求卡住或超时，不会拖慢排在它后面的其他账号
- 自动识别新发布的视频并逐条推送通知（带视频封面、标题、发布时间、观看链接）
- 视频被删除时单独推送通知，并能正确区分"真实删除"和"视频被新内容挤出抓取窗口"两种情况；删除判定带**二次确认**——普通视频需连续 2 轮检测都消失、置顶视频需连续 3 轮都消失才会真正判定为删除并推送通知，避免接口偶发抖动导致的"先误报删除、马上又误报新发布"的假警报
- 置顶视频的置顶状态变化、视频标题变化都会被静默检测并同步记录到本地状态（标题变化会记一条 INFO 日志，不会额外推送钉钉通知，只是保证本地保存的标题始终是最新的，避免以后这条视频真被删除时，通知里显示的还是旧标题）
- 已知视频列表数量超过上限时会自动裁剪最旧的记录，且不会裁剪置顶视频（置顶视频通常发布时间最早，若参与裁剪，裁掉后会被误判成"新视频"重新推送一次）
- 接口连续请求失败会触发告警，恢复后自动推送恢复通知，均带冷却时间避免刷屏
- 两级 Cookie 失效检测：API 响应内容长时间无变化 / 长期没有新视频，两种情况都会提醒你去更新 Cookie；同时对"接口返回的视频字段异常（缺少视频 ID）"这类畸形响应做了防御，避免被误判成批量删除
- 每个监控账号的运行状态独立保存为一个 JSON 文件，脚本重启不丢失检测进度
- 日志分文件夹保存，按级别区分用途，并自动轮转、压缩归档（见下文"日志"章节）
- 文件锁防止脚本被重复启动；支持 `Ctrl+C` / `systemctl stop` 优雅退出

## 环境要求

- Linux（依赖 `fcntl` 做文件锁，暂不支持 Windows）
- Python 3.8+
- 三个第三方库：`requests`（HTTP 请求）、`python-dotenv`（解析 `.env`）、`DingtalkChatbot`（钉钉机器人加签与推送）
- 并发检查功能使用的是 Python 标准库自带的 `concurrent.futures`，不需要额外安装依赖

## 文件说明

| 文件 | 作用 |
|---|---|
| `douyin_monitor.py` | 主脚本 |
| `requirements.txt` | 依赖列表，`pip install -r requirements.txt` 安装 |
| `.env.example` | 环境变量配置示例，部署时复制为 `.env` 并填写 |
| `users.conf.example` | 监控用户列表示例，部署时复制为 `users.conf` |

脚本运行时会在工作目录下自动创建以下内容：

```
<工作目录>/
├── .env                # 你的配置（需自己创建）
├── users.conf          # 你要监控的用户列表（需自己创建）
├── state/              # 每个用户的运行状态（脚本自动维护，JSON 格式）
├── log/
│   ├── info/            # 精简日志：关键事件 + 每轮汇总
│   └── debug/           # 完整日志：包含每个用户每一轮的详细过程
├── status.json          # 最近一次检测的状态快照（--status 查看的就是这个）
└── monitor.pid           # 防止重复启动的锁文件
```

## 部署步骤

```bash
# 1. 选一个工作目录，放入脚本和配置文件
sudo mkdir -p /opt/douyin-monitor
sudo cp douyin_monitor.py requirements.txt /opt/douyin-monitor/
sudo cp .env.example /opt/douyin-monitor/.env
sudo cp users.conf.example /opt/douyin-monitor/users.conf

# 2. 建议用虚拟环境安装依赖，避免污染系统 Python
cd /opt/douyin-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 编辑配置
vi .env          # 填入钉钉 token/secret、API_URL
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

### 用 systemd 常驻（推荐）

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

## 配置说明（`.env`）

| 配置项 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DINGTALK_TOKEN` | 是 | - | 钉钉自定义机器人 Webhook 的 access_token |
| `DINGTALK_SECRET` | 是 | - | 钉钉自定义机器人的加签密钥（安全设置选"加签"） |
| `API_URL` | 否 | `http://localhost/api/douyin/web/fetch_user_post_videos` | 抓取抖音视频列表的接口地址 |
| `STALE_THRESHOLD` | 否 | `604800`（7 天，单位秒） | API 响应内容连续多久不变就提醒"疑似 Cookie 过期" |
| `FETCH_COUNT` | 否 | `10` | 单次请求拉取的视频条数。如果某用户经常一次发布很多视频，建议调大，否则可能漏检 |
| `MAX_CONCURRENT_USERS` | 否 | `5` | 并发检查账号数的安全上限。正常情况下真正控制请求节奏的是脚本内置的 3~8 秒随机间隔，这个值只是防止极端情况下（比如同时有多个账号请求卡在超时边缘）同时挂起的请求数失控，一般不需要调整 |
| `AT_MOBILES` | 否 | 空 | 告警时需要 @ 的手机号，多个用逗号分隔 |
| `LOG_LEVEL` | 否 | `INFO` | 终端/`journalctl` 实时输出的详细程度，不影响日志文件内容（见下文） |

`.env` 支持值两边带引号的写法，例如 `DINGTALK_TOKEN="xxx"` 和 `DINGTALK_TOKEN=xxx` 都可以。

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
[2026-06-24 12:00:00] ✅ 抖音监控服务已启动（PID 12345，使用钉钉群机器人推送）
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

检查工作目录下的 `monitor.pid` 文件锁是否被异常进程占用，或者上次进程是否被强行杀死后系统还没释放文件锁。可以用 `lsof monitor.pid` 或 `fuser monitor.pid` 看看是否真的有进程持有锁；确认无进程占用后可以直接删除该文件再重启。

**Q: 一直收不到任何通知，是不是配置错了？**

先用 `python3 douyin_monitor.py --once` 跑一轮，看终端输出和 `log/debug/monitor.log` 里有没有报错（比如钉钉返回的 `errcode`、API 请求的 HTTP 状态码），大多数问题都能在这里看出来。

**Q: 想监控的用户突然不更新通知了？**

看一下 `status.json`（`python3 douyin_monitor.py --status`）里对应用户的 `consecutive_fails` 和 `hours_since_update`，结合钉钉群里是否收到过"连续失败"或"Cookie 过期"告警来判断。

**Q: 启动时报 `ModuleNotFoundError: No module named 'requests'`（或 `dotenv`/`dingtalkchatbot`）？**

说明依赖没装，或者装的位置跟运行脚本时用的 Python 不是同一个。先确认是否激活了虚拟环境（`source venv/bin/activate`），再 `pip install -r requirements.txt`。用 systemd 部署的话，检查 `ExecStart` 是否指向了虚拟环境里的 `python3`（见上文部署步骤里的提示）。

**Q: 钉钉群一直收到签名错误，或者完全没反应？**

检查 `DINGTALK_SECRET` 是不是以 `SEC` 开头——钉钉机器人"加签"方式的密钥都是这个格式，如果填的是别的字符串（比如不小心填了 access_token 或别的密钥），脚本启动时会在日志里打印警告提醒你，但不会阻止启动。去钉钉群里机器人设置页面重新核对一下 access_token 和加签密钥分别填对了没有。

**Q: 为什么删除视频的通知会晚一段时间才收到，而不是立刻收到？**

这是有意的设计，见上文"关于删除通知的延迟"章节，不是 bug。

**Q: `users.conf` 里配置了同一个账号两次，会怎么样？**

只有第一次出现的那一行会生效，重复的行会在日志里打印警告并跳过，不会导致重复通知或状态文件损坏。建议看到警告后手动清理一下配置文件。