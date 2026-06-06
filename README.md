# WiFiDog AuthServer - AD域认证版

> 支持企业AD域账号认证、设备数量限制、**双重僵尸设备清理机制**的WiFiDog认证服务器
>
> ⚠️ **注意**：手机系统（iOS/Android）默认启用 MAC 随机化，每次连接 MAC 都不同，**无法用 MAC 识别设备**。本系统每次连接均需用户重新认证。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| AD域认证 | 使用企业AD域账号密码直接登录WiFi |
| 设备数量限制 | 每用户可设置最多N台设备同时在线 |
| 自动踢出 | 超出限制时自动踢出最早登录的设备 |
| **心跳超时清除（按账号）** | 全局默认 + 每账号可单独设置心跳超时，超时无心跳自动清除 |
| **全局定时清理（独立开关）** | 按cron周期执行清理，与按账号清除并行工作 |
| 管理员UI | 直观简洁的Web管理后台，支持全局/按账号配置 |
| 用户自助管理 | 用户可登录管理页面，查看并踢出自己的在线设备 |
| 标准协议 | 完整实现WiFiDog协议（login/auth/ping/portal） |
| 管理API | 提供REST API查询在线设备、设置限额、手动/自动清理 |

---

## 双重清理机制说明

系统同时运行两种清理机制，互不冲突：

### 机制一：按账号心跳超时清除（并行）

- 每个账号可设置独立的心跳超时时间（小时）
- 全局有默认超时时间（`DEFAULT_IDLE_TIMEOUT_HOURS`）
- 账号未单独设置时，自动使用全局默认值
- **此机制在全局定时清理任务执行时统一触发**（扫描所有设备，按各自账号的超时时间判断）

### 机制二：全局定时清理任务（独立开关）

- 有独立的启用/禁用开关（`DEVICE_CLEANUP_ENABLED`）
- 通过cron表达式设置执行周期（默认每天0点）
- 执行时扫描所有设备，按各账号的超时设置清理僵尸设备
- 也可设置为统一使用指定的超时时间（`DEVICE_CLEANUP_IDLE_HOURS > 0` 时覆盖按账号设置）

```
┌─────────────────────────────────────────────────────┐
│  双重清理机制（并行）                            │
├─────────────────────────────────────────────────────┤
│  机制一：按账号心跳超时                         │
│    zhangsan → 168h 超时                      │
│    lisi     → 24h 超时（独立设置）          │
│    默认       → DEFAULT_IDLE_TIMEOUT_HOURS     │
│    → 全局清理任务执行时，各账号按各自时间判断 │
├─────────────────────────────────────────────────────┤
│  机制二：全局定时清理（独立开关）              │
│    DEVICE_CLEANUP_ENABLED = True/False 独立控制 │
│    DEVICE_CLEANUP_CRON = "0 0 * * *"         │
│    → 到期自动执行，无需人工干预              │
└─────────────────────────────────────────────────────┘
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，**必须修改**以下配置：

```bash
cp .env.example .env
```

```ini
# ===== AD域配置 =====
AD_SERVER=ldap://your-dc.company.com
AD_BIND_DN=cn=wifidog_svc,ou=ServiceAccounts,dc=company,dc=com
AD_BIND_PASSWORD=your_service_password
AD_BASE_DN=dc=company,dc=com
AD_USER_BASE_DN=ou=Users,dc=company,dc=com

# ===== Redis配置 =====
REDIS_HOST=127.0.0.1

# ===== 设备限制 =====
DEFAULT_MAX_DEVICES=3

# ===== 心跳超时（按账号）=====
# 全局默认心跳超时（小时），账号未独立设置时使用此值
DEFAULT_IDLE_TIMEOUT_HOURS=168

# ===== 全局定时清理 =====
# 是否启用定时清理（独立开关）
DEVICE_CLEANUP_ENABLED=True

# 清理周期（crontab格式，默认每天0点）
DEVICE_CLEANUP_CRON=0 0 * * *

# 清理时使用的超时时间：
#   0       = 使用各账号自己的超时设置（推荐）
#   大于0  = 统一使用此值，覆盖按账号设置
DEVICE_CLEANUP_IDLE_HOURS=0

# ===== 管理安全 =====
# 管理令牌（必须设置复杂字符串，否则管理页面无法登录）
ADMIN_TOKEN=change_this_to_a_strong_random_token
```

### 3. 启动Redis

```bash
# Ubuntu/Debian
sudo apt install redis-server && sudo systemctl start redis

# Windows: Docker方式
docker run -d -p 6379:6379 redis:alpine
```

### 4. 启动AuthServer

```bash
python app.py
```

启动成功日志示例：

```
============================================================
  WiFiDog AuthServer 启动 (AD域认证 + 自动清理 + 管理UI)
  监听地址    : http://0.0.0.0:5000
  AD域控      : ldap://dc.company.com
  Redis       : 127.0.0.1:6379
  默认设备数  : 3 台/用户
  默认心跳超时: 168 小时
  token有效期 : 8 小时
  用户管理页  : http://<服务器IP>:5000/manage
  管理员页面  : http://<服务器IP>:5000/admin
  定时清理    : ✅ 已启用
  清理周期    : 0 0 * * *
  清理超时    : 各账号独立
============================================================
[定时清理] 已启用，cron='0 0 * * *'，超时阈值=各账号独立，下次执行: 2026-06-07 00:00:00
```

---

## 锐捷AC配置

在锐捷AC的 **WiFiDog/Portal认证** 配置页：

| 配置项 | 值 |
|--------|-----|
| 认证服务器URL | `http://your-server-ip:5000/login` |
| 协议类型 | WiFiDog |
| 心跳间隔 | 建议60秒 |
| 认证超时 | 建议300秒 |
| 端口 | 2060（网关监听端口，无需修改） |

> ⚠️ 锐捷AC通常携带MAC参数，但由于手机MAC随机化，系统不依赖MAC做设备识别，每次连接均要求重新认证。

---

## 管理员UI使用说明

访问 `http://<服务器IP>:5000/admin`，输入 `ADMIN_TOKEN` 登录。

### 主界面（仪表盘）

```
┌─────────────────────────────────────────────────────┐
│  🔧 WiFiDog 管理后台             用户管理 →  退出 │
├─────────────────────────────────────────────────────┤
│  [ 15 ]      [ 8 ]       [ 3 ]      [ 3 ]    │
│  在线设备总数  活跃用户数   僵尸设备数  默认最大设备 │
├─────────────────────────────────────────────────────┤
│  ⚙️ 全局设置                                      │
│  定时清理任务    : ✅ 已启用                       │
│  清理周期(Cron) : 0 0 * * *                    │
│  清理超时阈值   : 使用各账号独立设置              │
│  全局默认心跳   : 168 小时                      │
│  下次自动清理   : 2026-06-07 00:00:00        │
│  [立即清理僵尸设备]  [管理用户设置]             │
└─────────────────────────────────────────────────────┘
```

### 用户管理页面

```
┌─────────────────────────────────────────────────────┐
│  ← 管理后台               退出登录                 │
├─────────────────────────────────────────────────────┤
│  👥 所有用户（8 人）                                │
│  用户名    在线设备  最大设备数  心跳超时    操作    │
│  zhangsan   3       3          默认(168h)   [管理]│
│  lisi       1       5          24h(独立)   [管理]│
│  wangwu     2       3          默认(168h)   [管理]│
└─────────────────────────────────────────────────────┘
```

### 单个用户管理页面

```
┌─────────────────────────────────────────────────────┐
│  ← 用户管理               退出登录                 │
├─────────────────────────────────────────────────────┤
│  ⚙️ 用户设置：lisi                                │
│  心跳超时(小时): [24    ] 留空使用全局默认(168h) │
│  最大设备数    : [5     ] 当前在线：1 台          │
│  [立即清理此用户的僵尸设备]                         │
├─────────────────────────────────────────────────────┤
│  📱 在线设备（1 台）                              │
│  IP地址      网关     登录时间          最近活跃    │
│  192.168.1.100  rg-ac  06-06 08:00:00  ... │
└─────────────────────────────────────────────────────┘
```

---

## 按账号心跳超时配置示例

**场景**：大多数员工使用默认168h（7天）超时，但高管账号需要24h超时。

**操作**：
1. 登录管理员UI → 用户管理
2. 找到 `lisi` → 点击"管理"
3. 心跳超时(小时) 填入 `24` → 保存
4. 该账号的设备超过24小时无心跳即被自动清除，不影响其他账号

**效果**：

```
全局默认: 168h
zhangsan : 使用默认 (168h)
lisi     : 独立设置 24h  ← 覆盖默认
wangwu   : 使用默认 (168h)
```

---

## 关于MAC地址和设备识别

| 问题 | 说明 |
|------|------|
| 手机MAC随机化 | iOS 14+、Android 10+ 默认启用随机MAC，每次连接都不同 |
| MAC能否用于识别 | **不能**，iOS/Android的随机MAC每24小时或每次忘记网络后变化 |
| 本系统如何应对 | 每次WiFi连接均要求用户输入AD域账号密码认证 |
| 设备数量限制原理 | 限制的是**同时有效的token数量**，不是MAC数量 |
| 用户换手机/重装系统 | 旧token仍在有效期内会占用名额，用户需登录管理页面手动踢出 |

---

## 设备限制逻辑

```
用户登录第N+1台设备时：
   ┌─────────────────────────────────────┐
   │  获取该用户已登录设备列表(按时间排序)  │
   │  若 当前设备数 >= MAX_DEVICES:      │
   │      → 标记最早设备的token为"已踢出"  │
   │      → 下次心跳时WiFiDog网关会断开该设备│
   │  添加新设备token到用户设备列表        │
   └─────────────────────────────────────┘
```

踢出机制：当设备被标记踢出后，WiFiDog网关在下次调用 `/auth` 接口时（心跳间隔，默认60秒），会收到 `Auth: 0`，随后断开该设备的网络。

---

## 管理API

所有管理接口均需要在请求中携带 `admin_token` 参数或 `X-Admin-Token` HTTP头。

### 管理员登录（Web）

```
GET  /admin/login         # 显示登录页
POST /admin/login         # 提交 admin_token，成功后设置session
POST /admin/logout        # 注销
```

### 查看全局统计

```bash
GET /admin/api/stats?admin_token=xxx
```

返回示例：

```json
{
  "total_devices": 15,
  "total_users": 8,
  "stale_count": 3,
  "default_idle_timeout_hours": 168,
  "default_max_devices": 3,
  "auto_cleanup_enabled": true,
  "auto_cleanup_cron": "0 0 * * *",
  "cleanup_idle_hours": 0,
  "next_cleanup": "2026-06-07T00:00:00",
  "timestamp": "2026-06-06T09:18:42"
}
```

### 查看所有用户

```bash
GET /admin/api/users?admin_token=xxx
```

### 手动触发清理

```bash
# Web: 登录后访问 /admin/cleanup

# API: 使用默认超时策略（各账号独立）
GET  /admin/cleanup?admin_token=xxx

# API: 指定统一超时时间（覆盖按账号设置）
GET  /admin/cleanup?admin_token=xxx&idle_hours=24
```

### 设置用户最大设备数

```bash
POST /admin/user/<username>/max_devices?admin_token=xxx
Content-Type: application/json

{"max_devices": 5}
```

### 设置用户心跳超时

```bash
POST /admin/user/<username>/idle_timeout?admin_token=xxx
Content-Type: application/json

# 设置独立超时
{"idle_timeout_hours": 24}

# 清除独立设置（使用全局默认）
{"idle_timeout_hours": 0}
```

---

## AD域认证说明

### 认证流程

```
用户打开WiFi → 连接SSID → 打开浏览器访问任意网址
    → 锐捷AC重定向到 AuthServer /login 页面
    → 用户输入 域用户名 + 域密码
    → AuthServer通过LDAP验证AD域控
    → 验证通过 → 生成token → 重定向回锐捷AC
    → 锐捷AC调用 /auth?token=xxx 验证
    → 验证通过 → 放行上网
```

### 用户名格式

根据AD域配置不同，用户名可能需要以下格式之一：
- `username`（sAMAccountName，推荐）
- `DOMAIN\username`
- `user@domain.com`（UPN格式）

---

## 目录结构

```
wifidog_authserver/
├── app.py              # 主应用，WiFiDog协议 + 用户管理 + 管理员UI + 定时清理
├── ad_auth.py          # AD域LDAP认证模块（ldap3，纯Python，Windows无坑）
├── device_manager.py   # 设备管理，设备数量限制 + 按账号心跳超时清除
├── config.py           # 配置加载（支持.env）
├── requirements.txt    # Python依赖
├── .env.example       # 环境变量模板（含双重清理配置）
├── docker-compose.yml # Docker部署配置
├── Dockerfile          # Docker镜像构建
└── README.md          # 本文档
```

---

## 故障排查

| 问题 | 排查方法 |
|------|---------|
| 用户无法认证 | 检查AD_SERVER是否可达，`GET /admin/test_ad` 测试连接 |
| 设备未被踢出 | 检查Redis是否正常运行，`GET /admin/api/stats?admin_token=xxx` |
| 锐捷AC无法跳转 | 检查AuthServer防火墙，确保5000端口可访问 |
| 用户名额总被占满 | 启用自动清理；或引导用户使用 `/manage` 页面踢出旧设备 |
| AD认证失败 | 检查 `AD_BIND_DN` 格式，尝试在服务器上用 `ldapsearch` 直接测试 |
| 自动清理未执行 | 检查 `DEVICE_CLEANUP_CRON` 格式；查看启动日志确认定时任务已注册 |
| 管理页面无法登录 | 确认 `.env` 中 `ADMIN_TOKEN` 已设置，且输入的值与之匹配 |
| 按账号超时不生效 | 检查 `DEVICE_CLEANUP_IDLE_HOURS` 是否为0（为0才使用按账号设置）|

---

## 生产部署建议

1. **使用Gunicorn** 替代Flask自带服务器：`gunicorn -w 4 -b 0.0.0.0:5000 app:app`
2. **配置Nginx反向代理**（提供HTTPS，保护AD密码传输）
3. **AD域控使用LDAPS**（端口636，加密传输用户名密码）
4. **Redis配置密码**并绑定内网访问
5. **配置防火墙**仅允许锐捷AC和管理员IP访问
6. **设置强随机 `ADMIN_TOKEN`**（建议64位以上随机字符串）
