# 升级

## 拉取新代码

**用 `git fetch && git reset --hard origin/main`，不要用 `git pull`。**

安装在 `/opt/douyin-monitor` 的这份是纯部署副本，永远不会在上面产生提交；而
本仓库的历史可能被 amend / force-push 改写过，`git pull`（以及
`git pull --ff-only`）会因为 "divergent branches" 直接拒绝。安全前提是"服务器
上没有独有提交"，这条一直成立：

```bash
cd /opt/douyin-monitor
git fetch origin
git reset --hard origin/main      # 本地若有过手工改动，会一并丢掉，先 git diff 看一眼
sudo bash deploy/install.sh --yes
```

## `install.sh` 在升级路径下做了什么

`install.sh` 会自动判断当前是首次安装还是升级（看 `.venv` 和 systemd 单元是否
已存在），两条路径完全不同。**升级路径**做的事：

1. **重装依赖**
2. 刷新 systemd 单元与日志轮转 cron（**并删掉老版本可能留在
   `/etc/logrotate.d/dywatch` 的那份配置**，见[日志与轮转](/operations/logging)）
3. 如果服务正在跑，问你要不要立即重启（`--yes` 直接重启，不问）
4. 如果安装目录**不是**要安装的那个目录（`SRC_DIR != MONITOR_HOME`），会先用
   `rsync --delete` 同步代码过去（`.env` / `users.conf` / `data/` / `log/` /
   `.venv/` 一律不碰）；按标准的 `/opt/douyin-monitor` 布局时两者是同一个目录，
   这一步会被跳过，代码由 `git` 就地更新

::: danger "重装依赖"这一步不能省
`.venv` 里装的是**复制**进去的一份代码（非可编辑安装），只
`git reset --hard` + `systemctl restart` **不会**让新代码生效——必须重新跑
`install.sh`（或手动 `pip install .`）才会把 `src/` 的内容刷进 `.venv`。
:::

## 全自动升级

```bash
sudo bash deploy/install.sh --yes
```

升级时如果服务正在跑会直接重启，不再询问，适合写进你自己的升级脚本
（比如 cron 定期升级，或者接到 CI 里）。

## 只想看看会发生什么，不想真的改动

```bash
sudo bash deploy/install.sh --check
```

只检测当前是首次安装还是升级、缺什么依赖，不做任何实际改动。

## 升级后确认

```bash
systemctl status dywatch
journalctl -u dywatch -f
dywatch config-check       # 确认新版本引入的配置项来源符合预期
```
