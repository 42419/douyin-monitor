# 架构总览

## 模块划分

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

## 依赖方向单向、无环

- **`dtk.py`** 是唯一知道 HTTP 的地方
- **`diff.py`** 是唯一知道判定规则的地方
- **`notifiers/`** 是唯一懂第三方渠道 payload 的地方

三者互不 `import`；**`pipeline.py`** 是唯一的编排者，负责把「取数 → 判定 → 落库 →
通知」串起来。于是最易错的判定逻辑（`diff.py`）与最琐碎的第三方渠道格式
（`notifiers/`）都能脱离网络做单元测试。

## 一轮的执行路径

```
loop.py（热加载 users.conf，MAX_CONCURRENT 并发调度）
  └─ pipeline.py（每个账号一次）
       ├─ dtk.py     取作者最新一页作品（经过 pacer.py 节流、scheduler.py 全局闸门）
       ├─ diff.py    与状态库对比，产出事件（纯函数，零 I/O）
       ├─ state.py   落库（SQLite，一轮一个事务）
       └─ notifiers/ 按 NOTIFY_PRIORITY 顺序投递（new_post 永远最先）
            └─（可选）触发 DTK 归档下载，见「归档下载」一章
```

轮末由 `pacer.py` 做一次随机等待（`POLL_INTERVAL_MIN` / `MAX`），随后 `loop.py`
重新热加载 `users.conf` 并开始下一轮。

## 状态与观测

- **SQLite** 是唯一的持久化出口（`state.py`），一轮一个事务，写入频率低、无并发写冲突。
- **`webui.py`** 提供的面板完全只读：列表读 `data/status.json`（每轮写一次的快照），
  详情读状态库，不产生任何对 DTK 的请求，也就不消耗身份、不会触发风控。
- 结构化日志、`/healthz`、`/readyz`、`/metrics`（Prometheus 格式）都由 `runtime.py`
  与 `webui.py` 提供，方便接入现有的监控体系。

想了解每一条规则的来历、以及"为什么不那样做"的取舍记录，可以直接读仓库里的
[`DESIGN.md`](https://github.com/42419/douyin-monitor/blob/main/DESIGN.md)——
真实接口契约（含实测数据）在它的第 2 章。
