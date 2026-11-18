"""Start independently restartable local services; never take over unknown processes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import time
from urllib import request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.releases import current_environment
from core.version import RELEASE, VERSION
from core.publisher_client import ensure_configuration
from core.utils import atomic_json
from scripts.launch import listener_pids,process_command,port_available


def read_health(spec,lightboat=False):
    header='X-Lightboat-Token' if lightboat else 'X-Workflow-Token'
    req=request.Request(spec['url']+'/api/v1/health',headers={header:spec['token']})
    with request.build_opener(request.ProxyHandler({})).open(req,timeout=2) as r:return json.load(r)


def health_matches(health, service, profile=None, version=None, compatible=False):
    if health.get('service') != service or health.get('api_version') != 1:
        return False
    if profile and health.get('profile') != profile:
        return False
    if not version or health.get('version') == version:
        return True
    if compatible:
        current = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(health.get('version', '')))
        required = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
        if current and required:
            actual, minimum = tuple(map(int,current.groups())), tuple(map(int,required.groups()))
            return actual[0] == minimum[0] and actual >= minimum
    return False


def lightboat_command_matches(actual, script, port):
    """Independent launchers may use a different Python and explicit default port."""
    if not actual or Path(actual[0]).name.lower() not in ('python.exe','pythonw.exe','python','python3'):
        return False
    args = list(actual[1:])
    while args and args[0] in ('-B','-u'):
        args.pop(0)
    if not args:
        return False
    # Only the absolute expected source is accepted; matching a script basename is insufficient.
    source = Path(args.pop(0))
    if not source.is_absolute() or source.resolve() != Path(script).resolve():
        return False
    seen = set()
    while args:
        flag = args.pop(0)
        if flag in seen:
            return False
        seen.add(flag)
        if flag == '--no-browser':
            continue
        if flag == '--port' and args and args.pop(0) == str(port):
            continue
        return False
    return port == 17653 or '--port' in seen


def start_service(name,command,cwd,env,spec,state,expected_service,profile=None,expected_version=None):
    record_path=state/(name+'.json')
    if not port_available(spec['port']):
        if name == 'lightboat':
            pids = listener_pids(spec['port'])
            if not pids or not all(lightboat_command_matches(process_command(pid), command[1], spec['port']) for pid in pids):
                raise RuntimeError('lightboat 端口上的进程不是指定目录的轻舟；保留该进程')
            health = read_health(spec, True)
            if not health_matches(health, expected_service, profile, expected_version, compatible=True):
                raise RuntimeError('lightboat 接口或版本不兼容；保留当前轻舟进程')
            print('lightboat 已运行，复用已核验的轻舟 ' + str(health.get('version')))
            return  # An independently launched service is never adopted as a process we own.
        if not record_path.is_file():raise RuntimeError(f'{name} 端口 {spec["port"]} 已被其他服务占用，请先核对版本并关闭旧窗口')
        record=json.loads(record_path.read_text(encoding='utf-8'))
        expected=record.get('command')
        if expected!=command or process_command(record['pid'])!=expected or not any(process_command(pid)==expected for pid in listener_pids(spec['port']) or []):
            raise RuntimeError(f'{name} 无法确认进程归属，不会关闭该进程')
        health=read_health(spec,name=='lightboat')
        if not health_matches(health,expected_service,profile,expected_version):raise RuntimeError(name+' 版本或运行环境不匹配；请使用停止自动服务入口后重新启动')
        print(name+' 已运行');return
    log=state/(name+'.log')
    with log.open('ab') as output:
        process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS) if os.name=='nt' else 0,
            start_new_session=os.name!='nt')
    record_path.write_text(json.dumps({'pid':process.pid,'command':command,'port':spec['port']},ensure_ascii=False,indent=2),encoding='utf-8')
    for _ in range(100):
        if process.poll() is not None:raise RuntimeError(name+' 启动失败，请查看 '+str(log))
        try:
            health=read_health(spec,name=='lightboat')
            if health_matches(health,expected_service,profile,expected_version,compatible=name=='lightboat'):
                print(name+' 已启动');return
        except (OSError,ValueError):pass
        time.sleep(.3)
    raise RuntimeError(name+' 启动超时，请查看 '+str(log))


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',choices=['gpu','cpu'],default='gpu')
    parser.add_argument('--lightboat-root',default=str(ROOT.parent/'软件下载/轻舟视频下载'))
    parser.add_argument('--hub-root',default=str(ROOT.parent/'FootballWorkflow'))
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--publishing', action='store_true', help='同时启动发布服务')
    args=parser.parse_args(argv)
    try:
        profile=args.profile
        version='final'
        code,env=current_environment(ROOT, profile)
        from core.config import load_config
        previous=dict(os.environ)
        try:
            os.environ.update(env)
            cfg=load_config(code/'config/settings.yaml')
        finally:
            os.environ.clear();os.environ.update(previous)
        output=Path(cfg['output_dir'])
        state=output/'.automation';state.mkdir(parents=True,exist_ok=True)
        lightboat=Path(args.lightboat_root).resolve();hub=Path(args.hub_root).resolve()
        settings=json.loads((lightboat/'config.json').read_text(encoding='utf-8'))
        connection_path=state/'connection.json'
        connection=ensure_configuration(cfg,profile)
        for name,port in [('lightboat',17653),('hub',17654)]:
            connection.setdefault(name,{'url':f'http://127.0.0.1:{port}','port':port,'token':settings['token'] if name=='lightboat' else secrets.token_urlsafe(32)})
        # Check the currently configured download root without discarding prior allowed roots.
        download_root=str(Path(settings['output_dir']).resolve())
        if not connection['download_roots']:connection['download_roots'].append(download_root)
        if download_root not in connection['download_roots']:raise RuntimeError('轻舟下载目录已变化，请关闭自动服务并更新连接文件中的 download_roots')
        atomic_json(connection_path,connection)
        env.update(FFMPEG_PATH=cfg['ffmpeg'],FFPROBE_PATH=cfg['ffprobe'],PYTHONUTF8='1')
        python=str(ROOT/'.venv/Scripts/python.exe')
        start_service('lightboat',[python,str(lightboat/'server.py'),'--no-browser'],lightboat,env,connection['lightboat'],state,'lightboat',expected_version='1.2.0')
        start_service('factory',[python,str(code/'scripts/factory_service.py'),'--connection',str(connection_path)],code,env,connection['factory'],state,'football-factory',profile,VERSION)
        runtime=hub/'runtime'/profile;runtime.mkdir(parents=True,exist_ok=True)
        start_service('hub',[python,str(hub/'main.py'),'--connection',str(connection_path),'--database',str(runtime/'flows.sqlite3')],hub,env,connection['hub'],state,'football-workflow',profile,'1.0.0')
        if args.publishing:
            publisher=ROOT.parent/'FootballPublisher';runtime=publisher/'runtime'/profile;runtime.mkdir(parents=True,exist_ok=True)
            start_service('publisher',[python,str(publisher/'main.py'),'--connection',str(connection_path),'--database',str(runtime/'publisher.sqlite3'),'--profile',profile],publisher,env,connection['publisher'],state,'football-publisher',profile,'1.1.0')
        command=[python,str(ROOT/'scripts/launch.py'),'--profile',profile]
        if args.no_browser:command.append('--no-browser')
        result=subprocess.run(command,cwd=ROOT,env=env)
        if result.returncode:raise RuntimeError('后台服务已就绪；工作台启动失败，请检查原有端口和启动日志')
        print('自动工作流已就绪：第一步选择“视频来源”；正式输出仍需第五步整批确认。')
        return 0
    except (OSError,ValueError,RuntimeError) as exc:
        print('启动失败：'+str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
