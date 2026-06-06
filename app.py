"""
WiFiDog AuthServer - AD域认证版
支持AD域账号认证、设备数量限制、心跳超时自动清除、定时清理僵尸设备

新增功能：
  - 按账号心跳超时清除（全局默认值 + 按账号单独设置）
  - 全局定时清理任务（独立开关，cron表达式配置）
  - 管理员UI（直观简洁的Web管理界面）
"""

import os
import sys
import time
import secrets
import string
import logging
import subprocess
import socket
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, request, redirect, render_template_string,
    jsonify, session, url_for, make_response
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import config, BINARY_DIR
from ad_auth import ad_auth
from device_manager import device_manager

# ==================== Redis 自动启动（绿色部署） ====================

_redis_process = None  # 保存 Redis 子进程引用


def _find_redis_executable():
    """查找 Redis 可执行文件"""
    # 1. 从配置中指定的相对路径
    exe_name = 'redis-server'
    if sys.platform.startswith('win'):
        exe_name = 'redis-server.exe'

    redis_path = BINARY_DIR / config.REDIS_EXECUTABLE
    if not redis_path.exists():
        # 尝试加上 .exe 后缀
        redis_path = BINARY_DIR / config.REDIS_EXECUTABLE
        if sys.platform.startswith('win') and not redis_path.suffix:
            redis_path = redis_path.with_suffix('.exe')
    if redis_path.exists():
        return redis_path

    # 2. 尝试 redis/ 子目录
    redis_path = BINARY_DIR / 'redis' / exe_name
    if redis_path.exists():
        return redis_path

    # 3. 系统 PATH 中查找
    import shutil
    system_redis = shutil.which(exe_name)
    if system_redis:
        return Path(system_redis)

    return None


def _check_redis_alive(host='127.0.0.1', port=6379):
    """检查 Redis 是否已运行"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _start_embedded_redis(redis_exe):
    """启动内嵌 Redis 进程"""
    global _redis_process
    try:
        # 数据目录
        data_dir = BINARY_DIR / config.REDIS_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        # 配置文件
        config_file = BINARY_DIR / config.REDIS_CONFIG_FILE
        if not config_file.exists():
            # 创建默认配置文件
            redis_conf = f"""# WiFiDog AuthServer - Redis 绿色部署配置
bind 127.0.0.1
port {config.REDIS_PORT}
dir {data_dir}
logfile {data_dir}/redis.log
save 900 1
save 300 10
save 60 10000
dbfilename dump.rdb
daemonize no
"""
            config_file.write_text(redis_conf, encoding='utf-8')

        # 启动 Redis
        cmd = [str(redis_exe), str(config_file)]
        import sys as _sys
        _redis_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(BINARY_DIR),
        )

        # 等待 Redis 启动（最多 15 秒）
        for i in range(50):
            time.sleep(0.3)
            if _check_redis_alive(config.REDIS_HOST, config.REDIS_PORT):
                print(f"[Redis] 已启动 (PID: {_redis_process.pid}, 端口: {config.REDIS_PORT})")
                print(f"[Redis] 数据目录: {data_dir}")
                return True
            # 检查 Redis 是否已退出
            poll = _redis_process.poll()
            if poll is not None:
                stderr_out = _redis_process.stderr.read().decode('utf-8', errors='replace')
                print(f"[Redis] 进程意外退出 (exit code: {poll})")
                if stderr_out:
                    print(f"[Redis] 错误信息: {stderr_out[:500]}")
                return False

        print("[Redis] 启动超时（15秒）！请检查 Redis 配置")
        return False

    except Exception as e:
        print(f"[Redis] 启动失败: {e}")
        return False


def _init_redis():
    """初始化 Redis 连接（自动检测/启动）"""
    # 1. 先检查 Redis 是否已经在运行
    if _check_redis_alive(config.REDIS_HOST, config.REDIS_PORT):
        print(f"[Redis] 检测到已有 Redis 运行在 {config.REDIS_HOST}:{config.REDIS_PORT}")
        return True

    # 2. 尝试自动启动
    if not config.REDIS_AUTO_START:
        print(f"[Redis] 未检测到 Redis，且 REDIS_AUTO_START=False，请手动启动 Redis")
        return False

    redis_exe = _find_redis_executable()
    if not redis_exe:
        print(f"[Redis] 未找到 redis-server！")
        print(f"  - 请将 redis-server 放在程序目录的 redis/ 子文件夹下")
        print(f"  - 或将 REDIS_AUTO_START 设为 False 后自行启动 Redis 服务")
        return False

    print(f"[Redis] 找到 Redis: {redis_exe}")
    return _start_embedded_redis(redis_exe)


# 启动时初始化 Redis
redis_ok = _init_redis()
if not redis_ok:
    print("\n" + "=" * 60)
    print("  [!] Redis 未就绪！AuthServer 可能无法正常工作")
    print("  请确保 Redis 已启动，或将其放在程序目录的 redis/ 子文件夹")
    print("=" * 60 + "\n")


app = Flask(__name__)
app.secret_key = os.urandom(24)

# ==================== 定时清理任务 ====================

scheduler = None


def check_admin_auth():
    """
    检查管理员权限（优先级：session > query参数 > header）
    用于Web UI（session）和API调用（token）两种场景
    """
    # 1. Web UI: 已通过session登录
    if session.get('admin_logged_in'):
        return True

    # 2. API: 从query参数或header获取admin_token
    token = request.args.get('admin_token') or request.headers.get('X-Admin-Token', '')
    if token and config.ADMIN_TOKEN and token == config.ADMIN_TOKEN:
        return True

    return False


def require_admin():
    """重定向到登录页（Web）或返回401（API）"""
    if request.path.startswith('/admin/api/') or request.is_json:
        return jsonify({'error': 'unauthorized'}), 401
    return redirect('/admin/login')


def scheduled_cleanup():
    """定时清理僵尸设备的任务函数"""
    idle_hours = config.DEVICE_CLEANUP_IDLE_HOURS
    if idle_hours > 0:
        # 使用指定的超时时间（覆盖按账号设置）
        print(f"\n[定时清理] 开始扫描（使用统一超时: {idle_hours}h）...")
        cleaned, detail = device_manager.cleanup_stale_devices(idle_timeout_hours=idle_hours)
    else:
        # idle_hours=0：使用每个账号自己的超时设置
        print(f"\n[定时清理] 开始扫描（使用各账号独立超时设置）...")
        cleaned, detail = device_manager.cleanup_stale_devices(idle_timeout_hours=None)

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if cleaned > 0:
        print(f"[定时清理] {now_str} 完成，共清理 {cleaned} 个僵尸设备")
        for user, cnt in detail.items():
            print(f"            - {user}: {cnt} 台")
    else:
        print(f"[定时清理] {now_str} 完成，无僵尸设备")


def init_scheduler():
    """根据配置初始化定时清理任务"""
    global scheduler

    if not config.DEVICE_CLEANUP_ENABLED:
        print("[定时清理] 已禁用（DEVICE_CLEANUP_ENABLED=False）")
        return

    if not config.ADMIN_TOKEN:
        print("[定时清理] 警告：ADMIN_TOKEN 未设置，管理接口无认证保护！")

    scheduler = BackgroundScheduler(daemon=True)
    cron_expr = config.DEVICE_CLEANUP_CRON

    try:
        trigger = CronTrigger.from_crontab(cron_expr)
        scheduler.add_job(
            scheduled_cleanup,
            trigger=trigger,
            id='device_cleanup',
            name=f'清理僵尸设备（超时={config.DEVICE_CLEANUP_IDLE_HOURS or "账号独立"}h）',
            replace_existing=True
        )
        scheduler.start()
        job = scheduler.get_job('device_cleanup')
        next_run = job.next_run_time if job else 'N/A'
        print(f"[定时清理] 已启用，cron='{cron_expr}'，"
              f"超时阈值={'各账号独立' if config.DEVICE_CLEANUP_IDLE_HOURS == 0 else f'{config.DEVICE_CLEANUP_IDLE_HOURS}h'}，"
              f"下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S') if hasattr(next_run, 'strftime') else next_run}")
    except Exception as e:
        print(f"[定时清理] 警告：cron表达式解析失败 '{cron_expr}'，将禁用自动清理。错误: {e}")


# ==================== 工具函数 ====================

def generate_token(length=32):
    """生成随机token"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_client_ip():
    """获取客户端真实IP"""
    if 'X-Forwarded-For' in request.headers:
        return request.headers['X-Forwarded-For'].split(',')[0].strip()
    return request.remote_addr


def get_redis_client():
    """获取Redis客户端（复用）"""
    import redis
    return redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        password=config.REDIS_PASSWORD,
        db=config.REDIS_DB,
        decode_responses=True
    )


# ==================== WiFiDog 协议接口 ====================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """WiFiDog网关重定向到此进行登录认证"""
    gw_address = request.args.get('gw_address', '')
    gw_port = request.args.get('gw_port', '2060')
    gw_id = request.args.get('gw_id', '')
    mac = request.args.get('mac', '')
    url = request.args.get('url', '')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        gw_address = request.form.get('gw_address', '')
        gw_port = request.form.get('gw_port', '2060')
        gw_id = request.form.get('gw_id', '')
        mac = request.form.get('mac', '')
        url = request.form.get('url', '')

        if not username or not password:
            return render_template_string(
                LOGIN_TEMPLATE,
                gw_address=gw_address, gw_port=gw_port, gw_id=gw_id,
                mac=mac, url=url, error="请输入用户名和密码",
                max_devices=config.DEFAULT_MAX_DEVICES
            )

        success, user_info = ad_auth.authenticate(username, password)
        if not success:
            return render_template_string(
                LOGIN_TEMPLATE,
                gw_address=gw_address, gw_port=gw_port, gw_id=gw_id,
                mac=mac, url=url, error="用户名或密码错误，请使用域账号登录",
                max_devices=config.DEFAULT_MAX_DEVICES
            )

        token = generate_token()
        client_ip = get_client_ip()
        device_mac = mac  # 手机MAC随机化，不依赖此值做设备识别

        max_devices = device_manager.get_max_devices(username)
        add_success, kicked_token = device_manager.add_device(
            username=username, token=token, mac=device_mac,
            ip=client_ip, gw_id=gw_id
        )

        if kicked_token:
            kicked_info = device_manager.get_token_info(kicked_token)
            kicked_mac = kicked_info.get('mac', 'N/A') if kicked_info else 'N/A'
            print(f"[AuthServer] 用户[{username}]设备数超限({max_devices}台)，"
                  f"已踢出最早设备 token={kicked_token[:8]}..., MAC={kicked_mac}")

        r = get_redis_client()
        r.setex(f"token_user:{token}", config.TOKEN_EXPIRE_SECONDS, username)
        if kicked_token:
            r.setex(f"kicked_token:{kicked_token}", config.TOKEN_EXPIRE_SECONDS, "1")

        print(f"[AuthServer] 用户[{username}]登录成功, token={token[:8]}..., IP={client_ip}")

        gateway_url = f"http://{gw_address}:{gw_port}/wifidog/auth?token={token}"
        return redirect(gateway_url)

    return render_template_string(
        LOGIN_TEMPLATE,
        gw_address=gw_address, gw_port=gw_port, gw_id=gw_id,
        mac=mac, url=url, error=None,
        max_devices=config.DEFAULT_MAX_DEVICES
    )


@app.route('/auth', methods=['GET'])
def auth():
    """WiFiDog网关心跳调用此接口验证用户在线状态"""
    stage = request.args.get('stage', '')
    client_ip = request.args.get('ip', '')
    mac = request.args.get('mac', '')
    token = request.args.get('token', '')
    gw_id = request.args.get('gw_id', '')

    print(f"[Auth] stage={stage}, ip={client_ip}, mac={mac}, "
          f"token={token[:8] if token else 'N/A'}..., gw_id={gw_id}")

    if not token:
        print("[Auth] 拒绝: 无token")
        return "Auth: 0\n", 200, {'Content-Type': 'text/plain'}

    r = get_redis_client()

    if r.exists(f"kicked_token:{token}"):
        print(f"[Auth] 拒绝: token={token[:8]}... 已被踢出")
        return "Auth: 0\n", 200, {'Content-Type': 'text/plain'}

    username = r.get(f"token_user:{token}")
    if not username:
        print(f"[Auth] 拒绝: token={token[:8]}... 无效或已过期")
        return "Auth: 0\n", 200, {'Content-Type': 'text/plain'}

    device_manager.update_last_seen(token)

    print(f"[Auth] 通过: token={token[:8]}..., user={username}, stage={stage}")
    return "Auth: 1\n", 200, {'Content-Type': 'text/plain'}


@app.route('/ping', methods=['GET'])
def ping():
    """WiFiDog网关心跳检测"""
    gw_id = request.args.get('gw_id', '')
    sys_uptime = request.args.get('sys_uptime', '')
    print(f"[Ping] gw_id={gw_id}, uptime={sys_uptime}s")
    return "Pong", 200, {'Content-Type': 'text/plain'}


@app.route('/portal', methods=['GET'])
def portal():
    """认证成功后，网关重定向用户到此页面"""
    gw_id = request.args.get('gw_id', '')
    token = request.args.get('token', '')
    return render_template_string(PORTAL_TEMPLATE, gw_id=gw_id, token=token)


@app.route('/gw_message', methods=['GET'])
def gw_message():
    """认证失败等消息页面"""
    message = request.args.get('message', 'unknown')
    return render_template_string(MESSAGE_TEMPLATE, message=message)


# ==================== 用户自助设备管理 ====================

@app.route('/manage', methods=['GET', 'POST'])
def manage():
    """用户自助设备管理页面"""
    action = request.form.get('action', '')

    if action == 'kick':
        if 'manage_user' not in session:
            return redirect('/manage')
        username = session['manage_user']
        kick_token = request.form.get('kick_token', '')
        if kick_token:
            device_manager.remove_device(username, kick_token)
            r = get_redis_client()
            r.setex(f"kicked_token:{kick_token}", config.TOKEN_EXPIRE_SECONDS, "1")
            print(f"[Manage] 用户[{username}]手动踢出设备 token={kick_token[:8]}...")
        return redirect('/manage')

    if action == 'logout':
        session.pop('manage_user', None)
        return redirect('/manage')

    if request.method == 'POST' and action == 'login':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            return render_template_string(MANAGE_LOGIN_TEMPLATE, error="请输入用户名和密码")
        success, _ = ad_auth.authenticate(username, password)
        if not success:
            return render_template_string(MANAGE_LOGIN_TEMPLATE, error="用户名或密码错误")
        session['manage_user'] = username
        print(f"[Manage] 用户[{username}]进入设备管理页面")
        return redirect('/manage')

    if 'manage_user' in session:
        username = session['manage_user']
        devices = device_manager.get_user_devices(username)
        device_list = []
        for d in devices:
            device_list.append({
                'token_short': d['token'][:12] + '...',
                'token': d['token'],
                'ip': d.get('ip', 'N/A'),
                'gw_id': d.get('gw_id', 'N/A'),
                'login_time': time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(float(d.get('login_time', 0)))),
                'last_seen': time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(float(d.get('last_seen', 0)))),
            })
        max_devices = device_manager.get_max_devices(username)
        return render_template_string(
            MANAGE_TEMPLATE,
            username=username, devices=device_list,
            max_devices=max_devices, current_count=len(devices),
        )

    return render_template_string(MANAGE_LOGIN_TEMPLATE, error=None)


# ==================== 管理员认证 ====================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """管理员登录页面"""
    if request.method == 'POST':
        token = request.form.get('admin_token', '').strip()
        if not config.ADMIN_TOKEN:
            # 未设置ADMIN_TOKEN时，禁止登录
            return render_template_string(
                ADMIN_LOGIN_TEMPLATE,
                error="ADMIN_TOKEN 未配置，请联系系统管理员"
            )
        if token == config.ADMIN_TOKEN:
            session['admin_logged_in'] = True
            session['admin_user'] = 'admin'
            return redirect('/admin')
        else:
            return render_template_string(ADMIN_LOGIN_TEMPLATE, error="管理令牌错误")

    # 已登录则跳转
    if session.get('admin_logged_in'):
        return redirect('/admin')
    return render_template_string(ADMIN_LOGIN_TEMPLATE, error=None)


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    """管理员注销"""
    session.pop('admin_logged_in', None)
    session.pop('admin_user', None)
    return redirect('/admin/login')


# ==================== 管理员UI ====================

@app.route('/admin', methods=['GET'])
def admin_dashboard():
    """管理员主界面"""
    if not check_admin_auth():
        return redirect('/admin/login')

    stats = device_manager.get_global_stats()

    # 下次清理时间
    next_cleanup = None
    if scheduler and scheduler.get_job('device_cleanup'):
        nt = scheduler.get_job('device_cleanup').next_run_time
        if nt:
            next_cleanup = nt.strftime('%Y-%m-%d %H:%M:%S')

    return render_template_string(
        ADMIN_TEMPLATE,
        stats=stats,
        config=config,
        next_cleanup=next_cleanup,
        now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )


@app.route('/admin/users', methods=['GET'])
def admin_users():
    """用户管理页面（查看/编辑用户设置）"""
    if not check_admin_auth():
        return redirect('/admin/login')

    users = device_manager.get_all_user_summaries()

    return render_template_string(
        ADMIN_USERS_TEMPLATE,
        users=users,
        default_idle=config.DEFAULT_IDLE_TIMEOUT_HOURS,
        default_max=config.DEFAULT_MAX_DEVICES,
    )


@app.route('/admin/user/<username>', methods=['GET', 'POST'])
def admin_user_detail(username):
    """单个用户的详细管理页面"""
    if not check_admin_auth():
        return redirect('/admin/login')

    message = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'set_idle':
            val = request.form.get('idle_timeout', '').strip()
            if val == '' or val == '0':
                device_manager.set_idle_timeout(username, None)
                message = f"已清除 {username} 的独立心跳超时（将使用全局默认值）"
            else:
                try:
                    hours = int(val)
                    device_manager.set_idle_timeout(username, hours)
                    message = f"已设置 {username} 的心跳超时为 {hours} 小时"
                except ValueError:
                    message = "❌ 请输入有效的数字"

        elif action == 'set_max':
            val = request.form.get('max_devices', '').strip()
            try:
                max_d = int(val)
                device_manager.set_max_devices(username, max_d)
                message = f"已设置 {username} 的最大设备数为 {max_d}"
            except ValueError:
                message = "❌ 请输入有效的数字"

        elif action == 'cleanup':
            # 清理此用户的僵尸设备
            idle_h = device_manager.get_idle_timeout(username)
            cleaned, _ = device_manager.cleanup_stale_devices(idle_timeout_hours=idle_h)
            message = f"已清理 {username} 的 {cleaned} 个僵尸设备"

    # 获取用户详情
    devices = device_manager.get_user_devices(username)
    idle_timeout = device_manager.get_idle_timeout(username)
    max_devices = device_manager.get_max_devices(username)

    return render_template_string(
        ADMIN_USER_DETAIL_TEMPLATE,
        username=username,
        devices=devices,
        idle_timeout=idle_timeout,
        max_devices=max_devices,
        default_idle=config.DEFAULT_IDLE_TIMEOUT_HOURS,
        message=message,
    )


# ==================== 管理API ====================

@app.route('/admin/api/stats', methods=['GET'])
def api_admin_stats():
    """查看全局设备统计（JSON）"""
    if not check_admin_auth():
        return jsonify({'error': 'unauthorized'}), 401

    stats = device_manager.get_global_stats()
    stats['auto_cleanup_enabled'] = config.DEVICE_CLEANUP_ENABLED
    stats['auto_cleanup_cron'] = config.DEVICE_CLEANUP_CRON
    stats['cleanup_idle_hours'] = config.DEVICE_CLEANUP_IDLE_HOURS

    if scheduler and scheduler.get_job('device_cleanup'):
        nt = scheduler.get_job('device_cleanup').next_run_time
        stats['next_cleanup'] = nt.strftime('%Y-%m-%d %H:%M:%S') if nt else None
    else:
        stats['next_cleanup'] = None

    stats['timestamp'] = datetime.now().isoformat()
    return jsonify(stats)


@app.route('/admin/api/users', methods=['GET'])
def api_admin_users():
    """获取所有用户列表（JSON，供UI Ajax使用）"""
    if not check_admin_auth():
        return jsonify({'error': 'unauthorized'}), 401

    users = device_manager.get_all_user_summaries()
    return jsonify({'users': users})


@app.route('/admin/cleanup', methods=['POST', 'GET'])
def admin_cleanup():
    """
    手动触发清理僵尸设备
    Web: 需要session登录
    API: 需要 admin_token 参数或 header
    """
    if not check_admin_auth():
        resp = require_admin()
        # 如果是Web请求，redirect；如果是API，返回JSON
        if resp is not None:
            return resp

    idle_hours_str = request.args.get('idle_hours') or request.form.get('idle_hours', '')
    if idle_hours_str:
        idle_hours = int(idle_hours_str)
        cleaned, detail = device_manager.cleanup_stale_devices(idle_timeout_hours=idle_hours)
    else:
        # 使用各账号独立设置
        cleaned, detail = device_manager.cleanup_stale_devices(idle_timeout_hours=None)

    if request.is_json or request.path.startswith('/admin/api/'):
        return jsonify({
            'success': True,
            'idle_timeout_hours': idle_hours_str or 'per-user',
            'cleaned_total': cleaned,
            'detail_by_user': detail,
            'timestamp': datetime.now().isoformat()
        })

    # Web: 重定向回仪表盘，带消息
    return redirect('/admin?msg=' + f'清理完成，共清理 {cleaned} 个僵尸设备')


# ==================== HTML模板 ====================

# ---------- 用户登录页 ----------
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WiFi 网络认证</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .container { background: white; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); padding: 40px; width: 380px; }
        h2 { text-align: center; color: #333; margin-bottom: 10px; }
        .subtitle { text-align: center; color: #999; font-size: 12px; margin-bottom: 25px; }
        .note { background: #fff8e1; border-left: 3px solid #ffc107; padding: 10px; border-radius: 0 6px 6px 0; margin-bottom: 20px; font-size: 12px; color: #666; line-height: 1.6; }
        input[type=text], input[type=password] { width: 100%; padding: 12px; margin: 10px 0; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        input[type=text]:focus, input[type=password]:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
        button { width: 100%; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; margin-top: 10px; }
        button:hover { opacity: 0.9; }
        .error { background: #ffe6e6; color: #cc0000; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .footer { text-align: center; margin-top: 20px; font-size: 12px; color: #999; }
    </style>
</head>
<body>
<div class="container">
    <h2>🔐 WiFi 网络认证</h2>
    <div class="subtitle">企业AD域账号登录</div>
    <div class="note">⚠️ 每次连接WiFi均需重新登录。<br>每个账号最多允许 <b>{{ max_devices }}</b> 台设备同时在线。</div>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <form method="POST">
        <input type="hidden" name="gw_address" value="{{ gw_address }}">
        <input type="hidden" name="gw_port" value="{{ gw_port }}">
        <input type="hidden" name="gw_id" value="{{ gw_id }}">
        <input type="hidden" name="mac" value="{{ mac }}">
        <input type="hidden" name="url" value="{{ url }}">
        <input type="text" name="username" placeholder="域用户名" required autofocus>
        <input type="password" name="password" placeholder="域密码" required>
        <button type="submit">登 录</button>
    </form>
    <div class="footer">企业WiFi认证系统</div>
</div>
</body>
</html>
"""

# ---------- 认证成功页 ----------
PORTAL_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>认证成功</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .container { background: white; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); padding: 50px; text-align: center; max-width: 420px; }
        .icon { font-size: 64px; margin-bottom: 20px; }
        h2 { color: #11998e; margin-bottom: 15px; }
        p { color: #666; line-height: 1.8; }
        .actions { margin-top: 30px; }
        .actions a { display: inline-block; padding: 10px 24px; background: #f0faf6; color: #11998e; border-radius: 6px; text-decoration: none; font-size: 14px; }
        .actions a:hover { background: #11998e; color: white; }
    </style>
</head>
<body>
<div class="container">
    <div class="icon">✅</div>
    <h2>认证成功！</h2>
    <p>您已成功连接到WiFi网络<br>请尽情享受网络服务</p>
    <div class="actions">
        <a href="/manage">管理我的设备</a>
    </div>
</div>
<script>setTimeout(function(){ window.location.href = '{{ url }}' || 'http://www.baidu.com'; }, 5000);</script>
</body>
</html>
"""

# ---------- 用户设备管理登录页 ----------
MANAGE_LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>设备管理 - 登录</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: #f0f2f5; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .container { background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); padding: 40px; width: 400px; }
        h2 { text-align: center; color: #333; margin-bottom: 8px; }
        .desc { text-align: center; color: #999; font-size: 13px; margin-bottom: 30px; }
        .error { background: #ffe6e6; color: #cc0000; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        input[type=text], input[type=password] { width: 100%; padding: 12px; margin: 10px 0; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        input[type=text]:focus, input[type=password]:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
        button { width: 100%; padding: 12px; background: #667eea; color: white; border: none; border-radius: 6px; font-size: 15px; cursor: pointer; }
        button:hover { background: #5a6fd6; }
    </style>
</head>
<body>
<div class="container">
    <h2>设备管理登录</h2>
    <div class="desc">使用AD域账号登录，管理您的在线设备</div>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <form method="POST">
        <input type="hidden" name="action" value="login">
        <input type="text" name="username" placeholder="域用户名" required autofocus>
        <input type="password" name="password" placeholder="域密码" required>
        <button type="submit">登 录</button>
    </form>
</div>
</body>
</html>
"""

# ---------- 用户设备管理主页 ----------
MANAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>我的设备管理</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: #f0f2f5; min-height: 100vh; padding: 40px; }
        .header { max-width: 800px; margin: 0 auto 20px; display: flex; justify-content: space-between; align-items: center; }
        .header h2 { color: #333; }
        .header .user { color: #666; font-size: 14px; }
        .header a { color: #667eea; text-decoration: none; font-size: 14px; margin-left: 15px; }
        .summary { max-width: 800px; margin: 0 auto 20px; background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); display: flex; gap: 30px; }
        .summary .item { text-align: center; flex: 1; }
        .summary .num { font-size: 32px; font-weight: bold; color: #667eea; }
        .summary .label { font-size: 13px; color: #999; margin-top: 5px; }
        .warn { max-width: 800px; margin: 0 auto 20px; background: #fff8e1; border-left: 3px solid #ffc107; padding: 12px 16px; border-radius: 0 8px 8px 0; font-size: 13px; color: #666; }
        .device-list { max-width: 800px; margin: 0 auto; background: white; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); overflow: hidden; }
        .device-list table { width: 100%; border-collapse: collapse; }
        .device-list th { background: #fafafa; padding: 12px 16px; text-align: left; font-size: 13px; color: #666; border-bottom: 1px solid #eee; }
        .device-list td { padding: 12px 16px; font-size: 13px; color: #333; border-bottom: 1px solid #f5f5f5; }
        .device-list tr:hover { background: #fafbff; }
        .btn-kick { background: #ff5252; color: white; border: none; padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; }
        .btn-kick:hover { background: #e04848; }
        .empty { text-align: center; padding: 40px; color: #999; font-size: 14px; }
        .note { max-width: 800px; margin: 20px auto 0; font-size: 12px; color: #bbb; text-align: center; }
    </style>
</head>
<body>
<div class="header">
    <div><h2>我的设备管理</h2></div>
    <div class="user">
        {{ username }}
        <a href="#" onclick="document.getElementById('logout-form').submit(); return false;">退出</a>
    </div>
</div>
<form id="logout-form" method="POST" style="display:none;">
    <input type="hidden" name="action" value="logout">
</form>
<div class="summary">
    <div class="item"><div class="num">{{ current_count }}</div><div class="label">当前在线设备</div></div>
    <div class="item"><div class="num">{{ max_devices }}</div><div class="label">最大允许设备数</div></div>
</div>
{% if current_count >= max_devices %}
<div class="warn">⚠️ 您已用完所有设备名额。连接新设备时，最早登录的设备将被自动断开。</div>
{% endif %}
<div class="device-list">
    {% if devices %}
    <table>
        <tr><th>IP地址</th><th>网关</th><th>登录时间</th><th>最近活跃</th><th>操作</th></tr>
        {% for d in devices %}
        <tr>
            <td>{{ d.ip }}</td><td>{{ d.gw_id }}</td><td>{{ d.login_time }}</td><td>{{ d.last_seen }}</td>
            <td>
                <form method="POST" style="display:inline;" onsubmit="return confirm('确定踢出该设备？');">
                    <input type="hidden" name="action" value="kick">
                    <input type="hidden" name="kick_token" value="{{ d.token }}">
                    <button type="submit" class="btn-kick">踢出</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <div class="empty">暂无在线设备</div>
    {% endif %}
</div>
<div class="note">提示：踢出设备后，该设备将被断开网络连接，需重新登录。</div>
</body>
</html>
"""

# ---------- 管理员登录页 ----------
ADMIN_LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理员登录</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: linear-gradient(135deg, #1a237e 0%, #283593 50%, #1565c0 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .container { background: white; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.3); padding: 40px; width: 400px; }
        h2 { text-align: center; color: #1a237e; margin-bottom: 8px; }
        .desc { text-align: center; color: #999; font-size: 13px; margin-bottom: 30px; }
        .error { background: #ffe6e6; color: #cc0000; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        input[type=password] { width: 100%; padding: 12px; margin: 10px 0; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        input[type=password]:focus { outline: none; border-color: #1a237e; box-shadow: 0 0 0 3px rgba(26,35,126,0.1); }
        button { width: 100%; padding: 12px; background: linear-gradient(135deg, #1a237e 0%, #283593 100%); color: white; border: none; border-radius: 6px; font-size: 15px; cursor: pointer; }
        button:hover { opacity: 0.9; }
        .hint { text-align: center; margin-top: 15px; font-size: 12px; color: #bbb; }
    </style>
</head>
<body>
<div class="container">
    <h2>🔒 管理员登录</h2>
    <div class="desc">请输入管理令牌（ADMIN_TOKEN）</div>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <form method="POST">
        <input type="password" name="admin_token" placeholder="管理令牌" required autofocus>
        <button type="submit">登 录</button>
    </form>
    <div class="hint">令牌在 .env 文件的 ADMIN_TOKEN 中配置</div>
</div>
</body>
</html>
"""

# ---------- 管理员主界面（仪表盘）----------
ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理后台 - WiFiDog AuthServer</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: #f0f2f5; min-height: 100vh; }
        .topbar { background: linear-gradient(135deg, #1a237e 0%, #283593 100%); color: white; padding: 0 30px; height: 56px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
        .topbar h1 { font-size: 18px; font-weight: 600; }
        .topbar a { color: rgba(255,255,255,0.8); text-decoration: none; font-size: 13px; }
        .topbar a:hover { color: white; }
        .main { max-width: 1100px; margin: 30px auto; padding: 0 20px; }
        .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin-bottom: 30px; }
        .card { background: white; border-radius: 10px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }
        .card .num { font-size: 36px; font-weight: bold; color: #1a237e; }
        .card .label { font-size: 13px; color: #999; margin-top: 6px; }
        .card.stale .num { color: #ff9800; }
        .card.users .num { color: #43a047; }
        .section { background: white; border-radius: 10px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }
        .section h3 { color: #333; font-size: 16px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #eee; }
        .field { display: flex; align-items: center; margin-bottom: 12px; }
        .field label { width: 180px; color: #666; font-size: 14px; }
        .field span { font-size: 14px; color: #333; }
        .field .on { color: #43a047; font-weight: bold; }
        .field .off { color: #e53935; font-weight: bold; }
        .btn { display: inline-block; padding: 8px 20px; border-radius: 6px; font-size: 13px; text-decoration: none; cursor: pointer; border: none; }
        .btn-primary { background: #1a237e; color: white; }
        .btn-primary:hover { background: #283593; }
        .btn-sm { padding: 6px 14px; font-size: 12px; }
        .btn-outline { background: white; color: #1a237e; border: 1px solid #1a237e; }
        .btn-outline:hover { background: #f0f0ff; }
        .btn-danger { background: #e53935; color: white; }
        .btn-danger:hover { background: #c62828; }
        .btn-row { display: flex; gap: 10px; margin-top: 16px; }
        .msg { background: #e8f5e9; color: #2e7d32; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 10px 12px; font-size: 13px; color: #999; border-bottom: 1px solid #eee; }
        td { padding: 10px 12px; font-size: 13px; color: #333; border-bottom: 1px solid #f5f5f5; }
        tr:hover { background: #fafbff; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
        .badge-default { background: #e8eaf6; color: #1a237e; }
        .badge-custom { background: #fff3e0; color: #e65100; }
        .badge-warn { background: #fff8e1; color: #f57f17; }
    </style>
</head>
<body>
<div class="topbar">
    <h1>🔧 WiFiDog 管理后台</h1>
    <div>
        <a href="/admin/users">用户管理 →</a>
        <a href="/admin/logout" style="margin-left:20px;">退出登录</a>
    </div>
</div>

<div class="main">
    <!-- 统计卡片 -->
    <div class="grid4">
        <div class="card">
            <div class="num">{{ stats.total_devices }}</div>
            <div class="label">在线设备总数</div>
        </div>
        <div class="card users">
            <div class="num">{{ stats.total_users }}</div>
            <div class="label">活跃用户数</div>
        </div>
        <div class="card stale">
            <div class="num">{{ stats.stale_count }}</div>
            <div class="label">僵尸设备数</div>
        </div>
        <div class="card">
            <div class="num">{{ stats.default_max_devices }}</div>
            <div class="label">默认最大设备数</div>
        </div>
    </div>

    <!-- 全局设置 -->
    <div class="section">
        <h3>⚙️ 全局设置</h3>
        <div class="field">
            <label>定时清理任务</label>
            <span class="{% if config.DEVICE_CLEANUP_ENABLED %}on{% else %}off{% endif %}">
                {% if config.DEVICE_CLEANUP_ENABLED %}✅ 已启用{% else %}❌ 已禁用{% endif %}
            </span>
        </div>
        <div class="field">
            <label>清理周期（Cron）</label>
            <span>{{ config.DEVICE_CLEANUP_CRON }}</span>
        </div>
        <div class="field">
            <label>清理超时阈值</label>
            <span>{% if config.DEVICE_CLEANUP_IDLE_HOURS > 0 %}{{ config.DEVICE_CLEANUP_IDLE_HOURS }} 小时（统一）{% else %}使用各账号独立设置{% endif %}</span>
        </div>
        <div class="field">
            <label>全局默认心跳超时</label>
            <span>{{ stats.default_idle_timeout_hours }} 小时</span>
        </div>
        <div class="field">
            <label>下次自动清理</label>
            <span>{% if next_cleanup %}{{ next_cleanup }}{% else %}未启用{% endif %}</span>
        </div>
        <div class="field">
            <label>当前时间</label>
            <span>{{ now }}</span>
        </div>
        <div class="btn-row">
            <a href="/admin/cleanup" class="btn btn-primary" onclick="return confirm('确定立即执行一次清理？');">立即清理僵尸设备</a>
            <a href="/admin/users" class="btn btn-outline">管理用户设置</a>
        </div>
    </div>

    <!-- 最近活跃用户（简要）-->
    <div class="section">
        <h3>👥 快捷操作</h3>
        <div class="btn-row">
            <a href="/admin/users" class="btn btn-primary">查看所有用户 →</a>
        </div>
    </div>
</div>
</body>
</html>
"""

# ---------- 用户管理列表页 ----------
ADMIN_USERS_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>用户管理 - 管理后台</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: #f0f2f5; min-height: 100vh; }
        .topbar { background: linear-gradient(135deg, #1a237e 0%, #283593 100%); color: white; padding: 0 30px; height: 56px; display: flex; align-items: center; justify-content: space-between; }
        .topbar h1 { font-size: 18px; font-weight: 600; }
        .topbar a { color: rgba(255,255,255,0.8); text-decoration: none; font-size: 13px; }
        .topbar a:hover { color: white; }
        .main { max-width: 1100px; margin: 30px auto; padding: 0 20px; }
        .section { background: white; border-radius: 10px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
        .section h3 { color: #333; font-size: 16px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #eee; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 10px 12px; font-size: 13px; color: #999; border-bottom: 1px solid #eee; }
        td { padding: 10px 12px; font-size: 13px; color: #333; border-bottom: 1px solid #f5f5f5; }
        tr:hover { background: #fafbff; }
        .btn { display: inline-block; padding: 6px 14px; border-radius: 6px; font-size: 12px; text-decoration: none; cursor: pointer; border: none; }
        .btn-primary { background: #1a237e; color: white; }
        .btn-primary:hover { background: #283593; }
        .btn-sm { padding: 4px 10px; font-size: 11px; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
        .badge-default { background: #e8eaf6; color: #1a237e; }
        .badge-custom { background: #fff3e0; color: #e65100; }
        .badge-warn { background: #fff8e1; color: #f57f17; }
        .stale-yes { color: #ff9800; font-weight: bold; }
        .empty { text-align: center; padding: 40px; color: #999; }
    </style>
</head>
<body>
<div class="topbar">
    <h1><a href="/admin" style="color:white; text-decoration:none;">← 管理后台</a></h1>
    <div><a href="/admin/logout">退出登录</a></div>
</div>
<div class="main">
    <div class="section">
        <h3>👥 所有用户（{{ users|length }} 人）</h3>
        {% if users %}
        <table>
            <tr>
                <th>用户名</th>
                <th>在线设备</th>
                <th>最大设备数</th>
                <th>心跳超时</th>
                <th>僵尸设备</th>
                <th>操作</th>
            </tr>
            {% for u in users %}
            <tr>
                <td><strong>{{ u.username }}</strong></td>
                <td>{{ u.device_count }}</td>
                <td>{{ u.max_devices }}</td>
                <td>
                    {% if u.idle_timeout_hours == default_idle %}
                    <span class="badge badge-default">默认（{{ default_idle }}h）</span>
                    {% else %}
                    <span class="badge badge-custom">{{ u.idle_timeout_hours }}h</span>
                    {% endif %}
                </td>
                <td>
                    {% if u.stale_count > 0 %}
                    <span class="stale-yes">{{ u.stale_count }} 台</span>
                    {% else %}
                    0
                    {% endif %}
                </td>
                <td>
                    <a href="/admin/user/{{ u.username }}" class="btn btn-primary btn-sm">管理</a>
                </td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div class="empty">暂无用户设备记录</div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""

# ---------- 单个用户管理页 ----------
ADMIN_USER_DETAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>用户详情 - 管理后台</title>
    <style>
        * { margin:0; padding:0; box-sizing: border-box; }
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; background: #f0f2f5; min-height: 100vh; }
        .topbar { background: linear-gradient(135deg, #1a237e 0%, #283593 100%); color: white; padding: 0 30px; height: 56px; display: flex; align-items: center; justify-content: space-between; }
        .topbar h1 { font-size: 18px; font-weight: 600; }
        .topbar a { color: rgba(255,255,255,0.8); text-decoration: none; font-size: 13px; }
        .main { max-width: 1000px; margin: 30px auto; padding: 0 20px; }
        .section { background: white; border-radius: 10px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }
        .section h3 { color: #333; font-size: 16px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #eee; }
        .field { display: flex; align-items: center; margin-bottom: 12px; }
        .field label { width: 160px; color: #666; font-size: 14px; }
        .field input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; width: 200px; }
        .field .val { font-size: 14px; color: #333; }
        .btn { display: inline-block; padding: 8px 20px; border-radius: 6px; font-size: 13px; text-decoration: none; cursor: pointer; border: none; }
        .btn-primary { background: #1a237e; color: white; }
        .btn-outline { background: white; color: #1a237e; border: 1px solid #1a237e; }
        .btn-danger { background: #e53935; color: white; }
        .btn-sm { padding: 6px 14px; font-size: 12px; }
        .btn-row { display: flex; gap: 10px; margin-top: 16px; }
        .msg { background: #e8f5e9; color: #2e7d32; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 10px 12px; font-size: 13px; color: #999; border-bottom: 1px solid #eee; }
        td { padding: 10px 12px; font-size: 13px; color: #333; border-bottom: 1px solid #f5f5f5; }
        .form-row { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
        .form-row label { width: 140px; color: #666; font-size: 14px; flex-shrink: 0; }
        .form-row input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; width: 160px; }
        .form-row .hint { font-size: 12px; color: #999; }
    </style>
</head>
<body>
<div class="topbar">
    <h1><a href="/admin/users" style="color:white; text-decoration:none;">← 用户管理</a></h1>
    <div><a href="/admin/logout">退出登录</a></div>
</div>

<div class="main">
    {% if message %}
    <div class="msg">{{ message }}</div>
    {% endif %}

    <!-- 用户设置 -->
    <div class="section">
        <h3>⚙️ 用户设置：{{ username }}</h3>

        <form method="POST" class="form-row">
            <input type="hidden" name="action" value="set_idle">
            <label>心跳超时（小时）</label>
            <input type="text" name="idle_timeout" value="{% if idle_timeout != default_idle %}{{ idle_timeout }}{% endif %}" placeholder="留空使用全局默认（{{ default_idle }}h）">
            <span class="hint">留空或填0 = 使用全局默认（{{ default_idle }}h）</span>
            <button type="submit" class="btn btn-primary btn-sm">保存</button>
        </form>

        <form method="POST" class="form-row">
            <input type="hidden" name="action" value="set_max">
            <label>最大设备数</label>
            <input type="text" name="max_devices" value="{{ max_devices }}">
            <span class="hint">当前在线：{{ devices|length }} 台</span>
            <button type="submit" class="btn btn-primary btn-sm">保存</button>
        </form>

        <form method="POST" class="form-row">
            <input type="hidden" name="action" value="cleanup">
            <label>清理僵尸设备</label>
            <span class="hint">立即清理此用户超过 {{ idle_timeout }}h 无心跳的设备</span>
            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('确定清理此用户的僵尸设备？');">立即清理</button>
        </form>
    </div>

    <!-- 设备列表 -->
    <div class="section">
        <h3>📱 在线设备（{{ devices|length }} 台）</h3>
        {% if devices %}
        <table>
            <tr><th>IP地址</th><th>网关</th><th>登录时间</th><th>最近活跃</th></tr>
            {% for d in devices %}
            <tr>
                <td>{{ d.get('ip', 'N/A') }}</td>
                <td>{{ d.get('gw_id', 'N/A') }}</td>
                <td>{{ (d.get('login_time', 0)|int)|datetimeformat }}</td>
                <td>{{ (d.get('last_seen', 0)|int)|datetimeformat }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div style="text-align:center;padding:30px;color:#999;">暂无在线设备</div>
        {% endif %}
    </div>
</div>

<script>
// 简单的时间戳格式化（Jinja2不直接支持，用JS补位）
document.querySelectorAll('td').forEach(td => {
    const t = parseInt(td.textContent);
    if (!isNaN(t) && t > 1000000000) {
        td.textContent = new Date(t * 1000).toLocaleString('zh-CN');
    }
});
</script>
</body>
</html>
"""

# ---------- 消息模板（暂未用到）----------
MESSAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>认证消息</title>
<style>body{font-family:'Microsoft YaHei',Arial,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;}
.container{background:white;padding:40px;border-radius:12px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,0.1);}
h2{color:#cc0000;}</style></head>
<body><div class="container"><h2>认证失败</h2><p>消息类型: {{ message }}</p><p><a href="/login">重新登录</a></p></div></body></html>
"""


# ==================== 启动入口 ====================

if __name__ == '__main__':
    print("=" * 60)
    print("  WiFiDog AuthServer 启动 (AD域认证 + 自动清理 + 管理UI)")
    print(f"  监听地址   : http://{config.FLASK_HOST}:{config.FLASK_PORT}")
    print(f"  AD域控     : {config.AD_SERVER}")
    print(f"  Redis      : {config.REDIS_HOST}:{config.REDIS_PORT}")
    print(f"  默认设备数 : {config.DEFAULT_MAX_DEVICES} 台/用户")
    print(f"  默认心跳超时: {config.DEFAULT_IDLE_TIMEOUT_HOURS} 小时")
    print(f"  token有效期 : {config.TOKEN_EXPIRE_SECONDS // 3600} 小时")
    print(f"  用户管理页 : http://<服务器IP>:{config.FLASK_PORT}/manage")
    print(f"  管理员页面 : http://<服务器IP>:{config.FLASK_PORT}/admin")
    print(f"  定时清理   : {'[已启用]' if config.DEVICE_CLEANUP_ENABLED else '[已禁用]'}")
    if config.DEVICE_CLEANUP_ENABLED:
        print(f"  清理周期   : {config.DEVICE_CLEANUP_CRON}")
        print(f"  清理超时   : {'各账号独立设置' if config.DEVICE_CLEANUP_IDLE_HOURS == 0 else f'{config.DEVICE_CLEANUP_IDLE_HOURS}h 统一'}")
    if not config.ADMIN_TOKEN:
        print(f"  [!] 警告  : ADMIN_TOKEN 未设置，管理页面无法登录！")
    print("=" * 60)

    init_scheduler()

    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
