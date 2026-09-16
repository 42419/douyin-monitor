#!/usr/bin/env bash
# dywatch 的 Ubuntu 一键安装/升级。
#
#   sudo bash deploy/install.sh                              # 装到默认的 /opt/douyin-monitor
#   sudo MONITOR_HOME=/srv/dywatch bash deploy/install.sh    # 或指定别的目录
#   sudo bash deploy/install.sh --check                      # 只检测、不改动，打印会做什么
#   sudo bash deploy/install.sh --yes                        # 非交互：升级后如服务在跑，直接重启
#
# 它自动判断这是「首次安装」还是「升级」（看 $HOME_DIR/.venv 和 systemd 单元
# 是否已存在），两条路径做的事不同：
#
#   首次安装：建 .venv 装依赖 → 生成 .env / users.conf 模板 → 装 systemd 单元与
#             logrotate 配置 → 交接目录属主
#   升级    ：用 rsync --delete 同步代码（不碰 .env / users.conf / data / log /
#             .venv）→ 重装依赖 → 刷新 systemd 单元与 logrotate 配置 → 如果服务
#             正在跑，询问（或 --yes 直接）重启，否则新代码不会生效
#
# 每一步都幂等：反复跑不会破坏已有配置，也不会重复做无意义的事。
#
# **不创建专用系统用户**：服务以"执行安装的那个账号"身份运行（sudo 时取 SUDO_USER），
# 所以你 vi / .venv/bin/python 都是自己的账号，不用 sudo -u 到处切换。
# 权限边界由 systemd 单元的 ProtectSystem=strict + ReadWritePaths 给出，不靠换用户。
set -euo pipefail

# ------------------------------------------------------------------ 输出 --
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[36m'
else
    C_RESET=''; C_BOLD=''
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi
say()  { printf '\n%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '\n%s错误:%s %s\n\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 参数 --
ASSUME_YES=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)   ASSUME_YES=1 ;;
        --check)    CHECK_ONLY=1 ;;
        -h|--help)
            cat <<'EOF'
用法: sudo bash deploy/install.sh [--yes] [--check]

  --yes, -y   非交互：升级后如果服务正在运行，直接重启而不询问
  --check     只检测环境和当前状态（首次安装 / 升级、缺什么依赖），不做任何改动
EOF
            exit 0 ;;
        *) die "未知参数: $arg（--help 看用法）" ;;
    esac
done

ask_restart_now() {
    # --yes 时直接同意；有 tty 时询问；两者都不满足（比如被别的脚本调用）时默认不重启，
    # 更安全的失败方向是"新代码还没生效"而不是"没打招呼就重启了正在用的服务"
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local reply
    printf '    服务正在运行，现在重启以应用新代码？[Y/n] '
    read -r reply || reply=""
    case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
        ""|y|yes) return 0 ;;
        *) return 1 ;;
    esac
}

# ------------------------------------------------------------- 前置检查 --
[[ $EUID -eq 0 ]] || die "请用 sudo 运行（要写 /etc/systemd/system 与 /etc/logrotate.d）"
command -v python3 >/dev/null 2>&1 || die "没有 python3：apt install python3 python3-venv"
python3 -c 'import venv' 2>/dev/null || die "缺少 venv 模块：apt install python3-venv"

HAVE_RSYNC=1
command -v rsync >/dev/null 2>&1 || HAVE_RSYNC=0

HOME_DIR="${MONITOR_HOME:-/opt/douyin-monitor}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_PATH="/etc/systemd/system/dywatch.service"

# 服务以谁的身份跑：sudo 时取真实登录用户；直接用 root 跑则明确警告
RUN_USER="${SUDO_USER:-}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    warn "以 root 身份安装，服务将以 root 运行。建议用 sudo 从普通账号执行本脚本。"
    RUN_USER="root"
fi
id -u "$RUN_USER" >/dev/null 2>&1 || die "用户 $RUN_USER 不存在"
RUN_GROUP="$(id -gn "$RUN_USER")"

# 判断首次安装还是升级：venv 和 systemd 单元都已存在才算"已装过"，
# 只有其中一个说明上次安装中途失败了，仍按首次安装处理更安全（幂等地补全缺的部分）
IS_UPGRADE=0
if [[ -x "$HOME_DIR/.venv/bin/python" && -f "$UNIT_PATH" ]]; then
    IS_UPGRADE=1
fi

SERVICE_WAS_ACTIVE=0
if systemctl is-active --quiet dywatch 2>/dev/null; then
    SERVICE_WAS_ACTIVE=1
fi

say "检测结果"
info "安装目录：$HOME_DIR"
info "运行身份：$RUN_USER ($RUN_GROUP)"
info "模式：$([ "$IS_UPGRADE" = 1 ] && echo 升级 || echo 首次安装)"
[ "$IS_UPGRADE" = 1 ] && info "服务当前状态：$([ "$SERVICE_WAS_ACTIVE" = 1 ] && echo 运行中 || echo 未运行)"
[ "$HAVE_RSYNC" = 0 ] && warn "没有 rsync，升级时会退化成 cp -r（不清理已删除的旧文件，参见 README 的升级说明）"

if [[ "$CHECK_ONLY" = 1 ]]; then
    say "仅检测（--check），未做任何改动"
    if [[ "$IS_UPGRADE" = 1 ]]; then
        info "会：同步代码、重装依赖、刷新 systemd/logrotate 配置"
        [[ "$SERVICE_WAS_ACTIVE" = 1 ]] && info "会：询问是否重启正在运行的服务（--yes 则直接重启）"
    else
        info "会：创建 .venv、安装依赖、生成 .env/users.conf 模板、安装 systemd 单元与 logrotate 配置"
    fi
    exit 0
fi

# --------------------------------------------------------------- 同步代码 --
mkdir -p "$HOME_DIR"
if [[ "$SRC_DIR" != "$HOME_DIR" ]]; then
    if [[ "$IS_UPGRADE" = 1 && "$HAVE_RSYNC" = 1 ]]; then
        say "同步代码到 $HOME_DIR（rsync --delete，不碰运行时数据）"
        # 只有 include 列出的路径会被同步/清理旧文件；其余一切（.env、users.conf、
        # data/、log/、.venv/、monitor.pid，以及任何脚本不认识的文件）落进末尾的
        # `--exclude '*'`——rsync 的默认语义是"被排除的文件也不会被删除"，
        # 所以这里不需要、也不应该再为运行时文件单独写 --exclude 保护规则。
        # --checksum：改动经常发生在同一秒内、文件大小也可能凑巧相同，
        # 光比 mtime+size 的快速校验会漏判，装机脚本不常跑，多算一次哈希不心疼。
        rsync -a --checksum --delete \
            --include 'src/***' --include 'deploy/***' \
            --include 'pyproject.toml' --include 'README.md' \
            --include '.env.example' --include 'users.conf.example' \
            --include '.gitignore' --include '.gitattributes' \
            --exclude '*' \
            "$SRC_DIR/" "$HOME_DIR/"
        ok "已清理源码目录里不再需要的旧文件（运行时数据不受影响）"
    else
        say "同步代码到 $HOME_DIR"
        [[ "$IS_UPGRADE" = 1 ]] && warn "没有 rsync，用 cp -r 同步：不会清理已从新版本删除的旧文件"
        for item in src deploy pyproject.toml README.md .env.example users.conf.example .gitignore .gitattributes; do
            # 用 if 而不是 [[ ]] && cp：后者在某一项缺失时会让 set -e 直接退出
            if [[ -e "$SRC_DIR/$item" ]]; then
                cp -r "$SRC_DIR/$item" "$HOME_DIR/"
            fi
        done
    fi
fi

# --------------------------------------------------------------------- 依赖 --
cd "$HOME_DIR"
if [[ ! -x .venv/bin/python ]]; then
    say "创建虚拟环境 .venv"
    python3 -m venv .venv
fi
say "$([ "$IS_UPGRADE" = 1 ] && echo 重新安装依赖 || echo 安装依赖)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet --upgrade .
ok "依赖就绪：$(.venv/bin/python -c 'import dywatch; print(dywatch.__version__)' 2>/dev/null || echo 未知版本)"

# ------------------------------------------------------- 配置模板（绝不覆盖已有） --
if [[ "$IS_UPGRADE" = 0 ]]; then
    [[ -f .env ]] || { say "生成 .env 模板（记得填 DTK_API_KEY）"; cp .env.example .env; }
    [[ -f users.conf ]] || { say "生成 users.conf 模板"; cp users.conf.example users.conf; }
fi
[[ -f .env ]] && chmod 600 .env

# ------------------------------------------------------------------ 属主 --
say "交接目录属主给 $RUN_USER"
chown -R "$RUN_USER:$RUN_GROUP" "$HOME_DIR"

# MONITOR_HOME 在 /home 或 /root 下时，单元里的 ProtectHome 必须放开，否则工作目录写不进去
protect_home="true"
case "$HOME_DIR" in
    /home/*|/root/*) protect_home="false" ;;
esac

say "安装 systemd 单元"
sed -e "s|@HOME_DIR@|$HOME_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    -e "s|@PROTECT_HOME@|$protect_home|g" \
    deploy/dywatch.service > "$UNIT_PATH"
systemctl daemon-reload

say "安装 logrotate 配置"
sed -e "s|@HOME_DIR@|$HOME_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    deploy/logrotate.conf > /etc/logrotate.d/dywatch

# --------------------------------------------------------- 升级：按需重启 --
RESTARTED=0
if [[ "$IS_UPGRADE" = 1 && "$SERVICE_WAS_ACTIVE" = 1 ]]; then
    say "服务正在运行"
    info "systemd 单元和依赖都已更新，但正在跑的进程还是旧代码，需要重启才会生效"
    if ask_restart_now; then
        systemctl restart dywatch
        ok "已重启"
        RESTARTED=1
    else
        warn "已跳过重启——新代码不会生效，记得手动执行: systemctl restart dywatch"
    fi
fi

# ------------------------------------------------------------------ 总结 --
if [[ "$IS_UPGRADE" = 1 ]]; then
    if [[ "$RESTARTED" = 1 ]]; then
        restart_note="  服务已重启，正在用新代码运行。"
    elif [[ "$SERVICE_WAS_ACTIVE" = 1 ]]; then
        restart_note="  服务还在用旧代码运行，记得: systemctl restart dywatch"
    else
        restart_note="  服务当前未运行，下次 systemctl start/enable 时会自动用上新代码。"
    fi
    cat <<EOF

$(say 升级完成)

  代码与依赖已更新到 $HOME_DIR。
$restart_note

  看日志：journalctl -u dywatch -f
EOF
else
    cat <<EOF

$(say 安装完成)

下一步（都在你自己的账号下，不需要 sudo -u）：
  1) 填凭据：  vi $HOME_DIR/.env
               （DTK_API_KEY 需要 douyin:read 与 archive:read，留空会让 run 直接拒绝启动）
  2) 填账号：  vi $HOME_DIR/users.conf
               或：$HOME_DIR/.venv/bin/python -m dywatch add "<主页链接>"
  3) 先自检：  cd $HOME_DIR && ./.venv/bin/python -m dywatch doctor
  4) 跑一轮：  ./.venv/bin/python -m dywatch once
  5) 常驻：    sudo systemctl enable --now dywatch
  6) 看日志：  journalctl -u dywatch -f
               tail -f $HOME_DIR/log/info/monitor.log

注意：doctor 与 once 每个账号会消耗 1 个身份，属正常开销。
改 .env 后要 systemctl restart dywatch；改 users.conf 不用（按 mtime 热加载）。

以后升级：git pull 到这份源码目录后，重新执行本脚本即可
（sudo bash deploy/install.sh --yes 可以做到"同步代码 + 服务在跑就自动重启"全自动）。
EOF
fi
