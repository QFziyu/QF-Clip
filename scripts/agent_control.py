"""JSON-only CLI using the same service layer as the paired HTTP API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from core.export_center import agent_call


def main():
    parser=argparse.ArgumentParser(description='切片工厂 AI 控制入口：先 capabilities / summary，再预检和提交')
    parser.add_argument('route',help='例如 summary、approved、exports/plan、jobs/ID/retry')
    parser.add_argument('--method',choices=['GET','POST'],default='GET')
    parser.add_argument('--json-file',help='UTF-8 JSON 请求文件')
    parser.add_argument('--config')
    args=parser.parse_args()
    try:
        data=json.loads(Path(args.json_file).read_text(encoding='utf-8-sig')) if args.json_file else {}
        result=agent_call(args.method,args.route,data,load_config(args.config))
        print(json.dumps({'ok':True,'result':result},ensure_ascii=False,indent=2))
        return 0
    except (ValueError,KeyError,TypeError,OSError) as exc:
        print(json.dumps({'ok':False,'error':{'code':'INVALID_OR_NOT_READY','message':str(exc),
            'retryable':False,'next_action':'Refresh summary and rebuild the plan; preserve existing caches.'}},ensure_ascii=False))
        return 2


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
