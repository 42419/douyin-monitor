# 日志与轮转

## 日志文件

```
log/info/monitor.log     关键事件 + 每轮汇总（日常看这个）
log/debug/monitor.log    完整细节（排障）
```

`LOG_LEVEL` 只影响终端输出；日志文件始终同时写 info 与 debug 两份，不受
`LOG_LEVEL` 影响。

```bash
journalctl -u dywatch -f                            # 实时日志（systemd 侧）
tail -f /opt/douyin-monitor/log/info/monitor.log    # 实时日志（应用侧）
```

## 轮转与压缩交给 logrotate

应用自己不做轮转。但配置**不装进 `/etc/logrotate.d`**，而是单独放：

| 东西     | 位置                                 | 说明                                                |
| -------- | ------------------------------------- | ------------------------------------------------------ |
| 轮转配置 | `/etc/dywatch/logrotate.conf`         | 由 `deploy/logrotate.conf` 生成，改"留多久"改这里        |
| 触发者   | `/etc/cron.d/dywatch`                 | 每小时第 17 分钟跑一次 `logrotate`                       |
| 状态文件 | `/var/lib/dywatch/logrotate.status`   | 与系统 logrotate 完全隔离                                |

节奏：`daily` + `maxsize 10M` → 跨天后第一次运行切一次（约等于每天一次），单
文件涨过 10M 最多延迟 1 小时切；`rotate 14` + `compress` 保留 14 份。

## 为什么不放进 `/etc/logrotate.d`

Armbian 的 `/etc/cron.d/armbian-truncate-logs` 每 15 分钟跑一次
`/usr/lib/armbian/armbian-truncate-logs`，当 `/var/log` 用量 ≥75% 时执行
`logrotate --force /etc/logrotate.conf`。`--force` 会**跳过"今天是否已轮转过"
的判断**，把 `/etc/logrotate.d` 下所有配置强制轮转一遍——包括这个工具的。表现
就是 `monitor.log` 每 15 分钟被切一次、14 份归档不到 4 小时就被挤掉，日志几乎
没法回看。

自带 cron + 独立 state 之后，别人的 `--force` 再也波及不到这里。如果是从旧版本
升级上来、`/etc/logrotate.d/dywatch` 里还留着老配置，重跑一遍
`deploy/install.sh` 会自动删掉它。

## 排障命令

```bash
ls -la --time-style=full-iso /opt/douyin-monitor/log/info/   # 归档时间戳应是按天，不是 15 分钟
cat /etc/cron.d/dywatch                                      # 看触发节奏
logrotate --debug /etc/dywatch/logrotate.conf                # 干跑一遍，验证配置
sudo logrotate --state /var/lib/dywatch/logrotate.status /etc/dywatch/logrotate.conf  # 手动轮转一次
journalctl -t dywatch-logrotate                              # cron 执行失败时会写这里
```

如果发现日志每 15 分钟被切一次，确认 `/etc/logrotate.d/dywatch` 是否已删；如果
日志一直不轮转，检查 `/etc/cron.d/dywatch` 是否存在、cron 服务是否活着
（`systemctl status cron`）。更多现象对照见[排障](/operations/troubleshooting)。
