#!/bin/bash
set -e

echo "=========================================="
echo "  WiFiDog AuthServer 启动器"
echo "=========================================="
echo ""

# 检查 Redis 是否运行
echo "[1/3] 检查 Redis 连接..."
if ! redis-cli ping > /dev/null 2>&1; then
    echo "⚠️  Redis 未运行！"
    echo ""
    echo "请先启动 Redis:"
    echo "  - Linux: sudo systemctl start redis"
    echo "  - Docker: docker run -d -p 6379:6379 redis:alpine"
    echo ""
    exit 1
else
    echo "✅ Redis 已运行"
fi

echo ""
echo "[2/3] 检查配置文件..."
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 配置文件"
    echo "   正在从 .env.template 创建..."
    if [ -f ".env.template" ]; then
        cp .env.template .env
        echo "   ✅ 已创建 .env，请编辑后重新运行此脚本"
        echo "   重点修改: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, ADMIN_TOKEN"
        exit 0
    else
        echo "❌ 未找到 .env.template，请重新下载完整包"
        exit 1
    fi
else
    echo "✅ 配置文件存在"
fi

echo ""
echo "[3/3] 启动 AuthServer (Gunicorn)..."
echo ""
gunicorn -w 4 -b 0.0.0.0:5000 app:app
