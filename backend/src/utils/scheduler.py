"""APScheduler 定时任务调度器（AsyncIO 模式）"""

import logging
import shutil
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from backend.src.utils.constants import STATIC_DIR, CLEANUP_AGE_SECONDS

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _cleanup_old_files():
    """删除 static 目录下超过 1 天的生成文件（音频缓存、视频、演示、PPT）"""
    now = time.time()
    dirs = [
        STATIC_DIR / "audio" / "_cache",
        STATIC_DIR / "videos",
        STATIC_DIR / "presentations",
        STATIC_DIR / "ppt",
    ]
    cleaned = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file() and now - f.stat().st_mtime > CLEANUP_AGE_SECONDS:
                    f.unlink()
                    cleaned += 1
            except OSError:
                logger.debug("清理过期文件失败 path=%s", f, exc_info=True)
        # 清理空的音频资源子目录
        if d.name != "_cache" and d.parent.name == "audio":
            continue
    # 清理 audio 下的空资源目录
    audio_dir = STATIC_DIR / "audio"
    if audio_dir.is_dir():
        for sub in audio_dir.iterdir():
            if sub.is_dir() and sub.name != "_cache":
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    logger.debug("清理空音频目录失败 path=%s", sub, exc_info=True)
    if cleaned:
        logger.info("清理过期文件 %d 个", cleaned)


def get_scheduler() -> AsyncIOScheduler:
    """获取全局调度器（懒初始化）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone="Asia/Shanghai",
            job_defaults={"coalesce": True, "max_instances": 1},
        )
    return _scheduler


def start():
    sched = get_scheduler()

    from backend.src.service.notification.service import (
        generate_weekly_report_and_ai_tip,
    )

    # 每天凌晨 3 点清理超过 1 天的静态文件
    sched.add_job(
        _cleanup_old_files,
        trigger="cron",
        hour=3,
        minute=13,
        id="cleanup_old_files",
        name="清理过期静态文件",
        replace_existing=True,
    )

    # 每周一 9:00 生成周报 + AI 建议
    sched.add_job(
        generate_weekly_report_and_ai_tip,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=7,
        id="weekly_report_and_ai_tip",
        name="周报与AI建议",
        replace_existing=True,
    )

    sched.start()
    logger.info("定时任务已启动：清理过期文件（每日3:13）+ 周报（周一9:07）")


def stop():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
