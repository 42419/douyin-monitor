# 排障

| 现象                                    | 原因与处理                                                                                                                                     |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 UNAUTHENTICATED`                    | Key 被拒（**不是权限不足**，那是 403）。确认复制的是完整 `dtk_...`（49 字符）、未吊销                                                              |
| `403 FORBIDDEN_SCOPE`                    | 缺 scope。`dywatch doctor` 会直接告诉你缺哪个                                                                                                       |
| `503 IDENTITY_POOL_EXHAUSTED`            | DTK 身份池空了。全局闸门会自动退避，看 DTK 控制台的身份池页                                                                                          |
| `502 UPSTREAM_RISK_CONTROL`              | 上游风控。该账号记一次失败并退避；持续出现说明 DTK 侧需要处理                                                                                        |
| 收不到通知                               | 先 `dywatch test-notify`；再确认 `SILENT_MODE=false`、渠道凭据齐全；查 `log/debug/monitor.log`                                                     |
| 某个账号一直"无作品"                     | 大概率 ID 写错了：`users.conf` 里换成主页链接重新 `dywatch add` 一遍                                                                                |
| 通知里没有"播放"数                       | 正常。抖音的 `play_count` 实测恒为 `null`，dywatch 不显示平台没说过的数字                                                                            |
| 面板打不开                               | 确认 `WEB_ENABLED=true`；局域网访问需要 `WEB_HOST=0.0.0.0`（面板无鉴权，请自行加反代）                                                               |
| 面板列表是空的 / 一直"加载中"             | 列表读 `data/status.json`，每轮写一次；没跑完一轮就没有快照。先跑一次 `dywatch once`                                                                |
| 点账号名详情报"状态库里没有这个账号"      | 账号还没写进状态库（第一次抓取还没成功），跑完一轮再看；报"状态库暂不可读"则是库/权限问题                                                           |
| 面板上的数字和 `dywatch status` 不一致    | 面板读的是快照（每轮写一次），`status` 读的是同一个文件；差异通常来自另一进程刚跑完一轮                                                              |
| 面板"本次运行第 51 轮"而库里几万轮        | **正常，不是丢数据**。那个数是进程内存计数，重启归零；跨重启的累计值在同行的「累计 N 轮」上。手工跑一次 `once` 也会把快照的"本次运行"打回 1          |
| `rounds` 表太大/太旧                     | 调大或调小 `ROUNDS_KEEP_DAYS`（默认 5 天）；下一轮 `maintenance()` 就会删掉超期行。**文件不会自己变小**，要回收得停服务后 `sqlite3 data/dywatch.db "VACUUM"` |
| 想确认配置有没有生效                     | `dywatch config-check` 会打印每一项的**来源**（默认值 / `.env` / 环境变量）                                                                         |
| 日志每 15 分钟被切一次                   | Armbian 的 `armbian-truncate-logs` 用 `logrotate --force` 强制轮转。确认 `/etc/logrotate.d/dywatch` 已删（重跑一遍 `install.sh` 就会删），详见[日志与轮转](/operations/logging) |
| 日志一直不轮转                           | 检查 `/etc/cron.d/dywatch` 是否存在、cron 服务是否活着（`systemctl status cron`）；手动跑一次 `logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf` 看报错 |

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
