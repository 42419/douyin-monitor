# 安装 dywatch

## 环境要求

**Python 必须 ≥ 3.11**（由 `pyproject.toml` 的 `requires-python` 决定）。
Ubuntu 24.04 自带的 3.12 够用，22.04 自带的 3.10 **不够**，需要先装一个更新的版本。

```bash
sudo apt update && sudo apt install -y python3 python3-venv   # 版本不够时先装一个 3.11+
```

## 安装

```bash
git clone https://github.com/42419/douyin-monitor.git && cd douyin-monitor
sudo bash deploy/install.sh      # 建 .venv、装 systemd 单元与日志轮转 cron、生成配置模板
```

`install.sh` 会自己在 `python3.14 / 3.13 / 3.12 / 3.11 / python3` 里挑版本最高的
一个去建虚拟环境，不会盲信 `python3` 指向的版本。如果解释器不在默认位置，可以显式
指定：

```bash
sudo PYTHON=/usr/bin/python3.12 bash deploy/install.sh
```

安装完成后，填入上一步拿到的 API Key 和要监控的账号：

```bash
vi /opt/douyin-monitor/.env          # 填 DTK_API_KEY（要用通知就一并填渠道）
vi /opt/douyin-monitor/users.conf    # 填要监控的账号，见「监控列表 users.conf」
```

## 先自检，再跑一轮，最后常驻

```bash
cd /opt/douyin-monitor
./.venv/bin/python -m dywatch doctor
./.venv/bin/python -m dywatch once
sudo systemctl enable --now dywatch
journalctl -u dywatch -f
```

`doctor` 与 `once` 每个账号会消耗 1 个身份，属正常开销；它们不打网络也能给出有用的
结论（配置校验、Key 的 scope、账号 ID 是否有效），详见[命令行](/guide/commands)。

## `install.sh` 做了什么

脚本会自动判断这是首次安装还是升级（看 `.venv` 和 systemd 单元是否已存在），
两条路径做的事完全不同：

- **首次安装**：建 `.venv` 装依赖 → 生成 `.env` / `users.conf` 模板 → 装 systemd
  单元与日志轮转 cron → 交接目录属主。它**不**替你填 API Key——凭据不该由脚本猜。
- **升级**：见 [升级](/operations/upgrade) 一章，流程完全不同，且有一步不能省。

其他常用参数：

| 参数        | 作用                                                             |
| ----------- | ------------------------------------------------------------------ |
| `--check`   | 只检测当前是首次安装还是升级、缺什么依赖，不做任何改动             |
| `--yes`     | 全自动模式：升级时如果服务正在跑，直接重启，不询问；适合写进自己的升级脚本 |

## `DTK_API_KEY` 留空会发生什么

`DTK_API_KEY` 留空时 `dywatch run` 会在启动时直接拒绝（退出码 2）。systemd 单元
配了 `RestartPreventExitStatus=2`，所以不会陷入"每 10 秒重启一次、日志刷屏"的
死循环——服务会停在 `failed` 状态，等你去 `vi .env` 填上，然后
`systemctl restart dywatch`。

## 常用运维命令

```bash
systemctl status dywatch          # 服务状态
systemctl restart dywatch         # 改完 .env 后重启（users.conf 改完不用重启）
journalctl -u dywatch -f          # 实时日志（systemd 侧）
tail -f /opt/douyin-monitor/log/info/monitor.log   # 实时日志（应用侧）
```

## 服务器侧更新方式

**用 `git fetch && git reset --hard origin/main`，不要用 `git pull`。**
安装在 `/opt/douyin-monitor` 的这份是纯部署副本，永远不会在上面产生提交；而本仓库
的历史可能被 amend / force-push 改写过，`git pull`（以及 `git pull --ff-only`）
会因为 "divergent branches" 直接拒绝。完整流程见[升级](/operations/upgrade)。

## 下一步

- 熟悉[命令行](/guide/commands)的其余子命令
- 配置[监控列表 users.conf](/guide/users-conf)
- 打开[只读面板](/guide/dashboard)
- 按需调整[配置参考](/config/reference)里的各项参数
