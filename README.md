# dywatch —— 抖音账号视频监控

监控多个抖音账号，检测**新作品**与**作品消失**，通过钉钉 / 企业微信 / Bark / Server 酱 /
Telegram / 通用 webhook 推送通知。

它建立在 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
**v5** 之上，但只做一件事：**盯着数据变没变**。

```
      抖音  ←—— 签名 / 身份池 / 调度器 / 熔断 / 归档 ——  DTK v5
                                                          ↑
                                        HTTP /api/v1（默认只读，一个 API Key）
                                                          ↓
                                   dywatch  ←—— 判定 → 通知 → 面板
```

**本工具不自带签名、不碰 Cookie、不碰身份池、不直接访问抖音。** 抓取、风控绕行、
身份轮换全部由 DTK v5 负责；dywatch 对 DTK **默认是纯只读的**（不提交任务、不改设置、
不注册 watchlist），所以只需要两个 scope、也不会把 DTK 实例搞坏。唯一的例外是一个
**默认关闭**的开关：打开 `ARCHIVE_DOWNLOAD_ENABLED` 后会请求 DTK 把新作品的媒体存一份
（见 §8），那需要额外授一个 `media:write`。

**部署形态只有一种：Ubuntu / Linux 服务器上的 systemd 服务。** 不提供容器镜像——
这个工具就是一个进程 + 一个 SQLite 文件 + 一份配置，systemd 已经把它该管的事
（开机自启、崩溃重启、日志归集、权限隔离）都管完了，再加一层编排只会多一处要维护的东西。

---

## 目录

- [dywatch —— 抖音账号视频监控](#dywatch--抖音账号视频监控)
  - [目录](#目录)
  - [1. 快速开始（Ubuntu）](#1-快速开始ubuntu)
    - [前置：DTK v5 实例 + 一把 API Key](#前置dtk-v5-实例--一把-api-key)
    - [安装](#安装)
  - [2. 命令](#2-命令)
  - [3. 面板（`WEB_ENABLED=true`）](#3-面板web_enabledtrue)
  - [4. 监控列表 `users.conf`](#4-监控列表-usersconf)
  - [5. 配置参考](#5-配置参考)
    - [上游](#上游)
    - [请求节奏 ⚙️（沿用旧项目已跑数月的实测值）](#请求节奏-️沿用旧项目已跑数月的实测值)
    - [判定](#判定)
    - [失败与退避](#失败与退避)
    - [归档](#归档)
    - [通知](#通知)
    - [面板与其他](#面板与其他)
  - [6. 容量估算](#6-容量估算)
    - [状态库会长多大](#状态库会长多大)
  - [7. 判定规则](#7-判定规则)
    - [新作品](#新作品)
    - [作品消失（分级确认）](#作品消失分级确认)
    - [漏检](#漏检)
    - ["作者 ID 写错了"怎么发现](#作者-id-写错了怎么发现)
    - [置顶标志从哪来](#置顶标志从哪来)
    - [事件类型（`events.kind`）](#事件类型eventskind)
  - [8. 归档下载（`ARCHIVE_DOWNLOAD_ENABLED`，默认关闭）](#8-归档下载archive_download_enabled默认关闭)
    - [权限：需要额外申请 `media:write`](#权限需要额外申请-mediawrite)
    - [不等下载完成，也不该等](#不等下载完成也不该等)
    - [节奏、预算与退避](#节奏预算与退避)
    - [会不会被自动清掉：取决于 `ARCHIVE_DOWNLOAD_PIN`](#会不会被自动清掉取决于-archive_download_pin)
  - [9. 排障](#9-排障)
    - [日志轮转](#日志轮转)
  - [10. 架构](#10-架构)

---

## 1. 快速开始（Ubuntu）

### 前置：DTK v5 实例 + 一把 API Key

在 DTK 控制台「用户与 API 密钥」创建，**监控本身只需要两个 scope**：

| scope          | 用途                                               |
| -------------- | -------------------------------------------------- |
| `douyin:read`  | 读作者作品列表（主抓手）、识别分享链接、读任务结果 |
| `archive:read` | 读本地归档，做删除交叉确认（零身份成本）           |

角色 `viewer` 即可——这些端点只按 scope 鉴权。**Key 的形态是
`dtk_<12位十六进制>_<32位base64url>`（共 49 字符），完整值只在创建时显示一次。**

如果打算开 §8 的「归档下载」，额外申请 `media:write`（写权限，见该节说明，默认不建议
在第一次部署时就开）。

### 安装

**Python 必须 ≥ 3.11**（`pyproject.toml` 的 `requires-python`；Ubuntu 24.04 自带 3.12 够用，
22.04 自带的 3.10 不够）。`install.sh` 会自己在 `python3.14 / 3.13 / 3.12 / 3.11 / python3`
里挑版本最高的一个，也可以用 `PYTHON=/usr/bin/python3.12` 显式指定——它不会再盲信 `python3`。

```bash
sudo apt update && sudo apt install -y python3 python3-venv   # 版本不够时先装一个 3.11+
git clone <本仓库> && cd douyin-monitor
sudo bash deploy/install.sh      # 建 .venv、装 systemd 单元与日志轮转 cron、生成配置模板
# 解释器不在默认位置时：sudo PYTHON=/usr/bin/python3.12 bash deploy/install.sh

vi /opt/douyin-monitor/.env          # 填 DTK_API_KEY（要用通知就一并填渠道）
vi /opt/douyin-monitor/users.conf    # 填要监控的账号

# 先自检，再跑一轮，最后常驻
cd /opt/douyin-monitor
./.venv/bin/python -m dywatch doctor
./.venv/bin/python -m dywatch once
sudo systemctl enable --now dywatch
journalctl -u dywatch -f
```

`install.sh` 会自动判断这是首次安装还是升级（看 `.venv` 和 systemd 单元是否已存在），
两条路径做的事不一样：

- **首次安装**：建 `.venv` 装依赖 → 生成 `.env` / `users.conf` 模板 → 装 systemd 单元与
  日志轮转 cron → 交接目录属主。它**不**替你填 API Key：凭据不该由脚本猜。
- **升级**（`git reset --hard` 后重跑同一条命令）：**重装依赖** → 刷新 systemd 单元与
  日志轮转 cron（**并删掉老版本留在 `/etc/logrotate.d/dywatch` 的那份配置**，见下面的
  日志轮转说明）→ 如果服务正在跑，问你要不要立即重启（`--yes` 直接重启，不问）。
  如果安装目录**不是**要安装的那个目录（`SRC_DIR != MONITOR_HOME`），它还会先用
  `rsync --delete` 同步代码过去（`.env` / `users.conf` / `data/` / `log/` / `.venv/`
  一律不碰）；按本 README 的 `/opt/douyin-monitor` 布局时两者是同一个目录，这一步跳过，
  代码由 `git` 就地更新。
  **"重装依赖"这一步不能省**：`.venv` 里装的是**复制**进去的一份代码（非可编辑安装），
  只 `git reset --hard` + `systemctl restart` 不会让新代码生效——重新跑 `install.sh`
  （或手动 `pip install .`）才会把 `src/` 的内容刷进 `.venv`。

其他参数：`--check` 只检测当前是首次安装还是升级、缺什么依赖，不做任何改动；
`sudo bash deploy/install.sh --yes` 可以做到全自动（升级 + 服务在跑就自动重启），
适合写进你自己的升级脚本。

**服务器侧的更新用 `git fetch && git reset --hard origin/main`，不要用 `git pull`。**
安装在 `/opt/douyin-monitor` 的这份是纯部署副本，永远不会在上面产生提交；
而本仓库的历史被 amend / force-push 改写过，`pull` 会因为 "divergent branches"
直接拒绝（`git pull --ff-only` 同样失败）。安全前提是"服务器上没有独有提交"，
这条一直成立：

```bash
cd /opt/douyin-monitor
git fetch origin
git reset --hard origin/main      # 本地若有过手工改动，会一并丢掉，先 git diff 看一眼
sudo bash deploy/install.sh --yes
```

**不创建专用系统用户。** 服务以"执行安装的那个账号"身份运行（`sudo` 时取 `SUDO_USER`，
直接用 root 跑则服务也以 root 跑，脚本会提醒你）。所以后面的 `vi`、`doctor`、`once`
都是你自己的账号，不需要 `sudo -u` 到处切换；隔离靠 systemd 单元里的
`ProtectSystem=strict` + `ReadWritePaths`，不靠换用户。
装到别的目录用 `sudo MONITOR_HOME=/srv/dywatch bash deploy/install.sh`。

`DTK_API_KEY` 留空时 `dywatch run` 会在启动时直接拒绝（退出码 2），systemd 单元配了
`RestartPreventExitStatus=2`，不会因此陷入"每 10 秒重启一次、日志刷屏"的死循环——
会停在 `failed` 状态等你去 `vi .env` 填上，然后 `systemctl restart dywatch`。

常用运维：

```bash
systemctl status dywatch          # 服务状态
systemctl restart dywatch         # 改完 .env 后重启（users.conf 改完不用重启）
journalctl -u dywatch -f          # 实时日志（systemd 侧）
tail -f /opt/douyin-monitor/log/info/monitor.log   # 实时日志（应用侧）
```

> 说明：`doctor` 与 `once` 每个账号会消耗 1 个身份，属正常开销；
> 它们不打网络也能给出有用的结论（配置校验、Key 的 scope、账号 ID 是否有效）。

---

## 2. 命令

```bash
./.venv/bin/python -m dywatch                # 常驻监控（systemd 用这个）
./.venv/bin/python -m dywatch once           # 只跑一轮后退出：上线前确认配置是否正确
./.venv/bin/python -m dywatch doctor         # 自检：实例可达 / Key 有效 / scope 够用 / 账号读得出来
./.venv/bin/python -m dywatch status         # 打印最近一轮的状态快照
./.venv/bin/python -m dywatch config-check   # 打印全部生效配置与每一项的来源
./.venv/bin/python -m dywatch add "<主页链接或 sec_user_id>" [昵称]
./.venv/bin/python -m dywatch test-notify    # 给每个渠道发一条测试消息
```

前缀写成 `./.venv/bin/python -m` 是为了"在安装目录里、不进虚拟环境也能直接抄着跑"
（systemd 用的也是这一条）。`source .venv/bin/activate` 之后可以直接写 `dywatch doctor`
——`install.sh` 装出来的 `.venv/bin/dywatch` 与 `python -m dywatch` 是同一个入口，
**本文其余部分一律用 `dywatch <子命令>` 这种简写**。

命令都接受 `--env /path/to/.env` 指定配置文件（默认 `$MONITOR_HOME/.env`，其次 `./.env`）；
`--version` 打印版本。

**先跑 `doctor`**：它把"配错了"和"上游坏了"分开——这两类的处理方式完全不同。

```
dywatch 0.1.0 —— 自检
上游实例: http://192.168.20.4:8000
✓ 凭据有效：username=yunfei role=admin via=api_key
  scopes: archive:read, douyin:read, media:read, media:write
✓ 必需的 scope 齐备
✓ 归档下载可用：已用 6.9MB / 上限 2048MB（0%），已 pin 0 条，下载器 在线   ← 开了归档下载才有这行
✓ 实例 5.1.0：身份池 douyin active=3 cooling=0 degraded=0
检查 1 个账号（每个消耗 1 个身份）…
  ✓ 示例账号: 18 条（非置顶 15 / 置顶 3，count=15） 2640ms has_more=True
✓ 自检通过
```

---

## 3. 面板（`WEB_ENABLED=true`）

```bash
WEB_ENABLED=true          # .env
WEB_HOST=127.0.0.1        # 默认只听回环；要局域网访问改 0.0.0.0
WEB_PORT=8787
```

打开 `http://127.0.0.1:8787/`：一屏看完"每个账号现在怎么样"。

- **LED 状态阵列**：24 格按状态计数分配（正常 / 失败 / 长期无更新 / 从未有作品 / 已移除），
  小类别保底 1 格——100 个账号里那 1 个在失败的，不会被舍成 0 格而看不见
- **数据条**：账号总数、正常、请求失败、长期无新作品、从未有作品、已移除
  （最后一项只在真有账号被移出 `users.conf` 时出现）
- **账号列表**：状态徽章、更新频率（悬停看"基于最近 N 条非置顶作品，平均 X 天/条"）、
  已知作品数、**距最新作品发布多久**（"3 天前发布"——看的是作品的发布时间，不是"上次检测到
  变化"的时间，后者会被一次删除/改名刷新）；整行都可以点，手机上按起来不费劲
- **详情弹窗**：已知作品（**置顶的排最前**，含类型、缺席轮数）、**已消失作品**（含消失原因）、
  **最近事件**、以及最新作品发布时刻 / 更新频率 / 首次记录 / 累计轮次 / 最近一次错误码
- **页面顶部有两个轮次数，口径不同**：
  - **本次运行第 N 轮** —— 这次进程启动以来跑了多少轮（内存计数）。**重启就从头数**，
    所以它小不代表丢数据
  - **累计 M 轮** —— 状态库里累计记录了多少轮，跨重启、也不受 `ROUNDS_KEEP_DAYS` 裁剪影响
    （取的是 SQLite 的自增号段，不是行数）
  - 详情弹窗里的「累计轮次」是第三个口径：**这个账号**被检查过的轮数（`authors.runs`）
- 上游闸门关闭时，页面顶部出现红色警示条（与通知里的"本轮整体跳过"是同一件事）

面板**只读、无鉴权、不发任何上游请求**：列表读 `data/status.json`，详情读状态库，
所以打开它不消耗身份、不会被风控。要对外暴露请自己加反代鉴权。

小屏（手机）下布局会自动重排：状态单独一行、账号名整行（好点），上游地址与 PID 不占位置；
详情变成底部抽屉，内容长的时候整页滚动（抽屉里不再嵌一层滚动条），并留出 iPhone 底部安全区。

顺带提供的机器接口：`/api/state`（快照原文）、`/api/health`（小结）、
`/api/user/<sec_user_id>`（单账号详情）、`/metrics`（Prometheus）、`/healthz` `/readyz`（探针）。

---

## 4. 监控列表 `users.conf`

```
# <sec_user_id>|<昵称>
MS4wLjABAAAA4MjTvxSsNOjHfi9kfyRdu0KMKRHA1dPNv1WQQwW0OKY|示例账号
```

`sec_user_id` 以 `MS4wLjABAAAA` 开头，是抖音账号的稳定 ID。**不知道怎么写就直接粘主页链接**：

```bash
./.venv/bin/python -m dywatch add "https://www.douyin.com/user/MS4wLjABAAAA..."
```

它会调用 DTK 的 `/api/v1/tools/parse-url` 把链接转成 ID（零成本、不消耗身份）。
短链（`v.douyin.com/...`）必须先跳转才能知道目标，本工具会提示你改粘主页链接——
而不是替你花一个身份。

**改完不用重启**：文件按 mtime 热加载，运行中的实例下一轮自动生效。

---

## 5. 配置参考

全部配置放在 `.env`（权限 `0600`）。这张表由 `src/dywatch/settings.py` 的 `SETTINGS`
注册表生成，所以代码与文档不会各说各的。带 ⚙️ 的是**不建议改**的实测值。

布尔值可以写成 `true/false`、`1/0`、`yes/no`、`on/off`（大小写不敏感，前后空格无所谓）。
**写成别的一律按非法处理**：该项回退到默认值，并在 `config-check` 的来源列里标成
`非法,已回退默认`——不会把 `ture` 这种手滑当成 false 悄悄放过去。

### 上游

| 键                   | 默认                    | 说明                                                                 |
| -------------------- | ----------------------- | -------------------------------------------------------------------- |
| `DTK_BASE_URL`       | `http://127.0.0.1:8000` | DTK 实例地址                                                         |
| `DTK_API_KEY`        | —                       | **必填**，形态 `dtk_...`（49 字符）                                  |
| `DTK_WAIT`           | `25`                    | `?wait=` 秒数；0 表示走纯异步（202 + 轮询）                          |
| `DTK_TIMEOUT`        | `35`                    | HTTP 超时，必须大于 `DTK_WAIT`                                       |
| `DTK_REFRESH` ⚙️     | `true`                  | 必须为 true，否则命中 DTK 的 300 秒列表缓存，监控粒度静默变成 5 分钟 |
| `INCLUDE_RAW`        | `auto`                  | 置顶标志策略：`auto` / `always` / `never`                            |
| `RAW_REFRESH_ROUNDS` | `20`                    | `auto` 模式下最多隔多少轮取一次 raw                                  |
| `DTK_USER_AGENT`     | `dywatch/0.1`           | 便于在 DTK 侧日志辨认                                                |

### 请求节奏 ⚙️（沿用旧项目已跑数月的实测值）

| 键                             | 默认        | 说明                                           |
| ------------------------------ | ----------- | ---------------------------------------------- |
| `REQUEST_INTERVAL_MIN` / `MAX` | `3` / `8`   | 全局相邻请求间隔（秒，随机）。**与并发数无关** |
| `POLL_INTERVAL_MIN` / `MAX`    | `15` / `40` | 轮末随机等待（秒），下限锁死 10                |
| `MAX_CONCURRENT`               | `5`         | 并发上限——只为不让慢账号拖累别人               |
| `FETCH_COUNT`                  | `15`        | 单页条数。注意抖音返回 `count + 置顶数` 条     |

### 判定

| 键                                 | 默认        | 说明                            |
| ---------------------------------- | ----------- | ------------------------------- |
| `DELETE_CONFIRM_ROUNDS`            | `2`         | 普通作品消失确认轮数            |
| `DELETE_CONFIRM_ROUNDS_TOP`        | `3`         | 置顶作品消失确认轮数            |
| `DELETE_CONFIRM_ROUNDS_ALL`        | `3`         | 全部作品同时消失的确认轮数      |
| `KNOWN_IDS_MAX`                    | `50`        | 每账号跟踪的作品上限            |
| `REMOVED_MAX` / `REMOVED_TTL_DAYS` | `200` / `7` | tombstone 上限与存活天数        |
| `EMPTY_ROUNDS_ALERT`               | `3`         | 连续空列表几轮后告警"请核实 ID" |
| `STALE_FALLBACK_DAYS`              | `14`        | 长期无新作品的一次性兜底提醒    |

### 失败与退避

| 键                                      | 默认        | 说明                                  |
| --------------------------------------- | ----------- | ------------------------------------- |
| `MAX_CONSECUTIVE_FAILS`                 | `5`         | 连续失败几次告警                      |
| `FAIL_COOLDOWN`                         | `300`       | 同类失败告警冷却（秒）                |
| `BACKOFF_AFTER` / `BACKOFF_MAX_SECONDS` | `2` / `600` | 上游 429/503 触发全局闸门后的翻倍退避 |

### 归档

| 键                               | 默认    | 说明                                                                                                       |
| -------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------- |
| `ARCHIVE_ENABLED`                | `true`  | 是否用 DTK 归档做删除交叉确认（零身份成本）                                                                |
| `ARCHIVE_DOWNLOAD_ENABLED`       | `false` | 新作品是否顺手触发 DTK 下载媒体存档，见 §8，需要 `media:write`                                             |
| `ARCHIVE_DOWNLOAD_PIN`           | `false` | 存下来的媒体是否永久保留：false=只留最近 2G（旧的自动删），true=都锁定不删（占满后新下载会失败），见 §8    |
| `ARCHIVE_DOWNLOAD_MAX_PER_ROUND` | `10`    | 每轮最多触发几条归档下载（请求过全局节奏器，所以这个数决定旁路最多把一轮拖多久）；超出的排队等下一轮，不丢 |

### 通知

| 键                                        | 默认                      | 说明                                                                |
| ----------------------------------------- | ------------------------- | ------------------------------------------------------------------- |
| `NOTIFY_CHANNELS`                         | `dingtalk`                | 逗号分隔，可多开：`dingtalk,wecom,bark,serverchan,telegram,webhook` |
| `SILENT_MODE`                             | `false`                   | 跳过全部推送，监控与面板照常                                        |
| `NOTIFY_GAP`                              | `1.0`                     | 相邻两条通知的间隔（秒）                                            |
| `DINGTALK_TOKEN` / `DINGTALK_SECRET`      | —                         | 钉钉机器人（加签密钥以 `SEC` 开头）                                 |
| `AT_MOBILES`                              | —                         | 告警时 @ 的手机号，逗号分隔                                         |
| `WECOM_WEBHOOK_KEY`                       | —                         | 企业微信群机器人                                                    |
| `BARK_SERVER` / `BARK_DEVICE_KEY`         | `https://api.day.app` / — | Bark                                                                |
| `SERVERCHAN_SENDKEY`                      | —                         | Server 酱                                                           |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | —                         | Telegram                                                            |
| `WEBHOOK_URL`                             | —                         | 通用 webhook，POST JSON                                             |

### 面板与其他

| 键                                      | 默认                           | 说明                                                                                                                            |
| --------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `WEB_ENABLED` / `WEB_HOST` / `WEB_PORT` | `false` / `127.0.0.1` / `8787` | 只读面板（状态页 + 账号详情），**无鉴权**，默认只听回环                                                                         |
| `LOG_LEVEL`                             | `INFO`                         | 只影响终端；日志文件始终是 info + debug 两份                                                                                    |
| `MONITOR_HOME`                          | 当前目录                       | 状态库、日志、users.conf 都在这里                                                                                               |
| `EVENTS_KEEP_DAYS` / `ROUNDS_KEEP_DAYS` | `30` / `5`                     | 事件审计与轮次汇总的保留期。**轮次周期只有几十秒**，`rounds` 是唯一会持续长大的表（1 个账号 ≈ 每天 2600 行），所以默认只留 5 天 |

---

## 6. 容量估算

节奏由 pacer 决定，**与账号数无关**：

```
请求速率上限 ≈ 60 / 平均(3, 8) ≈ 10.9 次/分钟
轮次周期     ≈ 账号数 × 5.5 秒 + random(15, 40) 秒
```

| 账号数 | 一轮耗时（估） | 请求速率 | 单账号多久被查一次 |
| ------ | -------------- | -------- | ------------------ |
| 5      | ~55 秒         | ~11/min  | ~1 分钟            |
| 10     | ~82 秒         | ~11/min  | ~1.5 分钟          |
| 20     | ~2.3 分钟      | ~11/min  | ~2.5 分钟          |
| 50     | ~5 分钟        | ~11/min  | ~5 分钟            |
| 100    | ~9.6 分钟      | ~11/min  | ~10 分钟           |

约 11 次/分钟远低于 DTK 默认的 120/分钟，对身份池也很温和。
**真正的约束是覆盖延迟**：账号越多，单个账号被检查的间隔越长。

### 状态库会长多大

一轮写一行 `rounds`、只在有事件时写 `events`，所以增长的几乎只有轮次表：

| 账号数 | 每天轮数 | `ROUNDS_KEEP_DAYS=5`（默认） | `=30`     |
| ------ | -------- | ---------------------------- | --------- |
| 1      | ~2600    | ~1.3 万行                    | ~7.8 万行 |
| 5      | ~1500    | ~7.5 千行                    | ~4.5 万行 |
| 20     | ~630     | ~3.2 千行                    | ~1.9 万行 |

单行很小（9 个字段 + 一个 `ts` 索引），所以几万行也不过几 MB 到十几 MB——
问题不在磁盘，而在这张表**没有任何读取方**（面板与 `--status` 都不看它，
只有排障时手工查），留着几十万行没有意义，所以默认只留 5 天。
改 `ROUNDS_KEEP_DAYS` 后**下一轮**就会删掉超期行，但 SQLite 删行不缩文件，
要真正回收得 `VACUUM`（见第 9 章排障表）。

---

## 7. 判定规则

### 新作品

本页出现、本地没记录过、也不在 tombstone 里 → 推送。首次见到一个账号时只记录**不推送**
（否则上线一个新账号会被历史作品刷屏）。

### 作品消失（分级确认）

| 类型             | 连续几轮不在本页才确认    |
| ---------------- | ------------------------- |
| 普通作品         | 2                         |
| 置顶作品         | 3                         |
| 全部作品同时消失 | 3（并在通知里附核实提醒） |

确认前只累计计数，不发任何通知；中途回来则计数清零。
**"被新作品挤出窗口"不算消失**：窗口整体前移了多少，就最多有多少条能用挤出解释，
这些走静默清理并写 tombstone——于是它们将来若因窗口回移重新出现，也不会被当成新作品重复推送。

### 漏检

抖音返回的是 `count + 本页置顶数` 条，所以"条数达到 count"不等于被截断。
本工具改用**时间连续性**判断：本页**非置顶**作品里最旧的发布时间，比上一轮非置顶作品里
最新的发布时间还新 → 中间必然有没采集到的作品，于是告警建议调大 `FETCH_COUNT`。

### "作者 ID 写错了"怎么发现

DTK 对**形态合法但不存在**的 `sec_user_id` 返回 `200 + items: []`，上游永远不会报错。
所以本工具区分两种"空列表"：从未见过作品的账号连续 3 轮为空 → 告警
**"该账号始终无作品，请核实 ID"**；曾经有作品的账号变空 → 走上面的"全部消失"确认。

### 置顶标志从哪来

DTK 的归一化结果不暴露置顶，只有 `include_raw=true` 时 `raw.is_top` 才有值——
**而且只有作品列表接口有效，详情接口的 `is_top` 恒为 0**。
带 raw 会让响应体积约 ×3，所以默认只在"发现新作品 / 标题变化 / 每 20 轮"时带一次。

### 事件类型（`events.kind`）

每次判定产出的东西都落进 `events` 表，`kind` 是下面 15 种之一。**只有 ✅ 的会推送**，
其余静默落库（它们只改变状态、不打扰人）；`SILENT_MODE=true` 时连 ✅ 也不推，但事件照记。

| `kind`              | 含义                                             | 推送 | 抑制窗口                               |
| ------------------- | ------------------------------------------------ | :--: | -------------------------------------- |
| `new_post`          | 新作品（首次见到这条作品）                       |  ✅  | 无                                     |
| `post_removed`      | 作品消失（分级确认之后）                         |  ✅  | 无                                     |
| `all_gone`          | 该账号全部作品同时消失                           |  ✅  | 无                                     |
| `never_seen`        | 从未见过作品且连续 3 轮为空（多半 ID 写错）      |  ✅  | 6 小时 / 账号                          |
| `gap_detected`      | 疑似漏检（两次观测之间有没采到的作品）           |  ✅  | 12 小时 / 账号                         |
| `account_failed`    | 连续失败达 `MAX_CONSECUTIVE_FAILS`               |  ✅  | 5 分钟 / 账号                          |
| `account_recovered` | 从连续失败中恢复                                 |  ✅  | 无                                     |
| `stale_no_update`   | `STALE_FALLBACK_DAYS` 天没有新作品               |  ✅  | 一次性                                 |
| `upstream_degraded` | 上游 429/503/熔断 → 全局闸门关闭                 |  ✅  | 1 小时 / 错误码                        |
| `self_degraded`     | 自身降级（磁盘/状态库超限）                      |  ✅  | 6 小时 —— **目前没有产生点，不会出现** |
| `revived`           | 曾消失的作品又出现（按设计**不算**新作品）       |  ✅  | 6 小时 / 账号                          |
| `title_changed`     | 已知作品的标题变了                               |  ✅  | 1 小时 / 账号                          |
| `scrolled_out`      | 被新作品挤出窗口（静默清理并写 tombstone）       |  —   |                                        |
| `trimmed`           | 超过 `KNOWN_IDS_MAX` 被裁剪（同时写 tombstone）  |  —   |                                        |
| `initialized`       | 首次记录该账号（否则上线新账号会被历史作品刷屏） |  —   |                                        |

查自己库里的事件分布：

```bash
./.venv/bin/python -c "
import sqlite3
for kind, n in sqlite3.connect('data/dywatch.db').execute(
        'SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 2 DESC'):
    print(f'{kind:18} {n}')"
```

`payload_json` 是这条事件的细节，`delivery_json` 记录投递结果（哪些渠道成功/失败）；
面板点账号名看到的"最近事件"就是这张表的最近 12 条。

**投递顺序永远是 `new_post` 第一**，其余按"越需要立刻知道越靠前"排（`alerts.NOTIFY_PRIORITY`），
**不是**按上面这张表的顺序、也不是按判定产出的顺序。理由：一轮里所有事件共用同一条投递通道
（每条间隔 `NOTIFY_GAP`，每渠道 8 秒超时、最多重试 2 次），排在 `new_post` 前面的每一条都可能
把它推迟几十秒，或者撞上渠道限流让它变成"发送失败"的那一条——而新作品是唯一错过就补不回来的东西。

---

## 8. 归档下载（`ARCHIVE_DOWNLOAD_ENABLED`，默认关闭）

监控只负责"看见/没看见"，不碰媒体本身。开了这个开关后，每次检测到**新作品**，
会额外请求 DTK 把这条作品的媒体存一份到它自己的磁盘上——目的是在作品被下架/删除之前，
手上能留一份证据，而不是只留一条"曾经存在过"的记录。

```bash
ARCHIVE_DOWNLOAD_ENABLED=true
ARCHIVE_DOWNLOAD_PIN=false   # 见下面「会不会被自动清掉」
```

### 权限：需要额外申请 `media:write`

比监控本身用的 `douyin:read`/`archive:read` 高一级的写权限，**不建议在第一次部署时就开**，
等确认监控本身跑稳了再考虑。开启后 `dywatch doctor` 会检查这个 scope 在不在，
不在会直接报错而不是等到第一次触发下载才发现。

### 不等下载完成，也不该等

触发下载只是"让 DTK 知道该去存了"——请求一发出去、DTK 确认接受（202）就算这一步完成，
不会等真正的下载在 DTK 那边跑完。而且它**排在通知之后**：先把这一轮该推的消息推出去，
再去操心存档。下载失败（不管什么原因：DTK 那边没媒体可下、存储满了、下载器离线……）
只会记一条日志，**不会**：

- 抛出异常打断这一轮的判定
- 影响这个账号的 `RoundResult`
- 触发监控自己的全局闸门（`GlobalGate`）

换句话说，这个功能坏了，监控本身不会跟着坏——它是旁路，不是链路的一环。

### 节奏、预算与退避

正因为是旁路，它不能"想发就发"：

| 约束                 | 数值                                           | 作用                                                                  |
| -------------------- | ---------------------------------------------- | --------------------------------------------------------------------- |
| 每个请求过全局节奏器 | 与抓取共用 3~8 秒那套                          | 一次来了 12 条新作品也不会连成一串                                    |
| 每轮触发上限         | `ARCHIVE_DOWNLOAD_MAX_PER_ROUND`（默认 10）    | 预算是**按轮算**的，不是按账号算——20 个账号同时出新品也还是这个数     |
| 失败的条目           | 留在内存队列里，下一轮接着发                   | `NEW_POST` 只会出现一次，所以"这一轮没发出去"必须补，否则就是永久丢档 |
| 放弃的条件           | 没有可下载的媒体（`INVALID_PARAM`）或试满 3 次 | 两种情况都有日志；一条永远失败的条目不会一直占着队首                  |
| 容量满的退避         | 用 DTK 给的 `retry_after`（通常 300 秒）       | 窗口内一条请求都不发，不会反复撞容量                                  |
| 配置错的退避         | 1 小时（缺 `media:write`、DTK 没配下载器）     | 这类问题重试没有意义，只记一条 warning                                |

副作用只有一个，且可接受：新作品特别多的一轮，归档会把**这一轮的结束**推迟若干秒
（每条 ≈ 一次节奏器间隔），于是下一轮的覆盖往后挪一点点。通知本身不受影响，它排在归档之前。

队列只在内存里，**进程重启会丢**——重启前积压的那几条就补不回来了（DTK 不知道我们想存哪几条）。

### 会不会被自动清掉：取决于 `ARCHIVE_DOWNLOAD_PIN`

DTK 那边给媒体目录设了 **2GB** 的上限（`media.max_bytes`，在 DTK 的配置里，本工具改不了）。
超过上限时，它会自动删掉**最旧的、且没有锁定**的那些，直到降回上限以内。

- `ARCHIVE_DOWNLOAD_PIN=false`（默认）：存下来的档**不锁定**，所以超过 2GB 之后，最旧的会被
  新下载的顶掉。等于"最近 2GB 的快照"——比完全不存强，但不是永久保留
- `ARCHIVE_DOWNLOAD_PIN=true`：每存一条就**锁定**它，永远不会被自动删。
  **代价**：2GB 被锁满之后，新的下载会一直失败（DTK 会拒绝，报 `QUEUE_FULL`，本工具按
  "失败只记日志"处理），需要你自己去 DTK 那边手动删掉一些腾地方

`dywatch doctor` 会额外显示当前存储用量、距上限还有多少、以及 DTK 下载器是否在线，
建议开启前先看一眼这几个数字心里有底，比出问题了再回头查方便。

---

## 9. 排障

| 现象                                   | 原因与处理                                                                                                                                                                       |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 UNAUTHENTICATED`                  | Key 被拒（**不是权限不足**，那是 403）。确认复制的是完整 `dtk_...`（49 字符）、未吊销                                                                                            |
| `403 FORBIDDEN_SCOPE`                  | 缺 scope。`--doctor` 会直接告诉你缺哪个                                                                                                                                          |
| `503 IDENTITY_POOL_EXHAUSTED`          | DTK 身份池空了。全局闸门会自动退避，看 DTK 控制台的身份池页                                                                                                                      |
| `502 UPSTREAM_RISK_CONTROL`            | 上游风控。该账号记一次失败并退避；持续出现说明 DTK 侧需要处理                                                                                                                    |
| 收不到通知                             | 先 `test-notify`；再确认 `SILENT_MODE=false`、渠道凭据齐全；查 `log/debug/monitor.log`                                                                                           |
| 某个账号一直"无作品"                   | 大概率 ID 写错了：`users.conf` 里换成主页链接重新 `add` 一遍                                                                                                                     |
| 通知里没有"播放"数                     | 正常。抖音的 `play_count` 实测恒为 null，本工具不显示平台没说过的数字                                                                                                            |
| 面板打不开                             | `WEB_ENABLED=true`；局域网访问需 `WEB_HOST=0.0.0.0`（面板无鉴权，请自行加反代）                                                                                                  |
| 面板列表是空的/一直"加载中"            | 列表读 `data/status.json`，它每轮写一次；没跑完一轮就没有快照。先 `dywatch once`                                                                                                 |
| 点账号名详情报"状态库里没有这个账号"   | 账号还没写进状态库（第一次抓取还没成功），跑完一轮再看；报"状态库暂不可读"则是库/权限问题                                                                                        |
| 面板上的数字和 `dywatch status` 不一致 | 面板读的是**快照**（每轮写一次），`status` 读的是同一个文件；差异通常来自另一进程刚跑完一轮                                                                                      |
| 面板"本次运行第 51 轮"而库里几万轮     | **正常，不是丢数据**。那个数是进程内存计数，重启归零；跨重启的累计值在同行的「累计 N 轮」上。手工跑一次 `once` 也会把快照的"本次运行"打回 1                                      |
| `rounds` 表太大/太旧                   | 调 `ROUNDS_KEEP_DAYS`（默认 5 天）；下一轮 `maintenance()` 就会删掉超期行。**文件不会自己变小**，要回收得停服务后 `sqlite3 data/dywatch.db "VACUUM"`                             |
| 想确认配置有没有生效                   | `config-check` 会打印每一项的**来源**（默认值 / `.env` / 环境变量）                                                                                                              |
| 日志每 15 分钟被切一次                 | Armbian 的 `armbian-truncate-logs` 用 `logrotate --force` 强制轮转。确认 `/etc/logrotate.d/dywatch` 已删（重跑一遍 `install.sh` 就会删）                                         |
| 日志一直不轮转                         | `ls /etc/cron.d/dywatch` 在不在、cron 服务活着没（`systemctl status cron`）；手动跑一次 `logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf` 看报错 |

日志：

```
log/info/monitor.log     关键事件 + 每轮汇总（日常看这个）
log/debug/monitor.log    完整细节（排障）
```

### 日志轮转

轮转与压缩交给 logrotate，应用自己不轮转。但**配置不装进 `/etc/logrotate.d`**，而是：

| 东西     | 位置                                | 说明                                              |
| -------- | ----------------------------------- | ------------------------------------------------- |
| 轮转配置 | `/etc/dywatch/logrotate.conf`       | 由 `deploy/logrotate.conf` 生成，改"留多久"改这里 |
| 触发者   | `/etc/cron.d/dywatch`               | 每小时第 17 分钟跑一次 `logrotate`                |
| 状态文件 | `/var/lib/dywatch/logrotate.status` | 与系统 logrotate 完全隔离                         |

节奏：`daily` + `maxsize 10M` → 跨天后第一次运行切一次（= 每天一次），单文件涨过 10M
最多延迟 1 小时切；`rotate 14` + `compress` 保留 14 份。

**为什么不放 `/etc/logrotate.d`**：Armbian 的 `/etc/cron.d/armbian-truncate-logs` 每 15 分钟
跑一次 `/usr/lib/armbian/armbian-truncate-logs`，当 `/var/log` 用量 ≥75% 时执行
`logrotate --force /etc/logrotate.conf`。`--force` 会**跳过"今天是否已轮转过"的判断**，
把 `/etc/logrotate.d` 下所有配置强制轮转一遍——包括本工具的。表现就是 `monitor.log`
每 15 分钟被切一次、14 份归档不到 4 小时就被挤掉，日志几乎没法回看。
自带 cron + 独立 state 之后，别人的 `--force` 再也波及不到这里。

排障用的几条命令：

```bash
ls -la --time-style=full-iso /opt/douyin-monitor/log/info/   # 归档时间戳应是按天，不是 15 分钟
cat /etc/cron.d/dywatch                                      # 看触发节奏
logrotate --debug /etc/dywatch/logrotate.conf                # 干跑一遍，验证配置
sudo logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf  # 手动轮转一次
journalctl -t dywatch-logrotate                              # cron 执行失败时会写这里
```

---

## 10. 架构

```
src/dywatch/
├── cli.py          命令入口（run/once/doctor/status/config-check/add/test-notify）
├── settings.py     SettingSpec 注册表 + 跨项校验 + 来源追溯
├── runtime.py      结构化日志 / 单实例锁 / 装配
├── loop.py         轮次循环（users.conf 热加载、并发、汇总、状态快照）
├── pacer.py        全局请求节奏（3~8 秒，与并发无关）+ 轮末随机等待
├── scheduler.py    全局闸门（429/503/熔断）+ 告警冷却
├── pipeline.py     单账号一轮的编排：取数 → 判定 → 落库 → 通知
├── dtk.py          DTK v5 HTTP 客户端（信封 / 错误码归一化 / wait→202→轮询）
├── users.py        users.conf 解析与校验
├── models.py       值对象（frozen dataclass）
├── diff.py         判定纯函数 —— 零 I/O，可脱网单测
├── state.py        SQLite（一轮一个事务）
├── messages.py     全部文案与格式化
├── render.py       事件 → Markdown / 纯文本 / 短标题
├── alerts.py       运维告警的抑制窗口
├── notifiers/      六个渠道 + 静默空通知器
└── webui.py        只读面板（状态页 + 账号详情）+ /healthz /readyz /metrics
```

**依赖方向单向、无环**：`dtk.py` 是唯一知道 HTTP 的地方，`diff.py` 是唯一知道判定规则的
地方，`notifiers/` 是唯一懂第三方 payload 的地方，三者互不 import；
`pipeline.py` 是唯一的编排者。于是最易错的判定与最琐碎的投递都能脱离网络单测。

设计取舍、每一条规则的来历、以及"为什么不那样做"，都在
[`DESIGN.md`](./DESIGN.md) 里；真实接口契约（含实测数据）在它的第 2 章。
