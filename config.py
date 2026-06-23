"""
配置加载模块
支持从 .env 文件读取配置

打包后路径处理：
  - 开发环境：从当前工作目录读取 .env
  - 打包后  ：从可执行文件同目录读取 .env / 查找 Redis 等

绿色部署目录结构（打包后）：
  wifidog-authserver-{os}/
  ├── wifidog-auth.exe       ← 主程序（PyInstaller onedir）
  ├── .env                   ← 配置文件
  ├── .env.template          ← 配置模板
  ├── redis/                 ← Redis 绿色版（Windows需要）
  │   ├── redis-server.exe
  │   ├── redis-cli.exe
  │   ├── redis.conf
  │   └── redis_data/        ← Redis 持久化数据（自动创建）
  ├── log/                   ← 应用日志（启动后自动创建）
  ├── start.bat / start.sh   ← 启动脚本（支持后台/停止模式）
  ├── stop.bat / stop.sh     ← 停止脚本（完全停止所有服务）
  └── README.txt             ← 部署说明
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv


def get_binary_dir():
    """
    获取程序所在目录（打包后=exe目录，开发模式=当前工作目录）
    用于查找 .env、Redis 等外部文件
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：exe 所在目录
        return Path(sys.executable).parent
    else:
        # 开发模式
        return Path(__file__).parent


# ── 全局常量：程序根目录 ──
BINARY_DIR = get_binary_dir()


# 确定 .env 文件的搜索路径
def _find_env_path():
    """
    查找 .env 文件路径，优先级：
    1. 可执行文件同目录的 .env
    2. 当前工作目录的 .env
    """
    env_path = BINARY_DIR / '.env'
    if env_path.exists():
        return str(env_path)

    cwd_env = Path.cwd() / '.env'
    if cwd_env.exists():
        return str(cwd_env)

    return str(env_path)


_env_path = _find_env_path()
load_dotenv(_env_path)

if not getattr(sys, 'frozen', False):
    print(f"[Config] 加载配置: {_env_path}")


class Config:
    # ========== 基础配置 ==========

    # Flask
    FLASK_HOST = os.getenv('AUTHSERVER_HOST', os.getenv('FLASK_HOST', '0.0.0.0'))
    FLASK_PORT = int(os.getenv('AUTHSERVER_PORT', os.getenv('FLASK_PORT', 5000)))
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    # AD域LDAP
    AD_SERVER = os.getenv('AD_SERVER', 'ldap://127.0.0.1')
    AD_BIND_DN = os.getenv('AD_BIND_DN', '')
    AD_BIND_PASSWORD = os.getenv('AD_BIND_PASSWORD', '')
    AD_BASE_DN = os.getenv('AD_BASE_DN', 'dc=domain,dc=com')
    AD_USER_FILTER_ATTR = os.getenv('AD_USER_FILTER_ATTR', 'sAMAccountName')
    AD_USER_FILTER = os.getenv(
        'AD_USER_FILTER',
        f'({AD_USER_FILTER_ATTR}={{username}})'
    )
    AD_SEARCH_BASE = os.getenv('AD_SEARCH_BASE', '')
    AD_USER_BASE_DN = os.getenv('AD_USER_BASE_DN', '') or AD_SEARCH_BASE or AD_BASE_DN
    AD_USERNAME_ATTR = os.getenv('AD_USERNAME_ATTR', 'sAMAccountName')
    AD_EMAIL_ATTR = os.getenv('AD_EMAIL_ATTR', 'mail')
    AD_DISPLAY_NAME_ATTR = os.getenv('AD_DISPLAY_NAME_ATTR', 'displayName')

    # Redis 连接配置
    REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
    REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
    REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None) or None
    REDIS_DB = int(os.getenv('REDIS_DB', 0))

    # Redis 自动启动配置（绿色部署用）
    # 是否自动查找并启动本地 Redis（True/False）
    REDIS_AUTO_START = os.getenv('REDIS_AUTO_START', 'True').lower() == 'true'
    # Redis 可执行文件路径（相对于程序根目录，默认 redis/redis-server）
    REDIS_EXECUTABLE = os.getenv('REDIS_EXECUTABLE', 'redis/redis-server')
    # Redis 数据持久化目录（相对于程序根目录，默认 redis/redis_data）
    REDIS_DATA_DIR = os.getenv('REDIS_DATA_DIR', 'redis/redis_data')
    # Redis 配置文件路径（相对于程序根目录，默认 redis/redis.conf）
    REDIS_CONFIG_FILE = os.getenv('REDIS_CONFIG_FILE', 'redis/redis.conf')

    # WiFiDog
    WIFIDOG_GATEWAY_ID = os.getenv('WIFIDOG_GATEWAY_ID', 'default')
    AUTHSERVER_URL = os.getenv('AUTHSERVER_URL', 'http://127.0.0.1:5000')
    # 默认网关地址（当AC未传递gw_address参数时使用）
    DEFAULT_GATEWAY_ADDRESS = os.getenv('DEFAULT_GATEWAY_ADDRESS', '')
    # 默认网关端口
    DEFAULT_GATEWAY_PORT = os.getenv('DEFAULT_GATEWAY_PORT', '2060')

    # ========== 设备限制 ==========
    DEFAULT_MAX_DEVICES = int(os.getenv('DEFAULT_MAX_DEVICES', 3))
    TOKEN_EXPIRE_SECONDS = int(os.getenv('TOKEN_EXPIRE_SECONDS', 28800))
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 60))

    # ========== 心跳超时清除（按账号）====================
    DEFAULT_IDLE_TIMEOUT_HOURS = int(os.getenv('DEFAULT_IDLE_TIMEOUT_HOURS', 168))

    # ========== 全局定时清理任务 ==========
    DEVICE_CLEANUP_ENABLED = os.getenv('DEVICE_CLEANUP_ENABLED', 'True').lower() == 'true'
    DEVICE_CLEANUP_CRON = os.getenv('DEVICE_CLEANUP_CRON', '0 0 * * *')
    DEVICE_CLEANUP_IDLE_HOURS = int(os.getenv('DEVICE_CLEANUP_IDLE_HOURS', 0))

    # ========== 认证模式 ==========
    # 认证模式: "ad" = AD域LDAP认证, "local" = 本地用户认证(SQLite)
    AUTH_MODE = os.getenv('AUTH_MODE', 'ad').strip().lower()
    if AUTH_MODE not in ('ad', 'local'):
        print(f"[Config] 警告: 未知的AUTH_MODE '{AUTH_MODE}'，回退为 'ad'")
        AUTH_MODE = 'ad'

    # 本地认证配置
    # 本地用户数据库路径（相对于程序根目录，默认 local_users.db）
    LOCAL_DB_PATH = os.getenv('LOCAL_DB_PATH', 'local_users.db')

    # ========== 管理安全 ==========
    ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')


config = Config()

# 自动修正 AUTHSERVER_URL 端口，使其与 AUTHSERVER_PORT 一致
if config.AUTHSERVER_URL:
    from urllib.parse import urlparse
    parsed = urlparse(config.AUTHSERVER_URL)
    if parsed.port and parsed.port != config.FLASK_PORT:
        # 端口不一致，自动修正
        new_url = f"{parsed.scheme}://{parsed.hostname}:{config.FLASK_PORT}{parsed.path or '/'}"
        if not getattr(sys, 'frozen', False):
            print(
                f"[Config] AUTHSERVER_URL 端口与 AUTHSERVER_PORT 不一致，已自动修正:\n"
                f"         原: {config.AUTHSERVER_URL}\n"
                f"         新: {new_url}"
            )
        config.AUTHSERVER_URL = new_url
