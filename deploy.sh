#!/bin/bash
# ====================================
# 抖音多用户视频监控 - 部署与运维脚本
# ====================================
#
# 用法：
#   chmod +x deploy.sh
#   sudo ./deploy.sh <命令> [--dir 工作目录]
#
# 命令：
#   install    首次安装（交互式配置推送渠道、监控账号、可选 systemd 服务）
#   update     更新代码和依赖到当前脚本所在版本，保留 .env/users.conf/state/log
#   uninstall  卸载（停止服务、可选删除工作目录，删除前会提示备份配置）
#   start      启动服务
#   stop       停止服务
#   restart    重启服务
#   status     查看服务运行状态 + 监控自身状态快照
#   logs       实时查看日志（journalctl 或日志文件）
#   config     重新走一遍配置向导（新增推送渠道 / 修改用户列表等），不改代码
#   help       显示本帮助
#
# 不带命令直接运行会进入交互菜单。
#
# 已有的 .env、users.conf、state/、log/ 在 update 时不会被覆盖或删除。

set -e
set -f  # 禁用通配符展开，防止路径遍历

# =================== 颜色 & 日志辅助 ===================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 输入验证：只允许安全字符，防止配置注入
validate_input() {
    local input="$1"
    local name="$2"
    if [[ ! "$input" =~ ^[a-zA-Z0-9_./:=@,\ -]*$ ]]; then
        error "$name 包含非法字符（只允许字母、数字、下划线、连字符、点、斜杠、冒号、等号、@、逗号、空格）"
    fi
}

# =================== 全局变量 ===================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="douyin-monitor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_WORK_DIR="/opt/douyin-monitor"
# 记录已安装工作目录的标记文件，让 update/uninstall/start/stop 等命令
# 不需要每次都重新问一遍装在哪
MARKER_FILE="/etc/douyin-monitor/workdir"
[ "$EUID" -ne 0 ] && MARKER_FILE="$HOME/.config/douyin-monitor/workdir"

WORK_DIR=""
OPT_DIR=""

# =================== 环境检测 ===================
SYSTEMD_AVAILABLE=false
check_environment() {
    info "检查运行环境..."

    if [ "$(uname)" != "Linux" ]; then
        error "此脚本仅支持 Linux 系统"
    fi

    if ! command -v python3 &> /dev/null; then
        error "未找到 python3，请先安装 Python 3.8+"
    fi

    local py_version py_major py_minor
    py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    py_major=$(echo "$py_version" | cut -d. -f1)
    py_minor=$(echo "$py_version" | cut -d. -f2)
    if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 8 ]; }; then
        error "Python 版本过低（当前 $py_version），需要 3.8+"
    fi
    ok "Python $py_version"

    if command -v systemctl &> /dev/null && [ -d /run/systemd/system ]; then
        SYSTEMD_AVAILABLE=true
        ok "检测到 systemd，可以配置常驻服务"
    else
        SYSTEMD_AVAILABLE=false
        warn "未检测到 systemd（可能在容器 / WSL 环境中），将使用 nohup 方式常驻"
    fi
}

require_root_for_install() {
    if [ "$EUID" -ne 0 ]; then
        warn "当前不是 root，如果要安装到系统目录（如 /opt）或配置 systemd 服务，建议用 sudo 运行"
    fi
}

# =================== 工作目录解析 ===================
save_workdir_marker() {
    mkdir -p "$(dirname "$MARKER_FILE")" 2>/dev/null || true
    echo "$WORK_DIR" > "$MARKER_FILE" 2>/dev/null || warn "无法写入标记文件 $MARKER_FILE（不影响本次操作）"
}

# 尝试自动找到已安装的工作目录：命令行参数 > 标记文件 > systemd 单元文件 > 默认路径
resolve_workdir() {
    if [ -n "$OPT_DIR" ]; then
        WORK_DIR="$OPT_DIR"
        return
    fi
    if [ -f "$MARKER_FILE" ]; then
        WORK_DIR="$(cat "$MARKER_FILE")"
        return
    fi
    if [ -f "$SERVICE_FILE" ]; then
        WORK_DIR="$(grep '^WorkingDirectory=' "$SERVICE_FILE" | cut -d= -f2)"
        [ -n "$WORK_DIR" ] && return
    fi
    if [ -f "$DEFAULT_WORK_DIR/douyin_monitor.py" ]; then
        WORK_DIR="$DEFAULT_WORK_DIR"
        return
    fi
    WORK_DIR=""
}

require_existing_install() {
    resolve_workdir
    if [ -z "$WORK_DIR" ] || [ ! -f "$WORK_DIR/douyin_monitor.py" ]; then
        error "找不到已安装的实例。如果装在非默认路径，请加 --dir 指定，例如：./deploy.sh $1 --dir /opt/douyin-monitor"
    fi
}

# =================== install: 配置工作目录 ===================
configure_workdir() {
    echo ""
    echo "=========================================="
    echo "  配置工作目录"
    echo "=========================================="
    echo ""
    echo "工作目录用于存放脚本、配置文件、状态数据和日志。"

    # 如果当前脚本所在目录本身就是一份代码（比如 git clone 下来直接跑），
    # 默认建议就地部署，不再额外建议 /opt/douyin-monitor，
    # 避免出现"clone 一份 + 复制一份"两份高度重复的代码。
    # 仍然可以在下面手动输入别的路径，回到"复制出一份独立运行目录"的老用法。
    local suggested_dir="$DEFAULT_WORK_DIR"
    if [ -f "$SCRIPT_DIR/douyin_monitor.py" ]; then
        suggested_dir="$SCRIPT_DIR"
        echo "检测到当前目录（$SCRIPT_DIR）已经包含源码，默认直接原地部署，"
        echo "不会再复制一份到 $DEFAULT_WORK_DIR。如果想装到别的目录，下面手动输入路径即可。"
    fi
    echo "默认路径: $suggested_dir"
    echo ""
    if [ -n "$OPT_DIR" ]; then
        WORK_DIR="$OPT_DIR"
        ok "使用 --dir 指定的工作目录: $WORK_DIR"
        return
    fi
    read -r -p "请指定工作目录路径（直接回车使用默认 $suggested_dir）: " custom_dir
    custom_dir="$(echo "$custom_dir" | xargs)"
    if [ -n "$custom_dir" ]; then
        validate_input "$custom_dir" "工作目录路径"
        WORK_DIR="$custom_dir"
    else
        WORK_DIR="$suggested_dir"
    fi
    ok "工作目录: $WORK_DIR"

    # WORK_DIR 就是 SCRIPT_DIR 时，douyin_monitor.py 只是刚 clone 下来的源码，
    # 不代表之前装过，不用弹"重新安装"确认；只有复制到别的目录、
    # 且那个目录已经有 douyin_monitor.py 时，才是真的"之前装过一次"。
    if [ "$WORK_DIR" != "$SCRIPT_DIR" ] && [ -f "$WORK_DIR/douyin_monitor.py" ]; then
        warn "该目录看起来已经装过一次了（存在 douyin_monitor.py）"
        read -r -p "确定要在这个目录上重新安装吗？已有的 .env/users.conf/state/log 不会被覆盖 (y/n): " CONFIRM
        if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
            error "已取消。如果只是想更新代码，用 ./deploy.sh update 即可"
        fi
    fi
}

# =================== 从 GitHub 下载最新代码 ===================
GITHUB_REPO="42419/douyin-monitor"

# 从 GitHub 拉取代码到临时目录，返回临时目录路径
_fetch_to_tmp() {
    local tmp_dir="${WORK_DIR}.fetch_tmp_$$"
    rm -rf "$tmp_dir"

    if command -v git &>/dev/null; then
        info "使用 git clone --depth 1 ..." >&2
        if ! git clone --depth 1 "https://github.com/$GITHUB_REPO.git" "$tmp_dir" 2>/tmp/fetch_err.log; then
            cat /tmp/fetch_err.log >&2
            error "git clone 失败，请检查网络连接"
        fi
        rm -rf "$tmp_dir/.git"
    else
        info "未检测到 git，使用 curl 下载 tarball ..." >&2
        if ! curl -fsSL "https://github.com/$GITHUB_REPO/archive/refs/heads/main.tar.gz" -o /tmp/douyin-monitor.tar.gz 2>/tmp/fetch_err.log; then
            cat /tmp/fetch_err.log >&2
            error "下载失败，请检查网络连接或安装 git"
        fi
        mkdir -p "$tmp_dir"
        tar xzf /tmp/douyin-monitor.tar.gz -C "$tmp_dir" --strip-components=1
        rm -f /tmp/douyin-monitor.tar.gz
    fi
    echo "$tmp_dir"
}

# install 时：全新下载，代码目录不存在或为空
fetch_code_fresh() {
    info "从 GitHub 下载最新代码（$GITHUB_REPO）..."
    local tmp_dir
    tmp_dir=$(_fetch_to_tmp)

    if [ -d "$WORK_DIR" ]; then
        local backup="${WORK_DIR}.old_$(date +%s)"
        mv "$WORK_DIR" "$backup"
        info "已备份旧目录到 $backup"
    fi
    mv "$tmp_dir" "$WORK_DIR"
    ok "代码下载完成"
}

# update 时：下载新代码，只覆盖代码文件本身，不碰其它任何东西。
# 之前的实现是 rm -rf 整个 WORK_DIR 再搬回去，坏处是：
#   1. cmd_update 在调用这里之前刚创建的 .backup_* 回滚备份，会被一起删掉，
#      等于"备份"从来没真正存在过；
#   2. venv/ 不在保留名单里，每次 update 都会被删掉，setup_venv 只能整个重建，
#      比按需 pip install 慢很多。
# 改成跟 sync_code_files 一样，只精确覆盖代码相关的几个文件/目录，
# .env / users.conf / state / log / venv / .backup_* 等全部原地不动，
# 不需要再手动备份-恢复。
fetch_code_update() {
    info "从 GitHub 下载最新代码（$GITHUB_REPO）..."
    local tmp_dir
    tmp_dir=$(_fetch_to_tmp)

    cp "$tmp_dir/douyin_monitor.py" "$WORK_DIR/"
    rm -rf "$WORK_DIR/douyin_monitor"
    cp -r "$tmp_dir/douyin_monitor" "$WORK_DIR/"
    find "$WORK_DIR/douyin_monitor" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    cp "$tmp_dir/requirements.txt" "$WORK_DIR/"
    cp "$tmp_dir/env.example" "$WORK_DIR/" 2>/dev/null || true
    cp "$tmp_dir/users.conf.example" "$WORK_DIR/" 2>/dev/null || true

    rm -rf "$tmp_dir"
    ok "代码更新完成"
}

# =================== 同步代码文件（从本地 SCRIPT_DIR 复制） ===================
# 仅在 WORK_DIR 和 SCRIPT_DIR 不同时使用（比如 --dir 指定了其他目录）
sync_code_files() {
    # 如果工作目录就是脚本所在目录，不需要复制
    if [ "$(cd "$SCRIPT_DIR" && pwd)" = "$(cd "$WORK_DIR" && pwd)" ]; then
        info "工作目录即脚本所在目录，跳过代码复制"
        return
    fi

    info "从 $SCRIPT_DIR 同步代码文件到 $WORK_DIR ..."
    mkdir -p "$WORK_DIR"

    cp "$SCRIPT_DIR/douyin_monitor.py" "$WORK_DIR/"
    rm -rf "$WORK_DIR/douyin_monitor"
    cp -r "$SCRIPT_DIR/douyin_monitor" "$WORK_DIR/"
    find "$WORK_DIR/douyin_monitor" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    cp "$SCRIPT_DIR/requirements.txt" "$WORK_DIR/"
    cp "$SCRIPT_DIR/env.example" "$WORK_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/users.conf.example" "$WORK_DIR/" 2>/dev/null || true

    ok "代码文件同步完成"
}

# 把 env.example 里新增的配置项（当前 .env 里没有的 KEY）追加到 .env 末尾，
# 已有的值绝不修改。用于 update 时让老用户自然获得新功能的配置开关。
sync_env_defaults() {
    local env_file="$WORK_DIR/.env"
    local example_file="$WORK_DIR/env.example"
    [ -f "$env_file" ] || return
    [ -f "$example_file" ] || return

    local added=0
    local tmp_appendix
    tmp_appendix="$(mktemp)"
    echo "" > "$tmp_appendix"
    echo "# ---- 以下配置项由 deploy.sh update 于 $(date '+%Y-%m-%d %H:%M:%S') 自动追加 ----" >> "$tmp_appendix"

    while IFS= read -r line; do
        # 只处理形如 KEY=... 的行，跳过注释和空行
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)= ]]; then
            local key="${BASH_REMATCH[1]}"
            if ! grep -q "^${key}=" "$env_file"; then
                echo "$line" >> "$tmp_appendix"
                added=$((added + 1))
            fi
        fi
    done < "$example_file"

    if [ "$added" -gt 0 ]; then
        cat "$tmp_appendix" >> "$env_file"
        ok "检测到 $added 个新配置项，已追加到 .env 末尾并注释说明来源（默认值均不影响现有行为）"
    fi
    rm -f "$tmp_appendix"
}

# =================== 虚拟环境 & 依赖 ===================
setup_venv() {
    info "准备虚拟环境..."
    cd "$WORK_DIR"

    if [ ! -d "venv" ]; then
        if ! python3 -m venv venv 2>/tmp/venv_err.log; then
            cat /tmp/venv_err.log >&2
            error "创建虚拟环境失败，Debian/Ubuntu 系统可能需要先安装: apt install python3-venv"
        fi
        ok "虚拟环境创建完成"
    fi

    info "安装/升级依赖..."
    # shellcheck disable=SC1091
    source venv/bin/activate
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    deactivate
    ok "依赖安装完成"
}

# =================== install: 交互式配置 .env ===================
configure_env() {
    local env_file="$WORK_DIR/.env"

    if [ -f "$env_file" ]; then
        warn ".env 已存在，跳过配置（如需新增推送渠道等，运行 ./deploy.sh config）"
        return
    fi

    echo ""
    echo "=========================================="
    echo "  配置推送渠道"
    echo "=========================================="
    echo ""
    echo "可以同时启用多个渠道，用空格分隔序号，例如输入 1 2 表示同时用钉钉和 Bark"
    echo "  1) 钉钉群机器人"
    echo "  2) Bark（iOS 推送）"
    echo "  3) 企业微信群机器人"
    echo "  4) Server 酱（可转发到微信）"
    echo "  5) Telegram Bot"
    echo ""
    read -r -p "请选择 [默认: 1]: " CHANNEL_CHOICES
    CHANNEL_CHOICES=${CHANNEL_CHOICES:-1}

    local channels=()
    local DINGTALK_TOKEN="" DINGTALK_SECRET="" AT_MOBILES=""
    local BARK_SERVER="https://api.day.app" BARK_DEVICE_KEY=""
    local WECOM_WEBHOOK_KEY=""
    local SERVERCHAN_SENDKEY=""
    local TELEGRAM_BOT_TOKEN="" TELEGRAM_CHAT_ID=""

    for choice in $CHANNEL_CHOICES; do
        case "$choice" in
            1)
                channels+=("dingtalk")
                echo ""
                echo "---- 钉钉群机器人 ----"
                read -r -p "access_token: " DINGTALK_TOKEN
                validate_input "$DINGTALK_TOKEN" "access_token"
                [ -z "$DINGTALK_TOKEN" ] && error "access_token 不能为空"
                read -r -p "加签密钥 (SEC 开头): " DINGTALK_SECRET
                validate_input "$DINGTALK_SECRET" "加签密钥"
                [ -z "$DINGTALK_SECRET" ] && error "加签密钥不能为空"
                [[ "$DINGTALK_SECRET" =~ ^SEC ]] || warn "加签密钥通常以 SEC 开头，请确认是否正确"
                read -r -p "告警时 @ 的手机号（多个用逗号分隔，可留空）: " AT_MOBILES
                validate_input "$AT_MOBILES" "手机号"
                ;;
            2)
                channels+=("bark")
                echo ""
                echo "---- Bark ----"
                read -r -p "服务器地址 [默认: https://api.day.app]: " input
                [ -n "$input" ] && { validate_input "$input" "Bark 服务器地址"; BARK_SERVER="$input"; }
                read -r -p "设备 Key: " BARK_DEVICE_KEY
                validate_input "$BARK_DEVICE_KEY" "设备 Key"
                [ -z "$BARK_DEVICE_KEY" ] && error "Bark 设备 Key 不能为空"
                ;;
            3)
                channels+=("wecom")
                echo ""
                echo "---- 企业微信群机器人 ----"
                read -r -p "Webhook key: " WECOM_WEBHOOK_KEY
                validate_input "$WECOM_WEBHOOK_KEY" "Webhook key"
                [ -z "$WECOM_WEBHOOK_KEY" ] && error "企业微信 Webhook key 不能为空"
                ;;
            4)
                channels+=("serverchan")
                echo ""
                echo "---- Server 酱 ----"
                read -r -p "SendKey: " SERVERCHAN_SENDKEY
                validate_input "$SERVERCHAN_SENDKEY" "SendKey"
                [ -z "$SERVERCHAN_SENDKEY" ] && error "Server 酱 SendKey 不能为空"
                ;;
            5)
                channels+=("telegram")
                echo ""
                echo "---- Telegram Bot ----"
                read -r -p "Bot Token: " TELEGRAM_BOT_TOKEN
                validate_input "$TELEGRAM_BOT_TOKEN" "Bot Token"
                read -r -p "Chat ID: " TELEGRAM_CHAT_ID
                validate_input "$TELEGRAM_CHAT_ID" "Chat ID"
                [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ] && error "Telegram Bot Token 和 Chat ID 都不能为空"
                ;;
            *)
                warn "忽略无效选项: $choice"
                ;;
        esac
    done

    if [ "${#channels[@]}" -eq 0 ]; then
        error "至少要启用一个推送渠道"
    fi
    local NOTIFY_CHANNELS
    NOTIFY_CHANNELS="$(IFS=,; echo "${channels[*]}")"

    echo ""
    echo "=========================================="
    echo "  配置抓取接口"
    echo "=========================================="
    read -r -p "抓取 API 地址 [默认: http://localhost/api/douyin/web/fetch_user_post_videos]: " API_URL
    API_URL=${API_URL:-"http://localhost/api/douyin/web/fetch_user_post_videos"}
    validate_input "$API_URL" "API 地址"

    echo ""
    echo "=========================================="
    echo "  Web 状态面板（可选）"
    echo "=========================================="
    read -r -p "是否启用只读状态面板网页？(y/n) [默认: n]: " WEB_CHOICE
    local WEB_ENABLED="false" WEB_HOST="127.0.0.1" WEB_PORT="8787"
    if [ "$WEB_CHOICE" = "y" ] || [ "$WEB_CHOICE" = "Y" ]; then
        WEB_ENABLED="true"
        read -r -p "监听地址 [默认: 127.0.0.1，局域网访问可填 0.0.0.0]: " input
        [ -n "$input" ] && { validate_input "$input" "监听地址"; WEB_HOST="$input"; }
        read -r -p "监听端口 [默认: 8787]: " input
        [ -n "$input" ] && { validate_input "$input" "端口"; WEB_PORT="$input"; }
    fi

    cat > "$env_file" << EOF
# 由 deploy.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')

# 启用的推送渠道，逗号分隔
NOTIFY_CHANNELS=$NOTIFY_CHANNELS

# 钉钉群机器人
DINGTALK_TOKEN=$DINGTALK_TOKEN
DINGTALK_SECRET=$DINGTALK_SECRET
AT_MOBILES=$AT_MOBILES

# Bark
BARK_SERVER=$BARK_SERVER
BARK_DEVICE_KEY=$BARK_DEVICE_KEY

# 企业微信群机器人
WECOM_WEBHOOK_KEY=$WECOM_WEBHOOK_KEY

# Server 酱
SERVERCHAN_SENDKEY=$SERVERCHAN_SENDKEY

# Telegram Bot
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID

# 抓取接口配置
API_URL=$API_URL

# 过时检测阈值（秒，默认 7 天）
STALE_THRESHOLD=604800

# 单次抓取视频数
FETCH_COUNT=10

# 并发检查上限
MAX_CONCURRENT_USERS=5

# 每轮检测之间的随机等待区间（秒）
POLL_INTERVAL_MIN=15
POLL_INTERVAL_MAX=40

# 日志级别（DEBUG/INFO/WARNING/ERROR）
LOG_LEVEL=INFO

# Web 状态面板
WEB_ENABLED=$WEB_ENABLED
WEB_HOST=$WEB_HOST
WEB_PORT=$WEB_PORT
EOF

    chmod 600 "$env_file"
    ok ".env 配置完成（推送渠道: $NOTIFY_CHANNELS）"
}

# =================== install: 配置 users.conf ===================
configure_users() {
    local conf_file="$WORK_DIR/users.conf"

    if [ -f "$conf_file" ]; then
        local user_count
        user_count=$(grep -vc '^#' "$conf_file" | grep -vc '^$' || true)
        warn "users.conf 已存在，跳过配置（如需修改，编辑 $conf_file 或运行 ./deploy.sh config）"
        return
    fi

    echo ""
    echo "=========================================="
    echo "  配置要监控的抖音账号"
    echo "=========================================="
    echo ""
    echo "格式：sec_user_id|昵称，每行一个账号"
    echo "sec_user_id 获取方式：浏览器打开抖音网页版 -> F12 开发者工具 -> 用户主页接口请求参数里找 sec_user_id"
    echo ""
    echo "逐行输入，输入空行结束："
    echo ""

    cat > "$conf_file" << 'EOF'
# 格式：sec_user_id|昵称
# sec_user_id 是抖音账号的加密用户 ID（不是抖音号）
# 昵称仅用于通知展示
# 以 # 开头的行和空行会被忽略

EOF

    while true; do
        read -r -p "> " line
        [ -z "$line" ] && break
        echo "$line" >> "$conf_file"
    done

    local user_count
    user_count=$(grep -v '^#' "$conf_file" | grep -vc '^$' || true)
    if [ "$user_count" -eq 0 ]; then
        warn "未添加任何用户，可以稍后编辑 $conf_file"
    else
        ok "已添加 $user_count 个监控用户"
    fi
}

# =================== install: 测试运行 ===================
test_run() {
    echo ""
    read -r -p "是否先跑一轮测试确认配置正确？(y/n) [默认: y]: " DO_TEST
    if [ "$DO_TEST" = "n" ] || [ "$DO_TEST" = "N" ]; then
        return
    fi

    info "测试运行（单轮检测）..."
    cd "$WORK_DIR"
    # shellcheck disable=SC1091
    source venv/bin/activate
    python3 douyin_monitor.py --once || warn "测试运行过程中出现了错误，请检查上面的输出和 $WORK_DIR/log/debug/monitor.log"
    deactivate
}

# =================== systemd 服务管理 ===================
setup_systemd() {
    if [ "$SYSTEMD_AVAILABLE" != "true" ]; then
        info "当前环境没有 systemd，将使用 ./deploy.sh start 以 nohup 方式常驻"
        return
    fi
    if [ "$EUID" -ne 0 ]; then
        warn "配置 systemd 服务需要 root 权限，跳过（可以用 sudo 重新运行，或用 ./deploy.sh start 临时启动）"
        return
    fi

    echo ""
    if [ -f "$SERVICE_FILE" ]; then
        warn "systemd 服务已存在: $SERVICE_FILE"
        return
    fi

    read -r -p "是否配置 systemd 常驻服务？(y/n) [默认: y]: " SETUP_SVC
    if [ "$SETUP_SVC" = "n" ] || [ "$SETUP_SVC" = "N" ]; then
        info "跳过 systemd 配置，之后可以用 ./deploy.sh start 启动"
        return
    fi

    write_systemd_unit
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    ok "systemd 服务已启用（开机自启）"

    read -r -p "是否立即启动服务？(y/n) [默认: y]: " START_SVC
    if [ "$START_SVC" != "n" ] && [ "$START_SVC" != "N" ]; then
        systemctl start "$SERVICE_NAME"
        sleep 1
        systemctl status "$SERVICE_NAME" --no-pager || true
    fi
}

write_systemd_unit() {
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Douyin multi-user video monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
ExecStart=$WORK_DIR/venv/bin/python3 $WORK_DIR/douyin_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

service_is_running() {
    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        systemctl is-active --quiet "$SERVICE_NAME"
        return $?
    fi
    if [ -f "$WORK_DIR/monitor.pid" ]; then
        local pid
        pid="$(cat "$WORK_DIR/monitor.pid" 2>/dev/null)"
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
        return $?
    fi
    return 1
}

cmd_start() {
    require_existing_install start
    if service_is_running; then
        warn "已经在运行了"
        return
    fi
    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        systemctl start "$SERVICE_NAME"
        ok "已通过 systemd 启动"
    else
        cd "$WORK_DIR"
        nohup "$WORK_DIR/venv/bin/python3" "$WORK_DIR/douyin_monitor.py" \
            > "$WORK_DIR/nohup.log" 2>&1 &
        disown
        sleep 1
        if service_is_running; then
            ok "已在后台启动（nohup），日志见 $WORK_DIR/nohup.log"
        else
            error "启动失败，查看 $WORK_DIR/nohup.log 排查"
        fi
    fi
}

cmd_stop() {
    require_existing_install stop
    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        systemctl stop "$SERVICE_NAME"
        ok "已通过 systemd 停止"
        return
    fi
    if [ -f "$WORK_DIR/monitor.pid" ]; then
        local pid
        pid="$(cat "$WORK_DIR/monitor.pid" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid"
            info "已发送停止信号，等待进程优雅退出..."
            for _ in $(seq 1 15); do
                kill -0 "$pid" 2>/dev/null || { ok "已停止"; return; }
                sleep 1
            done
            warn "等待超时，进程可能仍在退出中，可稍后用 status 确认"
        else
            warn "没有找到正在运行的进程"
        fi
    else
        warn "没有找到 monitor.pid，可能本来就没在运行"
    fi
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    require_existing_install status
    echo ""
    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        systemctl status "$SERVICE_NAME" --no-pager || true
    else
        if service_is_running; then
            ok "监控进程正在运行 (PID $(cat "$WORK_DIR/monitor.pid" 2>/dev/null))"
        else
            warn "监控进程当前未运行"
        fi
    fi
    echo ""
    info "监控自身状态快照（--status）："
    cd "$WORK_DIR"
    # shellcheck disable=SC1091
    source venv/bin/activate
    python3 douyin_monitor.py --status || true
    deactivate
}

cmd_logs() {
    require_existing_install logs
    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        journalctl -u "$SERVICE_NAME" -f
    elif [ -f "$WORK_DIR/log/info/monitor.log" ]; then
        tail -f "$WORK_DIR/log/info/monitor.log"
    else
        error "没有找到日志文件，服务可能还没启动过"
    fi
}

# =================== install ===================
cmd_install() {
    echo ""
    echo "=========================================="
    echo "  抖音多用户视频监控 - 一键部署"
    echo "=========================================="
    echo ""

    require_root_for_install
    configure_workdir

    # 如果脚本所在目录有代码文件，直接用本地的；否则从 GitHub 下载
    if [ -f "$SCRIPT_DIR/douyin_monitor.py" ]; then
        sync_code_files
    else
        fetch_code_fresh
    fi

    save_workdir_marker
    setup_venv
    configure_env
    configure_users
    test_run
    setup_systemd

    echo ""
    echo "=========================================="
    echo "  部署完成！"
    echo "=========================================="
    echo ""
    info "工作目录：$WORK_DIR"
    info "常用命令："
    info "  ./deploy.sh status    查看运行状态"
    info "  ./deploy.sh logs      实时查看日志"
    info "  ./deploy.sh restart   重启服务"
    info "  ./deploy.sh update    以后有新版本时用这个更新"
    info "  ./deploy.sh config    新增推送渠道 / 修改配置"
    echo ""
}

# =================== update ===================
cmd_update() {
    echo ""
    echo "=========================================="
    echo "  更新抖音监控"
    echo "=========================================="
    echo ""

    check_environment
    require_existing_install update
    ok "找到已安装实例: $WORK_DIR"

    local was_running=false
    if service_is_running; then
        was_running=true
        info "检测到服务正在运行，更新完成后会自动重启"
        cmd_stop
    fi

    # 更新前备份旧代码，防止更新出问题需要回滚
    local backup_dir
    backup_dir="$WORK_DIR/.backup_$(date '+%Y%m%d_%H%M%S')"
    if [ -d "$WORK_DIR/douyin_monitor" ]; then
        mkdir -p "$backup_dir"
        cp -r "$WORK_DIR/douyin_monitor" "$backup_dir/" 2>/dev/null || true
        cp "$WORK_DIR/douyin_monitor.py" "$backup_dir/" 2>/dev/null || true
        info "已备份旧代码到 $backup_dir（确认无误后可自行删除）"
    fi

    # 代码来源优先级：如果是从一个真实存在代码、且和 WORK_DIR 不是同一个目录的
    # 本地目录运行的（比如在开发中的仓库里执行 ./deploy.sh update --dir /opt/xxx），
    # 就用本地代码，方便本地改完直接测、也不会把 fork 出来的自定义代码覆盖掉；
    # 否则（比如 curl 一键部署、或者就在 WORK_DIR 自己里面装的 deploy.sh 自我更新）
    # 才从 GitHub 拉取最新代码。
    if [ -f "$SCRIPT_DIR/douyin_monitor.py" ] && [ "$(cd "$SCRIPT_DIR" && pwd)" != "$(cd "$WORK_DIR" && pwd)" ]; then
        sync_code_files
    else
        # 从 GitHub 拉取最新代码（只覆盖代码文件，.env/users.conf/state/log/venv 都不动）
        fetch_code_update
    fi

    setup_venv
    sync_env_defaults

    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        # WorkingDirectory / ExecStart 路径可能因为 --dir 变化，保险起见重写一次
        write_systemd_unit
        systemctl daemon-reload
    fi

    if [ "$was_running" = "true" ]; then
        cmd_start
    fi

    echo ""
    ok "更新完成"
    info "如果更新后运行异常，可以用 $backup_dir 里的旧代码手动回滚"
}

# =================== uninstall ===================
cmd_uninstall() {
    echo ""
    echo "=========================================="
    echo "  卸载抖音监控"
    echo "=========================================="
    echo ""

    require_existing_install uninstall
    warn "即将卸载安装于: $WORK_DIR"

    if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ -f "$SERVICE_FILE" ]; then
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
        ok "已停止并移除 systemd 服务"
    else
        cmd_stop 2>/dev/null || true
    fi

    read -r -p "是否在删除前备份 .env / users.conf / state 到 /tmp？(y/n) [默认: y]: " DO_BACKUP
    if [ "$DO_BACKUP" != "n" ] && [ "$DO_BACKUP" != "N" ]; then
        local backup_file
        backup_file="/tmp/douyin-monitor-backup-$(date '+%Y%m%d_%H%M%S').tar.gz"
        local backup_items=()
        for item in .env users.conf state; do
            [ -e "$WORK_DIR/$item" ] && backup_items+=("$item")
        done
        if [ "${#backup_items[@]}" -gt 0 ]; then
            tar -czf "$backup_file" -C "$WORK_DIR" "${backup_items[@]}" 2>/dev/null || true
            [ -f "$backup_file" ] && ok "已备份到 $backup_file"
        else
            info "没有找到可备份的配置文件"
        fi
    fi

    read -r -p "是否删除整个工作目录 $WORK_DIR？(y/n) [默认: n]: " DO_REMOVE
    if [ "$DO_REMOVE" = "y" ] || [ "$DO_REMOVE" = "Y" ]; then
        rm -rf "$WORK_DIR"
        ok "已删除工作目录"
    else
        info "工作目录已保留: $WORK_DIR"
    fi

    rm -f "$MARKER_FILE"
    ok "卸载完成"
}

# =================== config：重新走一遍配置向导 ===================
cmd_config() {
    require_existing_install config
    info "工作目录: $WORK_DIR"

    read -r -p "重新配置推送渠道？会覆盖 .env（原文件先备份）(y/n) [默认: n]: " RE_ENV
    if [ "$RE_ENV" = "y" ] || [ "$RE_ENV" = "Y" ]; then
        [ -f "$WORK_DIR/.env" ] && cp "$WORK_DIR/.env" "$WORK_DIR/.env.bak_$(date '+%Y%m%d_%H%M%S')"
        rm -f "$WORK_DIR/.env"
        configure_env
    fi

    read -r -p "重新编辑监控账号列表？(y/n) [默认: n]: " RE_USERS
    if [ "$RE_USERS" = "y" ] || [ "$RE_USERS" = "Y" ]; then
        "${EDITOR:-vi}" "$WORK_DIR/users.conf"
    fi

    if service_is_running; then
        read -r -p "配置已更新，是否重启服务使其生效？(y/n) [默认: y]: " DO_RESTART
        [ "$DO_RESTART" != "n" ] && [ "$DO_RESTART" != "N" ] && cmd_restart
    fi
}

# =================== 帮助 & 交互菜单 ===================
show_help() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

interactive_menu() {
    echo ""
    echo "=========================================="
    echo "  抖音多用户视频监控 - 部署与运维"
    echo "=========================================="
    echo ""
    echo "  1) 安装 (install)"
    echo "  2) 更新 (update)"
    echo "  3) 启动 (start)"
    echo "  4) 停止 (stop)"
    echo "  5) 重启 (restart)"
    echo "  6) 状态 (status)"
    echo "  7) 日志 (logs)"
    echo "  8) 重新配置 (config)"
    echo "  9) 卸载 (uninstall)"
    echo "  0) 退出"
    echo ""
    read -r -p "请选择: " choice
    case "$choice" in
        1) cmd_install ;;
        2) cmd_update ;;
        3) cmd_start ;;
        4) cmd_stop ;;
        5) cmd_restart ;;
        6) cmd_status ;;
        7) cmd_logs ;;
        8) cmd_config ;;
        9) cmd_uninstall ;;
        0) exit 0 ;;
        *) error "无效选择" ;;
    esac
}

# =================== 参数解析 & 入口 ===================
main() {
    local cmd="${1:-}"
    [ $# -gt 0 ] && shift || true

    while [ $# -gt 0 ]; do
        case "$1" in
            --dir)
                OPT_DIR="$2"
                shift 2
                ;;
            *)
                warn "忽略未知参数: $1"
                shift
                ;;
        esac
    done

    case "$cmd" in
        install)    check_environment; require_root_for_install; cmd_install ;;
        update)     cmd_update ;;
        uninstall)  check_environment; cmd_uninstall ;;
        start)      check_environment; cmd_start ;;
        stop)       check_environment; cmd_stop ;;
        restart)    check_environment; cmd_restart ;;
        status)     check_environment; cmd_status ;;
        logs)       check_environment; cmd_logs ;;
        config)     check_environment; cmd_config ;;
        help|-h|--help) show_help ;;
        "")         check_environment; interactive_menu ;;
        *)          error "未知命令: $cmd（可用命令见 ./deploy.sh help）" ;;
    esac
}

main "$@"
