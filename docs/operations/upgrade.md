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
dywatch doctor             # 应看到「✓ 定向读取身份 …（不走身份池）」
```

## 从「游客 + 隐藏核验」版本升级（2026-11 起）

这一版把读取方式改成了**单一登录态身份**，并删掉了整套隐藏作品核验。升级前按顺序做：

1. **DTK 侧**：确认有一个登录态身份（Import cookies），把它的 UUID 准备好；
   建议把它的「轮换范围」设为「只服务点名它的请求」。
2. **Key**：`DTK_API_KEY` 必须带 `identity:manage`，且 owner 至少 `operator`
   （DTK 的 `require_credential_access` 同时校验 scope 与角色）。没有就先补上或新建。
3. **`.env`**：
   - 新增 `DTK_IDENTITY=<那个 UUID>`（老的 `PINNED_IDENTITY_ID` 的值可以直接搬过来）；
   - 删掉 `HIDDEN_POST_CHECK_ENABLED`、`HIDDEN_CHECK_INTERVAL_MINUTES`、
     `PINNED_IDENTITY_ID`、`PIN_DTK_API_KEY`（留着不会拦启动，但日志里会逐条提醒）。
4. **备份状态库**：`cp -a data/dywatch.db* /somewhere/`。启动时 schema v6→v7 会删除
   隐藏核验的 5 个列（自动迁移，但**不可逆**；迁移后回滚旧版会因为"库比程序新"拒绝启动）。
5. 升级并启动，然后跑一次 `dywatch doctor`，再观察一轮日志。

::: warning 升级后的首轮可能补报一批历史事件
以前被游客视角藏住、从未被核验发现的作品，会作为 `new_post` 补报（发布时间是旧的）；
以前带「对访客不可见」标记、后来被作者删掉的作品，会从这一轮开始计缺席，约两轮后报
`post_removed`。两者都是正确结果，不是升级 bug。
:::
