"""Run the factory API independently of Streamlit."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from core.jobs import WorkerLock
from core.local_api import make_server
from core.production import FactoryService


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--connection',required=True)
    parser.add_argument('--config')
    args=parser.parse_args()
    connection=json.loads(Path(args.connection).read_text(encoding='utf-8'))
    cfg=load_config(args.config)
    service=FactoryService(cfg,connection['download_roots'],connection['profile'])
    lock=WorkerLock(str(Path(cfg['database']).parent/'.factory-api'))
    if not lock.acquire():raise SystemExit('当前工厂环境的控制服务已运行')
    stop=threading.Event()
    def tick():
        while not stop.is_set():
            service.advance();stop.wait(5)
    def dispatch(method,path,data):
        # Follow saved ASR/LLM settings without moving a running server to a
        # different data environment when .env paths are edited.
        with service.lock:
            live=load_config(args.config)
            live.update({key:cfg[key] for key in ('root','database','cache_dir','output_dir')})
            service.cfg=live
            return service.dispatch(method,path,data)
    server=make_server(connection['factory']['port'],connection['factory']['token'],dispatch,'切片工厂')
    worker=threading.Thread(target=tick,daemon=True);worker.start()
    try:server.serve_forever()
    finally:stop.set();server.server_close();worker.join(10);lock.release()


if __name__=='__main__':main()
