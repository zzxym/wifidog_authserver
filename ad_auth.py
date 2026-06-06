"""
AD域LDAP认证模块
使用 ldap3 库（纯Python，跨平台，Windows安装无坑）

认证流程：
  1. 用服务账号(AD_BIND_DN)绑定LDAP，搜索用户名获取用户DN
  2. 用用户DN + 用户密码尝试绑定LDAP
  3. 绑定成功 → 密码正确
"""

from ldap3 import Server, Connection, ALL
from config import config
import traceback


def _make_server():
    """构造ldap3 Server对象"""
    return Server(
        config.AD_SERVER,
        get_info=ALL,
        connect_timeout=5,
    )


def authenticate(username, password):
    r"""
    验证AD域用户凭据

    参数:
        username: 用户名，支持以下格式：
                   - sAMAccountName（如: zhangsan）
                   - DOMAIN\sAMAccountName（如: CORP\zhangsan）
                   - userPrincipalName（如: zhangsan@corp.com）
        password: 明文密码

    返回:
        (True, {'dn':..., 'sAMAccountName':..., 'displayName':..., 'mail':...})  → 认证成功
        (False, None)                                                                    → 认证失败
    """
    if not password:
        return False, None

    srv = _make_server()

    # ── 1. 用服务账号绑定，搜索用户DN ──────────────────
    try:
        search_conn = Connection(
            srv,
            user=config.AD_BIND_DN,
            password=config.AD_BIND_PASSWORD,
            auto_bind=True,
            read_only=True,
        )
    except Exception as e:
        print(f"[ADAuth] 服务账号绑定失败: {e}")
        return False, None

    # 解析用户名，构造搜索过滤器
    sam_account = username
    upn = None

    if '\\' in username:
        # DOMAIN\sAMAccountName 格式，注意转义反斜杠
        sam_account = username.split('\\')[-1]
    elif '@' in username:
        # user@domain.com 格式
        upn = username

    if upn:
        search_filter = f"(userPrincipalName={upn})"
    else:
        search_filter = config.AD_USER_FILTER.format(username=sam_account)

    search_base = config.AD_USER_BASE_DN or config.AD_BASE_DN

    try:
        ok = search_conn.search(
            search_base,
            search_filter,
            attributes=[config.AD_USERNAME_ATTR,
                        config.AD_EMAIL_ATTR,
                        config.AD_DISPLAY_NAME_ATTR]
        )
    except Exception as e:
        print(f"[ADAuth] 搜索用户失败: {e}")
        search_conn.unbind()
        return False, None

    if not ok or not search_conn.entries:
        print(f"[ADAuth] 用户未找到: username={username}, filter={search_filter}")
        search_conn.unbind()
        return False, None

    user_dn = search_conn.entries[0].entry_dn
    user_attrs = {
        'dn': user_dn,
        'sAMAccountName': str(getattr(search_conn.entries[0], 'sAMAccountName', '') or ''),
        'displayName': str(getattr(search_conn.entries[0], 'displayName', '') or ''),
        'mail': str(getattr(search_conn.entries[0], 'mail', '') or ''),
    }
    search_conn.unbind()

    # ── 2. 用用户DN + 密码尝试绑定 ─────────────────────
    try:
        user_conn = Connection(
            srv,
            user=user_dn,
            password=password,
            auto_bind=True,
        )
        print(f"[ADAuth] 认证成功: user_dn={user_dn}")
        user_conn.unbind()
        return True, user_attrs
    except Exception as e:
        print(f"[ADAuth] 密码验证失败: user_dn={user_dn}, err={e}")
        return False, None


def test_connection():
    """
    测试AD域控连接和服务账号绑定
    返回: (success: bool, message: str)
    """
    try:
        srv = _make_server()
        conn = Connection(
            srv,
            user=config.AD_BIND_DN,
            password=config.AD_BIND_PASSWORD,
            auto_bind=True,
        )
        info = srv.info
        domain = ''
        if info and hasattr(info, 'naming_contexts') and info.naming_contexts:
            domain = info.naming_contexts[0]
        conn.unbind()
        return True, f"AD连接成功, Server={srv.host}, Base={domain}"
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ADAuth] 连接测试失败: {e}\n{tb}")
        return False, f"AD连接失败: {e}"


# 全局单例，供 app.py 通过 `from ad_auth import ad_auth` 使用
class _AdAuth:
    authenticate = staticmethod(authenticate)
    test_connection = staticmethod(test_connection)

ad_auth = _AdAuth()
