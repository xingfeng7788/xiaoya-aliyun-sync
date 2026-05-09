"""SQLite 配置管理模块"""
import sqlite3
import os
import json
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
    """)
    conn.commit()


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

def create_schedule_task(name, local_dir, remote_folder_id, remote_folder_name, cron_expr):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO schedule_task (name, local_dir, remote_folder_id, remote_folder_name, cron_expr) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, local_dir, remote_folder_id, remote_folder_name, cron_expr)
    )
    conn.commit()
    return cur.lastrowid


def update_schedule_task(task_id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled):
    conn = get_conn()
    conn.execute(
        "UPDATE schedule_task SET name=?, local_dir=?, remote_folder_id=?, remote_folder_name=?, "
        "cron_expr=?, enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled, task_id)
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
        "SELECT id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled, created_at, updated_at "
        "FROM schedule_task ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_schedule_task(task_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, name, local_dir, remote_folder_id, remote_folder_name, cron_expr, enabled, created_at, updated_at "
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


def update_schedule_log(log_id, status, message='', detail=''):
    conn = get_conn()
    conn.execute(
        "UPDATE schedule_log SET status=?, message=?, detail=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, message, detail, log_id)
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
