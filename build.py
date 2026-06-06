"""
WiFiDog AuthServer 打包部署工具
===============================
将 AuthServer 打包成绿色单文件夹部署包，区分 Windows/Linux。

打包产物：
  wifidog-authserver-windows/
  ├── wifidog-auth.exe         ← PyInstaller onedir 主程序
  ├── .env.template            ← 配置模板
  ├── redis/                   ← Redis 绿色版
  │   ├── redis-server.exe
  │   └── redis.conf
  ├── redis_data/              ← Redis 数据（自动创建）
  ├── start.bat                ← Windows 启动脚本
  └── README.txt               ← 部署说明

  wifidog-authserver-linux/
  ├── wifidog-auth             ← PyInstaller onedir 主程序
  ├── .env.template
  ├── redis/
  │   ├── redis-server         ← Linux 绿色版 Redis
  │   └── redis.conf
  ├── redis_data/
  ├── start.sh                 ← Linux 启动脚本
  └── README.txt

使用方法:
  python build.py                        # 自动检测系统，打包绿色部署文件夹
  python build.py --platform windows     # 仅生成 Windows 包
  python build.py --platform linux       # 仅生成 Linux 包
  python build.py --skip-redis           # 跳过 Redis 下载（手动提供）
  python build.py --clean               # 清理构建文件
"""

import os
import sys
import shutil
import subprocess
import argparse
import urllib.request
import zipfile
import tarfile
import stat
import time
from pathlib import Path
from datetime import datetime


# ═══════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════

def get_project_dir():
    """获取项目根目录"""
    return Path(__file__).parent


def clean_build():
    """清理构建文件"""
    base = get_project_dir()
    for d in ['build', 'dist', '__pycache__']:
        p = base / d
        if p.exists():
            shutil.rmtree(p)
            print(f"  清理: {p}")
    for spec in base.glob('*.spec'):
        spec.unlink()
        print(f"  清理: {spec}")
    for pyc in base.rglob('*.pyc'):
        pyc.unlink()
    print("  清理完成")


def get_platform():
    """检测当前平台"""
    if sys.platform.startswith('win'):
        return 'windows'
    return 'linux'


# ═══════════════════════════════════════════
#  Redis 下载（Windows 绿色版）
# ═══════════════════════════════════════════

# tporadowski/redis 的 Windows 构建版（稳定可靠）
# 使用 GitHub releases API 或直接下载链接
REDIS_WIN_URLS = {
    # tporadowski/redis 5.0.14.1
    'tporadowski': {
        'url': 'https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip',
        'strip_prefix': '',  # zip 内根目录直接是 redis-server.exe 等文件
    },
}


def download_redis_windows(dest_dir):
    """
    下载 Windows Redis 绿色版到 dest_dir/redis/
    返回: True/False
    """
    redis_dir = Path(dest_dir) / 'redis'
    redis_exe = redis_dir / 'redis-server.exe'

    if redis_exe.exists():
        print(f"  Redis 已存在: {redis_exe}")
        return True

    redis_dir.mkdir(parents=True, exist_ok=True)

    print("  正在下载 Redis Windows 绿色版...")

    # 尝试从 tporadowski 下载
    zip_path = redis_dir / 'redis.zip'
    try:
        url = REDIS_WIN_URLS['tporadowski']['url']
        print(f"  下载地址: {url}")

        # 使用 urllib 下载（3次重试）
        for attempt in range(3):
            try:
                urllib.request.urlretrieve(url, str(zip_path))
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  重试 {attempt + 2}/3...")
                time.sleep(3)

        # 解压
        print("  正在解压...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # 列出文件结构
            for member in zf.namelist():
                # 提取直接是 exe 的文件，跳过子目录
                filename = Path(member).name
                if not filename:
                    continue
                # 只提取 redis-server.exe, redis-cli.exe 等
                if filename.startswith('redis-') or filename in ['RedisBenchmark.exe', 'RedisService.exe']:
                    target = redis_dir / filename
                    with zf.open(member) as src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

        zip_path.unlink()
        print(f"  Redis 下载完成")

        # 创建默认 redis.conf
        redis_conf = redis_dir / 'redis.conf'
        if not redis_conf.exists():
            conf_content = """# Redis 绿色部署配置
bind 127.0.0.1
port 6379
dir ../redis_data
logfile ../redis_data/redis.log
save 900 1
save 300 10
save 60 10000
dbfilename dump.rdb
# daemonize no (由 AuthServer 管理进程)
"""
            redis_conf.write_text(conf_content, encoding='utf-8')

        return True

    except Exception as e:
        print(f"  Redis 下载失败: {e}")
        print(f"  请手动下载 Redis-x64 到: {redis_dir}")

        # 创建说明文件
        manual_txt = redis_dir / '手动安装说明.txt'
        manual_txt.write_text(
            "请手动下载 Redis Windows 版本到此文件夹:\n"
            "1. 打开 https://github.com/tporadowski/redis/releases\n"
            "2. 下载 Redis-x64-5.0.14.1.zip\n"
            "3. 将 redis-server.exe 和 redis-cli.exe 复制到这里\n",
            encoding='utf-8'
        )
        return False


def bundle_redis_linux(dest_dir):
    """
    为 Linux 准备 Redis 说明和配置文件
    Linux 用户通常用系统包管理器安装，这里创建配置和说明
    """
    redis_dir = Path(dest_dir) / 'redis'
    redis_dir.mkdir(parents=True, exist_ok=True)

    # 创建 redis.conf
    redis_conf = redis_dir / 'redis.conf'
    conf_content = f"""# Redis 配置 - WiFiDog AuthServer 绿色部署
bind 127.0.0.1
port {os.getenv('REDIS_PORT', '6379')}
dir ../redis_data
logfile ../redis_data/redis.log
save 900 1
save 300 10
save 60 10000
dbfilename dump.rdb
"""
    redis_conf.write_text(conf_content, encoding='utf-8')

    # 创建安装说明
    install_txt = redis_dir / '安装说明.txt'
    install_txt.write_text(
        "# Linux Redis 绿色部署\n\n"
        "## 方案一：使用系统包管理器\n"
        "  Ubuntu/Debian: sudo apt install redis-server\n"
        "  CentOS/RHEL:   sudo yum install redis\n"
        "  安装后，AuthServer 会自动检测并连接\n\n"
        "## 方案二：绿色部署（复制到本目录）\n"
        "  1. 从已有系统复制: cp $(which redis-server) ./\n"
        "  2. 或从官网下载编译好的二进制文件\n"
        "  3. AuthServer 会自动启动本目录的 redis-server\n\n"
        "# 如果使用 Docker Redis:\n"
        "  docker run -d --name auth-redis -p 6379:6379 redis:alpine\n\n"
        "# 如果 Redis 在其他服务器:\n"
        "  修改 .env 中的 REDIS_HOST 和 REDIS_PORT\n"
        "  设置 REDIS_AUTO_START=False\n",
        encoding='utf-8'
    )

    print("  Linux Redis 配置已创建（使用系统 Redis 或手动放置二进制）")


# ═══════════════════════════════════════════
#  配置模板
# ═══════════════════════════════════════════

ENV_TEMPLATE = """# ============================================
# WiFiDog AuthServer 运行配置
# 将此文件重命名为 .env（去掉 .template 后缀）
# ============================================

# ----- AD域配置 -----
AD_SERVER=ldap://192.168.1.10
AD_BIND_DN=cn=wifidog_svc,cn=Users,dc=yourdomain,dc=com
AD_BIND_PASSWORD=your_service_password
AD_BASE_DN=dc=yourdomain,dc=com
AD_USER_FILTER_ATTR=sAMAccountName

# ----- AuthServer 配置 -----
AUTHSERVER_HOST=0.0.0.0
AUTHSERVER_PORT=5000
AUTHSERVER_URL=http://192.168.1.100:5000

# ----- Redis 配置 -----
# Redis 连接地址
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
# 是否自动启动本地 Redis（绿色部署使用，默认 True）
REDIS_AUTO_START=True
# Redis 可执行文件相对路径
REDIS_EXECUTABLE=redis/redis-server
# Redis 数据目录
REDIS_DATA_DIR=redis_data

# ----- WiFiDog 配置 -----
TOKEN_EXPIRE_SECONDS=28800
CHECK_INTERVAL=60

# ----- 设备管理 -----
DEFAULT_MAX_DEVICES=3

# ----- 自动清理 -----
DEVICE_IDLE_TIMEOUT_HOURS=168
DEVICE_CLEANUP_CRON=0 0 * * *
DEVICE_CLEANUP_ENABLED=True

# ----- 管理令牌 -----
# ⚠️ 必须修改！用于管理员网页登录
ADMIN_TOKEN=change_this_to_a_strong_random_token
"""


README_WINDOWS = """=======================================================
  WiFiDog AuthServer - Windows 绿色部署版
=======================================================

📋 部署步骤：

  1. 将本文件夹整个复制到目标服务器（如 D:\\wifidog-auth）

  2. 配置 .env 文件:
     - 重命名 .env.template 为 .env
     - 编辑 .env，填入你的 AD 域信息和管理令牌
     - 重点修改: AD_SERVER, AD_BIND_DN, AD_BIND_PASSWORD, ADMIN_TOKEN

  3. 确保 Redis 已就绪:
     - 如果 redis/ 目录下有 redis-server.exe，程序会自动启动
     - 也可自行安装 Redis 服务

  4. 双击 start.bat 启动服务

  5. 在锐捷 AC 上配置 WiFiDog 认证服务器:
     URL: http://服务器IP:5000/login

  6. 管理界面:
     浏览器打开: http://服务器IP:5000/admin
     使用 .env 中设置的 ADMIN_TOKEN 登录

📁 目录结构：
  wifidog-authserver-windows/
  ├── wifidog-auth.exe      ← 主程序
  ├── .env.template         ← 配置模板
  ├── redis/                ← Redis 绿色版（Windows x64）
  ├── redis_data/           ← Redis 持久化数据
  └── start.bat             ← 启动脚本

⚠️  依赖：
  - Windows 10/11 或 Windows Server 2016+
  - 64 位操作系统（x64）
  - Redis 已内置在 redis/ 子目录
  - 无需安装 Python 或其他运行时

🆘 常见问题：
  Q: 启动后显示"Redis 未就绪"
  A: 检查 redis/ 目录是否有 redis-server.exe，或防火墙是否阻止

  Q: 如何以 Windows 服务方式运行？
  A: 使用 NSSM (http://nssm.cc) 注册为服务:
     nssm install WiFiDogAuth
     程序路径: wifidog-auth.exe
     启动目录: 本文件夹路径

  Q: 如何查看日志？
  A: 程序日志输出到控制台窗口（请勿关闭）
     Redis 日志在 redis_data/redis.log
"""


README_LINUX = """=======================================================
  WiFiDog AuthServer - Linux 绿色部署版
=======================================================

📋 部署步骤：

  1. 将本文件夹复制到目标服务器:
     tar xzf wifidog-authserver-linux.tar.gz
     cd wifidog-authserver-linux

  2. 配置 .env 文件:
     cp .env.template .env
     vim .env  # 填入 AD 域信息和管理令牌

  3. 确保 Redis 已就绪:
     # 方案A: 系统安装
     sudo apt install redis-server  # Ubuntu/Debian
     # 方案B: 使用内置绿色版
     # 查看 redis/安装说明.txt

  4. 运行启动脚本:
     chmod +x wifidog-auth start.sh
     ./start.sh

  5. 配置锐捷 AC:
     WiFiDog 认证服务器 URL: http://服务器IP:5000/login

  6. 管理界面:
     http://服务器IP:5000/admin

📁 目录结构：
  wifidog-authserver-linux/
  ├── wifidog-auth           ← 主程序
  ├── .env.template
  ├── redis/                 ← Redis 配置
  ├── redis_data/
  └── start.sh               ← 启动脚本

🆘 常见问题：
  Q: 需要什么依赖？
  A: 无需安装 Python，已全部打包在内

  Q: 如何以 systemd 服务运行？
  A: 创建 /etc/systemd/system/wifidog-auth.service:
     [Unit]
     Description=WiFiDog AuthServer
     After=network.target
     [Service]
     Type=simple
     WorkingDirectory=/opt/wifidog-authserver
     ExecStart=/opt/wifidog-authserver/wifidog-auth
     Restart=always
     [Install]
     WantedBy=multi-user.target

     sudo systemctl enable wifidog-auth
     sudo systemctl start wifidog-auth
"""


# ═══════════════════════════════════════════
#  PyInstaller 打包
# ═══════════════════════════════════════════

def build_with_pyinstaller(for_platform):
    """用 PyInstaller 构建 onedir 产物"""
    proj = get_project_dir()
    dist_dir = proj / 'dist'

    print(f"\n  [debug] Python executable: {sys.executable}")
    print(f"  [debug] Project dir: {proj}")
    print(f"  [debug] Platform: {for_platform}")
    print(f"  [debug] sys.platform: {sys.platform}")

    # 确保 api-ms-win-* 等系统 DLL 不被包含（这些是系统文件）
    print("\n  执行 PyInstaller onedir 打包...")

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onedir',                    # 单文件夹模式
        '--name', 'wifidog-auth',
        '--clean',
        '--distpath', str(dist_dir),
        '--workpath', str(proj / 'build'),
        '--specpath', str(proj),
    ]

    # 排除不必要的系统文件
    cmd.extend([
        '--exclude-module', 'tkinter',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'numpy',
        '--exclude-module', 'PIL',
    ])

    # 收集关键包
    for pkg in ['flask', 'ldap3', 'redis', 'apscheduler', 'dotenv']:
        cmd.extend(['--collect-submodules', pkg])

    # 隐藏导入
    hidden = [
        'flask', 'flask_cors', 'ldap3', 'redis',
        'apscheduler', 'apscheduler.schedulers',
        'apscheduler.schedulers.background',
        'apscheduler.triggers', 'apscheduler.triggers.cron',
        'dotenv', 'python_dotenv',
    ]
    for imp in hidden:
        cmd.extend(['--hidden-import', imp])

    # 添加额外搜索路径
    extra_paths = []
    # managed Python venv
    venv_sp = Path(sys.executable).parent.parent.parent / 'envs' / 'default' / 'Lib' / 'site-packages'
    if venv_sp.exists():
        extra_paths.append(str(venv_sp))
    # 用户 site-packages
    import site
    for sp in site.getsitepackages():
        extra_paths.append(sp)
    for sp in set(extra_paths):
        if Path(sp).exists():
            cmd.extend(['--paths', sp])

    cmd.append(str(proj / 'app.py'))

    print(f"  [debug] PyInstaller cmd: {' '.join(str(c) for c in cmd)}")

    result = subprocess.run(cmd, cwd=str(proj))
    if result.returncode != 0:
        print(f"\n  [错误] PyInstaller 失败，返回码: {result.returncode}")
        print(f"  [提示] 请检查上方 PyInstaller 输出信息")
        return False

    print("\n  PyInstaller 打包完成")
    return True


# ═══════════════════════════════════════════
#  组装部署包
# ═══════════════════════════════════════════

def assemble_package(for_platform):
    """
    将 PyInstaller 产物 + Redis + 配置 + 脚本组装成最终部署包
    """
    proj = get_project_dir()
    pyinstaller_output = proj / 'dist' / 'wifidog-auth'  # onedir 输出

    if not pyinstaller_output.exists():
        print("  PyInstaller 输出目录不存在，请先执行打包")
        return False

    # 部署包名称和路径
    package_name = f'wifidog-authserver-{for_platform}'
    package_dir = proj / 'dist' / package_name
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)

    print(f"\n  组装部署包: {package_dir.name}/")

    # 1. 复制 PyInstaller onedir 中的所有文件（扁平化到根目录）
    exe_name = 'wifidog-auth.exe' if for_platform == 'windows' else 'wifidog-auth'
    for item in pyinstaller_output.iterdir():
        if item.is_dir() and item.name.startswith('_'):
            # PyInstaller 的内部目录（_internal 等），复制到 _internal/
            target = package_dir / item.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        elif item.is_file():
            shutil.copy2(item, package_dir / item.name)
    print(f"  ✅ 主程序已复制")

    # 2. 创建 .env.template
    env_tpl = package_dir / '.env.template'
    env_tpl.write_text(ENV_TEMPLATE, encoding='utf-8')
    print(f"  ✅ .env.template 已创建")

    # 3. 创建 redis_data 目录
    redis_data = package_dir / 'redis_data'
    redis_data.mkdir(parents=True, exist_ok=True)
    print(f"  ✅ redis_data/ 已创建")

    # 4a. 处理 Redis (Windows: 下载绿色版)
    if for_platform == 'windows':
        download_redis_windows(package_dir)
    else:
        bundle_redis_linux(package_dir)

    # 5. 创建启动脚本
    if for_platform == 'windows':
        # start.bat
        start_bat = package_dir / 'start.bat'
        start_bat.write_text("""@echo off
chcp 65001 >nul
title WiFiDog AuthServer
echo =============================================
echo   WiFiDog AuthServer - Windows 绿色部署版
echo =============================================
echo.
cd /d "%~dp0"

REM 检查 .env
if not exist ".env" (
    echo [警告] 未找到 .env 配置文件
    echo.
    echo 正在从 .env.template 创建...
    copy ".env.template" ".env" >nul
    echo 请编辑 .env 文件，填写 AD 域信息和管理令牌后重新启动
    echo.
    pause
    exit /b 0
)

echo [启动] WiFiDog AuthServer...
echo [提示] 请勿关闭此窗口
echo.
wifidog-auth.exe
pause
""", encoding='utf-8')
        print(f"  ✅ start.bat 已创建")
    else:
        # start.sh
        start_sh = package_dir / 'start.sh'
        start_sh.write_text("""#!/bin/bash
echo "============================================"
echo "  WiFiDog AuthServer - Linux 绿色部署版"
echo "============================================"
echo ""

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 检查 .env
if [ ! -f ".env" ]; then
    echo "[警告] 未找到 .env 配置文件"
    echo ""
    echo "正在从 .env.template 创建..."
    cp .env.template .env
    echo "请编辑 .env 文件，填写 AD 域信息和管理令牌后重新启动"
    exit 0
fi

echo "[启动] WiFiDog AuthServer..."
echo "[提示] 按 Ctrl+C 停止服务"
echo ""

./wifidog-auth
""")
        start_sh.chmod(start_sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  ✅ start.sh 已创建")

    # 6. 创建 README.txt
    if for_platform == 'windows':
        readme = README_WINDOWS
    else:
        readme = README_LINUX
    (package_dir / 'README.txt').write_text(readme, encoding='utf-8')
    print(f"  ✅ README.txt 已创建")

    # 7. 清理 PyInstaller 中间目录（保留部署包）
    shutil.rmtree(pyinstaller_output, ignore_errors=True)
    build_dir = proj / 'build'
    if build_dir.exists():
        shutil.rmtree(build_dir)
    spec_file = proj / 'wifidog-auth.spec'
    if spec_file.exists():
        spec_file.unlink()

    # 8. 显示结果
    print(f"\n{'=' * 60}")
    print(f"  打包完成!")
    print(f"{'=' * 60}")
    print(f"  部署包: {package_dir}")
    print(f"  文件大小: ", end="")

    total = sum(f.stat().st_size for f in package_dir.rglob('*') if f.is_file())
    if total > 1024 * 1024 * 1024:
        print(f"{total / (1024**3):.1f} GB")
    else:
        print(f"{total / (1024**2):.1f} MB")

    print(f"\n  部署步骤:")
    if for_platform == 'windows':
        print(f"    1. 将 {package_name}/ 文件夹复制到目标服务器")
        print(f"    2. 重命名 .env.template 为 .env，编辑配置")
        print(f"    3. 双击 start.bat 启动")
    else:
        print(f"    1. tar czf {package_name}.tar.gz {package_name}/")
        print(f"    2. 上传压缩包到 Linux 服务器并解压")
        print(f"    3. cp .env.template .env && vim .env")
        print(f"    4. ./start.sh")

    return True


# ═══════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════

def main():
    import time as _time

    parser = argparse.ArgumentParser(description='WiFiDog AuthServer 打包工具')
    parser.add_argument('--platform', choices=['windows', 'linux'], default=None,
                        help='目标平台 (默认: 自动检测)')
    parser.add_argument('--skip-redis', action='store_true',
                        help='跳过 Redis 下载（手动提供）')
    parser.add_argument('--clean', action='store_true',
                        help='清理构建文件')
    parser.add_argument('--skip-build', action='store_true',
                        help='跳过 PyInstaller 打包（仅组装已有产物）')

    args = parser.parse_args()

    for_platform = args.platform or get_platform()

    print("=" * 60)
    print(f"  WiFiDog AuthServer 打包工具")
    print(f"  目标平台: {for_platform}")
    print("=" * 60)

    if args.clean:
        print("\n[清理]")
        clean_build()
        return

    # 步骤1: PyInstaller 打包
    if not args.skip_build:
        print(f"\n[步骤1/2] PyInstaller 打包 ({for_platform})")
        if not build_with_pyinstaller(for_platform):
            print("\n打包失败!")
            sys.exit(1)
    else:
        print("\n[步骤1/2] 跳过 PyInstaller（--skip-build）")

    # 步骤2: 组装部署包
    print(f"\n[步骤2/2] 组装部署包 ({for_platform})")
    if not assemble_package(for_platform):
        print("\n组装失败!")
        sys.exit(1)

    print(f"\n🎉 全部完成!")


if __name__ == '__main__':
    main()
