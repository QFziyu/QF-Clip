"""Build a source-only four-component archive; never bundle pairing or runtime data."""
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.releases import source_files,sha256,release_directory,read_dotenv
from core.version import VERSION


def files(root,excluded):
    suffixes={'.py','.md','.txt','.json','.js','.cjs','.html','.css','.cmd','.ps1','.yaml','.yml','.toml'}
    for path in sorted(root.rglob('*')):
        name=path.relative_to(root).as_posix()
        if any(part in {'runtime','data','deps','__pycache__','.git','.venv','node_modules'} for part in path.relative_to(root).parts):continue
        if path.is_file() and name not in excluded and (path.suffix in suffixes or path.name=='.gitignore'):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise ValueError('源码不能包含目录外链接')
            yield path


def main():
    hub=ROOT.parent/'FootballWorkflow';boat=ROOT.parent/'软件下载/轻舟视频下载';publisher=ROOT.parent/'FootballPublisher'
    releases=release_directory(ROOT);releases.mkdir(exist_ok=True)
    archive=releases/f'FootballWorkflow-v{VERSION}-source.zip'
    manifest=archive.with_suffix('.manifest.json')
    if archive.exists() or manifest.exists():raise FileExistsError('组合源码包已存在，禁止覆盖')
    entries={}
    for root,paths,prefix in [(ROOT,source_files(ROOT),'FootballClipFactory'),
        (hub,files(hub,set()),'FootballWorkflow'),(publisher,files(publisher,set()),'FootballPublisher'),
        (boat,files(boat,{'config.json','extension/config.js','history.json'}),'软件下载/轻舟视频下载')]:
        for path in paths:entries[prefix+'/'+path.relative_to(root).as_posix()]=path
    # Fail closed if a real installation secret accidentally appears in source text.
    secrets=[v for k,v in read_dotenv(ROOT/'.env').items() if k=='OPENAI_API_KEY' and len(v)>=12]
    for config,key in [(boat/'config.json','token'),(ROOT/'output/.automation/connection.json',None)]:
        if config.is_file():
            data=json.loads(config.read_text(encoding='utf-8'))
            if key:secrets.append(data[key])
            else:secrets.extend(data[s]['token'] for s in ('hub','factory','lightboat','publisher') if s in data)
    for name,path in entries.items():
        raw=path.read_bytes()
        if any(value.encode() in raw for value in secrets if len(value)>=12):raise ValueError('源码中检测到本机凭据，已停止打包；请检查 '+name)
    hashes={name:sha256(path) for name,path in entries.items()}
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
        for name,path in entries.items():bundle.write(path,name)
    report={'version':VERSION,'components':{'factory':VERSION,'lightboat':'1.2.0','coordinator':'1.0.0','publisher':'1.1.0'},
        'archive':str(archive),'archive_sha256':sha256(archive),'files':hashes,'file_count':len(hashes),
        'secrets_included':False,'runtime_data_included':False}
    manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
