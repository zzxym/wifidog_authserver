#!/bin/bash
set -e

echo "=========================================="
echo "  WiFiDog AuthServer 启动器"
echo "=========================================="
echo ""

# 检查 Redis 是否运行
echo "[1/3] 检查 Redis 连接..."
REDIS_STARTED=false

if redis-cli ping > /dev/null 2>&1; then
    echo "✅ Redis 已运行"
    REDIS_STARTED=true
fi

# Docker 启动 Redis
if [ "$REDIS_STARTED" = false ]; then
    echo "🐳 使用 Docker 启动 Redis..."
    if ! command -v docker &> /dev/null; then
        echo "❌ 未安装 Docker，请先安装: curl -fsSL https://get.docker.com | sh"
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
        echo "✅ Redis Docker 版已启动"
        REDIS_STARTED=true
    else
        echo "❌ Docker Redis 启动失败，请检查 Docker 状态"
        exit 1
    fi
fi

echo ""
echo "[2/3] 检查配置文件..."
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 配置文件"
    echo "   正在从 .env.example 创建..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "   ✅ 已创建 .env，请编辑后重新运行此脚本"
        echo "   重点修改: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, ADMIN_TOKEN"
        exit 0
    else
        echo "❌ 未找到 .env.example，请重新下载完整包"
        exit 1
    fi
else
    echo "✅ 配置文件存在"
fi

echo ""
echo "[3/3] 启动 AuthServer (Gunicorn)..."
echo ""
gunicorn -w 4 -b 0.0.0.0:5000 app:app
