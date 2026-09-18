# 配置参考

全部配置放在 `.env`（权限 `0600`）。这张表由 `src/dywatch/settings.py` 的
`SETTINGS` 注册表生成，所以代码与文档不会各说各的。带 ⚙️ 的是**不建议改**的
实测值。

布尔值可以写成 `true/false`、`1/0`、`yes/no`、`on/off`（大小写不敏感，前后
空格无所谓）。**写成别的一律按非法处理**：该项回退到默认值，并在
`dywatch config-check` 的来源列里标成`非法,已回退默认`——不会把 `ture` 这种
手滑当成 `false` 悄悄放过去。

## 上游

| 键                   | 默认                    | 说明                                                                 |
| -------------------- | ----------------------- | ---------------------------------------------------------------------- |
| `DTK_BASE_URL`       | `http://127.0.0.1:8000` | DTK 实例地址                                                          |
| `DTK_API_KEY`        | —                       | **必填**，形态 `dtk_...`（49 字符）                                   |
| `DTK_WAIT`           | `25`                    | `?wait=` 秒数；0 表示走纯异步（202 + 轮询）                            |
| `DTK_TIMEOUT`        | `35`                    | HTTP 超时，必须大于 `DTK_WAIT`                                        |
| `DTK_REFRESH` ⚙️     | `true`                  | 必须为 true，否则命中 DTK 的 300 秒列表缓存，监控粒度静默变成 5 分钟   |
| `INCLUDE_RAW`        | `auto`                  | 置顶标志策略：`auto` / `always` / `never`                              |
| `RAW_REFRESH_ROUNDS` | `20`                    | `auto` 模式下最多隔多少轮取一次 raw                                    |
| `DTK_USER_AGENT`     | `dywatch/0.1`           | 便于在 DTK 侧日志辨认                                                  |

## 请求节奏 ⚙️（沿用旧项目已跑数月的实测值）

| 键                             | 默认        | 说明                                             |
| ------------------------------ | ----------- | -------------------------------------------------- |
| `REQUEST_INTERVAL_MIN` / `MAX` | `3` / `8`   | 全局相邻请求间隔（秒，随机）。**与并发数无关**       |
| `POLL_INTERVAL_MIN` / `MAX`    | `15` / `40` | 轮末随机等待（秒），下限锁死 10                     |
| `MAX_CONCURRENT`               | `5`         | 并发上限——只为不让慢账号拖累别人                    |
| `FETCH_COUNT`                  | `15`        | 单页条数。注意抖音返回 `count + 置顶数` 条          |

不建议随意调小请求间隔——这套数值是在旧项目上跑了数月得出的实测经验值，见
[容量估算](/config/capacity)了解它如何影响覆盖延迟。

## 判定

| 键                                 | 默认        | 说明                              |
| ----------------------------------- | ----------- | ----------------------------------- |
| `DELETE_CONFIRM_ROUNDS`            | `2`         | 普通作品消失确认轮数                |
| `DELETE_CONFIRM_ROUNDS_TOP`        | `3`         | 置顶作品消失确认轮数                |
| `DELETE_CONFIRM_ROUNDS_ALL`        | `3`         | 全部作品同时消失的确认轮数          |
| `KNOWN_IDS_MAX`                    | `50`        | 每账号跟踪的作品上限                |
| `REMOVED_MAX` / `REMOVED_TTL_DAYS` | `200` / `7` | tombstone 上限与存活天数            |
| `EMPTY_ROUNDS_ALERT`               | `3`         | 连续空列表几轮后告警"请核实 ID"      |
| `STALE_FALLBACK_DAYS`              | `14`        | 长期无新作品的一次性兜底提醒         |

详见[判定规则](/guide/detection-rules)。

## 失败与退避

| 键                                       | 默认        | 说明                                    |
| ----------------------------------------- | ----------- | ------------------------------------------ |
| `MAX_CONSECUTIVE_FAILS`                  | `5`         | 连续失败几次告警                          |
| `FAIL_COOLDOWN`                          | `300`       | 同类失败告警冷却（秒）                    |
| `BACKOFF_AFTER` / `BACKOFF_MAX_SECONDS`  | `2` / `600` | 上游 429/503 触发全局闸门后的翻倍退避      |

## 归档

| 键                               | 默认    | 说明                                                                                          |
| --------------------------------- | ------- | ------------------------------------------------------------------------------------------------ |
| `ARCHIVE_ENABLED`                | `true`  | 是否用 DTK 归档做删除交叉确认（零身份成本）                                                      |
| `ARCHIVE_DOWNLOAD_ENABLED`       | `false` | 新作品是否顺手触发 DTK 下载媒体存档，需要 `media:write`，见[归档下载](/guide/archive-download)   |
| `ARCHIVE_DOWNLOAD_PIN`           | `false` | 存下来的媒体是否永久保留：`false`=只留最近 2G（旧的自动删），`true`=都锁定不删（占满后新下载会失败） |
| `ARCHIVE_DOWNLOAD_MAX_PER_ROUND` | `10`    | 每轮最多触发几条归档下载（过全局节奏器，决定旁路最多把一轮拖多久）；超出的排队等下一轮，不丢       |

## 通知

| 键                                        | 默认                      | 说明                                                                   |
| ------------------------------------------- | -------------------------- | -------------------------------------------------------------------------- |
| `NOTIFY_CHANNELS`                          | `dingtalk`                 | 逗号分隔，可多开：`dingtalk,wecom,bark,serverchan,telegram,webhook`        |
| `SILENT_MODE`                              | `false`                    | 跳过全部推送，监控与面板照常                                              |
| `NOTIFY_GAP`                               | `1.0`                      | 相邻两条通知的间隔（秒）                                                  |
| `DINGTALK_TOKEN` / `DINGTALK_SECRET`       | —                          | 钉钉机器人（加签密钥以 `SEC` 开头）                                        |
| `AT_MOBILES`                               | —                          | 告警时 @ 的手机号，逗号分隔                                                |
| `WECOM_WEBHOOK_KEY`                        | —                          | 企业微信群机器人                                                          |
| `BARK_SERVER` / `BARK_DEVICE_KEY`          | `https://api.day.app` / — | Bark                                                                       |
| `SERVERCHAN_SENDKEY`                       | —                          | Server 酱                                                                 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`  | —                          | Telegram                                                                   |
| `WEBHOOK_URL`                              | —                          | 通用 webhook，POST JSON                                                   |

配好之后用 `dywatch test-notify` 逐渠道验证一遍。

## 面板与其他

| 键                                       | 默认                            | 说明                                                      |
| ------------------------------------------ | --------------------------------- | ------------------------------------------------------------ |
| `WEB_ENABLED` / `WEB_HOST` / `WEB_PORT`   | `false` / `127.0.0.1` / `8787`   | 只读面板（状态页 + 账号详情），**无鉴权**，默认只听回环        |
| `LOG_LEVEL`                                | `INFO`                            | 只影响终端；日志文件始终是 info + debug 两份                   |
| `MONITOR_HOME`                             | 当前目录                          | 状态库、日志、users.conf 都在这里                             |
| `EVENTS_KEEP_DAYS` / `ROUNDS_KEEP_DAYS`   | `30` / `5`                       | 事件审计与轮次汇总的保留期，见下方说明                          |

`rounds` 是唯一会持续长大的表（1 个账号 ≈ 每天 2600 行），轮次周期只有几十秒，
所以默认只留 5 天。详见[容量估算](/config/capacity)。
