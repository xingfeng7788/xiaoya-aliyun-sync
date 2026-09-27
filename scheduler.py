"""定时调度引擎 — 基于 APScheduler 实现 cron 调度上传"""
import os
import traceback
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db import (
    get_all_schedule_tasks, get_schedule_task,
    create_schedule_log, update_schedule_log, append_schedule_log_detail
)

_scheduler = BackgroundScheduler(daemon=True)
_upload_func = None  # 由 app.py 注入
_token_check_func = None  # Token 检测函数，由 app.py 注入
_token_refresh_func = None  # Token 刷新函数，由 app.py 注入
_task_notify_func = None  # 任务完成通知函数，由 app.py 注入


def set_upload_func(func):
    """注入上传执行函数，签名: func(task, log_id)"""
    global _upload_func
    _upload_func = func


def set_token_check_func(func):
    """注入 Token 检测函数，签名: func() -> bool (True=有效)"""
    global _token_check_func
    _token_check_func = func


def set_token_refresh_func(func):
    """注入 Token 刷新函数，签名: func() -> bool (True=成功)"""
    global _token_refresh_func
    _token_refresh_func = func


def set_task_notify_func(func):
    """注入任务完成通知函数，签名: func(task, log_id, trigger_type)。"""
    global _task_notify_func
    _task_notify_func = func


def _run_task(task_id, trigger_type='cron'):
    """调度执行入口"""
    task = get_schedule_task(task_id)
    if not task:
        return
    if not task['enabled'] and trigger_type == 'cron':
        return

    log_id = create_schedule_log(task_id, trigger_type)
    try:
        append_schedule_log_detail(log_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行任务: {task['name']}")
        append_schedule_log_detail(log_id, f"本地目录: {task['local_dir']}")
        append_schedule_log_detail(log_id, f"云盘目标: {task['remote_folder_name']} ({task['remote_folder_id']})")

        local_dir = task['local_dir']
        if not os.path.exists(local_dir):
            msg = f"本地目录不存在: {local_dir}"
            append_schedule_log_detail(log_id, f"[ERROR] {msg}")
            update_schedule_log(log_id, 'failed', msg)
            return

        if not os.path.isdir(local_dir):
            msg = f"路径不是目录: {local_dir}"
            append_schedule_log_detail(log_id, f"[ERROR] {msg}")
            update_schedule_log(log_id, 'failed', msg)
            return

        if _upload_func is None:
            msg = "上传函数未初始化"
            append_schedule_log_detail(log_id, f"[ERROR] {msg}")
            update_schedule_log(log_id, 'failed', msg)
            return

        _upload_func(task, log_id)

    except Exception as e:
        traceback.print_exc()
        append_schedule_log_detail(log_id, f"[ERROR] {str(e)}")
        update_schedule_log(log_id, 'failed', str(e))
    finally:
        # 通知统一放在 finally：成功、部分成功、校验失败及异常都能覆盖。
        if task.get('notify_enabled') and _task_notify_func is not None:
            try:
                result = _task_notify_func(task, log_id, trigger_type)
                # 通知函数返回 (是否成功, 失败原因)，兼容只返回 bool 的实现。
                if isinstance(result, tuple):
                    sent, reason = result
                else:
                    sent, reason = bool(result), ''
                if not sent:
                    reason = reason or '未知原因'
                    append_schedule_log_detail(log_id, f"[WARN] PushPlus 执行结果通知发送失败: {reason}")
            except Exception as e:
                # 通知失败不能反过来改变同步任务的执行结果。
                print(f"任务 [{task['name']}] PushPlus 通知异常: {e}")
                append_schedule_log_detail(log_id, f"[WARN] PushPlus 执行结果通知异常: {e}")


def run_task_manual(task_id):
    """手动触发任务（在新线程中执行）"""
    import threading
    thread = threading.Thread(target=_run_task, args=(task_id, 'manual'), daemon=True)
    thread.start()


def _make_job_id(task_id):
    return f"upload_task_{task_id}"


def add_task_job(task_id, cron_expr):
    """添加或更新 cron 调度"""
    job_id = _make_job_id(task_id)
    try:
        trigger = CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        print(f"无效的 cron 表达式 '{cron_expr}': {e}")
        return False

    existing = _scheduler.get_job(job_id)
    if existing:
        existing.reschedule(trigger)
    else:
        _scheduler.add_job(_run_task, trigger, args=[task_id, 'cron'], id=job_id, replace_existing=True)
    return True


def remove_task_job(task_id):
    """移除 cron 调度"""
    job_id = _make_job_id(task_id)
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass


def reload_all_tasks():
    """从数据库重新加载所有任务到调度器"""
    # 先清除所有任务
    for job in _scheduler.get_jobs():
        if job.id.startswith('upload_task_'):
            job.remove()

    tasks = get_all_schedule_tasks()
    for t in tasks:
        if t['enabled']:
            add_task_job(t['id'], t['cron_expr'])
    print(f"调度器已加载 {len([t for t in tasks if t['enabled']])} 个活跃任务")


def start_scheduler():
    """启动调度器"""
    if not _scheduler.running:
        _scheduler.start()
        reload_all_tasks()
        _register_builtin_jobs()
        print("调度器已启动")


def _register_builtin_jobs():
    """注册内置定时任务（Token 检测 + Token 刷新）"""
    # 每 30 分钟检测一次 Token 是否有效
    _scheduler.add_job(
        _builtin_token_check,
        IntervalTrigger(minutes=30),
        id='builtin_token_check',
        replace_existing=True,
        next_run_time=datetime.now()  # 启动后立即执行一次
    )
    print("内置任务已注册: Token 有效性检测 (每30分钟)")

    # 每天凌晨 4 点刷新一次 Token
    _scheduler.add_job(
        _builtin_token_refresh,
        CronTrigger(hour=4, minute=0),
        id='builtin_token_refresh',
        replace_existing=True,
    )
    print("内置任务已注册: Token 定时刷新 (每天 04:00)")


def _builtin_token_check():
    """内置任务: 检测 Token 是否有效，失效则发送通知"""
    if _token_check_func is None:
        return
    try:
        valid = _token_check_func()
        if valid:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 检测: 有效")
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 检测: 已失效")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 检测异常: {e}")


def _builtin_token_refresh():
    """内置任务: 主动刷新 Token 以延长有效期"""
    if _token_refresh_func is None:
        return
    try:
        success = _token_refresh_func()
        if success:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 定时刷新: 成功")
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 定时刷新: 失败")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Token 定时刷新异常: {e}")


def get_job_next_run(task_id):
    """获取任务下次执行时间"""
    job = _scheduler.get_job(_make_job_id(task_id))
    if job and job.next_run_time:
        return job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
    return None
