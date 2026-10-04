# 排障

| 现象                                   | 原因与处理                                                                                                                                                                             |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 UNAUTHENTICATED`                  | Key 被拒（**不是权限不足**，那是 403）。确认复制的是完整 `dtk_...`（49 字符）、未吊销                                                                                                  |
| `403 FORBIDDEN_SCOPE`                  | 缺 scope。`dywatch doctor` 会直接告诉你缺哪个                                                                                                                                          |
| `503 IDENTITY_POOL_EXHAUSTED`          | DTK 身份池空了。全局闸门会自动退避，看 DTK 控制台的身份池页                                                                                                                            |
| `502 UPSTREAM_RISK_CONTROL`            | 上游风控。该账号记一次失败并退避；持续出现说明 DTK 侧需要处理                                                                                                                          |
| 收不到通知                             | 先 `dywatch test-notify`；再确认 `SILENT_MODE=false`、渠道凭据齐全；查 `log/debug/monitor.log`                                                                                         |
| 某个账号一直"无作品"                   | 大概率 ID 写错了：`users.conf` 里换成主页链接重新 `dywatch add` 一遍                                                                                                                   |
| 通知里没有"播放"数                     | 正常。抖音的 `play_count` 实测恒为 `null`，dywatch 不显示平台没说过的数字                                                                                                              |
| 面板打不开                             | 确认 `WEB_ENABLED=true`；局域网访问需要 `WEB_HOST=0.0.0.0`（面板无鉴权，请自行加反代）                                                                                                 |
| 面板列表是空的 / 一直"加载中"          | 列表读 `data/status.json`，每轮写一次；没跑完一轮就没有快照。先跑一次 `dywatch once`                                                                                                   |
| 点账号名详情报"状态库里没有这个账号"   | 账号还没写进状态库（第一次抓取还没成功），跑完一轮再看；报"状态库暂不可读"则是库/权限问题                                                                                              |
| 面板上的数字和 `dywatch status` 不一致 | 面板读的是快照（每轮写一次），`status` 读的是同一个文件；差异通常来自另一进程刚跑完一轮                                                                                                |
| 面板"本次运行第 51 轮"而库里几万轮     | **正常，不是丢数据**。那个数是进程内存计数，重启归零；跨重启的累计值在同行的「累计 N 轮」上。手工跑一次 `once` 也会把快照的"本次运行"打回 1                                            |
| `rounds` 表太大/太旧                   | 调大或调小 `ROUNDS_KEEP_DAYS`（默认 5 天）；下一轮 `maintenance()` 就会删掉超期行。**文件不会自己变小**，要回收得停服务后 `sqlite3 data/dywatch.db "VACUUM"`                           |
| browser-rpc 显示「不可用」但能用       | DTK 对它的探测只等 1 秒，详见[下文](#browser-rpc-显示不可用-但它能用)                                                                                                                  |
| 想确认配置有没有生效                   | `dywatch config-check` 会打印每一项的**来源**（默认值 / `.env` / 环境变量）                                                                                                            |
| 日志每 15 分钟被切一次                 | Armbian 的 `armbian-truncate-logs` 用 `logrotate --force` 强制轮转。确认 `/etc/logrotate.d/dywatch` 已删（重跑一遍 `install.sh` 就会删），详见[日志与轮转](/operations/logging)        |
| 日志一直不轮转                         | 检查 `/etc/cron.d/dywatch` 是否存在、cron 服务是否活着（`systemctl status cron`）；手动跑一次 `logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf` 看报错 |

## browser-rpc 显示不可用，但它能用

browser-rpc 部署在**另一台机器**上（而不是和 DTK 同一台 / 同一个 compose 网络）时，这一条最容易遇到。

**面板只是转述 DTK `GET /api/v1/system/status` 里的读数，dywatch 自己不探测、也不调用 browser-rpc。**
DTK 那一侧是这样判定的（DTK v5 `routes/system.py`）：

- 没配 `DTK_BROWSER_RPC_URL` → `ok` 为空，面板显示「未配置」（灰色，不是故障）
- 配了 → 对 `<url>/rpc/health` 发一次 `GET`，**只等 1 秒**；抛任何异常（含超时）就是 `ok: false`

这个 1 秒来自 DTK 的设计假设：browser-rpc 和它在同一个内部网络里，健康检查是毫秒级的本地调用。
真正用它签名、造身份的超时要长得多（10 秒到 90 秒），所以「实际能用，但这一次探测超时」完全可能同时成立。
按 DTK 当前源码，这个 1 秒是常量，不能配置。

面板会在「不可用」下面写出 DTK 给的原因代码：

| 面板上的说明            | 代码          | DTK 的含义                                                              |
| ----------------------- | ------------- | ----------------------------------------------------------------------- |
| DTK 探测它时没连上      | `unreachable` | 探测抛了异常：1 秒内没连上 / 没读完（含超时）、连接被拒、域名解析失败等 |
| 它的健康检查没有返回 ok | `degraded`    | 连上了，但 `/rpc/health` 不是 2xx，或返回的 `status` 不是 `ok`          |

排查 `unreachable`：

1. 在 DTK 日志里搜 `system.browser_rpc.unreachable`，`error=` 后面是异常类型：`ConnectTimeout` /
   `ReadTimeout` 是超过了 1 秒（多半是跨机器的延迟，或浏览器那一端正忙）；`ConnectError`
   是根本连不上（地址、端口、防火墙）。
2. 在 **DTK 的 API 容器里**直接量一次，和探测走同样的客户端：
   `python -c "import httpx,time;t=time.time();r=httpx.get('<DTK_BROWSER_RPC_URL>/rpc/health',timeout=10);print(r.status_code,r.text,round(time.time()-t,3))"`。
   稳定超过 1 秒，就是上面说的情况。
3. DTK 容器里设了 `HTTP_PROXY` / `HTTPS_PROXY` 的话，确认 `NO_PROXY` 包含 browser-rpc 的地址：
   探测用的 httpx 默认会读这些环境变量，走了代理的局域网地址多半不通。

这个读数**不会让 dywatch 停下来**，它只是 DTK 的一次健康检查。真正该看的是 DTK 控制台里身份池
有没有在正常补充；补充正常，这一行可以放着。想让它变绿，就让 browser-rpc 到 DTK 的延迟稳定在
1 秒以内，或者把它放回同一台机器。

## 排查思路

1. **先跑 `dywatch doctor`**——它把"配错了"和"上游坏了"分开，大多数问题在这一步
   就能定位（详见[命令行](/guide/commands)）。
2. **确认更新方式对不对**——如果是升级后行为异常，先确认走的是
   `git fetch && git reset --hard origin/main` + 重跑 `install.sh`，而不是
   `git pull`（会失败）或只 `restart` 不重装依赖（新代码不会生效），见
   [升级](/operations/upgrade)。
3. **看日志**——`log/info/monitor.log` 日常够用，深挖用
   `log/debug/monitor.log`。

## 还没解决？

到 [GitHub Issues](https://github.com/42419/douyin-monitor/issues) 提交问题，
建议附上 `dywatch config-check` 与 `dywatch doctor` 的输出（记得脱敏
`DTK_API_KEY` 等凭据）。
