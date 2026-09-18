# 这是什么

**dywatch** 监控多个抖音账号，检测**新作品**与**作品消失**，通过钉钉 / 企业微信 /
Bark / Server 酱 / Telegram / 通用 webhook 推送通知。

它建立在 [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
（下文简称 **DTK**）**v5** 之上，但只做一件事：**盯着数据变没变**。

```
      抖音  ←—— 签名 / 身份池 / 调度器 / 熔断 / 归档 ——  DTK v5
                                                          ↑
                                        HTTP /api/v1（默认只读，一个 API Key）
                                                          ↓
                                   dywatch  ←—— 判定 → 通知 → 面板
```

## dywatch 与 DTK 的分工

DTK v5 是一个自托管的抖音 / TikTok 数据采集后端：签名算法、身份池维护、风控绕行、
PostgreSQL 归档、REST API / MCP server / Web 控制台，都由它负责。dywatch **不**重新实现
任何一部分——它只是 DTK 之上的一个 HTTP 客户端。

> dywatch 本工具不自带签名、不碰 Cookie、不碰身份池、不直接访问抖音。抓取、风控绕行、
> 身份轮换全部由 DTK v5 负责。

这个边界带来两个直接后果：

- dywatch 对 DTK **默认是纯只读的**——不提交采集任务、不改 DTK 的设置、不注册
  watchlist，所以只需要 `douyin:read` 与 `archive:read` 两个 scope（见
  [权限 / API Key](/reference/permissions)），也不会把你的 DTK 实例搞坏。
- 唯一的例外是一个**默认关闭**的开关：打开 `ARCHIVE_DOWNLOAD_ENABLED` 后会请求 DTK
  把新作品的媒体存一份，那需要额外授一个 `media:write`（见
  [归档下载](/guide/archive-download)）。

## 部署形态只有一种

**Ubuntu / Linux 服务器上的 systemd 服务。** dywatch 不提供容器镜像——它就是一个
进程 + 一个 SQLite 文件 + 一份配置，systemd 已经把开机自启、崩溃重启、日志归集、
权限隔离都管完了，再加一层容器编排只会多一处要维护的东西。

DTK 本身怎么部署是另一回事（以上游文档为准，见
[官方 Quick start](https://douyin.wtf/quickstart/)），dywatch 只要能通过 HTTP 访问到它即可，
两者不需要在同一台机器上。

## 判定 → 通知的核心逻辑

每一轮，dywatch 依次拉取每个监控账号的最新作品列表，与本地状态库对比：

- **新作品**（本页出现、本地没记录过）→ 立即推送
- **作品消失**（连续若干轮不在列表里）→ 分级确认后推送，避免抖音接口一次抖动就误报
- 还有疑似漏检、账号持续失败、上游降级等一系列运维类事件

完整规则见 [判定规则](/guide/detection-rules)，全部事件类型见
[事件类型](/reference/events)。

## 准备好了？

从 [前置：DTK v5 与 API Key](/guide/dtk-setup) 开始，或者如果你已经有一个可用的 DTK v5
实例和 API Key，直接跳到 [安装 dywatch](/guide/quick-start)。
