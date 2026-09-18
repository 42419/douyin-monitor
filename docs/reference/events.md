# 事件类型

每次判定产出的东西都落进 `events` 表，`kind` 是下面 15 种之一。**只有 ✅ 的会
推送**，其余静默落库（它们只改变状态、不打扰人）；`SILENT_MODE=true` 时连 ✅
也不推，但事件照记。

| `kind`               | 含义                                             | 推送 | 抑制窗口                                      |
| --------------------- | -------------------------------------------------- | :--: | ------------------------------------------------ |
| `new_post`            | 新作品（首次见到这条作品）                         | ✅   | 无                                                |
| `post_removed`        | 作品消失（分级确认之后）                           | ✅   | 无                                                |
| `all_gone`            | 该账号全部作品同时消失                             | ✅   | 无                                                |
| `never_seen`          | 从未见过作品且连续 3 轮为空（多半 ID 写错）        | ✅   | 6 小时 / 账号                                     |
| `gap_detected`        | 疑似漏检（两次观测之间有没采到的作品）             | ✅   | 12 小时 / 账号                                    |
| `account_failed`      | 连续失败达 `MAX_CONSECUTIVE_FAILS`                 | ✅   | 5 分钟 / 账号                                     |
| `account_recovered`   | 从连续失败中恢复                                   | ✅   | 无                                                |
| `stale_no_update`     | `STALE_FALLBACK_DAYS` 天没有新作品                 | ✅   | 一次性                                            |
| `upstream_degraded`   | 上游 429/503/熔断 → 全局闸门关闭                   | ✅   | 1 小时 / 错误码                                   |
| `self_degraded`       | 自身降级（磁盘/状态库超限）                        | ✅   | 6 小时 —— **目前没有产生点，不会出现**            |
| `revived`             | 曾消失的作品又出现（按设计**不算**新作品）         | ✅   | 6 小时 / 账号                                     |
| `title_changed`       | 已知作品的标题变了                                 | ✅   | 1 小时 / 账号                                     |
| `scrolled_out`        | 被新作品挤出窗口（静默清理并写 tombstone）         | —    |                                                    |
| `trimmed`             | 超过 `KNOWN_IDS_MAX` 被裁剪（同时写 tombstone）    | —    |                                                    |
| `initialized`         | 首次记录该账号（否则上线新账号会被历史作品刷屏）   | —    |                                                    |

## 查自己库里的事件分布

```bash
./.venv/bin/python -c "
import sqlite3
for kind, n in sqlite3.connect('data/dywatch.db').execute(
        'SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 2 DESC'):
    print(f'{kind:18} {n}')"
```

`payload_json` 是这条事件的细节，`delivery_json` 记录投递结果（哪些渠道成功/
失败）；面板点账号名看到的"最近事件"就是这张表的最近 12 条。

## 投递顺序

**永远是 `new_post` 第一**，其余按"越需要立刻知道越靠前"排（对应代码里的
`alerts.NOTIFY_PRIORITY`），**不是**按上表的顺序、也不是按判定产出的顺序。

理由：一轮里所有事件共用同一条投递通道（每条间隔 `NOTIFY_GAP`，每渠道 8 秒
超时、最多重试 2 次），排在 `new_post` 前面的每一条都可能把它推迟几十秒，或者
撞上渠道限流让它变成"发送失败"的那一条——而新作品是唯一错过就补不回来的东西。
