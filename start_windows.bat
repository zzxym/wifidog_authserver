@echo off
chcp 65001 > nul
echo ==========================================
echo  WiFiDog AuthServer 启动器
echo ==========================================
echo.

REM 检查 Redis 是否运行
echo [1/3] 检查 Redis 连接...
redis-cli ping > nul 2>&1
if %errorlevel% neq 0 (
    echo ⚠️  Redis 未运行！
    echo.
    echo 请先启动 Redis:
    echo   - Windows: 下载安装 Redis 或从 Docker 运行
    echo   - Docker: docker run -d -p 6379:6379 redis:alpine
    echo.
    pause
    exit /b 1
) else (
    echo ✅ Redis 已运行
)

echo.
echo [2/3] 检查配置文件...
if not exist ".env" (
    echo ⚠️  未找到 .env 配置文件
    echo    正在从 .env.template 创建...
    if exist ".env.template" (
        copy ".env.template" ".env" > nul
        echo    ✅ 已创建 .env，请编辑后重新运行此脚本
        echo    重点修改: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, ADMIN_TOKEN
        pause
        exit /b 0
    ) else (
        echo ❌ 未找到 .env.template，请重新下载完整包
        pause
        exit /b 1
    )
) else (
    echo ✅ 配置文件存在
)

echo.
echo [3/3] 启动 AuthServer (Waitress)...
echo.
waitress-serve --host=0.0.0.0 --port=5000 app:app

pause
