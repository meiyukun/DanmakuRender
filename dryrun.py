from tools import check_pypi
check_pypi()

import time
import argparse
from datetime import datetime
import os
import sys
import logging
import logging.handlers
from os.path import exists, splitext
from glob import glob

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('./tools')

from DMR.utils import *
from DMR import DanmakuRender
from DMR.Config import Config


def stop_replay_downloaders(engine):
    """Stop only downloader-backed replay tasks; independent tasks have no downloader."""
    stopped = []
    for task_key, task in list(engine.task_dict.items()):
        if task.get('task_type', 'replay') != 'replay':
            continue
        taskname = task.get('name') or task_key.split('/', 1)[-1]
        engine.pipeSend(PipeMessage(
            source='dryrun', target='downloader', event='stoptask', dtype='str', data=taskname,
        ))
        stopped.append(taskname)
    return stopped


def wait_for_highlight(engine, timeout, started_at):
    """Wait until every received highlight job reaches a terminal state."""
    deadline = time.time() + timeout
    saw_job = False
    while time.time() < deadline:
        task = engine.task_dict.get('highlight/Highlight')
        all_jobs = getattr(task.get('class'), 'jobs', {}) if task else {}
        jobs = {key: job for key, job in all_jobs.items() if job.get('received_at', 0) >= started_at}
        if jobs:
            saw_job = True
            active = [job for job in jobs.values() if job.get('status') not in ('completed', 'failed')]
            if not active:
                return True, jobs
        time.sleep(2)
    return False, jobs if saw_job else {}

if __name__ == '__main__':    
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/global.yml')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--duration', type=int, default=40, help='录制多少秒后停止下载器。')
    parser.add_argument('--wait-timeout', type=int, default=900, help='停止录制后等待热点任务完成的最长秒数。')
    parser.add_argument('--allow-upload', action='store_true', help='允许普通回放和热点成片执行真实上传。')
    parser.add_argument('--regular-pipeline', action='store_true', help='同时测试普通渲染、转录和清理流水线。')
    parser.add_argument('--ensure-output', action='store_true', help='短样本无真实热点时编码10秒测试片段，验证完整FFmpeg链路。')
    args = parser.parse_args()
    
    config = Config(args.config)

    # dryrun 默认只验证“录制 -> 整场结束 -> 热点分析/混剪”，避免意外上传、清理和平台转录。
    if not args.regular_pipeline:
        config.global_config['dmr_engine_args']['enabled_plugins'] = ['downloader']
    
    for name, rep_conf in config.replay_config.items():
        config.replay_config[name]['download_args']['segment'] = 20
        if not args.regular_pipeline:
            common = config.replay_config[name]['common_event_args']
            common['auto_render'] = False
            common['auto_transcode'] = False
            common['auto_transcribe'] = False
            common['auto_clean'] = False
        if not args.allow_upload:
            config.replay_config[name]['common_event_args']['auto_upload'] = False
        for upd_type, upd_configs in rep_conf.get('upload_args', {}).items():
            for upid, upd_conf in enumerate(upd_configs):
                config.replay_config[name]['upload_args'][upd_type][upid]['dtime'] = 86400
                config.replay_config[name]['upload_args'][upd_type][upid]['min_length'] = 0
    if not args.allow_upload:
        for highlight_config in config.highlight_config.values():
            for target in highlight_config.get('targets', {}).values():
                target.setdefault('upload', {})['enabled'] = False
    if args.ensure_output:
        for highlight_config in config.highlight_config.values():
            for target in highlight_config.get('targets', {}).values():
                target.setdefault('analysis', {})['test_fallback_clip_seconds'] = 10
    
    logger = logging.getLogger('DMR')
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s"))
    
    os.makedirs('logs', exist_ok=True)
    log_file = f'logs/DMR-dryrun-{datetime.now().strftime("%Y%m%d")}.log'
    if exists(log_file):
        _cnt = len(glob(splitext(log_file)[0] + '*'))
        log_file = splitext(log_file)[0] + f'({_cnt})' + splitext(log_file)[1]
    file_handler = logging.handlers.TimedRotatingFileHandler(log_file, when='D', interval=1, backupCount=0, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("[%(asctime)s][%(module)s][%(levelname)s]: %(message)s"))
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    dmr = DanmakuRender(config, debug=args.debug)

    logger.info('正在启动测试...')
    test_started_at = time.time()
    dmr.start()

    time.sleep(max(1, args.duration))
    # 引擎任务键包含类型前缀；这里只停止录制任务，不停止独立热点任务。
    stop_replay_downloaders(dmr.engine)

    try:
        completed, jobs = wait_for_highlight(dmr.engine, max(1, args.wait_timeout), test_started_at)
        for job_id, job in jobs.items():
            logger.info('热点任务 %s: status=%s manifest=%s error=%s',
                        job_id, job.get('status'), job.get('manifest'), job.get('error'))
        if not completed:
            logger.error('等待热点任务超时，或整场结束后未创建热点任务。')
            exit_code = 2
        elif any(job.get('status') == 'failed' for job in jobs.values()):
            exit_code = 1
        else:
            logger.info('dryrun 完整热点流程测试完成。')
            exit_code = 0
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        dmr.stop()
    exit(exit_code)
