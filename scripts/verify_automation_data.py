"""Read-only comparison with the pre-upgrade SQLite backup; never prints row data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.utils import atomic_json


def digest_rows(connection,table):
    quoted='"'+table.replace('"','""')+'"'
    rows=connection.execute('SELECT * FROM '+quoted+' ORDER BY rowid').fetchall()
    raw=json.dumps(rows,ensure_ascii=False,default=lambda v: {'blob':bytes(v).hex()},separators=(',',':')).encode()
    return {'count':len(rows),'sha256':hashlib.sha256(raw).hexdigest()}


def main():
    baseline=ROOT/'output/automation_upgrade/before.sqlite3'
    with closing(sqlite3.connect(baseline.as_uri()+'?mode=ro',uri=True)) as before, closing(sqlite3.connect(Path(load_config()['database']).as_uri()+'?mode=ro',uri=True)) as after:
        tables=[r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result=[]
        for table in tables:
            old,new=digest_rows(before,table),digest_rows(after,table)
            result.append(dict(table=table,before=old,after=new,unchanged=old==new))
        new_tables=[r[0] for r in after.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in tables and not r[0].startswith('sqlite_')]
    report={'baseline':str(baseline),'tables':result,'added_tables':new_tables,
        'note':'Workbench navigation preferences and one empty automation landing project may change during browser acceptance. No baseline is restored over user data.'}
    atomic_json(ROOT/'output/automation_upgrade/data_reconciliation.json',report)
    print(json.dumps({'tables':[{k:v for k,v in row.items() if k not in ('before','after')}|{'before_count':row['before']['count'],'after_count':row['after']['count']} for row in result],'added_tables':new_tables},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
