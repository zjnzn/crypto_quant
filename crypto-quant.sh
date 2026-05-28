#!/bin/bash
# Crypto Quant 量化交易系统管理脚本
# 用法: ./crypto-quant.sh {start|stop|restart|status|logs|backtest} [选项]

# ==================== 配置区域 ====================

APP_NAME="crypto-quant"

# Python 解释器（优先使用虚拟环境）
APP_HOME=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$APP_HOME/.venv"
if [ -d "$VENV_DIR" ]; then
    PYTHON="$VENV_DIR/bin/python"
else
    PYTHON=$(command -v python3 || command -v python)
fi

# 运行模式: live | paper | dry-run | backtest
RUN_MODE="dry-run"

# 配置文件
CONFIG_FILE="$APP_HOME/config_live.yaml"

# 交易标的（空格分隔）
INSTRUMENTS="BTCUSDT"

# 回测默认参数
BACKTEST_BARS=300
BACKTEST_INIT=10000

# 日志目录
LOG_DIR="$APP_HOME/logs"
LOG_FILE="$LOG_DIR/crypto-quant.log"
PID_FILE="$APP_HOME/$APP_NAME.pid"

# ==================== 工具函数 ====================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_python() {
    if [ -z "$PYTHON" ] || ! command -v "$PYTHON" &>/dev/null; then
        print_error "Python 未找到，请安装 Python 3.10+ 或创建虚拟环境"
        print_info "  python -m venv .venv && source .venv/bin/activate"
        exit 1
    fi
}

check_deps() {
    if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
        print_error "缺少依赖: pyyaml"
        print_info "  pip install pyyaml"
        exit 1
    fi
}

check_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        print_error "配置文件不存在: $CONFIG_FILE"
        exit 1
    fi
}

create_log_dir() {
    if [ ! -d "$LOG_DIR" ]; then
        mkdir -p "$LOG_DIR"
        print_info "创建日志目录: $LOG_DIR"
    fi
}

get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    fi
}

is_running() {
    local pid=$(get_pid)
    if [ -n "$pid" ]; then
        ps -p "$pid" > /dev/null 2>&1
        return $?
    fi
    return 1
}

# ==================== 核心功能 ====================

# 启动实盘/模拟交易
start() {
    local mode="${1:-$RUN_MODE}"

    print_info "正在启动 $APP_NAME (模式: $mode) ..."

    if is_running; then
        local pid=$(get_pid)
        print_warning "$APP_NAME 已经在运行 (PID: $pid)"
        return 0
    fi

    check_python
    check_deps
    check_config
    create_log_dir

    # 根据模式构建命令参数
    local args="--config $CONFIG_FILE"
    case "$mode" in
        live)
            args="$args"
            ;;
        paper)
            args="$args --paper"
            ;;
        dry-run)
            args="$args --dry-run"
            ;;
        *)
            print_error "未知模式: $mode (可选: live | paper | dry-run)"
            exit 1
            ;;
    esac

    # 附加交易标的
    if [ -n "$INSTRUMENTS" ]; then
        args="$args --instruments $INSTRUMENTS"
    fi

    cd "$APP_HOME"
    nohup "$PYTHON" run_live.py $args >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo $pid > "$PID_FILE"

    sleep 3

    if is_running; then
        print_success "$APP_NAME 启动成功! (PID: $pid, 模式: $mode)"
        print_info "日志文件: $LOG_FILE"
        print_info "使用 '$0 logs' 查看实时日志"
    else
        print_error "$APP_NAME 启动失败!"
        print_info "查看日志: tail -50 $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# 停止服务
stop() {
    print_info "正在停止 $APP_NAME ..."

    if ! is_running; then
        print_warning "$APP_NAME 未运行"
        rm -f "$PID_FILE"
        return 0
    fi

    local pid=$(get_pid)
    print_info "发送 TERM 信号到进程 $pid ..."

    kill -TERM $pid 2>/dev/null

    local count=0
    while is_running && [ $count -lt 30 ]; do
        sleep 1
        count=$((count + 1))
        echo -n "."
    done
    echo ""

    if is_running; then
        print_warning "进程未响应 TERM 信号，使用 KILL 信号强制停止..."
        kill -KILL $pid 2>/dev/null
        sleep 2

        if is_running; then
            print_error "无法停止进程 $pid"
            exit 1
        fi
    fi

    rm -f "$PID_FILE"
    print_success "$APP_NAME 已停止"
}

# 重启
restart() {
    local mode="${1:-$RUN_MODE}"
    print_info "正在重启 $APP_NAME ..."
    stop
    sleep 2
    start "$mode"
}

# 查看状态
status() {
    echo "=========================================="
    echo "  Crypto Quant 量化交易系统"
    echo "=========================================="
    echo ""

    if is_running; then
        local pid=$(get_pid)
        echo -e "状态: ${GREEN}运行中${NC}"
        echo "PID: $pid"
        echo "模式: $RUN_MODE"
        echo "配置: $CONFIG_FILE"

        local mem_info=$(ps -p $pid -o rss,vsz --no-headers 2>/dev/null)
        if [ -n "$mem_info" ]; then
            local rss=$(echo $mem_info | awk '{printf "%.1f", $1/1024}')
            local vsz=$(echo $mem_info | awk '{printf "%.1f", $2/1024}')
            echo "内存: ${rss}M (物理) / ${vsz}M (虚拟)"
        fi

        local start_time=$(ps -p $pid -o lstart --no-headers 2>/dev/null)
        if [ -n "$start_time" ]; then
            echo "启动时间: $start_time"
        fi

        echo ""
        echo "端口监听:"
        ss -tlnp 2>/dev/null | grep "pid=$pid" | awk '{print "  " $5}' || echo "  (无)"
    else
        echo -e "状态: ${RED}未运行${NC}"
        rm -f "$PID_FILE"
    fi

    echo ""
    echo "Python:  $PYTHON"
    echo "日志:    $LOG_FILE"
    echo "标的:    $INSTRUMENTS"
    echo ""
}

# 运行回测
backtest() {
    check_python
    check_deps

    local bars="${1:-$BACKTEST_BARS}"
    local init="${2:-$BACKTEST_INIT}"

    print_info "运行回测 (K线数: $bars, 初始资金: $init USDT) ..."
    cd "$APP_HOME"
    "$PYTHON" run_backtest.py
}

# 查看日志
logs() {
    if [ ! -f "$LOG_FILE" ]; then
        print_error "日志文件不存在: $LOG_FILE"
        return 1
    fi

    case "$2" in
        grep)
            if [ -z "$3" ]; then
                print_error "请指定搜索关键词"
                echo "用法: $0 logs grep <关键词>"
                return 1
            fi
            grep -i "$3" "$LOG_FILE" --color=auto
            ;;
        less)
            less "$LOG_FILE"
            ;;
        error|err)
            grep -i "error\|exception\|traceback" "$LOG_FILE" --color=auto | tail -50
            ;;
        *)
            local lines=${2:-100}
            print_info "持续输出日志 (最近 $lines 行开始，Ctrl+C 退出)..."
            tail -n "$lines" -f "$LOG_FILE"
            ;;
    esac
}

# 显示帮助
show_help() {
    echo "Crypto Quant 量化交易系统管理脚本"
    echo ""
    echo "用法: $0 {start|stop|restart|status|logs|backtest} [选项]"
    echo ""
    echo "命令:"
    echo "  start [模式]     启动交易服务 (默认: dry-run)"
    echo "  stop             停止交易服务"
    echo "  restart [模式]   重启交易服务"
    echo "  status           查看服务状态"
    echo "  backtest         运行回测"
    echo "  logs [选项]      查看日志 (默认显示最近 100 行)"
    echo ""
    echo "运行模式:"
    echo "  live       Binance 主网 (需主网 API Key)"
    echo "  paper      Binance 测试网 (需 testnet API Key)"
    echo "  dry-run    PaperExchange 模拟 (默认，不下真实订单)"
    echo ""
    echo "日志选项:"
    echo "  $0 logs              实时查看日志 (最近 100 行)"
    echo "  $0 logs less         使用 less 分页查看"
    echo "  $0 logs grep <词>    搜索日志"
    echo "  $0 logs error        查看最近错误"
    echo "  $0 logs 200          显示最近 200 行"
    echo ""
    echo "示例:"
    echo "  $0 start             # 默认 dry-run 模式启动"
    echo "  $0 start paper       # 测试网模式启动"
    echo "  $0 start live        # 主网模式启动"
    echo "  $0 backtest          # 运行回测"
    echo "  $0 logs grep ERROR   # 搜索错误日志"
    echo ""
    echo "系统架构:"
    echo "  策略引擎: 动量 + 均值回归 + 资金费率套利"
    echo "  风控管道: 最低名义值 → 仓位限制 → 杠杆限制 → 回撤熔断 → 资金费率"
    echo "  交易所:   Binance Futures (支持 testnet)"
    echo "  行情源:   WebSocket 实时 / CSV 回测"
    echo ""
}

# ==================== 主程序 ====================

if [ $# -eq 0 ]; then
    show_help
    exit 1
fi

case "$1" in
    start)
        start "$2"
        ;;
    stop)
        stop
        ;;
    restart)
        restart "$2"
        ;;
    status)
        status
        ;;
    backtest|bt)
        backtest "$2" "$3"
        ;;
    logs)
        logs "$1" "$2" "$3"
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "未知命令: $1"
        echo ""
        show_help
        exit 1
        ;;
esac

exit 0
