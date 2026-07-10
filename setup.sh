#!/bin/bash
# ====================================
# 抖音多用户视频监控脚本 - 一键安装部署
# ====================================
#
# 用法：
#   chmod +x setup.sh
#   sudo ./setup.sh
#
# 功能：
#   1. 检查运行环境（Python 版本、系统类型）
#   2. 创建工作目录并复制必要文件
#   3. 创建虚拟环境并安装依赖
#   4. 交互式配置 .env 和 users.conf（首次运行时）
#   5. 测试运行一次确认配置正确
#   6. 可选：配置 systemd 常驻服务
#
# 已有配置不会被覆盖（.env、users.conf）

set -e

# =================== 安全设置 ===================
# 禁用通配符展开，防止路径遍历攻击
set -f
# 检查脚本是否被符号链接调用
SCRIPT_REAL_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"

# =================== 清理函数 ===================
cleanup() {
    # 脚本退出时的清理工作（如有临时文件）
    :
}
trap cleanup EXIT

# =================== 颜色定义 ===================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =================== 工具函数 ===================
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 输入验证：只允许安全字符
validate_input() {
    local input="$1"
    local name="$2"
    # 只允许字母、数字、下划线、连字符、点、斜杠、冒号、等号、@、逗号、空格
    if [[ ! "$input" =~ ^[a-zA-Z0-9_./:=@,-]+$ ]]; then
        error "$name 包含非法字符，请重新输入（只允许字母、数字、下划线、连字符、逗号等）"
    fi
}

# =================== 配置变量 ===================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_WORK_DIR="/mnt/douyin-monitor"
SERVICE_NAME="douyin-monitor"

# =================== 步骤 1：检查运行环境 ===================
check_environment() {
    info "检查运行环境..."

    # 检查是否为 root 或有 sudo 权限
    if [ "$EUID" -ne 0 ]; then
        error "请使用 root 或 sudo 运行此脚本"
    fi

    # 检查操作系统
    if [ "$(uname)" != "Linux" ]; then
        error "此脚本仅支持 Linux 系统"
    fi

    # 检查 Python 版本
    if ! command -v python3 &> /dev/null; then
        error "未找到 python3，请先安装 Python 3.8+"
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

    if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]); then
        error "Python 版本过低（当前 $PYTHON_VERSION），需要 3.8+"
    fi

    ok "Python $PYTHON_VERSION"
}

# =================== 步骤 2：创建工作目录 ===================
setup_workdir() {
    info "创建工作目录: $DEFAULT_WORK_DIR"

    mkdir -p "$DEFAULT_WORK_DIR"

    # 复制脚本文件
    info "复制脚本文件..."
    cp "$SCRIPT_DIR/douyin_monitor.py" "$DEFAULT_WORK_DIR/"
    cp -r "$SCRIPT_DIR/douyin_monitor" "$DEFAULT_WORK_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$DEFAULT_WORK_DIR/"
    cp "$SCRIPT_DIR/.env.example" "$DEFAULT_WORK_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/users.conf.example" "$DEFAULT_WORK_DIR/" 2>/dev/null || true

    ok "工作目录准备完成"
}

# =================== 步骤 3：创建虚拟环境 ===================
setup_venv() {
    info "创建虚拟环境..."

    cd "$DEFAULT_WORK_DIR"

    if [ ! -d "venv" ]; then
        python3 -m venv venv
        ok "虚拟环境创建完成"
    else
        warn "虚拟环境已存在，跳过创建"
    fi

    info "安装依赖..."
    source venv/bin/activate
    pip install -q -r requirements.txt
    ok "依赖安装完成"
}

# =================== 步骤 4：配置 .env ===================
configure_env() {
    info "配置环境变量..."

    ENV_FILE="$DEFAULT_WORK_DIR/.env"

    if [ -f "$ENV_FILE" ]; then
        warn ".env 文件已存在，跳过配置"
        warn "如需修改，请编辑 $ENV_FILE"
        return
    fi

    echo ""
    echo "=========================================="
    echo "  请填写钉钉机器人配置"
    echo "=========================================="
    echo ""

    read -p "钉钉机器人 access_token: " DINGTALK_TOKEN
    if [ -z "$DINGTALK_TOKEN" ]; then
        error "access_token 不能为空"
    fi
    validate_input "$DINGTALK_TOKEN" "access_token"

    read -p "钉钉机器人加签密钥 (SEC开头): " DINGTALK_SECRET
    if [ -z "$DINGTALK_SECRET" ]; then
        error "加签密钥不能为空"
    fi
    validate_input "$DINGTALK_SECRET" "加签密钥"

    if [[ ! "$DINGTALK_SECRET" =~ ^SEC ]]; then
        warn "加签密钥通常以 SEC 开头，请确认是否正确"
    fi

    read -p "告警时 @ 的手机号 (多个用逗号分隔，留空跳过): " AT_MOBILES
    if [ -n "$AT_MOBILES" ]; then
        validate_input "$AT_MOBILES" "手机号"
    fi

    read -p "抓取 API 地址 (默认: http://localhost/api/douyin/web/fetch_user_post_videos): " API_URL
    API_URL=${API_URL:-"http://localhost/api/douyin/web/fetch_user_post_videos"}
    validate_input "$API_URL" "API 地址"

    cat > "$ENV_FILE" << EOF
# 钉钉机器人配置
DINGTALK_TOKEN=$DINGTALK_TOKEN
DINGTALK_SECRET=$DINGTALK_SECRET
AT_MOBILES=$AT_MOBILES

# 抓取接口配置
API_URL=$API_URL

# 过时检测阈值（秒，默认 7 天）
STALE_THRESHOLD=604800

# 单次抓取视频数
FETCH_COUNT=10

# 并发检查上限
MAX_CONCURRENT_USERS=5

# 日志级别（DEBUG/INFO/WARNING/ERROR）
LOG_LEVEL=INFO
EOF

    chmod 600 "$ENV_FILE"
    ok ".env 配置完成"
}

# =================== 步骤 5：配置 users.conf ===================
configure_users() {
    info "配置监控用户列表..."

    CONF_FILE="$DEFAULT_WORK_DIR/users.conf"

    if [ -f "$CONF_FILE" ]; then
        USER_COUNT=$(grep -v '^#' "$CONF_FILE" | grep -v '^$' | wc -l)
        warn "users.conf 已存在，包含 $USER_COUNT 个用户"
        warn "如需修改，请编辑 $CONF_FILE"
        return
    fi

    echo ""
    echo "=========================================="
    echo "  配置要监控的抖音账号"
    echo "=========================================="
    echo ""
    echo "格式：sec_user_id|昵称"
    echo "每行一个账号，# 开头为注释"
    echo ""
    echo "sec_user_id 获取方式："
    echo "  1. 浏览器打开抖音网页版"
    echo "  2. F12 打开开发者工具"
    echo "  3. 找到用户主页的 API 请求"
    echo "  4. 从请求参数或 URL 中找到 sec_user_id"
    echo ""
    echo "输入完毕后按 Ctrl+D 结束输入"
    echo ""

    cat > "$CONF_FILE" << 'EOF'
# 格式：sec_user_id|昵称
# sec_user_id 是抖音账号的加密用户 ID（不是抖音号）
# 昵称仅用于通知展示
# 以 # 开头的行和空行会被忽略

EOF

    # 等待用户输入
    while IFS= read -r line; do
        echo "$line" >> "$CONF_FILE"
    done

    USER_COUNT=$(grep -v '^#' "$CONF_FILE" | grep -v '^$' | wc -l)
    if [ "$USER_COUNT" -eq 0 ]; then
        warn "未添加任何用户，你可以稍后编辑 $CONF_FILE"
    else
        ok "已添加 $USER_COUNT 个监控用户"
    fi
}

# =================== 步骤 6：测试运行 ===================
test_run() {
    info "测试运行（单轮检测）..."

    cd "$DEFAULT_WORK_DIR"
    source venv/bin/activate

    echo ""
    echo "=========================================="
    echo "  开始测试运行"
    echo "=========================================="
    echo ""

    python3 douyin_monitor.py --once

    echo ""
    echo "=========================================="
    echo "  测试运行完成"
    echo "=========================================="
    echo ""

    read -p "测试是否成功？(y/n): " TEST_OK
    if [ "$TEST_OK" != "y" ] && [ "$TEST_OK" != "Y" ]; then
        warn "请检查配置后重新运行此脚本"
        warn "常见问题："
        warn "  - 钉钉收不到通知：检查 token 和 secret"
        warn "  - API 请求失败：检查 API_URL 和 Cookie 是否过期"
        warn "  - 查看日志：$DEFAULT_WORK_DIR/log/debug/monitor.log"
    fi
}

# =================== 步骤 7：配置 systemd ===================
setup_systemd() {
    info "配置 systemd 服务..."

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    if [ -f "$SERVICE_FILE" ]; then
        warn "systemd 服务已存在，跳过配置"
        warn "如需重新配置，请先运行：systemctl disable --now $SERVICE_NAME"
        return
    fi

    read -p "是否配置 systemd 常驻服务？(y/n): " SETUP_SVC
    if [ "$SETUP_SVC" != "y" ] && [ "$SETUP_SVC" != "Y" ]; then
        info "跳过 systemd 配置"
        info "手动启动命令：cd $DEFAULT_WORK_DIR && source venv/bin/activate && python3 douyin_monitor.py"
        return
    fi

    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Douyin multi-user video monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=$DEFAULT_WORK_DIR
ExecStart=$DEFAULT_WORK_DIR/venv/bin/python3 $DEFAULT_WORK_DIR/douyin_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"

    echo ""
    read -p "是否立即启动服务？(y/n): " START_SVC
    if [ "$START_SVC" = "y" ] || [ "$START_SVC" = "Y" ]; then
        systemctl start "$SERVICE_NAME"
        ok "服务已启动"
        systemctl status "$SERVICE_NAME" --no-pager
    else
        info "启动命令：systemctl start $SERVICE_NAME"
    fi

    echo ""
    info "常用命令："
    info "  查看状态：systemctl status $SERVICE_NAME"
    info "  查看日志：journalctl -u $SERVICE_NAME -f"
    info "  停止服务：systemctl stop $SERVICE_NAME"
    info "  重启服务：systemctl restart $SERVICE_NAME"
}

# =================== 主流程 ===================
main() {
    echo ""
    echo "=========================================="
    echo "  抖音多用户视频监控脚本 - 一键部署"
    echo "=========================================="
    echo ""

    check_environment
    setup_workdir
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
    info "工作目录：$DEFAULT_WORK_DIR"
    info "查看状态：cd $DEFAULT_WORK_DIR && source venv/bin/activate && python3 douyin_monitor.py --status"
    info "查看日志：tail -f $DEFAULT_WORK_DIR/log/info/monitor.log"
    echo ""
}

main "$@"
