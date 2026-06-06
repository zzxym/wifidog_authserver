"""
设备管理模块
使用Redis管理用户设备列表，支持设备数量限制和自动踢出最早登录设备

核心设计：
  - 每次操作前清理已过期（Redis自动删除）的token，防止设备数虚高
  - 每次心跳（update_last_seen）时滑动续期token TTL，避免在线用户被误踢
  - 支持全局默认心跳超时 + 按账号单独设置心跳超时
  - 全局定时清理 & 按账号心跳超时清除 并行工作
"""

import time
import redis
from config import config


class DeviceManager:
    """
    设备管理：每个用户允许最多N个设备同时在线
    当第N+1个设备登录时，自动踢出最早登录的设备

    Redis数据结构:
    - user_devices:{username}        -> sorted set (member: token, score: 登录时间戳)
    - device_token:{token}           -> hash {username, mac, ip, gw_id, login_time, last_seen, kicked}
    - user_max_devices:{username}    -> int (用户设备数量限制，可选，默认使用DEFAULT_MAX_DEVICES)
    - user_idle_timeout:{username}   -> int (用户心跳超时小时数，可选，默认使用DEFAULT_IDLE_TIMEOUT_HOURS)
    """

    def __init__(self):
        self.redis_client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            password=config.REDIS_PASSWORD,
            db=config.REDIS_DB,
            decode_responses=True,
            protocol=2  # 使用 RESP2 协议兼容旧版 Redis
        )
        self.default_max_devices = config.DEFAULT_MAX_DEVICES
        self.default_idle_timeout_hours = config.DEFAULT_IDLE_TIMEOUT_HOURS
        self.token_expire = config.TOKEN_EXPIRE_SECONDS

    # ==================== 内部方法 ====================

    def _cleanup_expired(self, username):
        """
        清理已过期/已删除的token（Redis已自动删除device_token:{t}的）
        这些token仍留在sorted set里会导致设备数统计错误
        """
        user_devices_key = f"user_devices:{username}"
        all_tokens = self.redis_client.zrange(user_devices_key, 0, -1)
        if not all_tokens:
            return 0

        expired = []
        for t in all_tokens:
            if not self.redis_client.exists(f"device_token:{t}"):
                expired.append(t)

        if expired:
            self.redis_client.zrem(user_devices_key, *expired)
            print(f"[DeviceManager] 清理{len(expired)}个过期token, 用户: {username}")
        return len(expired)

    def _kick_device(self, username, token, reason="kicked"):
        """
        踢出设备（从Redis中删除，wifidog会通过auth接口检测到token失效）
        """
        user_devices_key = f"user_devices:{username}"
        device_key = f"device_token:{token}"

        # 从用户设备列表中移除
        self.redis_client.zrem(user_devices_key, token)

        # 标记设备为已踢出（此时device_key可能还存在，也可能已过期）
        if self.redis_client.exists(device_key):
            self.redis_client.hset(device_key, 'kicked', '1')
            self.redis_client.hset(device_key, 'kick_reason', reason)
            self.redis_client.hset(device_key, 'kick_time', int(time.time()))

        print(f"[DeviceManager] 已标记踢出设备: {token[:8]}..., 原因: {reason}")

    # ==================== 设备数量限制 ====================

    def get_max_devices(self, username):
        """获取用户允许的最大设备数"""
        key = f"user_max_devices:{username}"
        value = self.redis_client.get(key)
        if value:
            return int(value)
        return self.default_max_devices

    def set_max_devices(self, username, max_devices):
        """设置用户允许的最大设备数"""
        key = f"user_max_devices:{username}"
        self.redis_client.set(key, max_devices)
        print(f"[DeviceManager] 设置用户 {username} 最大设备数: {max_devices}")

    # ==================== 心跳超时（按账号）====================

    def get_idle_timeout(self, username):
        """
        获取用户的心跳超时小时数
        优先使用按账号设置的值，否则返回全局默认值
        """
        key = f"user_idle_timeout:{username}"
        value = self.redis_client.get(key)
        if value:
            return int(value)
        return self.default_idle_timeout_hours

    def set_idle_timeout(self, username, idle_timeout_hours):
        """
        设置用户的心跳超时小时数
        设为 None 或 0 则使用全局默认值（删除按账号设置）
        """
        key = f"user_idle_timeout:{username}"
        if not idle_timeout_hours:
            self.redis_client.delete(key)
            print(f"[DeviceManager] 清除用户 {username} 的独立心跳超时，将使用全局默认值")
        else:
            self.redis_client.set(key, int(idle_timeout_hours))
            print(f"[DeviceManager] 设置用户 {username} 心跳超时: {idle_timeout_hours}h")

    def cleanup_stale_devices(self, idle_timeout_hours=None):
        """
        清理所有超过心跳超时时间的僵尸设备
        支持两种模式：
          1. idle_timeout_hours 有值：所有用户统一使用此超时时间（全局定时清理任务用）
          2. idle_timeout_hours 为 None：对每个设备使用其用户的独立超时设置（按账号清除用）
        返回: (总清理数, 按用户分布的清理详情)
        """
        now = int(time.time())
        cleaned_total = 0
        cleaned_detail = {}  # {username: count}

        cursor = 0
        while True:
            cursor, keys = self.redis_client.scan(cursor, match="device_token:*", count=200)
            for key in keys:
                token = key.split(":", 1)[1]
                info = self.redis_client.hgetall(key)
                if not info:
                    continue

                username = info.get('username', 'unknown')

                # 决定此设备使用的超时时间
                if idle_timeout_hours is not None:
                    timeout_h = idle_timeout_hours
                else:
                    timeout_h = self.get_idle_timeout(username)

                timeout_seconds = timeout_h * 3600

                last_seen_str = info.get('last_seen') or info.get('login_time', '0')
                last_seen = int(last_seen_str)

                if now - last_seen > timeout_seconds:
                    # 超时，清理
                    self.redis_client.delete(key)
                    user_devices_key = f"user_devices:{username}"
                    self.redis_client.zrem(user_devices_key, token)
                    self.redis_client.delete(f"token_user:{token}")
                    cleaned_total += 1
                    cleaned_detail[username] = cleaned_detail.get(username, 0) + 1
                    print(f"[DeviceManager] 清理僵尸设备: token={token[:8]}..., "
                          f"user={username}, idle={(now - last_seen) // 3600}h, "
                          f"threshold={timeout_h}h")

            if cursor == 0:
                break

        if cleaned_total > 0:
            print(f"[DeviceManager] 本次共清理 {cleaned_total} 个僵尸设备")
        else:
            print("[DeviceManager] 没有僵尸设备需要清理")
        return cleaned_total, cleaned_detail

    # ==================== 设备增删改查 ====================

    def add_device(self, username, token, mac, ip, gw_id):
        """
        添加用户设备
        如果超过限制，自动踢出最早登录的设备
        返回: (success: bool, kicked_token: str or None)
        """
        user_devices_key = f"user_devices:{username}"
        device_key = f"device_token:{token}"

        # 先清理已过期的token，防止设备数虚高
        self._cleanup_expired(username)

        current_time = int(time.time())

        # 获取当前有效设备列表
        devices = self.redis_client.zrange(user_devices_key, 0, -1, withscores=True)
        max_devices = self.get_max_devices(username)

        kicked_token = None

        # 如果超过限制，踢出最早的设备
        if len(devices) >= max_devices:
            oldest_token = devices[0][0]
            self._kick_device(username, oldest_token, reason="device_limit_exceeded")
            kicked_token = oldest_token
            print(f"[DeviceManager] 踢出最早设备: {oldest_token[:8]}..., 用户: {username}")

        # 添加新设备到sorted set（score为登录时间）
        self.redis_client.zadd(user_devices_key, {token: current_time})

        # 存储设备详细信息
        device_info = {
            'username': username,
            'mac': mac,
            'ip': ip,
            'gw_id': gw_id,
            'login_time': current_time,
            'last_seen': current_time,
        }
        self.redis_client.hset(device_key, mapping=device_info)
        self.redis_client.expire(device_key, self.token_expire)

        print(f"[DeviceManager] 添加设备: {token[:8]}..., 用户: {username}, MAC: {mac}, "
              f"当前有效设备数: {len(devices) + 1}")
        return True, kicked_token

    def remove_device(self, username, token):
        """主动移除设备（用户注销/手动踢出时调用）"""
        user_devices_key = f"user_devices:{username}"
        device_key = f"device_token:{token}"

        self.redis_client.zrem(user_devices_key, token)
        self.redis_client.delete(device_key)
        self.redis_client.delete(f"token_user:{token}")

        print(f"[DeviceManager] 移除设备: {token[:8]}..., 用户: {username}")

    def get_user_devices(self, username):
        """获取用户的所有有效在线设备（按登录时间排序，最早的在前面）"""
        # 先清理过期token
        self._cleanup_expired(username)

        user_devices_key = f"user_devices:{username}"
        devices = self.redis_client.zrange(user_devices_key, 0, -1, withscores=True)

        result = []
        for token, login_time in devices:
            device_key = f"device_token:{token}"
            device_info = self.redis_client.hgetall(device_key)
            if device_info:
                device_info['token'] = token
                device_info['login_time'] = login_time
                result.append(device_info)

        return result

    def update_last_seen(self, token):
        """
        更新设备最后活跃时间，并滑动续期token TTL
        每次网关调用/auth接口时触发
        返回: bool (token是否有效)
        """
        device_key = f"device_token:{token}"
        if not self.redis_client.exists(device_key):
            return False

        now = int(time.time())
        self.redis_client.hset(device_key, 'last_seen', now)

        # 滑动续期：只要设备还在活跃，token就不会过期
        self.redis_client.expire(device_key, self.token_expire)

        # 同时续期 token_user:{token}（用于/auth接口查找对应用户）
        self.redis_client.expire(f"token_user:{token}", self.token_expire)

        return True

    def is_token_valid(self, token):
        """检查token是否有效（未被踢出且未过期）"""
        device_key = f"device_token:{token}"
        if not self.redis_client.exists(device_key):
            return False

        device_info = self.redis_client.hgetall(device_key)
        if device_info.get('kicked') == '1':
            return False

        return True

    def get_token_info(self, token):
        """获取token对应的设备信息"""
        device_key = f"device_token:{token}"
        if not self.redis_client.exists(device_key):
            return None
        return self.redis_client.hgetall(device_key)

    # ==================== 统计方法 ====================

    def get_all_users(self):
        """获取所有有设备记录的用户列表"""
        users = set()
        cursor = 0
        while True:
            cursor, keys = self.redis_client.scan(cursor, match="user_devices:*", count=100)
            for key in keys:
                username = key.split(":", 1)[1]
                users.add(username)
            if cursor == 0:
                break
        return list(users)

    def get_all_user_summaries(self):
        """
        获取所有用户的简要统计（用于管理员UI列表）
        返回: [{'username', 'device_count', 'max_devices', 'idle_timeout_hours', 'stale_count'}, ...]
        """
        users = self.get_all_users()
        summaries = []
        now = int(time.time())

        for username in users:
            devices = self.get_user_devices(username)
            idle_h = self.get_idle_timeout(username)
            # 统计僵尸设备数
            stale_count = 0
            for d in devices:
                last_seen = int(d.get('last_seen') or d.get('login_time', '0'))
                if now - last_seen > idle_h * 3600:
                    stale_count += 1

            summaries.append({
                'username': username,
                'device_count': len(devices),
                'max_devices': self.get_max_devices(username),
                'idle_timeout_hours': idle_h,
                'stale_count': stale_count,
            })

        return summaries

    def get_global_stats(self):
        """
        获取全局统计信息
        返回: dict with total_devices, total_users, stale_count, etc.
        如果 Redis 不可用，返回带 error 标记的字典
        """
        try:
            return self._get_global_stats_inner()
        except redis.exceptions.ConnectionError as e:
            return {
                'error': True,
                'error_msg': f"Redis 连接失败: {e}",
                'total_devices': 0,
                'total_users': 0,
                'stale_count': 0,
                'default_idle_timeout_hours': self.default_idle_timeout_hours,
                'default_max_devices': self.default_max_devices,
            }

    def _get_global_stats_inner(self):
        now = int(time.time())
        total_devices = 0
        total_users = 0
        stale_count = 0

        cursor = 0
        while True:
            cursor, keys = self.redis_client.scan(cursor, match="user_devices:*", count=200)
            for key in keys:
                username = key.split(":", 1)[1]
                total_users += 1
                devices = self.redis_client.zrange(key, 0, -1)
                idle_h = self.get_idle_timeout(username)
                for token in devices:
                    total_devices += 1
                    dk = f"device_token:{token}"
                    info = self.redis_client.hgetall(dk)
                    if info:
                        last_seen = int(info.get('last_seen') or info.get('login_time', '0'))
                        if now - last_seen > idle_h * 3600:
                            stale_count += 1
                    else:
                        stale_count += 1  # token已过期但还在sorted set里
            if cursor == 0:
                break

        return {
            'total_devices': total_devices,
            'total_users': total_users,
            'stale_count': stale_count,
            'default_idle_timeout_hours': self.default_idle_timeout_hours,
            'default_max_devices': self.default_max_devices,
        }


# 全局实例
device_manager = DeviceManager()
