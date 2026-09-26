"""SQLite 配置管理模块"""
try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3
import os
import json
import hashlib
import secrets
import threading

DB_PATH = os.getenv("DB_PATH", "xiaoya.db")
_local = threading.local()


def get_conn():
    """获取线程安全的数据库连接"""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def init_db():
    """初始化数据库表"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            description TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ali_token (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refresh_token TEXT,
            access_token TEXT,
            token_data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS schedule_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            local_dir TEXT NOT NULL,
            remote_folder_id TEXT NOT NULL DEFAULT 'root',
            remote_folder_name TEXT DEFAULT '根目录',
            cron_expr TEXT NOT NULL,
            -- snapshot=按日期全量备份；mirror=以本地为准的 1:1 镜像同步
            sync_mode TEXT NOT NULL DEFAULT 'snapshot',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS schedule_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'cron',
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES schedule_task(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 兼容已有数据库：SQLite 的 CREATE TABLE IF NOT EXISTS 不会补充新字段。
    task_columns = {row['name'] for row in conn.execute("PRAGMA table_info(schedule_task)").fetchall()}
    if 'sync_mode' not in task_columns:
        conn.execute("ALTER TABLE schedule_task ADD COLUMN sync_mode TEXT NOT NULL DEFAULT 'snapshot'")
    conn.commit()
    # 如果无用户则创建默认 admin
    _ensure_default_user(conn)


def get_config(key, default=None):
    """获取配置值"""
    conn = get_conn()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default


def set_config(key, value, description=''):
    """设置配置值"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO config (key, value, description, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, description=excluded.description, updated_at=CURRENT_TIMESTAMP",
        (key, value, description)
    )
    conn.commit()


def delete_config(key):
    """删除配置"""
    conn = get_conn()
    conn.execute("DELETE FROM config WHERE key = ?", (key,))
    conn.commit()


def get_all_configs():
    """获取所有配置"""
    conn = get_conn()
    rows = conn.execute("SELECT key, value, description, updated_at FROM config ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def save_ali_token(refresh_token, access_token='', token_data=None):
    """保存阿里云盘 token"""
    conn = get_conn()
    token_json = json.dumps(token_data) if token_data else ''
    conn.execute(
        "INSERT INTO ali_token (id, refresh_token, access_token, token_data, updated_at) VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(id) DO UPDATE SET refresh_token=excluded.refresh_token, access_token=excluded.access_token, "
        "token_data=excluded.token_data, updated_at=CURRENT_TIMESTAMP",
        (refresh_token, access_token, token_json)
    )
    conn.commit()


def get_ali_token():
    """获取阿里云盘 token"""
    conn = get_conn()
    row = conn.execute("SELECT refresh_token, access_token, token_data, updated_at FROM ali_token WHERE id = 1").fetchone()
    if row:
        result = dict(row)
        if result.get('token_data'):
            try:
                result['token_data'] = json.loads(result['token_data'])
            except json.JSONDecodeError:
                result['token_data'] = None
        return result
    return None


def sync_env_to_db():
    """将环境变量中的配置信息（非端口和host）同步到数据库"""
    env_mappings = {
        'ALIST_URL': '小雅 Alist 地址',
        'XIAOYA_URL': '小雅搜索地址',
        'ADMIN_TOKEN': 'Alist 管理员 Token',
        'ALI_REFRESH_TOKEN': '阿里云盘 Refresh Token',
        'PUSHPLUS_TOKEN': 'PushPlus 通知 Token',
        'PUSHPLUS_TOPIC': 'PushPlus 群组编码',
        'XIAOYA_EXTERNAL_URL': '小雅助手外部访问地址（用于通知链接）',
    }
    for key, desc in env_mappings.items():
        val = os.getenv(key)
        if val:
            # 只在数据库中没有该配置时才从环境变量同步
            existing = get_config(key)
            if existing is None:
                set_config(key, val, desc)


# ==================== 调度任务 CRUD ====================

def create_schedule_task(name, local_dir, remote_folder_id, remote_folder_name, cron_expr, sync_mode='snapshot'):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO schedule_task (name, local_dir, remote_folder_id, remote_folder_name, cron_expr, sync_mode) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, local_dir, remote_folder_id, remote_folder_name, cron_expr, sync_mode)
    )
    conn.commit()
    return cur.lastrowid


def update_schedule_task(task_id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled, sync_mode='snapshot'):
    conn = get_conn()
    conn.execute(
        "UPDATE schedule_task SET name=?, local_dir=?, remote_folder_id=?, remote_folder_name=?, "
        "cron_expr=?, enabled=?, sync_mode=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled, sync_mode, task_id)
    )
    conn.commit()


def delete_schedule_task(task_id):
    conn = get_conn()
    conn.execute("DELETE FROM schedule_log WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM schedule_task WHERE id=?", (task_id,))
    conn.commit()


def get_all_schedule_tasks():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, sync_mode, enabled, created_at, updated_at "
        "FROM schedule_task ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_schedule_task(task_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, sync_mode, enabled, created_at, updated_at "
        "FROM schedule_task WHERE id=?", (task_id,)
    ).fetchone()
    return dict(row) if row else None


def toggle_schedule_task(task_id, enabled):
    conn = get_conn()
    conn.execute("UPDATE schedule_task SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (enabled, task_id))
    conn.commit()


# ==================== 调度日志 ====================

def create_schedule_log(task_id, trigger_type='cron'):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO schedule_log (task_id, trigger_type, status) VALUES (?, ?, 'running')",
        (task_id, trigger_type)
    )
    conn.commit()
    return cur.lastrowid


def update_schedule_log(log_id, status, message=''):
    conn = get_conn()
    conn.execute(
        "UPDATE schedule_log SET status=?, message=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, message, log_id)
    )
    conn.commit()


def append_schedule_log_detail(log_id, line):
    """追加日志详情"""
    conn = get_conn()
    conn.execute(
        "UPDATE schedule_log SET detail = COALESCE(detail, '') || ? WHERE id=?",
        (line + '\n', log_id)
    )
    conn.commit()


def get_schedule_logs(task_id, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, task_id, trigger_type, status, message, started_at, finished_at "
        "FROM schedule_log WHERE task_id=? ORDER BY id DESC LIMIT ?",
        (task_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_schedule_log_detail(log_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, task_id, trigger_type, status, message, detail, started_at, finished_at "
        "FROM schedule_log WHERE id=?", (log_id,)
    ).fetchone()
    return dict(row) if row else None


# ==================== 用户管理 ====================

def _hash_password(password, salt):
    """使用 PBKDF2 + SHA256 哈希密码"""
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()


def _ensure_default_user(conn):
    """无用户时创建默认 admin 账户"""
    row = conn.execute("SELECT COUNT(*) as cnt FROM user").fetchone()
    if row['cnt'] == 0:
        default_password = secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        password_hash = _hash_password(default_password, salt)
        conn.execute(
            "INSERT INTO user (username, password_hash, salt) VALUES (?, ?, ?)",
            ('admin', password_hash, salt)
        )
        conn.commit()
        print("=" * 50)
        print(f"  默认管理员账户已创建")
        print(f"  用户名: admin")
        print(f"  密码: {default_password}")
        print(f"  请登录后立即修改密码！")
        print("=" * 50)


def verify_user(username, password):
    """验证用户密码，返回用户 dict 或 None"""
    conn = get_conn()
    row = conn.execute("SELECT id, username, password_hash, salt FROM user WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    if _hash_password(password, row['salt']) == row['password_hash']:
        return {'id': row['id'], 'username': row['username']}
    return None


def change_user_password(user_id, new_password):
    """修改用户密码"""
    conn = get_conn()
    salt = secrets.token_hex(16)
    password_hash = _hash_password(new_password, salt)
    conn.execute(
        "UPDATE user SET password_hash=?, salt=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (password_hash, salt, user_id)
    )
    conn.commit()
