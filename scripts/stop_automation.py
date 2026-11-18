"""Stop only command-verified automation processes; keep Streamlit and user files."""
import json
import os
from pathlib import Path
import signal
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.launch import process_command,listener_pids
from core.releases import release_directory


def main():
    records=[ROOT/'output/.automation', ROOT/'runtime-cpu/output/.automation']
    releases=release_directory(ROOT)/'release-data'
    if releases.is_dir():records.extend(p/'output/.automation' for p in releases.iterdir() if p.is_dir())
    for directory in records:
        for name in ('publisher','hub','factory','lightboat'):
            path=directory/(name+'.json')
            if not path.exists():continue
            record=json.loads(path.read_text(encoding='utf-8'))
            expected=record['command']
            pids={record['pid'],*(listener_pids(record['port']) or [])}
            for pid in sorted(pids,reverse=True):
                actual=process_command(pid)
                if not actual:continue
                if actual!=expected:
                    print(f'{name}: PID {pid} 不属于记录的服务，保留');continue
                try:os.kill(pid,signal.SIGTERM)
                except (ProcessLookupError,PermissionError,OSError):
                    if process_command(pid)==expected:print(f'{name}: 无法停止 PID {pid}，请关闭对应服务窗口')
            print(name+': 已核对并停止已知进程')
    return 0


if __name__=='__main__':raise SystemExit(main())
