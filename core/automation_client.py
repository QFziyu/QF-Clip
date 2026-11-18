"""Workbench connects only to the coordinator's public HTTP API."""
import json
from pathlib import Path
from urllib import request,error


class AutomationClient:
    def __init__(self,cfg):
        path=Path(cfg['output_dir'])/'.automation/connection.json'
        if not path.is_file():raise RuntimeError('自动工作流尚未启动，请运行 launch_automation_gpu.cmd（或 CPU 入口）')
        self.connection=json.loads(path.read_text(encoding='utf-8'))
        self.endpoint=self.connection['hub']
    def call(self,path,data=None):
        req=request.Request(self.endpoint['url']+'/api/v1/'+path,
            data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
            headers={'X-Workflow-Token':self.endpoint['token'],'Content-Type':'application/json'})
        try:
            with request.build_opener(request.ProxyHandler({})).open(req,timeout=3 if path=='health' else 65) as response:
                value=json.load(response)
        except error.HTTPError as exc:
            try:message=json.load(exc).get('error','请求被拒绝')
            except (ValueError,OSError):message='请求被拒绝'
            raise RuntimeError(message) from None
        except (OSError,ValueError):raise RuntimeError('自动工作流服务未连接，请使用自动工作流启动入口，或稍后刷新') from None
        if path=='health' and (value.get('service')!='football-workflow' or value.get('api_version')!=1 or value.get('profile')!=self.connection['profile']):
            raise RuntimeError('自动工作流版本或运行环境不匹配')
        return value
