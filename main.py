from tools import check_pypi, check_update
check_pypi()

import time
import argparse
from datetime import datetime
import os
import sys
import logging
import logging.handlers
import yaml
from glob import glob
from os.path import exists, splitext

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('./tools')

VERSION = '2026.02.17'

from DMR import DanmakuRender
from DMR.Config import Config

if __name__ == '__main__':    
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/global.yml')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--skip_update', action='store_true')
    args = parser.parse_args()

    if args.version:
        print(f'DanmakuRender-5 {VERSION}.')
        print('https://github.com/SmallPeaches/DanmakuRender')
        exit(0)
    
    if not args.skip_update:
        check_update(VERSION)
    
    config = Config(args.config)

    logger = logging.getLogger('DMR')
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s"))
    
    os.makedirs('logs', exist_ok=True)
    log_file = f'logs/DMR-{datetime.now().strftime("%Y%m%d")}.log'
    if exists(log_file):
        _cnt = len(glob(splitext(log_file)[0] + '*'))
        log_file = splitext(log_file)[0] + f'({_cnt})' + splitext(log_file)[1]
    file_handler = logging.handlers.TimedRotatingFileHandler(log_file, when='D', interval=1, backupCount=3, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("[%(asctime)s][%(module)s][%(levelname)s]: %(message)s"))

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    logger.debug(f'VERSION: {VERSION}')

    dmr = DanmakuRender(config, logger=logger, debug=args.debug)
    dmr.start()
    
    try:
        last_flush_time = time.time()
        while 1:
            time.sleep(5)
            if time.time() - last_flush_time >= 60:
                file_handler.flush()
                last_flush_time = time.time()
            if dmr.should_restart_now():
                restart_status = dmr.get_restart_status()
                if restart_status.get('force'):
                    logger.warning('正在强制重启 DanmakuRender 以重新加载配置和代码。')
                else:
                    logger.info('当前无活跃任务，正在重启 DanmakuRender 以重新加载配置和代码。')
                dmr.stop()
                for handler in logger.handlers:
                    handler.flush()
                os.execv(sys.executable, [sys.executable] + sys.argv)
    except KeyboardInterrupt:
        dmr.stop()
        exit(0)

