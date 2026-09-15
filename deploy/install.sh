#!/usr/bin/env bash
# dywatch 的 Ubuntu 一键安装。
#
#   sudo bash deploy/install.sh                    # 装到默认的 /opt/douyin-monitor
#   sudo MONITOR_HOME=/srv/dywatch bash deploy/install.sh   # 或指定别的目录
#
# 它做四件事，每件都幂等：建 .venv 装依赖 → 生成 .env / users.conf 模板 →
# 装 systemd 单元与 logrotate 配置 → 交接目录属主。
#
# **不创建专用系统用户**：服务以"执行安装的那个账号"身份运行（sudo 时取 SUDO_USER），
# 所以你 vi / .venv/bin/python 都是自己的账号，不用 sudo -u 到处切换。
# 权限边界由 systemd 单元的 ProtectSystem=strict + ReadWritePaths 给出，不靠换用户。
set -euo pipefail

HOME_DIR="${MONITOR_HOME:-/opt/douyin-monitor}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 前置检查 -------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "请用 sudo 运行（要写 /etc/systemd/system 与 /etc/logrotate.d）"
command -v python3 >/dev/null 2>&1 || die "没有 python3：apt install python3 python3-venv"
python3 -c 'import venv' 2>/dev/null || die "缺少 venv 模块：apt install python3-venv"

# 服务以谁的身份跑：sudo 时取真实登录用户；直接用 root 跑则明确警告
RUN_USER="${SUDO_USER:-}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    say "警告：以 root 身份安装，服务将以 root 运行。建议用 sudo 从普通账号执行本脚本。"
    RUN_USER="root"
fi
id -u "$RUN_USER" >/dev/null 2>&1 || die "用户 $RUN_USER 不存在"
RUN_GROUP="$(id -gn "$RUN_USER")"

say "安装目录：$HOME_DIR"
say "运行身份：$RUN_USER ($RUN_GROUP)"

# --- 同步代码 -------------------------------------------------------------
mkdir -p "$HOME_DIR"
if [[ "$SRC_DIR" != "$HOME_DIR" ]]; then
    say "同步代码到 $HOME_DIR"
    for item in src deploy pyproject.toml README.md .env.example users.conf.example .gitignore .gitattributes; do
        # 用 if 而不是 [[ ]] && cp：后者在某一项缺失时会让 set -e 直接退出
        if [[ -e "$SRC_DIR/$item" ]]; then
            cp -r "$SRC_DIR/$item" "$HOME_DIR/"
        fi
    done
fi

# --- 依赖 -----------------------------------------------------------------
cd "$HOME_DIR"
if [[ ! -x .venv/bin/python ]]; then
    say "创建虚拟环境 .venv"
    python3 -m venv .venv
fi
say "安装依赖"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .

# --- 配置模板（绝不覆盖已有的） ------------------------------------------
[[ -f .env ]] || { say "生成 .env 模板（记得填 DTK_API_KEY）"; cp .env.example .env; }
[[ -f users.conf ]] || { say "生成 users.conf 模板"; cp users.conf.example users.conf; }
chmod 600 .env

# --- 属主与 systemd / logrotate ------------------------------------------
say "交接目录属主给 $RUN_USER"
chown -R "$RUN_USER:$RUN_GROUP" "$HOME_DIR"

# MONITOR_HOME 在 /home 下时，单元里的 ProtectHome 必须放开，否则工作目录写不进去
protect_home="true"
case "$HOME_DIR" in
    /home/*|/root/*) protect_home="false" ;;
esac

say "安装 systemd 单元"
sed -e "s|@HOME_DIR@|$HOME_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    -e "s|@PROTECT_HOME@|$protect_home|g" \
    deploy/dywatch.service > /etc/systemd/system/dywatch.service
systemctl daemon-reload

say "安装 logrotate 配置"
sed -e "s|@HOME_DIR@|$HOME_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    deploy/logrotate.conf > /etc/logrotate.d/dywatch

cat <<EOF

$(say 完成)

下一步（都在你自己的账号下，不需要 sudo -u）：
  1) 填凭据：  vi $HOME_DIR/.env
               （DTK_API_KEY 需要 douyin:read 与 archive:read）
  2) 填账号：  vi $HOME_DIR/users.conf
               或：$HOME_DIR/.venv/bin/python -m dywatch add "<主页链接>"
  3) 先自检：  cd $HOME_DIR && ./.venv/bin/python -m dywatch doctor
  4) 跑一轮：  ./.venv/bin/python -m dywatch once
  5) 常驻：    sudo systemctl enable --now dywatch
  6) 看日志：  journalctl -u dywatch -f
               tail -f $HOME_DIR/log/info/monitor.log

注意：doctor 与 once 每个账号会消耗 1 个身份，属正常开销。
改 .env 后要 systemctl restart dywatch；改 users.conf 不用（按 mtime 热加载）。
EOF
