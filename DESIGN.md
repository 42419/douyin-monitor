# douyin-monitor — 设计稿

基于 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) **v5**
的抖音账号视频监控工具。DTK v5 是被依赖的采集引擎，本工具是它的只读监控客户端。

> 本文件是设计的唯一出处。
> 第 2 章盘点"会用到 v5 的哪些接口"（逐行核实源码，非凭文档推测）；
> 第 3 章是从 v5 学到并要落地的架构原则；
> 第 4 章是本工具自己的架构；
> **第 9 章记录全部已定决策**——其中节奏与删除确认规则**沿用旧项目
> `douyin-monitor-enhance` 已实测跑数月的结论**，不另行发明。

---

## 1. 定位与边界

**DTK v5 负责"把数据抓回来"，本工具负责"盯着它变没变，变了就通知你"。**

本工具是 DTK v5 HTTP API 的只读客户端：不自带签名、不碰 Cookie、不碰代理、不碰身份池、
不直接访问抖音。上游能力（a_bogus 签名、身份池铸造与退役、调度器限速与时序轮换、
接口级熔断、风控分类、归档）全部由 DTK v5 提供。

这条边界是地基。旧工具混乱的一半原因，就是它同时兼职爬虫和监控：自带 `douyin_api` 包
（a_bogus + SM3 纯 Python 复刻）、自带 browserless 取 Cookie、自带 403 自动换 Cookie、
还要兼容外部 v4 接口 —— 这些东西 v5 都做得更好，再实现一遍就是第二个需要跟进的签名算法。

**非目标**：不做评论、粉丝曲线、搜索监控；不实现抖音签名；不 import `dtk` 包；
不直连 DTK 的 Postgres / Redis。

**默认不碰媒体**：下载与存储是 DTK 自己的事。唯一的例外是一个**默认关闭**的可选开关
——`ARCHIVE_DOWNLOAD_ENABLED=true` 时，检测到新作品会顺手请 DTK 存一份媒体（见 D17），
它需要额外申请 `media:write`，属于"使用方显式选择的能力升级"，不是本工具的默认行为。

### 1.1 目标运行环境：Ubuntu / Linux 服务器（首要约束）

只面向 Linux（Ubuntu 20.04+/22.04/24.04 LTS 为主），与 DTK v5 的部署形态一致。
**Windows 不是运行目标**：开发调试可以在 Windows，但不写 Windows 兼容代码、不给 Windows 部署文档。

| 面向 | 具体做法 |
|---|---|
| 进程模型 | 单实例锁用 `fcntl.flock`（**不再写旧工具的 `msvcrt` Windows 分支**） |
| 常驻 | **systemd**（`Type=simple`、`Restart=always`、`RestartSec=10`），这是唯一的部署形态 |
| 信号 | 正确处理 `SIGTERM`（systemd 停止）/ `SIGINT`，等在飞请求收尾后退出 |
| 路径 | 工作目录默认 `/opt/douyin-monitor`，`MONITOR_HOME` 可覆盖 |
| 日志 | 应用只负责分级写入；**轮转压缩交给 logrotate，但由本工具自己的 cron 触发**（不进 `/etc/logrotate.d`，见 D16） |
| 解释器 | 要求 **≥ 3.11**（`requires-python`）。`install.sh` 不盲信 `python3`，而是在 `python3.14 → 3.11` 与 `python3` 里挑版本最高的一个，`PYTHON=/usr/bin/pythonX.Y` 可显式指定；已有 `.venv` 低于 3.11 时直接重建（见第 10 章修正 #8） |
| 时区 | 跟随宿主机（systemd 下自然继承）——通知里的时间必须是本地时间 |
| 权限 | systemd 单元用运行账号（安装者的账号，**不另建专用用户**，见 D15）+ `NoNewPrivileges` + `ProtectSystem=strict` + `ReadWritePaths=` |
| 文件权限 | 状态库 `0600`、日志目录 `0700`、`.env`（含 API Key）`0600` |
| 网络 | 与 DTK 同机时 `DTK_BASE_URL=http://127.0.0.1:8000`（DTK 默认只发布到回环） |
| 部署形态 | **只有 systemd 一种**，不做容器镜像：一个进程 + 一个 SQLite 文件 + 一份配置，systemd 已经管完自启/重启/日志/权限隔离，再包一层只会多一处要维护的东西 |

`sec_user_id` 仍需校验（它进 SQLite 与日志）：非空、无空白、无控制字符、长度上限。
旧工具为 Windows 保留的 `?*:<>|` 检查**不需要**。

---

## 2. 上游接口盘点：会用到 v5 的哪些接口

### 2.1 v5 提供的全部 API 面（按路由前缀）

| 前缀 | 端点 | 本工具是否使用 |
|---|---|---|
| `/api/v1` (content) | `POST /parse`、`POST /tasks/batch`、`GET /{platform}/video`、`/{platform}/user`、`/{platform}/user/posts`、`/likes`、`/reposts`、`/collections`、`/bookmarks`、`/{platform}/collection`、`/collection/posts`、`/mix/posts`、`/{platform}/user/followers`、`/following`、`/{platform}/video/comments`、`/comments/replies` | ✅ 用到 1 个 |
| `/api/v1/archive` | 列表 / `/stats` / `/export` / `/recheck` / `/backfill` / `/collections*` / `/delete` / `/{platform}/{content_id}` | ✅ 用到 2 个（可选增强） |
| `/api/v1/tasks` | `GET /{task_id}`、`DELETE /{task_id}`、`GET /{task_id}/events`(SSE) | ✅ 用到 1 个（兜底轮询） |
| `/api/v1/tools` | `GET /parse-url`、`POST /parse-batch`、`POST /sign`、`POST /decode`、`POST /identity` | ✅ 用到 2 个（URL 识别，零成本） |
| `/api/v1/auth` | `POST /login`、`GET /session`、`POST /logout`、`GET /me`、`POST /password` 等 | ✅ 用到 1 个（`GET /me`，自检） |
| `/api/v1/system/status` | 版本、组件健康、身份池普查、存储 | ✅ 用到（面板展示上游健康） |
| `/healthz`、`/readyz` | 存活 / 就绪探针 | ✅ 用作服务探针的参照 |
| `/api/v1/ios` | iOS 快捷指令配套 | ❌ 不用 |
| `/api/v1/downloads` | 媒体下载索引与文件 | ⚠️ 默认不用；`ARCHIVE_DOWNLOAD_ENABLED=true` 时用到 3 个（见 2.2 ⑨ 与 D17） |
| `/api/v1/admin/*` | identities / proxies / users / api-keys / settings / watchlist / health / logs / maintenance / demo | ❌ 不用（见 2.3） |
| `/api/setup/*` | 首次初始化 | ❌ 不用（一次性运维动作） |

### 2.2 本工具实际使用的接口（逐个说明）

#### ★ ① 主抓手：作者最新一页作品

```
GET /api/v1/douyin/user/posts?sec_user_id=<MS4wLjABAAAA...>&count=10
    &refresh=true&wait=25&include_raw=<按策略>
Header: X-API-Key: <key>
```

- **用途**：每轮监控的唯一必需调用。一页拿到作者最新作品列表，每条都是完整 `Content`
  （标题、类型、时长、四项互动数据、封面、话题、发布时间）——新作品通知需要的字段一次齐活。
- **成本**：1 个身份租约 + 1 次抖音上游请求。
- **`refresh=true` 必须带**：v5 对重复调用有两重"给你同一个答案"的机制——请求合并
  （coalescing）与响应缓存（列表类 `cache.list_ttl` 默认 **300 秒**）。
  不带它，监控粒度会静默变成 5 分钟。
- **`wait=25`**：服务端替客户端长轮询，一次往返拿结果；上限 `api.max_wait_seconds`（默认 30），
  超上限是 400 而不是被悄悄截短。
- **`count` 的实际语义（实测，与直觉不同）**：抖音会把**置顶项额外塞进返回**，所以
  `len(items) = count + 本页置顶数`。实测：`count=5` 返回 **8 条**（3 条置顶）、
  `count=15` 返回 **18 条**（3 条置顶）。默认取 15（旧项目 10），上限 50，见第 9 章 D1-b。
  **因此不能用 `len(items) >= count` 判漏检**（旧项目的判据在这里会误报），见 4.7。
- **返回（实测）**：HTTP 200，**单层 `data`**，形状为 `{items: [...], cursor: "1779365942000", has_more: true}`。
  `meta` 另带 `request_id / cached / duration_ms / cursor{next,has_more} / task_id / endpoint / platform`。
  **同步 200 也会返回 `meta.task_id`**，可用于事后回溯到 DTK 侧的任务记录。
- **实测体积**：`count=15` 不带 raw ≈ **645 KB**（约 36 KB/条，含完整 media 清单）；
  带 `include_raw=true` 时约 **109 KB/条**（约 3 倍）。

#### ★ ② `include_raw`：取回"置顶"标志（D3 依赖它）

```
include_raw=true
```

- **为什么需要**：v5 的 `Content` 模型**没有暴露作品是否置顶**（只有评论有 `is_pinned`），
  而旧项目的删除确认分级（普通 2 轮 / 置顶 3 轮）依赖它。旧项目读的是抖音原始字段
  `item["is_top"]`。
- **怎么拿到**：`include_raw=true` 时，响应里每条 item 携带 `raw` = 平台未加工的完整 aweme 节点
  （`content_from_node` → `raw=detail.raw()`），里面有 `is_top`。
  不带时 DTK 用 `strip_raw` 剥掉（`services/fetch.py:_dump`）。
- **这条链路已离线验证完毕**（不需要真实调用，证据逐条可复查）：

  | 步 | 结论 | 证据 |
  |---|---|---|
  | 1 | 抖音 `aweme_list` 每项确实带 `is_top`（`0`/`1`，置顶项通常排在最前） | v5 自己录制的真实样例 `tests/fixtures/douyin/user_posts_page1.json`：第 12 行 `"is_top": 1`、第 252 行 `"is_top": 0` |
  | 2 | 列表解析逐项把整个 aweme 节点交给内容解析器 | `platforms/douyin/parser.py:393` `content_from_node(item) for item in root.children("aweme_list")` |
  | 3 | 内容解析器把整个节点原样存入 `raw` | `parser.py:369` `raw=detail.raw()` |
  | 4 | `Node.raw()` 就是原始 dict，不裁剪 | `platforms/common.py:304` `return dict(self.data)` |
  | 5 | 只有 `include_raw=true` 才不会被剥掉 | `services/fetch.py:778 _dump()` → 否则 `strip_raw` |

  ⇒ `include_raw=true` 时 `data.items[i].raw["is_top"]` 即置顶标志。
  v4 的建模也印证了字段名（`upstream/v4:crawlers/douyin/web/models.py:272` `is_top: int = 1`）。
  注意 `stick_position` 是**评论**的置顶字段，与作品无关。
  `is_top` 缺失时按"非置顶"处理（不要假定它一定出现）。
- ⚠️ **必须从列表接口取，绝不能用作品详情接口**（这是实测出来的硬结论，一条视频 A/B 对比）：

  | 同一条视频 `7328330582012841251` | `raw.is_top` |
  |---|---|
  | 经 `GET /douyin/user/posts`（列表） | **1** ✅ 与"这是置顶视频"一致 |
  | 经 `GET /douyin/video`（详情） | **0** ❌ 详情接口恒为 0 |

  实测依据：该账号页面前 3 条 `7496063824002403638 / 7494588251178634535 / 7328330582012841251`
  在列表里 `is_top` 全为 `1`（且都排在最前），其余 5 条为 `0`；
  而把其中任一条单独走 `/video` 详情，`is_top` 都是 `0`。
  → **所以 `pipeline` 必须只从 `user/posts` 的 `items[].raw` 读 `is_top`**，
  详情接口的 `raw` 只用于其它字段补充，不看 `is_top`。
  列表里还有 `label_top_text`（实测为 `null`）与 `common_left_top_labels`，本工具不用。
- **代价（实测）**：不带 raw ≈ 36 KB/条，带 raw ≈ **109 KB/条（约 3 倍）**；
  `count=15` 带 raw 约 1.6 MB/轮。
  **因此绝不每轮都带**——本工具用 `INCLUDE_RAW=auto` 策略：
  只在"上一轮发现了新作品 / 某作品标题变化 / 距上次 raw 拉取超过 `RAW_REFRESH_ROUNDS` 轮"时带一次，
  其余轮次用 `include_raw=false`。置顶变化极罕见，滞后十几轮（约 5~10 分钟）完全可接受。
- **注意**：`include_raw` 是**缓存 key 的一部分**（`services/cache.py`），
  带与不带不会互相污染；`refresh=true` 已绕过缓存，所以两种策略都拿的是实时结果。
- 可选值：`auto`（默认）/ `always` / `never`。`never` 时 `is_top` 恒为 false，
  删除确认退化为统一 2 轮（即第 9 章 D3 的降级形态）。

#### ★ ③ 兜底：任务轮询

```
GET /api/v1/tasks/<task_id>
```

- **用途**：`wait` 没能按时完成时，退化为轮询任务态。
- **实测**：不传 `wait` 立即返回 **HTTP 202**，`data = {task_id, state: "queued"}`
  （注意状态是 **`queued`** 而不是 `running`——不要写死字符串，按"非终态"判断）。
  轮询 `GET /api/v1/tasks/{id}` 得到
  `data = {task_id, state, endpoint, created_at, finished_at, data, result_meta}`，
  真正的载荷在 **`data.data`**（第二层），形状与同步 200 时的 `data` 完全一致
  （`{items, cursor, has_more}`）。实测：第 1 次轮询（间隔 3 秒）即为 `done`。
- **成本**：只读本地数据库，几乎为零。
- **为何必须实现**：身份池被占满时 `wait` 到点就返回 202；不实现这条路径，
  高峰期会大面积误判为"抓取失败"。

#### ★ ④ 零成本：分享链接识别（优化录入体验）

```
GET /api/v1/tools/parse-url?url=<抖音主页链接或整段分享文案>
```

- **用途**：加监控账号时用户直接粘主页链接，本工具拿到 `data.resource_id`（即 `sec_user_id`）、
  `data.resource == "user"`。
- **成本**：**不碰网络、不消耗身份**。
- **解决旧工具最大的使用痛点**：旧工具要求用户自己从浏览器开发者工具里抠 `sec_user_id`；现在粘链接即可。
- `POST /api/v1/tools/parse-batch`（一次最多 1000 项、512 KiB 文本）用于批量导入。
- 短链（`v.douyin.com/...`）返回 `needs_expansion: true` 且无 id（须先跳转）。本工具提示用户改粘主页链接，
  而不是替他花一个身份去 `POST /api/v1/parse`。

#### ⑤ 本地归档（删除交叉确认）——**已确认启用**

```
GET /api/v1/archive?platform=douyin&author_uid=<sec_user_id>&limit=50
GET /api/v1/archive/douyin/<content_id>
```

- **用途（实测字段）**：`data` 是 **`{items, cursor, has_more}`**（不是裸数组；
  `cursor` 是不透明 base64，`meta` 里只有 `request_id`）。
  归档 item 的键为：
  `platform, content_id, kind, web_url, title, description, created_at, duration_ms, author,
  music, tags, location, cover_url, classification, availability, first_seen_at, last_seen_at,
  media, stored, collections`。
  **归档里没有 `stats`**（不存互动数据），也**没有 `is_deleted`/`is_private`**，
  状态只由 `availability` 一个字段表达。单条接口 `GET /archive/douyin/{id}` 额外带
  `first_seen_at` / `last_seen_at` / `classification` / `stored`（未下载时 `stored = null`）。
- **实测**：单条归档 `availability = "live"`，返回约 36 KB（含 media 清单）。
- **成本**：**零身份成本**（纯查本地 Postgres）。
- **重要限制**：`availability` 依赖 DTK 自己的 recheck（默认每 6 小时排一次、单批 25 条、
  只查 7 天没查过的），**只能当辅助证据，不能替代 feed 窗口的确认轮数**。
- **权限**：API Key 必须带 `archive:read`。启动自检（`--doctor`）发现缺失时**明确报错**并给出申请指引，
  而不是静默降级——因为这是已确认要用的能力（见第 9 章 D8）。
  确实想关掉时用 `ARCHIVE_ENABLED=false`，此时只走 feed 窗口判定。

#### ⑥ 可选：实例与身份池状态（面板展示上游健康）

```
GET /api/v1/system/status
```

- **用途**：面板显示 DTK 版本、组件健康、**身份池各状态计数**、存储占用。这样"收不到通知"时
  能一眼区分是"作者没发"还是"上游池子空了"。
- **成本**：只读本地；**只需一个有效凭据，不需要 admin scope**。

#### ⑦ 可选：凭据自检（`--doctor`）

```
GET /api/v1/auth/me
```

- **用途**：启动前自检——这把 API Key 是谁的、带哪些 scope、每分钟限额多少。
  比"发了请求才发现 403 FORBIDDEN_SCOPE"好得多。

#### ⑧ 归档检索的过滤参数（实测确认全 11 个）

```
GET /api/v1/archive?platform=douyin&author_uid=<sec_user_id>&availability=deleted&limit=50
```

`platform`、`author_uid`、`tag`、`kind`、`duration_bucket`、`availability`、
`q`（子串搜索，**参数名是 `q` 而不是 `query`**）、`collection`、`stored`、`cursor`、`limit`。
交叉确认实际只用 `author_uid`（+ 可选 `availability`）。

#### ⑨ 可选（唯一的写操作）：新作品的媒体归档下载

```
POST /api/v1/downloads            {"platform":"douyin","content_id":"<id>","skip_existing":true}
POST /api/v1/downloads/<id>/pin   {"pinned":true}
GET  /api/v1/downloads/storage
```

- **用途**：`ARCHIVE_DOWNLOAD_ENABLED=true` 时，检测到新作品请 DTK 把它的媒体存一份到
  DTK 自己的磁盘上（决定见 D17）。`storage` 只被 `doctor` 用来显示用量/上限/下载器是否在线。
- **scope**：前两个要 `media:write`，`storage` 要 `media:read`——比监控用的两个读 scope 高一级。
- **受理即返回**：`202` + `download_id`/`task_id`/`state`/`archived`。真正的下载在 DTK 侧异步跑，
  本工具不轮询、不等它。同一条作品"已在飞 / 已存过"时 DTK 返回 **`200 + reused`**，不是错误。
- **失败语义**（读源码确认）：容量过硬线 → `QUEUE_FULL`（自带 `retry_after`）；
  没配下载器或 `media.enabled=false` → `NOT_CONFIGURED`（501）；
  这条作品没有可下载的媒体 → `INVALID_PARAM`（终局，重试无意义）。
  调用方 `ArchiveTrigger` 按这三类分别处置：退避、退避、丢弃。
- **成本**：`media:write` 之所以独立成 scope，是因为"启动下载会花身份、会占磁盘"。
  实际路径上我们刚看到的新作品通常已被 DTK 归档（读取时就写了 archive），此时直接按存档里的
  镜像计划下载，**不花身份**；只有镜像过期、worker 需要重新解析那条帖子时才花 1 个。
  **磁盘才是硬约束**：`media.max_bytes` 默认 2 GiB，满了按时间顺序淘汰未 pin 的下载。

### 2.3 明确不用的接口及原因

| 接口 | 为什么不用 |
|---|---|
| `POST /api/v1/parse` | 需跟随短链/全量解析，消耗身份；本工具只需 id 与列表 |
| `POST /api/v1/tasks/batch` | 批量任务是为拿内容详情，本工具不需要 |
| `/{platform}/user/likes\|reposts\|collections\|bookmarks` | 收藏/点赞不是"账号发布"语义 |
| `/{platform}/collection*`、`/mix/posts` | 合集是另一类对象，第一版不做 |
| `/video/comments*` | 评论监控不在范围内 |
| `/{platform}/user/followers\|following` | TikTok only，抖音不支持 |
| `/api/v1/archive/recheck`、`/backfill` | 会大量消耗身份池，应由 DTK 侧按自己策略运行；本工具**不主动触发**，只读结果 |
| `/api/v1/archive/delete`、`/collections*` | 写操作，本工具对 DTK **纯只读** |
| `/api/v1/downloads/{id}`、`DELETE /downloads/{id}`、`/downloads/retry`、`/downloads/deduplicate` | 查单条、取消、重试、去重都是**人去 DTK 控制台做的决定**；本工具只发起新下载与 pin（`/downloads/storage` 只被 `doctor` 读一眼） |
| `/api/v1/downloads/{id}/files/{name}` | 本工具不取回媒体字节（取回就该由人来下） |
| `/api/v1/admin/watchlist/*` | 需 `identity:manage` + operator（权限过重），且间隔下限 900 秒；见 D2 |
| `/api/v1/admin/*` 其余 | 身份池/代理/用户/密钥/设置/日志/维护，都是 DTK 控制台自己的职责 |

**关键取舍：本工具对 DTK 默认纯只读。** 不提交任务、不改设置、不触发 recheck；
API Key 只需 `douyin:read` 与 `archive:read` 两个 scope，不会改变 DTK 实例的状态。

**唯一的例外**是那个默认关闭的媒体归档开关（D17）：打开它需要额外授予 `media:write`，
于是"写"这件事从"永远不做"变成"你明确点头才做，且只做一件事——让 DTK 存一份新作品的媒体"。
即便如此，本工具仍然不取消、不重试、不去重、不读回字节，也不删除任何东西。

### 2.4 必须写进代码的契约细节

**认证**：`X-API-Key: <key>`，或 `Authorization: Bearer <key>`（`api/deps.py:32`）。

**统一信封**（`api/envelope.py`）：

```jsonc
// 成功
{ "success": true, "data": {...}, "error": null,
  "meta": { "request_id": "...", "cached": false, "duration_ms": 812 } }
// 失败
{ "success": false, "data": null,
  "error": { "code": "RATE_LIMITED", "message": "本地化文案", "retry_after": 12,
             "details": {...} },
  "meta": { "request_id": "..." } }
```

`code` 是稳定枚举（可分支）；`message` 是本地化文案，**永远不要解析**。

**`?wait=` 的精确语义**（`api/routes/operations.py:370-432`，**已实测确认**）：

- 按时完成 → **HTTP 200**，`data` 就是载荷本身：`body.data.items`（**单层 `data`**）
- 没按时完成 / 不传 `wait` → **HTTP 202**，`body.data = {task_id, state: "queued"}` → 转任务轮询
  （状态值是 `queued`，不是 `running`；按"非终态"判断，别比对字符串）
- 两者都要处理。这是最容易写错的一处。

**错误码 → 本工具动作**（`core/errors.py`；★ 行为实测标注）：

| code | HTTP | 动作 | 实测 |
|---|---|---|---|
| `INVALID_PARAM` | 400 | **不重试**，该账号标记为"ID 无效"并告警 | ★ 畸形/不可能的 id 走这里：`aweme_id=1234567890`（内嵌时间戳不合法）→ 400；`sec_user_id=not-a-sec-uid`（形态非法）→ 400，`details` 里带 `accepts/unknown/endpoint` |
| `RATE_LIMITED` | 429 | 按 `error.retry_after` 全局闸门暂停 | — |
| `IDENTITY_POOL_EXHAUSTED` | 503 | 全局闸门（60s 起，翻倍至 10 分钟）+ 一条运维告警 | — |
| `ENDPOINT_CIRCUIT_OPEN` | 503 | 全局闸门更长（5 分钟起），不推送 | — |
| `UPSTREAM_RISK_CONTROL` | 502 | 该账号记一次失败；连续多次告警"可能被风控" | — |
| `UPSTREAM_CHANGED` / `SIGNING_FAILED` | 502 | 退避 + 一条运维告警（**DTK 该升级的信号**） | — |
| `CONTENT_PRIVATE` | 403 | 作品层面结论：私密 | — |
| `NOT_FOUND` | 404 | 作品层面结论：已删除 | — |
| `QUEUE_FULL` | 503 | 全局闸门短停 | — |
| `UNAUTHENTICATED` / `FORBIDDEN_SCOPE` | 401/403 | **不重试**，直接当配置错误抛给用户（启动自检就该拦住） | ★ Key 错误就是 401 `UNAUTHENTICATED`，不是 403 |

**⚠️ 一个必须写进代码的语义黑洞（实测）**：

> **形态合法但不存在的 `sec_user_id` 返回 HTTP 200 + `{items: [], cursor: null, has_more: false}`**
> —— 和"作者把作品全删了"**完全无法区分**。

实测：`sec_user_id=MS4wLjABAAAAnonexistent_xyz` → 200、`items: []`、`has_more: false`、无 error。

这意味着**"作者 ID 写错"不会被上游报错**，只能靠本工具自己发现。因此：

- 状态上区分 **`never_seen`（从未成功拿到过作品）** 和 **`all_gone`（曾经有、现在全没了）**；
- `never_seen` 且连续 `EMPTY_ROUNDS_ALERT`(3) 轮为空 → 告警**"该账号始终无任何作品，
  请核实 sec_user_id 是否正确"**（而不是报"作品被删光"）；
- 面板上 `never_seen` 的账号单独标色，避免"加了一个错 ID 后一直安静"。

**结构性利好**：v5 用 HTTP 状态 + 错误码把"抓取失败"与"拿到了数据"分得很干净
（400 参数错 / 5xx 上游问题 / 200 有数据）。旧工具最纠结的"空列表是不是 Cookie 过期"，
在 v5 下前一半（Cookie）已由身份池接走、后一半（风控）由 5xx 错误码表达。
**但"空列表"这一支仍要按上面的语义黑洞处理**，且旧项目的空列表处理流程整体保留（见 4.7）。

**`Content` 模型**（`models/content.py`，只取用得到的字段）：

```jsonc
{
  "content_id": "7408915107113127220",   // 字符串，19 位数字超出 JS 安全整数
  "kind": "video",                        // video | image_album | live（枚举实测含 live，需容错）
  "web_url": "https://www.douyin.com/video/...",
  "title": "第一行描述", "description": "完整描述",
  "created_at": "...", "duration_ms": 15300,
  "is_deleted": false, "is_private": false,
  "author": { "uid": "...", "sec_uid": "...", "nickname": "..." },
  "stats": { "play_count": 1, "digg_count": 1, "comment_count": 1,
             "share_count": 1, "collect_count": 1 },
  "media": { "covers": [{"url": "..."}], "images": [...], "video": {...} },
  "music": { "title": "...", "author": "..." },
  "tags": ["话题"], "location": null, "fetched_at": "...",
  "raw": { "is_top": 0, ... }             // 仅 include_raw=true 时存在
}
```

三条硬契约：**缺值是 `null` 而不是 `0`**（"没有播放数"与"播放数为 0"是两件事，
混了就会在通知里显示假的 0）；`content_id` 一律按字符串处理；
`raw` 默认不返回，要它必须显式 `include_raw=true`。

**列表缓存与合并**：`cache.content_ttl=1800`、`cache.author_ttl=900`、`cache.list_ttl=300`；
`refresh=true` 同时关掉缓存与合并，并把这次的新结果写回缓存。

### 2.5 权限与 API Key 配置（逐端点核实的精确清单）

#### 需要的 scope —— 只有两个

| scope | 覆盖的本工具调用 | 必要性 |
|---|---|---|
| **`douyin:read`** | `GET /api/v1/douyin/user/posts`（主抓手）、`GET /api/v1/douyin/video`、`GET /api/v1/tools/parse-url`、`POST /api/v1/tools/parse-batch`、`GET /api/v1/tasks/{id}`（读自己提交的任务结果） | **必需** |
| **`archive:read`** | `GET /api/v1/archive`、`GET /api/v1/archive/{platform}/{content_id}`（删除交叉确认） | **必需**（已确认启用，见第 9 章 D8） |

#### 不需要的 scope —— 明确不要勾

| scope | 为什么不需要 |
|---|---|
| `tiktok:read` | 第一版只监控抖音。**勾上也无副作用**（本工具不会调用 TikTok 接口），想留着以后扩平台可以顺手勾 |
| `media:read` | 那是「看本实例磁盘上存了什么媒体」「取已存文件」，本工具完全不碰媒体 |
| `media:write` | 发起/取消/置顶下载、改合集、删归档作品——本工具对 DTK **纯只读** |
| `archive:export` | 一次性 NDJSON 全量导出——本工具不用，且有它等于能把数据库拉一份走 |
| `identity:manage` | 身份池管理、pin 身份、watchlist、`tools/identity`——权限过重，本工具不注册 watchlist（D2） |
| `admin` | 绝不使用。`admin` 会短路所有 scope 检查，等于把实例交出去 |

#### 两个「不需要任何 scope」的接口

以下两个只要凭据本身有效即可（代码里是 `guard(min_role=DEMO)`，不检查 scope）：

- `GET /api/v1/system/status` —— 面板展示上游健康（身份池普查、组件健康）
- `GET /api/v1/auth/me` —— `--doctor` 自检

#### 角色要求

**viewer 角色就够。** 本工具用到的全部端点都只按 **scope** 鉴权，不按角色；
只有 watchlist 的写操作、pin 身份这类才要求 operator 及以上（本工具不用）。
所以建 Key 时挂在哪个账号上不重要，**scope 才是唯一的限制**。

#### 授权语义（读源码确认，避免多给权限）

`Principal.permits()` 是**交集判断**（`bool(set(needed) & self.scopes)`，`api/deps.py:83`），
即"任一满足"。所以：

- `tools/parse-url` / `parse-batch` 声明的是 `(douyin:read, tiktok:read)` → **给 `douyin:read` 即可**，不必为此加 `tiktok:read`
- `GET /api/v1/tasks/{id}` 要求"创建该任务时所需的 scope"（`operations.scopes_for_task`），
  我们提交的都是 douyin 内容任务 → 同样是 `douyin:read`

#### Key 的形态（这是上一次失败的原因）

```
dtk_<12 位十六进制前缀>_<32 位 base64url>     共 49 字符，必须以 dtk_ 开头
```

`prefix` 用于控制台列表展示，入库校验的是 `sha256(full_key)`（`core/crypto.py:62-75`），
**所以完整 Key 只在创建那一刻显示一次，之后无法找回，只能轮换**。

> **实测教训**：第一把凭据被实例以 `401 UNAUTHENTICATED` 拒绝，
> 原因是形态不符（48 字符、以 `aX_` 开头、不含 `dtk_` 前缀）——
> **是认证失败，不是权限不足**（权限不足会是 `403 FORBIDDEN_SCOPE`）。
> 复制时务必把整串 `dtk_...` 连同前缀一起复制。
>
> 当前已验证可用的 Key 携带 `douyin:read / tiktok:read / archive:read / media:read`，
> 是本设计所需两个 scope 的超集（多出的 `tiktok:read` 与 `media:read` 本工具不会用到，
> 属无害冗余）。

`api.default_rate_limit_per_min` 默认 120；本工具的请求速率由自身 pacer 限制在约 11 次/分钟（见第 6 章），
余量充足，无需为速率额外调整。

### 2.6 线上实例核对结果（2026-09-15 实测）

对 `http://192.168.20.4:8000` 做了实测核对（探针脚本用后即删，未留痕）：

| 核对项 | 结果 |
|---|---|
| 实例版本 | **dtk 5.1.0**，与本机源码 `pyproject.toml` 一致 → 第 2 章读的源码就是该实例在跑的代码 |
| `/healthz`、`/readyz` | 均 200；postgres 13ms、redis 2ms，实例健康 |
| API 路径总数 | 88 个 |
| `user/posts` 参数 | `platform, url, sec_user_id, cursor, count, include_raw, wait, proxy, identity, refresh, explain, lang` ✅ 与设计一致 |
| `video` 参数 | `platform, url, aweme_id, include_raw, wait, proxy, identity, refresh, explain, lang` ✅ |
| `tasks/{task_id}` | GET / DELETE 均在 ✅ |
| `tools/parse-url` | `url` 必填 ✅ |
| `archive` 过滤参数 | 全 11 个，含 `q`（见 2.2 ⑧）✅ |
| `system/status`、`auth/me` | 均在，只需一个有效凭据 ✅ |
| 响应状态码集合 | 内容接口均声明 `200/202/400/401/403/404/429/500/502/503`，且都是同一个 `DtkResponse` 信封 ✅ **`202` 确实是写进契约的一部分**（对应 `wait` 超时） |
| `DtkResponse` 形状 | 必需四键 `success / data / error / meta`；`data` 可为 null；`error` 指向 `DtkError` ✅ |
| **`Content` 模型** | **OpenAPI 里根本不枚举**：内容接口的 200 返回泛型 `DtkResponse`，`data` 是 "The endpoint's own payload"（不透明）。所以 `data.items[]` 的确切字段**只能靠一次真实调用核对，从规格里读不出来** |
| **`is_top` / `stick_position`** | 规格里出现 0 次（v5 不暴露作品置顶）；**运行时已实测：列表接口 `raw.is_top` 有效，详情接口恒为 0**，见 2.2 ② |
| `ContentKind` 枚举 | **`video` / `image_album` / `live`** —— 多出 `live`，渲染必须容错（live 无时长、无视频流） |

**真实载荷核对（已全部完成，用有效 Key 实测）**：

| 核对项 | 实测结果 |
|---|---|
| `auth/me` 字段 | `data.user = {id, username, role, created_at, last_login_at, scopes, via}`；`rate_limit_per_min = null`（用实例默认 120）；`api_key_id` 有值 |
| 本次 Key | `username=yunfei`，`role=admin`，`scopes=[archive:read, douyin:read, media:read, tiktok:read]`，均可用 ✅ |
| `tools/parse-url` | `resource_id` 与输入的 `sec_user_id` 完全一致 ✅（粘主页链接的 UX 成立） |
| `user/posts` 载荷 | HTTP 200，`data = {items, cursor, has_more}`（**单层**）；`meta` 含 `task_id/endpoint/platform/cached/duration_ms/cursor` ✅ |
| `data.items[]` 字段 | 与 `models/content.py` **逐字段一致**：`content_id, kind, title, description, web_url, created_at, duration_ms, is_deleted, is_private, author, stats, media, music, tags, location, fetched_at`（不带 raw 时无 `raw`）✅ |
| 缺值语义 | `stats.play_count = null`（抖音不返回真实播放数，**不是 0**）✅ 证实 `None ≠ 0` 契约 |
| `content_id` 类型 | **字符串**（19 位数字）✅ |
| `media` 结构 | `{video, covers(3), images(0), streams(14)}`；我们只取 `covers[0].url` ✅ |
| `count` 语义 | **返回 `count + 置顶数`**（`count=5`→8 条；`count=15`→18 条）⚠️ 影响漏检判据 |
| **`raw.is_top`** | 列表接口：3 条置顶为 `1`、其余为 `0`（含用户指认的置顶视频 `7328330582012841251` = `1`）；详情接口同一条视频 = `0` ✅ **D9 成立，但只能走列表** |
| 202 → 任务轮询 | 不传 `wait` → 202 + `{task_id, state:"queued"}`；轮询后 `data.data = {items,cursor,has_more}`（**两层 data**）✅ |
| 归档单条 | `availability = "live"`，`stored = null`，约 36 KB ✅ |
| 归档列表 | `data = {items, cursor, has_more}`（**不是裸数组**），item 无 `stats`，状态只在 `availability` ✅ |
| `system/status` | `version=5.1.0`；`pool.douyin.active=2`；`browser_rpc.configured=false`（未启用，符合预期）✅ |
| **不存在但形态合法的作者** | **HTTP 200 + `items: []` + `has_more: false`** ⚠️ 与"作者删光作品"无法区分，见 2.4 的语义黑洞 |
| 畸形 id | 400 `INVALID_PARAM`（不是 404）✅ |

以上全部已合入第 2 章与 4.7。S0 不再有待核对项，可以直接进入实现。

---

## 3. 从 v5 学到并要落地的架构原则

v5 的代码质量主要来自一批**成文且被强制执行的规矩**。本工具直接继承，不自己重新发明：

| # | v5 的做法 | 在本工具的落地 |
|---|---|---|
| 1 | **严格的接缝与单向依赖**：`api` 从不打开 socket，`worker` 不 import `api`；路由只做 authorize/validate/submit/answer | 依赖方向单向、无环（见 4.1）：`dtk.py` 是唯一知道 HTTP 的模块，`diff.py` 是唯一知道判定规则的模块，`notifiers/` 是唯一知道第三方 payload 的模块，三者互不 import |
| 2 | **统一信封 + 稳定错误码**，人类可读文案永不参与判断 | 内部错误模型 `MonitorError(code, details, retry_after)`；`dtk.py` 把四类失败（连接 / 非信封响应 / 信封失败 / 载荷违契约）**归一化**，上层只按 `code` 分支 |
| 3 | **配置即声明式注册表**：`SettingSpec(key, default, scope, type, description)` 集中登记，控制台可改、有描述 | `settings.py` 一张 `SETTINGS` 表 + `.env` 覆盖 + `--config-check` 打印全表与来源；README 配置章节由这张表生成 |
| 4 | **纯函数 + 不可变值对象**：解析、分类、渲染都是纯函数，`frozen=True` 模型 | `diff.py` 输入输出全是 frozen dataclass，事件用 `StrEnum`（照搬 v5 `NotifyEvent` 风格）；判定零 I/O，可脱网单测 |
| 5 | **提交与跟踪分离**：`202 + task_id`，长轮询只是便利，工作始终走队列 | 不必实现任务队列，但借其分离原则：`fetch` / `diff` / `notify` 三段解耦，各自可重试、可单测、可单独关掉 |
| 6 | **去重窗口 + 作用域**：`TriggerSpec(severity, dedup_seconds, scope_fields)`；10 分钟咆哮会让人关掉通知 | 运维类告警（上游故障 / 池耗尽 / 连续失败 / 长期无更新）**整套照搬**；作品级事件靠 known/tombstone 天然去重 |
| 7 | **退避作用在"条目"上而非"任务"上**（watchlist 的教训） | 每账号独立失败计数与告警冷却（沿用旧项目 `MAX_CONSECUTIVE_FAILS=5` + `FAIL_COOLDOWN=300`）；**上游级** 429/503 另有全局闸门——这是 v5 的错误码才给得起的精确信息 |
| 8 | **容量护栏**：磁盘超硬线时停掉"无人等待的写入方"，交互式读取继续；护栏本身不删东西 | 自身状态库/日志超限、或上游持续 503 时，降级为"只记录不推送"并在面板标红，**不崩、不丢状态** |
| 9 | **可选组件不可用是"一种正确配置"而不是故障**（无 browser-rpc 用人工导入） | 未授予 `archive:read` → 关闭交叉确认并说明；`/system/status` 不可达 → 面板隐藏上游健康卡片；都不报错 |
| 10 | **健康探针分离**：`/healthz` 不碰任何依赖（DB 挂了也要活），`/readyz` 才探依赖 | 本工具 `/healthz` 只回进程存活；`/readyz` 才探"能连 DTK + 能写状态库" |
| 11 | **文档化契约**：每个模块头部写"它做什么、以及它拒绝做的两三件事、为什么" | 同规矩，逐模块执行 |
| 12 | **测试分层**：`tests/{unit,integration,replay,contract}`，`live` 打标记不进 CI | `tests/unit`（纯逻辑）、`tests/replay`（录制真实响应 JSON 回放，跑客户端+判定）、`tests/live`（默认跳过） |
| 13 | **一次只跑一个 migrate**：并发初始化的竞态是"第一次启动就自己弄坏自己" | PID 锁 + SQLite 迁移启动时单次执行 + **一轮一个事务**提交（不留半轮状态） |
| 14 | **文案集中在 catalogue，不散落在逻辑里**（v5 全量中英） | 第一版中文即可，但文案集中在 `messages.py`，逻辑里不出现字符串拼接 |

---

## 4. 本工具架构

### 4.1 分层与依赖方向

```
                    ┌──────────────────────────────┐
                    │  cli.py / __main__.py        │  参数、信号、PID 锁、--once/--status/--doctor
                    └──────────────┬───────────────┘
                                   │ 只做组装（wiring）
                    ┌──────────────▼───────────────┐
                    │  runtime.py                  │  config / state / client / notifier / loop
                    └──────────────┬───────────────┘
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
┌───────▼────────┐      ┌──────────▼─────────┐      ┌─────────▼────────┐
│  loop.py       │      │  pipeline.py       │      │  webui.py        │
│  轮次循环       │─────▶│  fetch→diff→persist│      │  只读面板 + 探针  │
│  pacer 节奏     │      │  →notify 编排       │      │                  │
└───────┬────────┘      └──────────┬─────────┘      └─────────┬────────┘
        │                          │                          │
        │            ┌─────────────┼─────────────┐            │
        │     ┌──────▼─────┐ ┌─────▼──────┐ ┌────▼────────┐   │
        │     │ diff.py    │ │ render.py  │ │ notifiers/  │   │
        │     │ ★纯函数     │ │ 事件→文案   │ │ 渠道投递     │   │
        │     └──────┬─────┘ └─────┬──────┘ └────┬────────┘   │
        │            │             │             │            │
        │            │      ┌──────▼─────────────▼──────┐     │
        │            │      │  messages.py（文案集中）    │     │
        │            │      └───────────────────────────┘     │
   ┌────▼────────────▼─────┐                    ┌─────────────▼──────┐
   │  state.py (SQLite)    │                    │  dtk.py ★          │
   │  唯一持久化出口        │                    │  唯一 HTTP 出口      │
   └───────────────────────┘                    └─────────┬──────────┘
                                                          │
                                                     DTK v5 /api/v1
```

**没有类型能突破的边界**（每条都要在代码里成立）：

- `diff.py` / `render.py` / `messages.py` / `models.py` **不 import** `dtk.py`、`state.py`、
  `notifiers/`、任何网络或数据库库。只吃 dataclass、吐 dataclass。
- `notifiers/` **不 import** `dtk.py`、`state.py`；只接收渲染好的 `Message`。
- `state.py` **不 import** `dtk.py`、`notifiers/`。
- `dtk.py` **不 import** 上面任何一个（它不知道"监控"这件事存在）。
- `webui.py` 只读 `state.py` 与一个 runtime 快照，**不触发任何上游请求**。
- 只有 `pipeline.py` 同时知道 `dtk` / `diff` / `state` / `notifiers`——它是唯一的编排者。

收益：判定规则（最易错）与投递格式（最琐碎）都能脱离网络单测；
换通知渠道不需要理解判定；DTK 换版本只改一个文件。

### 4.2 模块职责（含"明确不做什么"）

| 模块 | 职责 | 明确不做 |
|---|---|---|
| `cli.py` | 参数解析、信号、PID 锁、`--once`/`--status`/`--doctor`/`--config-check` | 不含业务逻辑 |
| `runtime.py` | 组装依赖、生命周期（启动迁移、优雅关闭、通知器 aclose） | 不做判定、不做调度决策 |
| `loop.py` | **轮次**循环：加载 users.conf（按 mtime 热加载）→ 并发检查全部账号 → 轮末随机等待 → 输出本轮汇总 | 不发通知、不碰 HTTP |
| `pacer.py` | 全局请求节奏器：相邻两次"请求报到"间隔随机 3~8 秒，与并发数无关。**凡是发往 DTK 的请求都要来报到**——抓取、归档交叉确认、归档下载都不例外 | 不执行请求、不做重试 |
| `scheduler.py` | 全局闸门（上游故障退避）与每账号失败计数/告警冷却决策 | 不执行、不持久化 |
| `pipeline.py` | 单账号一轮：fetch → diff → 单事务持久化 → 发通知 → （可选）归档下载 → 回写运行结果。归档下载由 `ArchiveTrigger` 承担：节奏、每轮预算、内存队列、退避都在它内部 | 不决定"什么时候跑"、不决定"文案长什么样"；`ArchiveTrigger` 的所有失败在这里终结，绝不外抛 |
| `dtk.py` | HTTP 客户端：认证头、信封解包、`wait`→202→任务轮询、错误码归一化、超时与重试；`start_download`/`pin_download`/`download_storage` 不走 `wait`/202 封装（受理即返回），写操作用 `WRITE_TIMEOUT`（10 秒）而不是 `DTK_TIMEOUT` | **不解析业务语义**，不认识"新作品"；不自己决定要不要重试、要不要 pin |
| `models.py` | `Content` 子集、`AuthorState`、`PostState`、`Event` 等 frozen dataclass | 无方法、无 I/O |
| `diff.py` | ★纯函数 `(prev_state, page, now, cfg) -> (events, next_state)` | 无时间副作用（`now` 外部传入） |
| `state.py` | SQLite：schema 迁移、读写、事件审计；**唯一持久化出口** | 不做网络、不做判定 |
| `render.py` | 事件 → 三类文案（markdown / 纯文本 / 短标题） | 不发送 |
| `messages.py` | 全部文案常量与格式化（时间、时长、数字缩写、**更新频率分级**、事件与 tombstone 原因的中文） | 不含逻辑分支 |
| `notifiers/*` | 渠道 payload 构造 + 投递 + 单渠道失败隔离 | 不跨渠道重试、不改写文案 |
| `webui.py` | 只读面板（状态页 + 单账号详情）、`/healthz`、`/readyz`、`/api/state`、`/api/health`、`/api/user/{id}`、`/metrics` | 不做鉴权、**不发上游请求**、不写状态库 |

### 4.3 并发与运行时模型

**asyncio + httpx 单进程**（对齐 v5 的形态，而非旧工具的多线程 + `requests`）+ **沿用旧项目测过的请求节奏**：

- **轮次模型**（不是每账号独立周期）：一圈检查 `users.conf` 里的**全部**账号，轮末随机等待后再开始下一圈。
  旧项目用这个模型跑通并稳定运行数月，直接沿用。
- **两层随机，互不替代**：

| 层 | 参数 | 旧项目值 | 作用 |
|---|---|---|---|
| 账号之间（pacer） | `REQUEST_INTERVAL_MIN/MAX` | **3 ~ 8 秒** | 相邻两次请求**发起**至少间隔随机 3~8 秒；与并发数无关 |
| 轮次之间 | `POLL_INTERVAL_MIN/MAX` | **15 ~ 40 秒** | 一圈跑完后的随机等待（整数秒，`random.randint`） |

- **并发上限** `MAX_CONCURRENT=5`（旧项目 `MAX_CONCURRENT_USERS=5`）：
  并发的意义是"一个慢账号不拖累其它账号"，请求节奏由 pacer 统一压住。
  请求执行在报到锁之外，所以一个请求卡住不会连带卡住排队者（`pacer.py` 的原设计）。
- **全局闸门**（新增能力，来自 v5 错误码）：收到 `RATE_LIMITED` / `IDENTITY_POOL_EXHAUSTED` /
  `ENDPOINT_CIRCUIT_OPEN` / `QUEUE_FULL` 时，暂停后续轮次（`retry_after` 或默认 60s 起、翻倍至 10 分钟）。
  这一层**只会在上游明确报错时触发**，正常情况不改变上面的节奏。
- **SQLite 写只用一条路径**：`pipeline` 在事件循环里同步写（WAL + 单写者）；
  读连接给 `webui` 单独开。
- **优雅退出**：`SIGTERM`/`SIGINT` → 取消本轮剩余账号、等在飞请求收尾（上限 10 秒）、
  关闭 HTTP 客户端、保存状态快照（照搬 v5 `stop_grace_period` 的意图）。
- **单实例锁**：`fcntl.flock(MONITOR_HOME/monitor.pid, LOCK_EX|LOCK_NB)`；拿不到就打印
  "已在运行"并退出。**不实现 Windows 分支**（见 1.1）。
- **日志**：应用只分级写入（`log/info` 与 `log/debug`），**轮转压缩交给 logrotate**
  （`deploy/logrotate.conf` 随包提供，装到 `/etc/dywatch/logrotate.conf`，
  由 `/etc/cron.d/dywatch` 每小时触发）。**不装 `/etc/logrotate.d`**，见 D16。

一轮的形状（对齐旧项目 `run_loop`）：

```
round:
  1. users.conf 按 mtime 热加载（变了就重载并记日志；空配置只警告一次）
  2. 取全局闸门状态；若闸门未开 → 记录并跳到步骤 5
  3. 并发上限 5，对每个账号跑 pipeline：
       a. pacer.wait_for_turn()          # 随机 3~8 秒的全局节奏
       b. dtk.fetch_author_posts(...)    # refresh=true, wait=25, include_raw 按策略
       c. diff → events
       d. 单事务持久化（posts / pending_deletes / tombstones / authors / events）
       e. 发通知（每条之间间隔 NOTIFY_GAP=1s，旧项目同样做法，避免渠道限流）
  4. 汇总：检查 N 个，新作品 M 条，删除 K 条，标题变更 T 条，失败 F 个 —— 写 info 日志
  5. 写 status 快照（供 Web 面板）
  6. --once 则退出；否则 random(15, 40) 秒后进入下一轮
```

### 4.4 配置注册表

照 v5 的 `SettingSpec` 思路，一张表登记全部设置项，`.env` 只做覆盖：

```python
@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str            # POLL_INTERVAL_MIN
    default: Any
    cast: type          # str/int/float/bool/list[str]
    note: str           # 一句话说明 → 生成 README 配置表
```

| 组 | 键 | 默认 | 说明 |
|---|---|---|---|
| 上游 | `DTK_BASE_URL` | `http://127.0.0.1:8000` | DTK v5 地址 |
| | `DTK_API_KEY` | — | 必填 |
| | `DTK_WAIT` | `25` | `?wait=` 秒数，0 表示走纯异步 |
| | `DTK_TIMEOUT` | `35` | HTTP 超时（必须大于 `DTK_WAIT`） |
| | `DTK_REFRESH` | `true` | 必须 true，见 2.2 |
| | `INCLUDE_RAW` | `auto` | `auto` / `always` / `never`，见 2.2 ② |
| | `RAW_REFRESH_ROUNDS` | `20` | `auto` 时最多隔多少轮带一次 `include_raw` |
| | `DTK_USER_AGENT` | `dywatch/0.1` | 便于 DTK 侧日志辨认（与 `settings.py` 默认值一致） |
| 节奏 | `REQUEST_INTERVAL_MIN` | `3` | pacer 下限（秒） |
| | `REQUEST_INTERVAL_MAX` | `8` | pacer 上限（秒） |
| | `POLL_INTERVAL_MIN` | `15` | 轮末随机等待下限（秒） |
| | `POLL_INTERVAL_MAX` | `40` | 轮末随机等待上限（秒） |
| | `MAX_CONCURRENT` | `5` | 并发上限 |
| | `FETCH_COUNT` | `15` | 单页条数（抖音上限 50；旧项目取 10，已确认调至 15） |
| 确认 | `DELETE_CONFIRM_ROUNDS` | `2` | 普通作品消失确认轮数 |
| | `DELETE_CONFIRM_ROUNDS_TOP` | `3` | 置顶作品消失确认轮数 |
| | `DELETE_CONFIRM_ROUNDS_ALL` | `3` | 全部作品同时消失确认轮数 |
| | `KNOWN_IDS_MAX` | `50` | 每账号跟踪的作品上限 |
| | `REMOVED_MAX` | `200` | tombstone 条数上限 |
| | `REMOVED_TTL_DAYS` | `7` | tombstone 存活天数 |
| 失败 | `MAX_CONSECUTIVE_FAILS` | `5` | 连续失败几次告警 |
| | `FAIL_COOLDOWN` | `300` | 同类失败告警冷却（秒） |
| | `BACKOFF_AFTER` | `2` | 连续失败几次后全局闸门开始翻倍 |
| | `BACKOFF_MAX_SECONDS` | `600` | 全局闸门上限 |
| 兜底提醒 | `STALE_FALLBACK_DAYS` | `14` | 长期无新作品的一次性提醒（指纹类检测已删，见 D7） |
| 归档 | `ARCHIVE_ENABLED` | `on` | `on` 走归档交叉确认；`off` 只走 feed 窗口 |
| 通知 | `NOTIFY_CHANNELS` | `dingtalk` | 逗号分隔，可多开 |
| | `SILENT_MODE` | `false` | 跳过推送，监控与面板照常 |
| | `NOTIFY_GAP` | `1.0` | 相邻两条通知的间隔（秒） |
| | 各渠道凭据 | — | `DINGTALK_TOKEN/SECRET`、`WECOM_WEBHOOK_KEY`、`BARK_DEVICE_KEY`、`SERVERCHAN_SENDKEY`、`TELEGRAM_BOT_TOKEN/CHAT_ID`、`WEBHOOK_URL` |
| 面板 | `WEB_ENABLED` / `WEB_HOST` / `WEB_PORT` | `false` / `127.0.0.1` / `8787` | 无鉴权，默认只听回环 |
| 日志 | `LOG_LEVEL` | `INFO` | 只影响终端，不影响日志文件 |

`--config-check` 打印生效值 + 每项来源（默认值 / `.env` / 环境变量），并校验跨项约束：
`DTK_WAIT < DTK_TIMEOUT`、`REQUEST_INTERVAL_MIN ≤ MAX`、`POLL_INTERVAL_MIN ≤ MAX`、
`FETCH_COUNT ≤ 50`、`POLL_INTERVAL_MIN ≥ 10`（低于 10 秒会显著提高风控暴露面，直接拒绝）、
渠道名合法、`DELETE_CONFIRM_ROUNDS ≥ 2`（1 轮会把接口抖动当删除）。

### 4.5 错误模型

```python
class MonitorError(Exception):
    code: str          # 稳定枚举
    details: dict
    retry_after: int | None
```

`dtk.py` 负责把五种失败**归一化**（这是它最重要的职责）：

| 来源 | 归一化后的 code |
|---|---|
| 连接失败 / DNS / 超时 | `DTK_UNREACHABLE` |
| HTTP 非 2xx 且响应不是信封 | `DTK_MALFORMED` |
| 信封 `success:false` | 直接采用 `error.code`（`RATE_LIMITED` 等） |
| 信封成功但载荷违契约（缺 `items`、`content_id` 非字符串…） | `CONTRACT_VIOLATION` |
| `wait` 超时拿到 202，任务轮询也没跑完 | `TASK_TIMEOUT` |

上层只按 `code` 分支，**永远不解析 message**。
`CONTRACT_VIOLATION` 单独一类很重要：它意味着"DTK 升级后响应形状变了"，
应告警提醒升级本工具，而不是当网络抖动反复重试。

### 4.6 状态模型（SQLite，唯一持久化出口）

```sql
PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;

schema_version(version INTEGER);

authors(
  sec_user_id TEXT PRIMARY KEY, nickname TEXT,
  initialized_at TEXT,                   -- 首次见到该账号的时间
  ever_had_posts INTEGER NOT NULL DEFAULT 0,     -- ★ 是否成功见到过作品：
                                                 --   0 = never_seen，1 = 曾有过（区别对待"空列表"，见 2.4）
  consecutive_fails INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, last_error_code TEXT,
  fail_alerted INTEGER NOT NULL DEFAULT 0,      -- 已发过"连续失败"告警
  last_fail_alert_at TEXT,                       -- 告警冷却
  all_gone_rounds INTEGER NOT NULL DEFAULT 0,    -- "全部消失"连续轮数
  empty_rounds INTEGER NOT NULL DEFAULT 0,       -- 连续空列表轮数（含从未有过作品的情况）
  never_seen_alerted INTEGER NOT NULL DEFAULT 0, -- "该账号始终无作品，请核实 ID"只发一次
  newest_seen_created_at TEXT,                   -- ★ 上轮"非置顶"项里最新的发布时间，用于漏检判定
  raw_refresh_round INTEGER NOT NULL DEFAULT 0,  -- 距上次带 include_raw 过了几轮
  last_new_video_at TEXT,                        -- 距上次发布的间隔基准
  last_update_at TEXT,
  stale_alerted INTEGER NOT NULL DEFAULT 0,      -- 14 天兜底提醒只发一次
  last_seen_at TEXT, runs INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

posts(                                    -- 本页窗口内"见过的作品"
  sec_user_id TEXT NOT NULL, content_id TEXT NOT NULL,
  kind TEXT, title TEXT, created_at TEXT,
  is_top INTEGER NOT NULL DEFAULT 0,     -- 来自 raw.is_top，见 2.2 ②
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  absent_rounds INTEGER NOT NULL DEFAULT 0,   -- 连读几轮不在本页（= 旧项目 pending_deletes 的计数）
  absent_is_top INTEGER NOT NULL DEFAULT 0,   -- 计数期间记录的置顶状态（决定阈值 2 还是 3）
  PRIMARY KEY (sec_user_id, content_id));

tombstones(                               -- 消失过的作品，防"窗口回移"重复推送
  sec_user_id TEXT NOT NULL, content_id TEXT NOT NULL,
  removed_at TEXT NOT NULL, reason TEXT,        -- scrolled_out | confirmed | trimmed
  PRIMARY KEY (sec_user_id, content_id));

rounds(                                   -- 每轮汇总。**没有任何读取方**，纯排障用；
                                          -- `id` 的自增号段是面板"累计 M 轮"的来源
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, checked INTEGER, new_count INTEGER, deleted_count INTEGER,
  title_changed INTEGER, failed INTEGER, gate_state TEXT, duration_ms INTEGER);

events(                                   -- 通知审计：能回答"当时到底推了什么"
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, sec_user_id TEXT, content_id TEXT,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  delivery_json TEXT);                    -- 每渠道成功/失败
```

`kind` 的取值与"哪一种会推送"以 `models.EventKind` / `NOTIFY_KINDS` 为准，清单与抑制窗口
抄在 README §6；**`self_degraded` 目前没有任何产生点**（见第 10 章"尚未做"）。

维护任务（每轮顺带，不单独起线程）：tombstone 按上限与 TTL 回收、
`events` 保留 `EVENTS_KEEP_DAYS`（默认 30）、`rounds` 保留 `ROUNDS_KEEP_DAYS`（默认 5）、
`posts` 清理已不在窗口且已 tombstone 的行。

> `rounds` 是**唯一会持续增长**的表（轮次周期只有几十秒：1 个账号 ≈ 每天 2600 轮，
> 30 天就是 7.8 万行）。它没有任何读取方，保留期就是这个表的唯一取舍——默认只给 5 天，
> 嫌短再往上调（调小后下一轮 `maintenance()` 就会删掉超期行，但删行不缩文件，
> 要真正回收磁盘得 `VACUUM`，README 排障表有命令）。

**为什么不用每账号一个 JSON**（旧工具做法）：文件数随账号数增长、跨账号视图要遍历目录、
并发写要自己防。SQLite 是标准库、单文件、有事务、断电安全，直接解决这三个问题。

### 4.7 判定算法（`diff.py` 纯函数，**完整沿用旧项目逻辑**）

```python
def diff(prev: AuthorState, page: Page, now: datetime, cfg: DiffConfig
        ) -> tuple[tuple[Event, ...], AuthorState]: ...
```

一轮的完整流程（与旧项目 `_check_user_inner` 一一对应）：

```
① 抓取失败（MonitorError）
     不打断状态机：consecutive_fails += 1
     ≥ MAX_CONSECUTIVE_FAILS 且距上次告警 ≥ FAIL_COOLDOWN → FailAlert 事件
     return

② HTTP 200 但 items 为空
     empty_rounds += 1（拿到非空时清零）
     ★ 先分两种情形（实测出来的语义黑洞，见 2.4）：
     - ever_had_posts == 0（从未成功见到过作品）→ **never_seen** 分支：
         不写"作品被删光"，也不进 all_gone。
         连续 empty_rounds ≥ EMPTY_ROUNDS_ALERT(3) 且未告警过 →
         产出 NeverSeenAlert 事件："该账号始终没有返回任何作品，
         请核实 sec_user_id 是否正确（也可能是账号作品全清空或设为私密）"，
         置 never_seen_alerted=1（一次性，或按 6 小时窗口去重）
     - ever_had_posts == 1（曾有过作品）→ 走旧项目的"全部同时消失"路径（见 ⑤/⑨b）

③ 归一化本页
     current_map[content_id] = {title, desc, created_at, is_top, cover_url,
                                digg/comment/share/collect_count, duration_ms}
     若本页非空但一条 content_id 都取不到 → 判为畸形列表，记一次失败，return

④ 首次记录（known 为空且 ever_had_posts == 0 且本页非空）
     写入全部作品，置 initialized_at / ever_had_posts=1 / last_update_at，**不推送**
     newest_seen_created_at = 本页**非置顶**项里最新的 created_at
     return {status: "init"}

⑤ 全部消失计数
     if known 非空 and current_ids 为空 → all_gone_rounds += 1
     else → all_gone_rounds = 0        （放在"无变化提前返回"之前，保证回来的轮次也能清零）

⑥ 标题变更 & 置顶状态同步（本页与已知的交集）
     title 变了 → 更新库内标题、记日志、title_changed += 1（**静默，不推送**）
     同时把库内 is_top 更新为本轮值
     （is_top 只在带 include_raw 的轮次才是新值；不带时**不要**把已有值覆盖成 0——
      否则置顶状态会被"没带 raw 的轮次"抹掉，这是本设计里最容易写错的一处）

⑦ 回归静默恢复
     reappeared = (current − known) ∩ tombstones
     → 从 tombstone 摘除、写回 known、记日志，**不推送**（防窗口回移重复通知）

⑧ 无新增无消失 → 只更新 last_update_at（若标题变过）+ 兜底提醒，return

⑨ 删除判定（分级 + 挤出预算，逐条照搬旧项目）
     all_gone = known 非空 and current_ids 为空
     scrolled_out_budget = len(new_ids)      # 被新作品挤出窗口的条数 ≤ 本轮新增数
     遍历 disappeared（按 created_at 从旧到新）：
       a. 非置顶 且 还有预算 → 判为"挤出窗口"：静默清理 + 写 tombstone(reason=scrolled_out)
       b. all_gone → 整批按 all_gone_rounds 统一确认，达 DELETE_CONFIRM_ROUNDS_ALL(3) 即确认，
          未达则把 all_gone_rounds 写成该作品的进度（忽略各作品此前单独攒的进度，
          避免同一次"删光"被拆成多轮通知）
       c. 否则 置顶 → 阈值 DELETE_CONFIRM_ROUNDS_TOP(3)；非置顶 → DELETE_CONFIRM_ROUNDS(2)
          absent_rounds += 1，达阈值 → 确认删除
     确认删除 → PostRemoved 事件（all_gone 时附"全部同时消失"的核实提醒）
              + 写 tombstone(reason=confirmed) + 从 known 与计数中摘除
     未达阈值 → 留在 pending 状态，仅 debug 日志说明"疑似消失，待后续确认"

⑩ 新作品通知 & 漏检判定
     ★ 漏检判据（**已按实测修正，不能沿用旧项目的 `len(new_ids) >= FETCH_COUNT`**）：
         因为抖音返回 `count + 置顶数` 条，命中上限不等于被截断，用旧判据会误报。
         改用时间连续性：
             newest_now = 本页**非置顶**项里最新的 created_at     （排除置顶，置顶时间任意）
             oldest_now = 本页**非置顶**项里最旧的 created_at
             if newest_seen_created_at is not None and oldest_now > newest_seen_created_at:
                 → GapDetected 事件：两次观测之间出现了没见过的作品区间，
                    说明单次发布数超过了窗口，建议调大 FETCH_COUNT
         辅助信号（仅日志，不推送）：本页非置顶项数 == FETCH_COUNT 且全部是新 id
     newest_seen_created_at = max(newest_now, 旧的 newest_seen_created_at)
     stale_alerted 重置为 false            # 用户又发了新视频，兜底提醒重新计时
     gap_days = now − (last_new_video_at or last_update_at or initialized_at)
     按 created_at 升序逐条 NewPost 事件（同轮多条共用同一个 gap_days）
     写库、last_new_video_at = now

⑪ 裁剪已知列表：len(known) > KNOWN_IDS_MAX(50)
     优先淘汰**最旧的非置顶**，写 tombstone(reason=trimmed)

⑫ include_raw 计数器：本轮带了 → raw_refresh_round = 0；否则 += 1
     （下一轮的策略决定：有新作品 / 标题变过 / raw_refresh_round ≥ RAW_REFRESH_ROUNDS(20) → 本轮带）

⑫ 兜底提醒（见下），return {status: ok, new/deleted/title_changed 计数}
```

**长期无更新兜底提醒（旧项目检测 2，保留）**：距上次更新 ≥ `STALE_FALLBACK_DAYS(14)` 天 →
一次性提醒，发过置 `stale_alerted=true`，直到该账号有新作品才重置。原样保留。

> **旧项目检测 1（响应指纹连续 7 天不变 → "疑似 Cookie 过期"）已删除**（第 9 章 D7）。
> 理由：它的前提是"上游悄悄返回旧数据"，而在 v5 下 `refresh=true` 每次都真实打上游，
> 缓存与请求合并都被绕过；Cookie 也由身份池自维护。该检测在 v5 下只会产生误报，
> 其真正想覆盖的场景（上游异常）已由错误码告警更准确地覆盖。

**为什么保留旧项目的窗口回移 / 挤出预算 / 分级确认**：这三条都是被真实误报打出来的补丁
（旧项目 61 个提交里有相当一部分在修这类问题），v5 换了抓取层但**没有改变这些误报的成因**
（页面边界、置顶项位置、作者删新视频导致窗口回移）。删掉它们等于把踩过的坑再踩一遍。

**v5 让哪些旧代码可以删掉**：旧项目 `_check_stale` 里"空列表可能是 Cookie 过期"的反复猜测，
在 v5 下成为确定性事实（5xx + 错误码 vs 200 + 空数组）。因此新增一条更准确的告警
（上游错误码持续出现 → `UpstreamDegraded` 事件），而不再用启发式去猜 Cookie。

**通知内容**（沿用旧项目已验证的高信息密度版式，字段 v5 全都有）：

```
【新作品】示例用户A 发布了新视频
标题：<title>
类型：视频 / 图文（N 张）
发布：2026-09-14 20:03（距上次发布 3 天 4 小时）
时长：00:15
数据：点赞 1.2w · 评论 340 · 收藏 890 · 分享 120
话题：#话题1 #话题2
封面：<covers[0].url>
链接：<web_url>
```

- **缺值不显示该行**（不要显示 0 —— 这就是 v5 坚持 `None ≠ 0` 的用处）。
  **实测：抖音的 `stats.play_count` 恒为 `null`**（平台不返回真实播放数），
  所以抖音通知里**不要输出"播放"这一项**，否则每次都等于空转。
- **`kind` 三态都要渲染**：`video` / `image_album`（N 张图）/ `live`（直播回放：无时长、无视频流，
  只显示标题、时间与链接；`media.video` 为空时不要输出"时长"行）
- 链接用 `web_url`；**不要用媒体 CDN 直链**（几小时过期，v5 明确不持久化它）
- 删除通知：列出被删作品，`all_gone` 时附旧项目那段核实提醒
- 故障与恢复：连续失败达阈值发一条，恢复时发一条"已恢复"（旧项目 `_on_success` 的行为）

### 4.8 通知层

**渠道**：钉钉（加签 HMAC-SHA256）/ 企业微信 markdown / Bark / Server 酱 / Telegram / 通用 webhook。
payload 形状直接参考 v5 `ops/channels.py`（已验证可用的形状，不自己发明）：

```python
# 钉钉：URL 追加 timestamp 与 sign
#   sign = base64(HMAC_SHA256(secret, f"{timestamp}\n{secret}")), url-encoded
#   body = {"msgtype": "markdown", "markdown": {"title": subject, "text": markdown}}
# 企业微信：{"msgtype": "markdown", "markdown": {"content": markdown}}
# Bark：{"title": subject, "body": body, "level": "timeSensitive|active|passive", "group": ...}
# Telegram：{"chat_id", "text", "disable_web_page_preview": true}
# 通用 webhook：{"source": "dywatch", "event", "severity", "subject", "text", "markdown", ...}
```

**纪律**：

- **单渠道失败隔离**：一个渠道挂了不影响其它渠道（返回 `sent[]` / `failed{}`，照 v5 `Delivery` 形状）
- **重试上限 2 次 + 1 秒退避**，超时 8 秒（照 v5：告警不能变成它正在报告的那个故障）
- **相邻通知间隔 `NOTIFY_GAP=1s`**（旧项目同样做法，避免渠道限流）
- **运维类告警带去重窗口**（照 v5 `TriggerSpec`）：

| 事件 | 级别 | 窗口 | 作用域 |
|---|---|---|---|
| 上游不可达 / 畸形响应 / 契约变更 | error | 1 小时 | global |
| 身份池耗尽 | error | 15 分钟 | global |
| 接口熔断 | error | 30 分钟 | global |
| 风控 / 签名失败 | error | 1 小时 | global |
| 账号连续失败（≥5 次） | warning | `FAIL_COOLDOWN`=5 分钟 | sec_user_id |
| 账号已恢复 | info | 不参与去重 | sec_user_id |
| 账号始终无作品（never_seen） | warning | 6 小时 | sec_user_id |
| 长期无新作品（14 天） | info | 一次性 | sec_user_id |
| 可能漏检 | warning | 12 小时 | sec_user_id |
| 自身容量降级 | warning | 6 小时 | global |
| 标题变更 | info | 1 小时 | sec_user_id |
| 作品回归 | info | 6 小时 | sec_user_id |
| 渠道自测（手动触发） | info | 不参与去重 | channel |

- **作品级事件不去重**：一个新作品只产生一次通知（靠 `posts`/`tombstones` 天然保证），
  窗口只用于"会重复为真"的运维状态。`title_changed` 与 `revived` 也在这里列了窗口——它们
  不是"只发生一次"的事件（作者发完再补话题标签、窗口挪动让同一条作品出去又回来），
  不想清楚窗口就会变成刷屏
- **★ 投递顺序：`new_post` 永远第一个发**（`alerts.NOTIFY_PRIORITY`，见 D18）。
  一轮里所有事件共用一条投递通道——每条之间的 `NOTIFY_GAP`、每渠道 8 秒超时 × 2 次重试，
  排在它前面的每一条都可能把它推到几十秒之后，或者撞上渠道限流让它变成"失败的那一条"。
  `new_post` 是唯一**错过就补不回来**的东西（用户不会知道曾经有过这条作品），
  所以排序按"它对人的价值"来，不按判定产出的顺序，也不按事件类型表
- **`SILENT_MODE`**：跳过全部推送，监控/状态/面板照常
- 文案**集中在 `messages.py`**，逻辑里不拼字符串

### 4.9 可观测性与降级

**面板**（`webui.py`，`WEB_ENABLED=true` 时随主循环起一个后台线程）：

| 路由 | 内容 | 备注 |
|---|---|---|
| `GET /` | 状态页：**LED 状态阵列**（24 格，按状态计数用最大余数法量化，小类别保底 1 格）+ 数据条 + 账号列表（含频率气泡）+ 详情弹窗 | 服务端渲染，`<meta refresh>` 30 秒自刷，只有弹窗用 JS |
| `GET /api/user/{sec_user_id}` | 单账号详情：作者行、作品、**已消失作品（tombstone）**、**最近事件** | 读 SQLite（只 `SELECT`）；`400` 非法 ID / `404` 查无此人 / `503` 库读不出来，三者分开 |
| `GET /api/state` | `status.json` 原文 | 给脚本用 |
| `GET /api/health` | 精简小结（账号数 / 失败数 / 快照时间 / 闸门） | 机器可读 |
| `/healthz` `/readyz` | 进程活着 / 依赖探针（状态库可读 + DTK 可达） | 见下 |

- **面板不发任何上游请求、不消耗身份**：列表读每轮写一次的 `status.json`，详情读状态库。
  打开它不会被风控，也不会因为上游抖动而变慢。这也是为什么"上游健康卡片"仍然是**可选未做**：
  那需要面板去调 `GET /api/v1/system/status`，与上面这条性质冲突（见第 10 章）。
- 视觉与交互（LED 阵列 / 数据条 / 列表 / 弹窗）来自旧项目 `douyin-monitor-enhance` 的面板；
  数据源换成 SQLite 之后多出"已消失作品"与"最近事件"，`never_seen`（抖音对**形态合法但不存在**
  的 `sec_user_id` 返回 `200 + items:[]`）单独一色标注——旧面板没有这个概念，
  只有这里能让人一眼看出"加错 ID 了"。
- `/healthz` 不碰任何依赖；`/readyz` 才探依赖（DTK 的 `/healthz`，无需鉴权），
  "凭据对不对"是启动自检该回答的问题，不在每次探针里重答。
- `/api/state` 每账号状态、失败次数、已知作品数、**最新作品发布时间（`newest_post_at` 与
  `hours_since_newest_post`）**、**更新频率分级
  （日更 / 隔天更新 / 周更 / 半月更 / 月更 / 更新较少：排除置顶后按相邻发布时间间隔均值分类，
  与 `messages.frequency_stats()` 同一个实现）**。注意这里刻意**不**提供"距上次检测到变化"
  的小时数：那个数会被一次删除/改名刷新，放在面板上会让人以为账号很活跃（见第 10 章修正 #13）
- **轮次数有两个口径，面板上必须并排显示并标明**（第 10 章修正 #15）：
  「本次运行第 N 轮」= 进程内存计数（`MonitorLoop._rounds`，重启归零）；
  「累计 M 轮」= 状态库里的累计值（`StateStore.rounds_total()`，跨重启、不受 `ROUNDS_KEEP_DAYS`
  裁剪影响，取 `sqlite_sequence` 的自增号段而不是行数）。详情弹窗里的「累计轮次」是第三个口径：
  **这个账号**被检查过多少轮（`authors.runs`）。三者互不相等，少标一个就会被读成"轮数丢了"
- `/metrics`：Prometheus 文本：`dywatch_users`、`dywatch_rounds_total`（**进程级**，重启归零，
  符合 counter 语义）、`dywatch_rounds_recorded_total`（状态库累计）、`dywatch_gate_open`、
  `dywatch_known_posts{author}`、`dywatch_account_failures{author}`、`dywatch_never_seen_accounts`
  （设计稿早期写的 `monitor_*` 前缀未落地，见第 10 章"尚未做"）
- **启动横幅**（沿用旧项目：分组对齐打印关键信息，一眼确认生效配置）：
  PID、推送渠道、面板地址、抓取窗口、**请求节奏（账号间 3~8s / 轮询间隔 15~40s / 并发 5）**、
  确认轮数、日志级别
- **降级矩阵**：

| 条件 | 行为 |
|---|---|
| 上游 429 / 503 / 熔断 | 全局闸门关闭若干分钟，只记录不推送，面板标红 |
| `archive:read` 未授予 | 关闭交叉确认，面板小字说明，不报错 |
| 自身磁盘/状态库超限 | 停推送、保留判定记录，日志 WARN |
| `SILENT_MODE` | 只记录不推送 |

---

## 5. 目录结构

```
douyin-monitor/
├── DESIGN.md                  # 本文件
├── README.md                  # 用法 + 由 SETTINGS 表生成的配置参考（S5 产出）
├── pyproject.toml             # 依赖：httpx + python-dotenv（SQLite / asyncio 用标准库）
├── .env.example
├── users.conf.example
├── .gitignore                # 必须含 .tmp/ 、.env 、data/ 、*.db
├── .tmp/                     # ★ 所有临时/探针/调试产物都放这里，**不落到 projects 根目录**
├── deploy/                   # 部署形态只有 systemd 一种，不做容器镜像
│   ├── dywatch.service            # systemd 单元（硬化选项齐全）
│   ├── logrotate.conf             # 日志轮转（应用不自己轮转；装到 /etc/dywatch/，见 D16）
│   └── install.sh                 # Ubuntu 一键：目录/venv/systemd/日志轮转 cron
├── src/dywatch/
│   ├── __init__.py  __main__.py
│   ├── cli.py                 # 参数、信号、PID 锁、--once/--status/--doctor/--config-check
│   ├── settings.py            # SettingSpec 注册表 + 跨项校验 + 来源追溯
│   ├── runtime.py             # 组装与生命周期
│   ├── loop.py                # 轮次循环（users.conf 热加载 / 汇总 / 轮末随机等待）
│   ├── pacer.py               # 全局请求节奏（3~8 秒随机，独立于并发）
│   ├── scheduler.py           # 全局闸门 + 每账号失败计数与告警冷却
│   ├── pipeline.py            # 单账号一轮：fetch→diff→persist→notify
│   ├── dtk.py                 # ★ HTTP 客户端（信封/错误码/wait→poll/归一化）
│   ├── models.py              # frozen dataclass：Content 子集、AuthorState、PostState、Event
│   ├── diff.py                # ★ 判定纯函数（旧项目逻辑的完整移植）
│   ├── state.py               # SQLite：迁移、读写、rounds/events 审计
│   ├── render.py              # 事件 → markdown / 纯文本 / 短标题
│   ├── messages.py            # 全部文案与格式化
│   ├── alerts.py              # 运维告警：TriggerSpec + 窗口去重
│   ├── notifiers/
│   │   ├── base.py  composite.py  null.py
│   │   └── dingtalk.py wecom.py bark.py serverchan.py telegram.py webhook.py
│   └── webui.py               # 只读面板（状态页 + 账号详情）+ 探针 + metrics
└── tests/
    ├── unit/test_diff.py      # 新/删/分级确认/回归/挤出预算/全部消失/漏检/裁剪（重点）
    ├── unit/test_pacer.py     # 节奏与并发无关性
    ├── unit/test_scheduler.py # 全局闸门、失败计数、告警冷却
    ├── unit/test_render.py    # 缺值不显示 0 等边界
    ├── unit/test_settings.py  # 跨项校验
    ├── replay/                # 录制真实响应 JSON，回放跑 dtk.py + diff.py
    └── live/                  # 默认 skip 标记，手动打真实例
```

### 工作区约定（必须遵守）

- **所有临时产物都放本项目的 `.tmp/`**：探针脚本、抓取下来的 JSON、调试输出、`*.tmp`、日志草稿。
  不在 `D:\desktop\projects` 根目录留任何临时文件——那里只放各个项目目录本身。
- `.tmp/`、`.env`、`data/`、`*.db` 一律进 `.gitignore`；`.env` 里会有 API Key，权限 `0600`。
- 探针/调试脚本**用后即删**（它们可能含凭据），留下的只有结论（写进本文件或 `tests/replay/` 的样例）。

---

## 6. 容量与限速

节奏由 pacer 决定，**与账号数无关**：

```
请求速率上限 ≈ 60 / 平均(3, 8) = 60 / 5.5 ≈ 10.9 次 / 分钟
轮次周期     ≈ 账号数 N × 5.5 秒（pacer 串行化发起节奏）+ random(15, 40) 秒
```

| 账号数 N | 一轮耗时（估） | 请求速率 | 覆盖延迟（单账号多久被查一次） |
|---|---|---|---|
| 5 | ~28 + 27 ≈ 55 秒 | ~11/min | ~1 分钟 |
| 10 | ~55 + 27 ≈ 82 秒 | ~11/min | ~1.5 分钟 |
| 20 | ~110 + 27 ≈ 2.3 分钟 | ~11/min | ~2.5 分钟 |
| 50 | ~275 + 27 ≈ 5 分钟 | ~11/min | ~5 分钟 |
| 100 | ~550 + 27 ≈ 9.6 分钟 | ~11/min | ~10 分钟 |

结论：

- **速率恒定在约 11 次/分钟**，远低于 DTK 的 `api.default_rate_limit_per_min=120`，
  对身份池（`pool.target_size=8`）也很温和——**这正是旧项目长期不被风控的原因**，
  新工具把这条特性当成硬约束守住。
- 真正的约束是**覆盖延迟**：账号数越多，单账号被检查的间隔越长。
  100 个账号时约 10 分钟一次，仍然可用；再往上应拆分实例或调大 pacer 间隔（更慢但更安全）。
- `POLL_INTERVAL_MIN` 的下限在代码里锁到 **10 秒**（配置为更低直接拒绝）：
  轮末等待低于 10 秒意味着整体速率接近 20 次/分钟，风控暴露面明显上升。

---

## 7. 与旧工具的关键差异

| 维度 | douyin-monitor-enhance | douyin-monitor |
|---|---|---|
| 抓取实现 | 自带 `douyin_api`（a_bogus + SM3 复刻）或调 v4 接口 | 只调 DTK v5 `/api/v1`，零签名代码 |
| Cookie | browserless 自动获取 + 403 自动换 | **完全不涉及**，身份池由 DTK 自维护 |
| 失败语义 | 空列表 / 异常 / Cookie 过期靠启发式猜 | v5 给 HTTP 状态 + 稳定错误码；仍保留旧项目的空列表处理流程，但新增更准确的归因告警 |
| 加账号 | 用户自己从开发者工具抠 `sec_user_id` | 粘主页链接即可（`/tools/parse-url`，零成本） |
| 请求节奏 | 账号间 3~8s + 轮询 15~40s + 并发 5 | **完全一致**（D1） |
| 删除确认 | 普通 2 / 置顶 3 / 全空 3 轮 + 挤出预算 + 窗口回移 | **完全一致**（D3），置顶标志改由 `include_raw` 取 |
| 状态存储 | 每账号一个 JSON + 原子写 | SQLite 单文件 + 事务 |
| 运行时 | 多线程 + requests | asyncio + httpx + pacer |
| 配置 | 散落的 `os.environ.get` | `SettingSpec` 注册表 + 跨项校验 + 来源追溯 |
| 运维告警 | 无窗口去重 | 照搬 v5 的 `TriggerSpec` 窗口 + 作用域 |
| 上游故障 | 无法区分 | 全局闸门（429/503/熔断 → 精确退避） |
| 依赖 | requests + python-dotenv + browserless 配额 | httpx + python-dotenv |
| 目标平台 | Windows/Linux 都凑合（含 `msvcrt` 分支） | **只面向 Ubuntu/Linux**（见 1.1） |
| 常驻 | `deploy.sh` 交互式安装器 | systemd 单元（硬化齐全）+ logrotate；**不做容器镜像**（见第 9 章 D14） |
| 规模预估 | ~2500 行 | ~2200 行（含测试），单进程 |

**不搬运的旧设计**：`douyin_api/` 整包、`guest_cookie.py`、`deploy.sh` 交互式安装器、
每账号一个 JSON 的状态存储、**所有 Windows 兼容分支**。

**原样保留的旧设计**（这些是被实测验证过的，不重写）：请求节奏双层随机、
删除确认分级 + 挤出预算 + 窗口回移 tombstone、已知列表上限与裁剪优先级、
`SILENT_MODE`、`users.conf` 热加载、info/debug 双日志分级、`/api/health`、
多渠道广播互不影响、富文本新作通知版式、连续失败告警 + 恢复通知、
长期无更新一次性兜底提醒、启动横幅、逐条通知间隔 1 秒、更新频率分级统计。

---

## 8. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| **S0** 骨架与契约 | `settings.py` + `models.py` + `dtk.py` + `cli.py --doctor/--config-check` | **✅ 契约部分已完成**（2.6 的真实载荷核对全部通过，`is_top`、202 两层 `data`、归档结构、错误码均实测钉死）。剩下的代码骨架按本文件实现即可 |
| **S1** 判定与状态 | `diff.py`（旧逻辑完整移植）+ `state.py` + 迁移 | 单测覆盖：初始化 / 新作品 / 普通 2 轮 / 置顶 3 轮 / 全空 3 轮 / 挤出预算 / 窗口回移静默恢复 / 标题变更静默 / 裁剪 / 漏检 / 畸形列表；`now` 由参数注入 |
| **S2** 循环与节奏 | `loop.py` + `pacer.py` + `scheduler.py` + `pipeline.py` + 日志 | 5 账号连续跑 30 分钟：实测速率 ≈ 11 次/分钟、节奏与并发无关；`kill -9` 后重启不丢状态、不重复推送 |
| **S3** 通知层 | `render.py` + `messages.py` + `notifiers/*` + `alerts.py` | 钉钉 + 通用 webhook 首发，其余按模板补齐；单渠道挂掉其它照常；`SILENT_MODE` 可用；窗口去重有单测 |
| **S4** 面板与降级 | `webui.py` + 探针 + metrics + 降级矩阵 | 面板显示每账号状态、更新频率与上游健康；`/readyz` 在 DTK 不可达时正确报不就绪 |
| **S5** 交付 | `deploy/`（systemd 单元 + logrotate + `install.sh`）+ README（由 SETTINGS 生成配置表、容量算例、排障） | Ubuntu 上 `install.sh` 装成 systemd 服务后 `systemctl restart` 正常。README 覆盖 Key 权限、容量算例、常见故障 |

---

## 9. 决策点状态

| # | 问题 | 结论 | 依据 |
|---|---|---|---|
| D1 | 监控节奏 | **沿用旧项目：账号间 3~8 秒随机、轮末 15~40 秒随机、并发 5** | 旧项目实测跑数月未被风控，不另行发明 |
| D2 | 是否注册 DTK watchlist | **不注册** | 需 `identity:manage` + operator，权限过重；间隔下限 900s 与本工具节奏不匹配 |
| D3 | 删除确认规则 | **沿用旧项目：普通 2 轮 / 置顶 3 轮 / 全部消失 3 轮，含挤出预算与窗口回移 tombstone** | 同上；置顶标志经 `include_raw` 取 `raw.is_top` 补齐 v5 未暴露的字段 |
| D4 | 标题/数据变化是否推送 | **静默更新，不推送** | 与旧项目一致 |
| D5 | 面板鉴权 | 无鉴权，默认只听 `127.0.0.1` | 暴露公网需自加反代鉴权 |
| D6 | 第一版通知渠道 | 钉钉 + 通用 webhook 先做，其余按模板补齐 | 缩短 S3 |
| D1-b | `FETCH_COUNT` 默认值 | **15**（旧项目 10，已确认调大），上限 50 | 降低"作者一次发很多条"时的漏检概率；代价是每轮响应体积增加约一半 |
| D7 | 旧项目"响应指纹连续 7 天不变"这条过时检测 | **删除**，只保留 14 天长期无更新的一次性兜底提醒 | 它的前提（上游悄悄返回旧数据）在 v5 下不成立：`refresh=true` 每次都真打上游，Cookie 由身份池自维护。保留只会误报，其真正想覆盖的场景由错误码告警更准确地覆盖 |
| D8 | 归档交叉确认 | **启用**，API Key 必须带 `archive:read` | 零身份成本即可拿到 `availability` 作第二信源；缺 scope 时启动自检直接报错并给申请指引 |
| D9 | 置顶标志来源 | **`INCLUDE_RAW=auto`**：仅新作品 / 标题变更 / 每 20 轮带一次 `include_raw` 取 `raw.is_top` 入库。**且只从 `user/posts` 列表取，绝不用 `/video` 详情** | v5 归一化模型不暴露置顶；实测 raw 让体积约 ×3（109 KB/条 vs 36 KB/条），置顶变化极罕见。**实测：同一视频列表 `is_top=1`、详情 `is_top=0`** —— 详情接口不带置顶语义 |
| D10 | 目标 DTK 实例与凭据 | 实例 `http://192.168.20.4:8000` = **dtk 5.1.0**（与源码同版本）且健康；**新 Key 已验证可用**（`douyin:read`/`tiktok:read`/`archive:read`/`media:read`），2.6 的真实载荷核对全部完成 | 凭据形态必须为 `dtk_<12hex>_<32b64>`（49 字符）。Key 只写进 `.env`（gitignore），不进文档、不进提交 |
| D11 | `count` 的实际语义 | **返回 `count + 本页置顶数` 条**（实测 `count=5`→8 条、`count=15`→18 条）。`FETCH_COUNT` 只控制"非置顶窗口"大小 | 抖音会把置顶项额外塞进返回；这直接推翻了旧项目的漏检判据 |
| D12 | 漏检判据 | **改用时间连续性**：本页**非置顶**项中最旧的 `created_at` > 上轮非置顶项中最新的 `created_at` → 判为漏检。**不再用 `len(new_ids) >= FETCH_COUNT`**（实测会误报） | 旧判据的前提"命中 count 就是被截断"在抖音上不成立；时间连续性不依赖条数语义 |
| D13 | "作者 ID 写错"怎么发现（**新增，由实测暴露**） | 新增 `never_seen` 状态：从未成功见到过作品 + 连续 3 轮空 → 告警"该账号始终无作品，请核实 sec_user_id"，与"作品被删光"（`all_gone`）严格区分 | **实测：形态合法但不存在的 sec_user_id 返回 200 + `items:[]`**，上游永远不会报错；这是唯一的防线 |
| D14 | 部署形态 | **只做 systemd，不做容器镜像** | 一个进程 + 一个 SQLite 文件 + 一份配置；systemd 已经管完开机自启、崩溃重启、日志归集与权限隔离，再包一层编排只会多一处要长期维护的东西 |
| D15 | 是否创建专用系统用户 | **不创建**。服务以执行安装的账号身份运行（`sudo` 时取 `SUDO_USER`），单元里的 `User=` 由 `install.sh` 填入 | 这是给自己用的单机工具，专用账号带来的只有 `sudo -u` 的摩擦；真正的权限边界由单元的 `ProtectSystem=strict` + `ReadWritePaths=工作目录` 给出。代价是账号本身是登录账号，所以单元的其余加固项全部保留 |
| D16 | 日志轮转配置放在哪里（**上线后由实测暴露**） | **不装 `/etc/logrotate.d`**。配置装到 `/etc/dywatch/logrotate.conf`，由 `/etc/cron.d/dywatch` 每小时触发，state 文件独立放 `/var/lib/dywatch/logrotate.status`；`install.sh` 升级时删掉老版本留下的 `/etc/logrotate.d/dywatch` | 实测：Armbian 的 `/etc/cron.d/armbian-truncate-logs` 每 15 分钟跑 `armbian-truncate-logs`，`/var/log` 用量 ≥75% 时执行 `logrotate --force /etc/logrotate.conf`——`--force` **跳过日期判断**，把 `/etc/logrotate.d` 下所有配置强制轮转，`monitor.log` 于是每 15 分钟被切一次、`rotate 14` 的归档不到 4 小时就被挤掉。自己的 cron + 独立 state 让轮转节奏只由本项目决定，别人的 `--force` 不再波及 |
| D17 | 是否触发 DTK 的媒体下载归档 | **做，但默认关（`ARCHIVE_DOWNLOAD_ENABLED=false`）**，需要额外的 `media:write`。落地形状 = `ArchiveTrigger`：只等 DTK 受理（202）；**每个请求过 pacer**；**每轮预算**（`ARCHIVE_DOWNLOAD_MAX_PER_ROUND`，默认 10）按轮算不按账号算；失败/退避/超预算的条目进**内存队列**下轮补发；容量满与配置错进**退避窗口**；调用点排在通知**之后**；任何失败只记日志 | ①监控"只读"是刻意的设计，写操作与权限升级应当是使用方主动的决定，不该默认打开。②DTK 的 `media.max_bytes` 默认 2GiB，满了按时间淘汰未 pin 的下载——`ARCHIVE_DOWNLOAD_PIN` 因此也默认关：全 pin 满之后新下载会持续 `QUEUE_FULL`，该永久保留哪些要人判断。③`NEW_POST` **只会出现一次**（`diff` 保证），所以旁路不能"失败了就算了"——那等于永久丢档，必须有队列；反过来队列只在内存里，进程重启会丢，这是明知而接受的代价（重启前那几条本来也无从补，DTK 不知道我们想存哪几条）。④旁路的"不影响主流程"必须包含**耗时**：第一版把它串行放在通知之前且不过 pacer，DTK 一慢，这一轮的通知就跟着卡住（见第 10 章修正 #11） |
| D18 | 通知的投递顺序与"哪些事件会推送" | **投递按 `alerts.NOTIFY_PRIORITY` 排序，`new_post` 恒定第一个发**；`revived` / `title_changed` 从"只落库"改为**也推送**，各自带窗口（6 小时 / 1 小时，按账号） | ①一轮里所有事件共用同一条投递通道：每条之间 `NOTIFY_GAP`、每渠道 8 秒超时、最多重试 2 次，再加上渠道限流——排在 `new_post` 前面的每一条都在赌"会不会漏掉一条作品"，而它是唯一错过就补不回来的事件（用户不会知道曾经有过）。②`revived`/`title_changed` 静默是旧项目留下的做法，但"作品回归""标题被改"都是作者真实做过的动作，只躺在库里没人会翻；担心刷屏的部分用窗口解决（作者发完再补话题标签是常态 → 1 小时；窗口挪动让同一条作品反复进出 → 6 小时），而不是靠不推 |

---

## 10. 实现状态（2026-09-15）

### 已完成

| 阶段 | 状态 | 说明 |
|---|---|---|
| **S0** 骨架与契约 | ✅ | `dtk.py` 与真实实例跑通；`--doctor` 实测输出 **"18 条（非置顶 15 / 置顶 3，count=15）"**——`count + 置顶数` 与 `raw.is_top` 两条实测结论在真实数据上复现 |
| **S1** 判定与状态 | ✅ | `diff.py` 纯函数 + `state.py` SQLite；**112 个测试通过** |
| **S2** 循环与节奏 | ✅ | `loop.py` + `pacer.py` + `scheduler.py` + `pipeline.py`；`once` 从空库跑两轮：第一轮 `新增初始化 1 个`，第二轮 `均无变化`，无重复推送 |
| **S3** 通知层 | ✅ | 六个渠道 + 静默空通知器 + `alerts.py` 抑制窗口；钉钉/企微/Bark/Server 酱/Telegram/webhook 的 payload 形状均有单测 |
| **S4** 面板与探针 | ✅ | 只读面板 + `/healthz` `/readyz` `/metrics`；`status` 命令输出账号表。**面板后从旧项目整体移植过一次**（LED 阵列 / 数据条 / 详情弹窗，见修正 #9） |
| **S5** 交付 | ✅ | systemd 单元（加固齐全）+ 日志轮转（独立 cron，D16）+ `install.sh` + README。**不做容器镜像**（D14） |

代码规模：`src/dywatch` **18 个模块**，测试 **149 项**（unit + replay）。

### 实现过程中对设计的修正（都记在这里，免得以后当成 bug）

| # | 原设计 | 改成 | 为什么 |
|---|---|---|---|
| 1 | 通知里"距上次发布 N 天"用**我们上次观测到更新**的时间算 | 用**两条作品的发布时间**之差算 | 后者才是"距上次发布"的字面意思；监控停机一段时间后，前者会给出一个偏小的数字（显示"1 天"而实际隔了 10 天）。只在拿不到发布时间时才退回观测时间 |
| 2 | 归档交叉确认：`deleted` 少等一轮、`live` 多等一轮 | **只保留加速方向**：`deleted` 少等一轮；`live` 不改变阈值 | 归档的可用性由 DTK 的 recheck 更新（默认 6 小时一批、只查 7 天没查过的），"live"可能只是陈旧。拿陈旧的正向记录去否决刚刚观察到的消失，会把真实删除压成"再等等"，而那个等没有终点 |
| 3 | `/readyz` 探 `GET /api/v1/auth/me` | 探**无需鉴权的** `/healthz` | 两者都证明"连得上"，但探针不该需要凭据、也不该消耗任何东西；"凭据对不对"是启动自检该回答的**配置问题**，不该每 15 秒重答一遍 |
| 4 | `fmt_count` 有 `k` 一级（9999 → "10k"） | 去掉 `k`，只用**万/亿** | 中文阅读习惯里没有 k；而且 9999 显示成 "10k" 是把四位数说成五位数 |
| 5 | 认证头挂在 `httpx.AsyncClient` 的默认头上 | **每次请求显式带上** | 注入一个 client（测试、将来复用连接池）时默认头不会跟着来，而"少一个头"的表现是 401——读起来像凭据错了，不像少写了一行 |
| 6 | 轮次汇总只报新/删/标题变更/失败 | 增加 **"新增初始化 N 个"** | 上线新账号时第一轮必然是初始化，不报出来会让人以为"什么都没发生" |
| 7 | 轮转配置装进 `/etc/logrotate.d/`（由系统 logrotate 管） | 改由 `/etc/cron.d/dywatch` 调 `/etc/dywatch/logrotate.conf`，state 文件独立（见 D16） | 在 Armbian 上 `/etc/logrotate.d` 里的配置会被 `armbian-truncate-logs` 的 `logrotate --force` 每 15 分钟强制轮转一次（归档时间戳 `19:30 / 19:15 / 19:00…` 就是这么来的），`daily` 形同虚设、归档被迅速挤掉 |
| 8 | `install.sh` 直接 `python3 -m venv` | 先在 `python3.14 → 3.11` 与 `python3` 中挑版本最高的（`PYTHON=` 可覆盖），已有 `.venv` 低于 3.11 时重建 | 项目要求 ≥3.11，但 Ubuntu 22.04 自带的 `python3` 是 3.10，要装到 `pip install .` 那一步才失败、报错还看不出是版本问题；实际部署时是在服务器上手工把脚本里三处 `python3` 改成 `python3.14` 才过去的——"每台机器打一次补丁"该由脚本自己解决 |
| 9 | 面板"每个账号现在怎么样"（一张表 + 5 个数字） | **移植旧项目的面板**：LED 状态阵列 + 数据条 + 账号列表 + 详情弹窗；详情读 SQLite 后多出"已消失作品"与"最近事件"，`never_seen` 单独一色，闸门关闭时页面顶部出红色警示条 | 一屏的表格说不清"它是变了还是没变"：账号数一多就看不出谁在失败；旧面板那套读数式布局是跑过数月的成品。适配点是数据源——旧项目每账号一个 JSON 文件，这里换成 `authors`/`posts`/`tombstones`/`events` 四张表，于是"已消失"和"最近事件"本来就有落库，只是旧面板没有地方显示 |
| 10 | 面板详情直接读每账号状态文件 | 改读状态库，并且**读不出来 ≠ 查无此人**：`400` 非法 ID、`404` 库里没这个账号、`503` 库打不开，前端分别显示 | 旧项目里两者都是"查不到"，**看的人会以为是配置问题去翻 users.conf**，而实际是库的问题 |
| 11 | 归档下载"检测到新作品就 for 循环 await 发出去" | 收成一个 `ArchiveTrigger`：每请求过 pacer、每轮预算、失败进内存队列下轮补、容量/配置类进退避窗口，调用点挪到**通知之后** | 第一版的形状有两个真问题：①一轮 15 条新作品就是 15~30 个**突发**写请求（绕过了节奏器，与 D17 想遵守的"约 11 次/分钟"自相矛盾）；②串行 await 排在**通知之前**，DTK 慢一点这一轮的新作品通知就跟着卡住——旁路不影响主流程必须包括**耗时**，不只是异常。顺带把"失败只记日志"补成"连非 `MonitorError` 的意外也兜住（`CancelledError` 除外）" |
| 12 | §1 非目标与 §2.1/§2.3 写着"不下载媒体 / 不碰媒体" | 改成"**默认**不碰媒体，唯一例外是默认关闭的归档下载开关"（D17），并在 §2.2 补上 ⑨ 这条写操作的接口说明 | D17 决定加了一条可选的写路径，但那三处表述没跟着改，设计稿会自己打自己——以后有人照着 §2.3 断定"本工具永不写"，就会把 D17 的功能当成 bug 删掉 |
| 13 | 面板列表用 `hours_since_update`（距上次检测到变化）当"多久没更新" | 改用 `hours_since_newest_post`（最新一条作品的发布时间），`dywatch status` 同一口径；快照不再提供前者 | `last_update_at` 在**任何变化**时都会刷新（删掉一条、改个标题），于是"距上次更新 1 小时"和"20 天没发新作品"可以同时成立——列表上显示前者，等于告诉人"这账号挺活跃"。顺带三处交互：详情里已知作品改为**置顶排最前**（作者自己摆在最上面的那几条，往往就是点开想看的东西）、弹窗取消内部滚动（改由整层滚动，手机上不再两层滚动打架）、小屏下状态与账号名分两行且整行可点 |
| 14 | `revived` / `title_changed` 只落库不推送，通知按 `diff` 的输出顺序投递 | 两个事件进 `NOTIFY_KINDS` 并各自带窗口（1 小时 / 6 小时，按账号）；投递改为按 `alerts.NOTIFY_PRIORITY` 排序，`new_post` 恒定第一 | 旧做法两头都不对：①"作品回归""标题被改"是作者真实做过的动作，只躺在库里没人会去看——怕刷屏的部分交给窗口就行，不是靠不推；②按 `diff` 的输出顺序投递，等于让"标题变更"这类可以先等的事件排在 `new_post` 前面，而它们共用一条投递通道（间隔 + 8 秒超时 × 2 次重试 + 渠道限流），排前面的每一条都在拿"会不会漏掉一条作品"去赌 |
| 15 | 面板顶部只显示一个轮次数（`status.json` 的 `rounds`，即进程内存计数） | 改成并排两个：「**本次运行**第 N 轮」+「**累计** M 轮」；`status.json` 增 `rounds_total`（`StateStore.rounds_total()`，取 `sqlite_sequence` 号段）；`/api/health`、`/metrics`（新增 `dywatch_rounds_recorded_total`）、`dywatch status` 同步 | 上线后被真实读成"轮数丢了"：库里 `rounds` 表几万行（**跨重启累计**），面板却写"第 51 轮"（**本次进程**），两个数口径完全不同却摆在同一处、且没标。修的时候注意三点：①累计值不能用 `COUNT(*)`（会被 `ROUNDS_KEEP_DAYS` 裁剪，数会变小），也不能用 `MAX(id)`（旧行被删光后是 NULL，会掉回 0），用 `AUTOINCREMENT` 的号段计数器才是单调的；②`/metrics` 那个**保持进程级**（counter 重启归零符合 Prometheus 语义），累计值另起一个名字，不把原指标改成另一个口径；③第三个轮次数（详情弹窗的「累计轮次」= `authors.runs`，按账号）不改，但要在 README 里把三者摆在一起说清楚 |

### 尚未做（明确不在第一版范围）

- `known_ids_max` 之外的**历史回溯**（DTK 的 `/archive/backfill` 能做，但那会大量消耗身份）
- 评论监控、粉丝曲线、多平台（TikTok）
- 通知语言切换（文案已集中在 `messages.py`，加英文只改那一个文件）
- **自身降级护栏**：`EventKind.SELF_DEGRADED` 已经声明、4.9 的降级矩阵与第 3 章原则 #8 也都写了
  "自身状态库/磁盘超限 → 停推送、面板标红、不崩不丢状态"，但**一处都没实现**：没有地方产生这个
  事件，也没有对自身磁盘余量的检查。当前的实际行为是"该推的照推，磁盘满了由 SQLite/systemd 去报错"
- **面板的上游健康卡片**（4.9 的"可选"）：要显示 DTK 版本 / 组件 / 身份池计数，就得让面板去调
  `GET /api/v1/system/status`，这与"打开面板不产生任何上游请求"冲突。要做的话应当是**主循环**
  定期取一次写进 `status.json`，面板继续只读快照——而不是让面板自己发请求
- **`/metrics` 的指标名**：设计稿写的是 `monitor_*` 前缀（含 `monitor_polls_total{result}`、
  `monitor_new_posts_total{author}`、`monitor_removed_total`、`monitor_upstream_errors_total{code}`、
  `monitor_last_success_timestamp`），实现里用的是 `dywatch_*` 且少了几个。补齐需要从
  `rounds` / `events` 两张审计表汇总，留给专门做监控接入的时候一次改掉（改名前先想清楚谁在抓它）
