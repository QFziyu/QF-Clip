"""Verify active paired services and repeat-launch deduplication without printing secrets."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.releases import launch_environment
from core.version import RELEASE,VERSION
from core.utils import atomic_json
from scripts.launch import process_command,listener_pids,healthy,owned_server
from scripts.launch_automation import read_health


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',choices=['gpu','cpu'],default='gpu');args=parser.parse_args()
    version='final' if args.profile=='gpu' else RELEASE.removesuffix('-gpu')+'-cpu'
    code,env=launch_environment(ROOT,version)
    output=Path(load_config()['output_dir']) if version=='final' else Path(env['OUTPUT_DIR'])
    state=output/'.automation';config=json.loads((state/'connection.json').read_text(encoding='utf-8'))
    rows=[];pids={}
    for name,service,expected in [('lightboat','lightboat','1.2.0'),('factory','football-factory',VERSION),('hub','football-workflow','1.0.0'),('publisher','football-publisher','1.0.0')]:
        record=json.loads((state/(name+'.json')).read_text(encoding='utf-8'))
        assert process_command(record['pid'])==record['command'],name+' ownership'
        assert any(process_command(pid)==record['command'] for pid in listener_pids(record['port']) or []),name+' listener ownership'
        health=read_health(config[name],name=='lightboat')
        assert health['service']==service and health['api_version']==1 and health['version']==expected,name+' version'
        if name!='lightboat':assert health['profile']==args.profile
        if name=='factory':
            assert health['asr_device']==('cuda' if args.profile=='gpu' else 'cpu')
            if args.profile=='cpu':assert health['compute_type']=='int8' and health['encoder']=='libx264'
        pids[name]=record['pid'];rows.append({'component':name,**{k:v for k,v in health.items() if k!='instance'},'ownership_verified':True})
    port=8501 if args.profile=='gpu' else 8502
    assert healthy(port) and owned_server(output/'.launcher'/f'{version}-{port}.json',code,port,version)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/launch_automation.py'),'--profile',args.profile,'--no-browser'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50)
    assert result.returncode==0,'repeat launcher failed'
    for name,pid in pids.items():assert json.loads((state/(name+'.json')).read_text(encoding='utf-8'))['pid']==pid,'duplicate process'
    destination=ROOT/'output/publishing_upgrade/runtime_checks.json'
    report=json.loads(destination.read_text(encoding='utf-8')) if destination.exists() else {}
    report[args.profile]={'success':True,'services':rows,'ui_port':port,'ui_ownership_verified':True,'repeat_launch_reused_processes':True,
        'scope':'Service settings and ownership only; no actual Whisper inference or paid LLM request.'}
    atomic_json(destination,report)
    print(json.dumps(report[args.profile],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
