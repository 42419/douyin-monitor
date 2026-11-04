# 参与开发

这个仓库是一个 Python 包（`src/dywatch`）+ 一个 VitePress 文档站（`docs/`）+
一套 systemd 部署脚本（`deploy/`）。目标运行环境是 Linux；Windows 只用于开发调试。

## 环境

- Python **≥ 3.11**（CI 用 3.12）。
- 文档站需要 Node 22（只在改 `docs/` 时需要）。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## 常用命令

用 Makefile（Linux/WSL）：

```bash
make check        # CI 做的事：ruff format 检查 + 全部测试
make test
make format       # 写入式格式化
make docs         # 本地文档站
```

Windows 下没有 make 时用等价命令：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff format --check src tests
```

> **测试与 `tmp_path`**：大量用例用 pytest 的 `tmp_path`。在受限环境（某些 Windows
> 沙箱 / 只读挂载）里，pytest 对临时基准目录的 `chmod`/`rm_rf` 会被拒，导致这些
> 用例在 **setup** 阶段报 `PermissionError`。这类环境请在 **Linux / WSL** 上跑，
> 或直接依赖 CI。

## 目录结构

```
src/dywatch/            监控本体（src-layout，见下）
  errors.py             MonitorError + 错误码分类（dtk 与 diff 共用）
  dtk.py                唯一知道 HTTP 的地方（DTK v5 只读客户端）
  diff.py               判定纯函数（(上一轮状态, 本页) -> 事件, 新状态）
  state.py              唯一持久化出口（SQLite，一轮一事务）
  pipeline.py           单账号一轮的编排（fetch→diff→persist→notify→归档）
  loop.py / pacer.py / scheduler.py   轮次循环、请求节奏、全局闸门
  render.py / messages.py             事件 → 文案（集中放这里）
  notifiers/            各渠道 payload 与投递
  webui/                只读 Web 面板（不发上游请求、不写状态库）
  settings.py           配置注册表 + 跨项校验 + 来源追溯
  cli.py / runtime.py   命令行与生命周期
tests/
  unit/                 纯逻辑与单模块测试（大部分在这里）
  replay/               录制真实响应回放
  conftest.py           让 src/ 可导入（无需可编辑安装）
docs/                   VitePress 文档站
deploy/                 systemd 单元、日志轮转、install.sh
DESIGN.md               设计与「实现修正」记录（改行为前先看这里）
```

依赖方向是单向、无环的，改代码时请守住：`cli → runtime → loop → pipeline →
{dtk, diff, state, notifiers, alerts}`；`diff` / `render` / `messages` /
`models` / `errors` 是纯逻辑，**不 import `dtk` / `state` / `notifiers`**。

## 提交约定

- 首行 `<type>(<scope>): 一句话`，下面空一行再用 **2 空格缩进**的 `- ` 列表。
- 正文解释**为什么**（踩过的坑、实测结论、与设计的出入），不只罗列改了什么。
- 一次提交只做一件事；中文多行提交信息写成文件再 `git commit -F`。
- 未经明确要求**不要 push**。

## 改行为前

判定规则、删除确认、漏检、通知版式的来历都在 `DESIGN.md`（尤其第 4 章与
第 10 章的「实现修正」）。动这些逻辑前先读一遍，很多「看起来能简化」的地方其实
是踩过的坑留下的补丁。
