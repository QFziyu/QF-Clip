"""One-time additive deployment into the established 8501 installation.

Explicit operator script: copies code only, snapshots both SQLite databases,
imports missing IDs, and never replaces existing workspace/human records.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile


def deploy(source,target,backup):
    source=Path(source).resolve();target=Path(target).resolve();backup=Path(backup).resolve()
    if source==target or not (target/'ui/app.py').is_file():raise ValueError('需要两个现有安装目录')
    backup.mkdir(parents=True,exist_ok=False)
    connections=[]
    for label,root in [('canonical',target),('staging',source)]:
        conn=sqlite3.connect(root/'output/factory.sqlite3');conn.row_factory=sqlite3.Row
        if conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]:
            raise ValueError('部署前需要等待运行任务完成：'+label)
        with sqlite3.connect(backup/(label+'.sqlite3')) as dest:conn.backup(dest)
        connections.append(conn)
    dst,src=connections
    with zipfile.ZipFile(backup/'canonical_source.zip','w',zipfile.ZIP_DEFLATED,strict_timestamps=False) as archive:
        for directory in ('core','ui','storage','scripts','tests','config','prompts','docs'):
            for path in (target/directory).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:archive.write(path,path.relative_to(target))
        for name in ('.env','.env.llm','.env.asr','README.md','SKILL.md','AGENTS.md','main.py','requirements.txt','launch.cmd'):
            path=target/name
            if path.is_file():archive.write(path,name)
    old={r['id']:tuple(r) for r in dst.execute('SELECT * FROM workspaces')}
    added=[];counts={}
    def insert(table,row,omit=()):
        keys=[k for k in row.keys() if k not in omit]
        result=dst.execute('INSERT OR IGNORE INTO "'+table+'" ('+','.join('"'+k+'"' for k in keys)+') VALUES ('+','.join('?' for k in keys)+')',[row[k] for k in keys])
        counts[table]=counts.get(table,0)+result.rowcount
    with dst:
        for table in ('projects','clips','editing_plans'):
            for row in src.execute('SELECT * FROM '+table):insert(table,row)
        for row in src.execute('SELECT * FROM workspaces'):
            if row['id'] not in old:insert('workspaces',row);added.append(row['id'])
        seen={tuple(r) for r in dst.execute('SELECT workspace_id,item_id,snapshot_json,result_json,preview FROM workflow_media')}
        for row in src.execute('SELECT * FROM workflow_media'):
            signature=tuple(row[k] for k in ('workspace_id','item_id','snapshot_json','result_json','preview'))
            if signature not in seen:insert('workflow_media',row,('id',));seen.add(signature)
        for row in src.execute('SELECT * FROM workflow_audit'):
            if row['workspace_id'] in added:insert('workflow_audit',row,('id',))
        for row in src.execute('SELECT * FROM workflow_deliveries'):insert('workflow_deliveries',row,('sequence',))
        for row in src.execute('SELECT * FROM workflow_delivery_outbox'):insert('workflow_delivery_outbox',row)
        for row in src.execute("SELECT * FROM jobs WHERE status NOT IN ('queued','running')"):
            insert('jobs',row)
        for row in src.execute('SELECT * FROM job_requests'):insert('job_requests',row)
        for key,before in old.items():
            assert tuple(dst.execute('SELECT * FROM workspaces WHERE id=?',(key,)).fetchone())==before,'Existing workspace changed'
        assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    changed=[]
    for directory in ('core','ui','storage','scripts','tests','prompts'):
        for path in (source/directory).rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts:continue
            if directory=='storage' and path.parent!=source/'storage':continue
            if path.suffix not in (('.md','.txt') if directory=='prompts' else ('.py',)):continue
            destination=target/path.relative_to(source)
            if destination.is_file() and destination.read_bytes()==path.read_bytes():continue
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
            changed.append(str(path.relative_to(source)))
    report={'backup':str(backup),'copied_code':changed,'added_records':counts,'added_workspaces':added,
            'existing_workspaces_unchanged':len(old),'integrity_check':'ok','source_media':'not moved or deleted'}
    (backup/'deployment.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    for conn in connections:conn.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--target',required=True);p.add_argument('--backup',required=True)
    a=p.parse_args();deploy(a.source,a.target,a.backup)
