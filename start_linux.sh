#!/bin/bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/authserver.pid"
LOG_DIR="$SCRIPT_DIR/log"

# ── 解析参数 ──
RUN_MODE="foreground"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--daemon) RUN_MODE="daemon" ;;
        stop)
            # ── 停止模式 ──
            echo "=========================================="
            echo "  停止 WiFiDog AuthServer"
            echo "=========================================="
            echo ""
            echo "[1/2] 停止 AuthServer..."
            if [ -f "$PID_FILE" ]; then
                PID=$(cat "$PID_FILE")
                if kill -0 "$PID" 2>/dev/null; then
                    kill "$PID"
                    sleep 2
                    if kill -0 "$PID" 2>/dev/null; then
                        echo "  强制终止..."
                        kill -9 "$PID" 2>/dev/null
                    fi
                    echo "   ✅ AuthServer 已停止 (PID: $PID)"
                else
                    echo "   ℹ️  PID $PID 不在运行"
                fi
                rm -f "$PID_FILE"
            else
                # 无 PID 文件，按进程名终止
                echo "   按进程名查找并终止..."
                pkill -f "gunicorn.*app:app" 2>/dev/null && echo "   ✅ 已终止 gunicorn 进程" || echo "   ℹ️  未找到 gunicorn 进程"
            fi
            echo ""
            echo "[2/2] 停止 Redis (Docker)..."
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^wifidog-redis$'; then
                docker stop wifidog-redis
                echo "   ✅ wifidog-redis 已停止"
            else
                echo "   ℹ️  wifidog-redis 未在运行"
            fi
            echo ""
            echo "=========================================="
            echo "  ✅ 所有服务已停止"
            echo "=========================================="
            exit 0
            ;;
        *) echo "用法: $0 [-d|--daemon] [stop]"; exit 1 ;;
    esac
    shift
done

# ── 启动流程 ──
if [ "$RUN_MODE" = "daemon" ]; then
    echo "=========================================="
    echo "  WiFiDog AuthServer 启动器（后台模式）"
    echo "=========================================="
else
    echo "=========================================="
    echo "  WiFiDog AuthServer 启动器"
    echo "=========================================="
fi
echo ""

# 检查 Redis 是否运行
echo "[1/3] 检查 Redis 连接..."
REDIS_STARTED=false

if redis-cli ping > /dev/null 2>&1; then
    echo "   ✅ Redis 已运行"
    REDIS_STARTED=true
fi

# Docker 启动 Redis
if [ "$REDIS_STARTED" = false ]; then
    echo "   🐳 使用 Docker 启动 Redis..."
    if ! command -v docker &> /dev/null; then
        echo "   ❌ 未安装 Docker，请先安装: curl -fsSL https://get.docker.com | sh"
        exit 1
    fi
    if docker ps -a --format '{{.Names}}' | grep -q '^wifidog-redis$'; then
        docker start wifidog-redis 2>/dev/null
    else
        docker run -d \
            --name wifidog-redis \
            --restart unless-stopped \
            -p 127.0.0.1:6379:6379 \
            -v wifidog_redis_data:/data \
            redis:7-alpine redis-server --appendonly yes
    fi
    sleep 2
    if redis-cli ping > /dev/null 2>&1; then
        echo "   ✅ Redis Docker 版已启动"
        REDIS_STARTED=true
    else
        echo "   ❌ Docker Redis 启动失败，请检查 Docker 状态"
        exit 1
    fi
fi

echo ""
echo "[2/3] 检查配置文件..."
if [ ! -f ".env" ]; then
    echo "   ⚠️  未找到 .env 配置文件"
    echo "      正在从 .env.template 创建..."
    if [ -f ".env.template" ]; then
        cp .env.template .env
        echo "      ✅ 已创建 .env，请编辑后重新运行此脚本"
        echo "      重点修改: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, ADMIN_TOKEN"
        exit 0
    else
        echo "   ❌ 未找到 .env.template，请重新下载完整包"
        exit 1
    fi
else
    echo "   ✅ 配置文件存在"
fi

# 确保日志目录存在
mkdir -p "$LOG_DIR"

echo ""
echo "[3/3] 启动 AuthServer..."
echo "   日志目录: $LOG_DIR/"
echo ""

if [ "$RUN_MODE" = "daemon" ]; then
    echo "   后台启动中..."
    nohup gunicorn -w 4 -b 0.0.0.0:5000 app:app > "$LOG_DIR/authserver.log" 2>&1 &
    GPID=$!
    echo $GPID > "$PID_FILE"
    sleep 2
    if kill -0 "$GPID" 2>/dev/null; then
        echo ""
        echo "=========================================="
        echo "  ✅ AuthServer 已后台启动"
        echo "  PID: $GPID  (记录在 $PID_FILE)"
        echo "  管理后台: http://127.0.0.1:5000/admin"
        echo "  停止服务: $0 stop"
        echo "  查看日志: tail -f $LOG_DIR/authserver.log"
        echo "=========================================="
    else
        rm -f "$PID_FILE"
        echo "  ❌ 启动失败，请查看日志: $LOG_DIR/authserver.log"
        exit 1
    fi
else
    echo "   前台运行中（Ctrl+C 停止）..."
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
fi
