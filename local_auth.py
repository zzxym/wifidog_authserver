"""
本地用户认证模块
使用 SQLite 存储用户信息，passlib + bcrypt 进行密码哈希

设计要点：
  - SQLite 轻量级嵌入式数据库，适合 ~3000 用户规模
  - 写操作自动使用 WAL 模式 + 连接池，支持并发读写
  - bcrypt 密码哈希，抵御暴力破解
  - 支持创建、查询、修改、删除、批量导入用户
  - 线程安全：使用 threading.Lock 保护写操作

数据库表结构:
  users:
    - id          INTEGER PRIMARY KEY AUTOINCREMENT
    - username    TEXT UNIQUE NOT NULL          -- 登录用户名
    - password    TEXT NOT NULL                 -- bcrypt 哈希
    - display_name TEXT DEFAULT ''             -- 显示名称
    - email       TEXT DEFAULT ''              -- 邮箱
    - enabled     INTEGER DEFAULT 1            -- 是否启用 (0=禁用, 1=启用)
    - is_admin    INTEGER DEFAULT 0            -- 是否管理员 (0=否, 1=是)
    - created_at  TEXT DEFAULT (datetime('now','localtime'))
    - updated_at  TEXT DEFAULT (datetime('now','localtime'))
"""

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import bcrypt

from config import config, BINARY_DIR


def hash_password(password):
    """使用 bcrypt 哈希密码"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password, hashed):
    """验证密码"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


class LocalAuth:
    """本地用户认证管理器（SQLite）"""

    def __init__(self):
        # 数据库文件放在程序根目录
        db_dir = str(BINARY_DIR)
        self.db_path = os.path.join(db_dir, 'local_users.db')
        self._lock = threading.Lock()
        self._init_db()

    # ==================== 数据库初始化 ====================

    def _get_conn(self):
        """获取数据库连接（自动启用 WAL 模式和外键）"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        """初始化数据库表"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        username    TEXT UNIQUE NOT NULL,
                        password    TEXT NOT NULL,
                        display_name TEXT DEFAULT '',
                        email       TEXT DEFAULT '',
                        enabled     INTEGER DEFAULT 1,
                        is_admin    INTEGER DEFAULT 0,
                        created_at  TEXT DEFAULT (datetime('now','localtime')),
                        updated_at  TEXT DEFAULT (datetime('now','localtime'))
                    )
                """)
                # 创建索引加速查询
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_username
                    ON users(username)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_enabled
                    ON users(enabled)
                """)
                conn.commit()
            finally:
                conn.close()

    # ==================== 认证核心 ====================

    def authenticate(self, username, password):
        """
        验证本地用户凭据

        参数:
            username: 用户名
            password: 明文密码

        返回:
            (True, {'username':..., 'display_name':..., 'email':..., ...})  → 认证成功
            (False, None) → 认证失败
        """
        if not username or not password:
            return False, None

        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND enabled = 1",
                (username,)
            ).fetchone()

            if not row:
                return False, None

            if not verify_password(password, row['password']):
                return False, None

            return True, {
                'username': row['username'],
                'display_name': row['display_name'] or row['username'],
                'email': row['email'],
                'enabled': bool(row['enabled']),
                'is_admin': bool(row['is_admin']),
            }
        finally:
            conn.close()

    # ==================== 用户 CRUD ====================

    def create_user(self, username, password, display_name='', email='',
                    enabled=True, is_admin=False):
        """
        创建本地用户

        返回: (success: bool, message: str)
        """
        if not username or not password:
            return False, "用户名和密码不能为空"

        username = username.strip().lower()

        if len(password) < 4:
            return False, "密码长度不能少于4位"

        password_hash = hash_password(password)

        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO users
                       (username, password, display_name, email, enabled, is_admin,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'),
                               datetime('now','localtime'))""",
                    (username, password_hash, display_name or username, email,
                     1 if enabled else 0, 1 if is_admin else 0)
                )
                conn.commit()
                return True, f"用户 {username} 创建成功"
            except sqlite3.IntegrityError:
                return False, f"用户名 {username} 已存在"
            finally:
                conn.close()

    def update_user(self, username, **kwargs):
        """
        更新用户信息
        支持更新: password, display_name, email, enabled, is_admin

        返回: (success: bool, message: str)
        """
        username = username.strip().lower()

        with self._lock:
            conn = self._get_conn()
            try:
                # 检查用户是否存在
                existing = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if not existing:
                    return False, f"用户 {username} 不存在"

                updates = []
                params = []

                if 'password' in kwargs and kwargs['password']:
                    if len(kwargs['password']) < 4:
                        return False, "密码长度不能少于4位"
                    updates.append("password = ?")
                    params.append(hash_password(kwargs['password']))

                if 'display_name' in kwargs:
                    updates.append("display_name = ?")
                    params.append(kwargs['display_name'])

                if 'email' in kwargs:
                    updates.append("email = ?")
                    params.append(kwargs['email'])

                if 'enabled' in kwargs:
                    updates.append("enabled = ?")
                    params.append(1 if kwargs['enabled'] else 0)

                if 'is_admin' in kwargs:
                    updates.append("is_admin = ?")
                    params.append(1 if kwargs['is_admin'] else 0)

                if not updates:
                    return False, "没有需要更新的字段"

                updates.append("updated_at = datetime('now','localtime')")
                params.append(username)

                conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE username = ?",
                    params
                )
                conn.commit()
                return True, f"用户 {username} 更新成功"
            finally:
                conn.close()

    def delete_user(self, username):
        """删除用户"""
        username = username.strip().lower()

        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM users WHERE username = ?", (username,)
                )
                conn.commit()
                if cursor.rowcount > 0:
                    return True, f"用户 {username} 已删除"
                return False, f"用户 {username} 不存在"
            finally:
                conn.close()

    def get_user(self, username):
        """
        获取单个用户信息（不含密码哈希）

        返回: dict or None
        """
        username = username.strip().lower()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, username, display_name, email, enabled, is_admin, "
                "created_at, updated_at FROM users WHERE username = ?",
                (username,)
            ).fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def list_users(self, page=1, per_page=100, search=''):
        """
        分页获取用户列表

        返回: {
            'users': [...],
            'total': int,
            'page': int,
            'per_page': int,
            'total_pages': int,
        }
        """
        conn = self._get_conn()
        try:
            if search:
                where = "WHERE username LIKE ? OR display_name LIKE ? OR email LIKE ?"
                like = f"%{search}%"
                count_row = conn.execute(
                    f"SELECT COUNT(*) as cnt FROM users {where}",
                    (like, like, like)
                ).fetchone()
                total = count_row['cnt']

                offset = (page - 1) * per_page
                rows = conn.execute(
                    f"SELECT id, username, display_name, email, enabled, is_admin, "
                    f"created_at, updated_at FROM users {where} "
                    f"ORDER BY username ASC LIMIT ? OFFSET ?",
                    (like, like, like, per_page, offset)
                ).fetchall()
            else:
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM users"
                ).fetchone()
                total = count_row['cnt']

                offset = (page - 1) * per_page
                rows = conn.execute(
                    "SELECT id, username, display_name, email, enabled, is_admin, "
                    "created_at, updated_at FROM users "
                    "ORDER BY username ASC LIMIT ? OFFSET ?",
                    (per_page, offset)
                ).fetchall()

            return {
                'users': [dict(r) for r in rows],
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': max(1, (total + per_page - 1) // per_page),
            }
        finally:
            conn.close()

    def count_users(self):
        """获取用户总数"""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            return row['cnt']
        finally:
            conn.close()

    def change_password(self, username, old_password, new_password):
        """
        修改密码（需要验证旧密码）

        返回: (success: bool, message: str)
        """
        if not self.authenticate(username, old_password)[0]:
            return False, "旧密码错误"

        if len(new_password) < 4:
            return False, "新密码长度不能少于4位"

        return self.update_user(username, password=new_password)

    def bulk_import(self, users_list):
        """
        批量导入用户

        参数:
            users_list: [{'username': str, 'password': str, 'display_name': str,
                          'email': str, 'enabled': bool}, ...]

        返回: (success_count: int, fail_count: int, errors: list)
        """
        success_count = 0
        fail_count = 0
        errors = []

        for u in users_list:
            ok, msg = self.create_user(
                username=u.get('username', ''),
                password=u.get('password', ''),
                display_name=u.get('display_name', ''),
                email=u.get('email', ''),
                enabled=u.get('enabled', True),
            )
            if ok:
                success_count += 1
            else:
                fail_count += 1
                errors.append(msg)

        return success_count, fail_count, errors


# 全局单例
local_auth = LocalAuth()
