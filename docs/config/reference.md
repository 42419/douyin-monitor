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

## 隐藏作品核验

| 键                          | 默认    | 说明                                                                                                    |
| ---------------------------- | ------- | ------------------------------------------------------------------------------------------------------- |
| `HIDDEN_POST_CHECK_ENABLED` | `false` | 游客身份有时看不到作者主页最新发布的作品（抖音的访客限制）。开启后核对作者的发布总数：对不上时才用登录态身份重新核实一遍，分清"只是对访客不可见"还是"作者真删了"。五种触发时机：账号初始化 / 本轮有新作品 / 有作品被确认消失 / 长期无更新兜底 / 低频保底，见[判定规则](/guide/detection-rules#游客视角的隐藏问题) |
| `HIDDEN_CHECK_INTERVAL_MINUTES` | `30` | 低频保底间隔（分钟，0 = 关闭）：基准值超过这么久没核对过就无条件核对一次。**必需**——"新作品从发布起就不可见"和"对访客不可见的作品被删"在游客视角完全不留痕迹，事件触发等不到它们 |
| `PINNED_IDENTITY_ID`        | —       | 登录态身份的 UUID（DTK 控制台 Identities 页面可查）。`HIDDEN_POST_CHECK_ENABLED=true` 时必填，需要 Key 带 `identity:manage` scope，且 owner 账号至少 operator |
| `PIN_DTK_API_KEY`           | —       | 定向核验专用的 Key（留空则复用 `DTK_API_KEY`）。`identity:manage` 能解密查看任意身份的 cookie 明文，建议单独开一把 Key 只给这一处用，泄露的影响面不牵连主监控用的只读凭据 |

## 通知

| 键                                        | 默认                      | 说明                                                                   |
| ------------------------------------------- | -------------------------- | -------------------------------------------------------------------------- |
| `NOTIFY_CHANNELS`                          | `dingtalk`                 | 逗号分隔，可多开：`dingtalk,wecom,bark,serverchan,telegram,webhook`        |
| `NOTIFY_TARGETS`                           | —                          | 通知目标（**可多实例**）：一行一个 `类型 字段=值, 字段=值`，见下面[通知目标语法](#通知目标语法) |
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

### 通知目标语法

一个类型只能配一个实例时，`NOTIFY_CHANNELS` + 那一组单值键就够了；**要给同一个类型配多个**
（两个钉钉群、两个 Telegram 会话）就用 `NOTIFY_TARGETS` —— **一行一个目标**：

```bash
NOTIFY_TARGETS="
dingtalk name=市场部, token=xxx, secret=SECyyy
dingtalk name=运营群, token=zzz
# 想临时停掉某个渠道就注释掉这一行
telegram bot_token=1:AA, chat_id=-100
"
```

`.env` 支持带引号的多行值（记得首尾那对引号），所以读起来跟 `users.conf`（一行一个账号）是一个路子。

- 每行**第一个词是类型**（后面跟不跟 `:` 都行），其余是 `key=value`，字段之间用
  **空格或逗号**分隔（`name=市场部, token=xxx` 和空格版等价，混着写也行）
- 也可以写成一行、用 `;` 分隔目标：`NOTIFY_TARGETS=dingtalk token=a;wecom key=k`
- `#` 出现在一个字段的**开头**就是注释，直到行尾；**注释掉等于停用**，不用删配置
- `key=value` 只按**第一个** `=` 切分，所以值里可以带 `=` 和 `:`（base64、Telegram 的 `123:AA` 都行）
- 值里**不能有空格、逗号或 `;`**（都是分隔符）；要带它们（比如 URL 里就有逗号）就**加引号**：
  `webhook url='https://x/a,b?q=1'`（**内层用单引号**，原因见下面那条）。引号没闭合会直接报错，
  不会把后面的内容一起吞掉
- 值里出现**没加引号**的 `#` 会报错：想写注释就在 `#` 前留一个空格，值里真要 `#`（URL 片段）
  就加引号。这条是刻意的——不报错的话 `secret=SECx#备注` 会被当成完整密钥，加签失败、通知**静默**发不出去
- 同一行里同一个字段写两遍（`token=A, token=B`）会报错，不静默取最后一个
- `name=` 是**实例名**，会出现在投递记录与只读面板里，**必须全局唯一**（投递失败的记录是按名字存的
  字典，同名会互相覆盖）。不写就按类型自动编号：`dingtalk`、`dingtalk-2`、`dingtalk-3`…
- 写错的地方会**逐条**报错（未知类型 / 缺必填字段 / 不认识的字段 / 名字重复），启动时直接拒绝，
  不会带着半份配置跑起来

::: warning 多行值里的引号要换一种
整个 `NOTIFY_TARGETS` 是多行值，外层已经是一对 `"`。里面的值要加引号时**必须用另一种引号**
（`webhook url='https://x/a,b'`）：**内外同一种 `"` 会让 `.env` 解析器读不出整个键**。
真出现这种情况时 dywatch 会直接报错、拒绝启动并提示换引号，**不会**悄悄改用旧写法。
:::

| 类型 | 必填字段 | 选填字段 |
| --- | --- | --- |
| `dingtalk` | `token` | `secret`（加签，以 `SEC` 开头） |
| `wecom` | `key` | — |
| `bark` | `device_key` | `server`（默认 `https://api.day.app`） |
| `serverchan` | `sendkey` | — |
| `telegram` | `bot_token`、`chat_id` | — |
| `webhook` | `url` | — |

::: tip 用 `dywatch config-check` 核对
它会把目标**一行一个**列出来、凭据打码：`- dingtalk:市场部(secret=*** token=***)`。
:::

::: warning 目标越多，一条消息发得越久
渠道是**串行**发送的：每个目标最坏 `2 次 × 8 秒超时 + 1 秒退避`。目标到 4 个时，一条消息最坏要
68 秒才发完，而通知是按优先级排队发的（`新作品` 恒定最前），排在后面的会被推得更晚。
:::

配了 `NOTIFY_TARGETS` 之后，`NOTIFY_CHANNELS` 与 `DINGTALK_TOKEN` 这类单值凭据键会被忽略。

## 面板与其他

| 键                                       | 默认                            | 说明                                                      |
| ------------------------------------------ | --------------------------------- | ------------------------------------------------------------ |
| `WEB_ENABLED` / `WEB_HOST` / `WEB_PORT`   | `false` / `127.0.0.1` / `8787`   | 只读面板（状态页 + 账号详情），**无鉴权**，默认只听回环        |
| `LOG_LEVEL`                                | `INFO`                            | 只影响终端；日志文件始终是 info + debug 两份                   |
| `MONITOR_HOME`                             | 当前目录                          | 状态库、日志、users.conf 都在这里                             |
| `EVENTS_KEEP_DAYS` / `ROUNDS_KEEP_DAYS`   | `30` / `5`                       | 事件审计与轮次汇总的保留期，见下方说明                          |

`rounds` 是唯一会持续长大的表（1 个账号 ≈ 每天 2600 行），轮次周期只有几十秒，
所以默认只留 5 天。详见[容量估算](/config/capacity)。
