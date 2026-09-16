# dywatch —— 抖音账号视频监控

监控多个抖音账号，检测**新作品**与**作品消失**，通过钉钉 / 企业微信 / Bark / Server 酱 /
Telegram / 通用 webhook 推送通知。

它建立在 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
**v5** 之上，但只做一件事：**盯着数据变没变**。

```
      抖音  ←—— 签名 / 身份池 / 调度器 / 熔断 / 归档 ——  DTK v5
                                                          ↑
                                        HTTP /api/v1（只读，一个 API Key）
                                                          ↓
                                   dywatch  ←—— 判定 → 通知 → 面板
```

**本工具不自带签名、不碰 Cookie、不碰身份池、不直接访问抖音。** 抓取、风控绕行、
身份轮换全部由 DTK v5 负责；dywatch 对 DTK 是**纯只读**的（不提交任务、不改设置、
不注册 watchlist）。这条边界让它可以只依赖两个 scope、也不会把 DTK 实例搞坏。

**部署形态只有一种：Ubuntu / Linux 服务器上的 systemd 服务。** 不提供容器镜像——
这个工具就是一个进程 + 一个 SQLite 文件 + 一份配置，systemd 已经把它该管的事
（开机自启、崩溃重启、日志归集、权限隔离）都管完了，再加一层编排只会多一处要维护的东西。

---

## 1. 快速开始（Ubuntu）

### 前置：DTK v5 实例 + 一把 API Key

在 DTK 控制台「用户与 API 密钥」创建，**scope 只需要两个**：

| scope          | 用途                                               |
| -------------- | -------------------------------------------------- |
| `douyin:read`  | 读作者作品列表（主抓手）、识别分享链接、读任务结果 |
| `archive:read` | 读本地归档，做删除交叉确认（零身份成本）           |

角色 `viewer` 即可——这些端点只按 scope 鉴权。**Key 的形态是
`dtk_<12位十六进制>_<32位base64url>`（共 49 字符），完整值只在创建时显示一次。**

### 安装

```bash
sudo apt update && sudo apt install -y python3 python3-venv
git clone <本仓库> && cd douyin-monitor
sudo bash deploy/install.sh      # 建 .venv、装 systemd 单元与日志轮转 cron、生成配置模板

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
- **升级**（`git pull` 后重跑同一条命令）：用 `rsync --delete` 同步代码（`.env` /
  `users.conf` / `data/` / `log/` / `.venv/` 一律不碰，旧版本删掉的源码文件会被清理）
  → 重装依赖 → 刷新 systemd 单元与日志轮转 cron（**并删掉老版本留在
  `/etc/logrotate.d/dywatch` 的那份配置**，见下面的日志轮转说明）→ 如果服务正在跑，
  问你要不要立即重启
  （`--yes` 直接重启，不问）。**不加 `--yes` 又不重启的话，新代码不会生效**，脚本会在
  最后提醒你手动 `systemctl restart dywatch`。

其他参数：`--check` 只检测当前是首次安装还是升级、缺什么依赖，不做任何改动；
`sudo bash deploy/install.sh --yes` 可以做到全自动（升级 + 服务在跑就自动重启），
适合写进你自己的升级脚本。

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
python -m dywatch                # 常驻监控（systemd 用这个）
python -m dywatch once           # 只跑一轮后退出：上线前确认配置是否正确
python -m dywatch doctor         # 自检：实例可达 / Key 有效 / scope 够用 / 账号读得出来
python -m dywatch status         # 打印最近一轮的状态快照
python -m dywatch config-check   # 打印全部生效配置与每一项的来源
python -m dywatch add "<主页链接或 sec_user_id>" [昵称]
python -m dywatch test-notify    # 给每个渠道发一条测试消息
```

**先跑 `doctor`**：它把"配错了"和"上游坏了"分开——这两类的处理方式完全不同。

```
dywatch 0.1.0 —— 自检
上游实例: http://192.168.20.4:8000
✓ 凭据有效：username=yunfei role=admin via=api_key
  scopes: archive:read, douyin:read, media:read, tiktok:read
✓ 必需的 scope 齐备
✓ 实例 5.1.0：身份池 douyin active=3 cooling=0 degraded=0
检查 1 个账号（每个消耗 1 个身份）…
  ✓ 示例账号: 18 条（非置顶 15 / 置顶 3，count=15） 2640ms has_more=True
✓ 自检通过
```

---

## 3. 监控列表 `users.conf`

```
# <sec_user_id>|<昵称>
MS4wLjABAAAA4MjTvxSsNOjHfi9kfyRdu0KMKRHA1dPNv1WQQwW0OKY|示例账号
```

`sec_user_id` 以 `MS4wLjABAAAA` 开头，是抖音账号的稳定 ID。**不知道怎么写就直接粘主页链接**：

```bash
python -m dywatch add "https://www.douyin.com/user/MS4wLjABAAAA..."
```

它会调用 DTK 的 `/api/v1/tools/parse-url` 把链接转成 ID（零成本、不消耗身份）。
短链（`v.douyin.com/...`）必须先跳转才能知道目标，本工具会提示你改粘主页链接——
而不是替你花一个身份。

**改完不用重启**：文件按 mtime 热加载，运行中的实例下一轮自动生效。

---

## 4. 配置参考

全部配置放在 `.env`（权限 `0600`）。这张表由 `src/dywatch/settings.py` 的 `SETTINGS`
注册表生成，所以代码与文档不会各说各的。带 ⚙️ 的是**不建议改**的实测值。

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

| 键                                      | 默认                           | 说明                                         |
| --------------------------------------- | ------------------------------ | -------------------------------------------- |
| `WEB_ENABLED` / `WEB_HOST` / `WEB_PORT` | `false` / `127.0.0.1` / `8787` | 只读面板，**无鉴权**，默认只听回环           |
| `LOG_LEVEL`                             | `INFO`                         | 只影响终端；日志文件始终是 info + debug 两份 |
| `MONITOR_HOME`                          | 当前目录                       | 状态库、日志、users.conf 都在这里            |
| `EVENTS_KEEP_DAYS` / `ROUNDS_KEEP_DAYS` | `90` / `30`                    | 事件审计与轮次汇总的保留期                   |

---

## 5. 容量估算

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

---

## 6. 判定规则

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

---

## 7. 排障

| 现象                          | 原因与处理                                                                             |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `401 UNAUTHENTICATED`         | Key 被拒（**不是权限不足**，那是 403）。确认复制的是完整 `dtk_...`（49 字符）、未吊销  |
| `403 FORBIDDEN_SCOPE`         | 缺 scope。`--doctor` 会直接告诉你缺哪个                                                |
| `503 IDENTITY_POOL_EXHAUSTED` | DTK 身份池空了。全局闸门会自动退避，看 DTK 控制台的身份池页                            |
| `502 UPSTREAM_RISK_CONTROL`   | 上游风控。该账号记一次失败并退避；持续出现说明 DTK 侧需要处理                          |
| 收不到通知                    | 先 `test-notify`；再确认 `SILENT_MODE=false`、渠道凭据齐全；查 `log/debug/monitor.log` |
| 某个账号一直"无作品"          | 大概率 ID 写错了：`users.conf` 里换成主页链接重新 `add` 一遍                           |
| 通知里没有"播放"数            | 正常。抖音的 `play_count` 实测恒为 null，本工具不显示平台没说过的数字                  |
| 面板打不开                    | `WEB_ENABLED=true`；局域网访问需 `WEB_HOST=0.0.0.0`（面板无鉴权，请自行加反代）        |
| 想确认配置有没有生效          | `config-check` 会打印每一项的**来源**（默认值 / `.env` / 环境变量）                    |
| 日志每 15 分钟被切一次        | Armbian 的 `armbian-truncate-logs` 用 `logrotate --force` 强制轮转。确认 `/etc/logrotate.d/dywatch` 已删（重跑一遍 `install.sh` 就会删） |
| 日志一直不轮转                | `ls /etc/cron.d/dywatch` 在不在、cron 服务活着没（`systemctl status cron`）；手动跑一次 `logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf` 看报错 |

日志：

```
log/info/monitor.log     关键事件 + 每轮汇总（日常看这个）
log/debug/monitor.log    完整细节（排障）
```

### 日志轮转

轮转与压缩交给 logrotate，应用自己不轮转。但**配置不装进 `/etc/logrotate.d`**，而是：

| 东西 | 位置 | 说明 |
| ---- | ---- | ---- |
| 轮转配置 | `/etc/dywatch/logrotate.conf` | 由 `deploy/logrotate.conf` 生成，改"留多久"改这里 |
| 触发者 | `/etc/cron.d/dywatch` | 每小时第 17 分钟跑一次 `logrotate` |
| 状态文件 | `/var/lib/dywatch/logrotate.status` | 与系统 logrotate 完全隔离 |

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

## 8. 架构

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
└── webui.py        只读面板 + /healthz /readyz /metrics
```

**依赖方向单向、无环**：`dtk.py` 是唯一知道 HTTP 的地方，`diff.py` 是唯一知道判定规则的
地方，`notifiers/` 是唯一懂第三方 payload 的地方，三者互不 import；
`pipeline.py` 是唯一的编排者。于是最易错的判定与最琐碎的投递都能脱离网络单测。

设计取舍、每一条规则的来历、以及"为什么不那样做"，都在
[`DESIGN.md`](./DESIGN.md) 里；真实接口契约（含实测数据）在它的第 2 章。
