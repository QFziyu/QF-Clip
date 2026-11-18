"""Factory-side production API. The orchestrator never accesses the factory database."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
import re
import shutil
import threading
import uuid

from core.editing_options import normalize_editing_options
from core.export_options import default_export_options, normalize_export_options
from core.utils import fingerprint
from core.version import VERSION
from core.local_api import ApiError
from core.jobs import submit_job, get_job, retry_job, ensure_worker
from core.workflow import active_variant, export_snapshot, validate_snapshot, snapshot_is_current, freeze_subtitles
from storage.workspaces import WorkspaceStore
from storage.database import _json, _now


class ProductionStore(WorkspaceStore):
    def __init__(self, path):
        super().__init__(path)
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS production_templates (
                id TEXT NOT NULL, revision INTEGER NOT NULL, document_json TEXT NOT NULL,
                PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS productions (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                signature TEXT NOT NULL, document_json TEXT NOT NULL);
        ''')

    def production(self, identity):
        row = self.connection.execute('SELECT document_json FROM productions WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ApiError('找不到制作任务', 404)
        return json.loads(row[0])

    def save_production(self, doc):
        with self.connection:
            self.connection.execute('UPDATE productions SET document_json=? WHERE id=?', (_json(doc), doc['id']))


def normalize_template(value, cfg):
    if not isinstance(value, dict) or set(value) - {'id','revision','name','settings','packaging','export','source'}:
        raise ApiError('制作模板含未知字段')
    settings = copy.deepcopy(value.get('settings', {}))
    if set(settings) - {'form','editing_options','audio_track','allow_cpu','organization'} or settings.get('organization','single')!='single':
        raise ApiError('制作模板仅支持单路剪辑参数，不接受凭据或测试参数')
    form = settings.get('form', 'shorts')
    if form not in ('shorts','continuous','montage','full'):
        raise ApiError('成品形式无效')
    settings.update(form=form, organization='single')
    if form != 'full':
        settings['editing_options'] = normalize_editing_options({**settings.get('editing_options', {}), 'format': form})
        # Match-minute anchors refer to a particular recording, not a reusable template.
        if settings['editing_options']['anchors'] or settings['editing_options']['events']:
            raise ApiError('复用模板不能固定某场录像的时间校准或必剪事件，请在项目中添加')
    elif settings.get('editing_options'):
        raise ApiError('完整输出模板不接受选片参数')
    track = settings.get('audio_track', 0)
    if type(track) is not int or track < 0 or type(settings.get('allow_cpu', False)) is not bool:
        raise ApiError('音轨或 CPU 回退设置无效')
    package = value.get('packaging') or {'subtitles': True, 'export_options': default_export_options(cfg)}
    if set(package) - {'subtitles','export_options'} or type(package.get('subtitles', True)) is not bool:
        raise ApiError('包装参数无效')
    package = {**package, 'export_options': normalize_export_options(package.get('export_options', default_export_options(cfg)))}
    export = copy.deepcopy(value.get('export') or {})
    if set(export) - {'orientations','render','output_dir'}:
        raise ApiError('导出模板参数无效')
    export.setdefault('orientations', ['horizontal'])
    if not export['orientations'] or any(x not in ('horizontal','vertical') for x in export['orientations']):
        raise ApiError('导出画幅无效')
    export.setdefault('render', {k: cfg['render'][k] for k in ('horizontal','vertical','encoder','fps','bitrate')})
    render=export['render']
    if not isinstance(render,dict) or set(render)-{'horizontal','vertical','encoder','fps','bitrate'}:
        raise ApiError('导出编码参数含未知字段')
    render={**{k:cfg['render'][k] for k in ('horizontal','vertical','encoder','fps','bitrate')},**render}
    if render['encoder'] not in ('auto','libx264','h264_nvenc'):
        raise ApiError('编码器无效')
    fps=render['fps']
    if isinstance(fps,bool) or not isinstance(fps,(int,float)) or not math.isfinite(fps) or not 1<=fps<=120:
        raise ApiError('帧率需要在 1～120 之间')
    if not isinstance(render['bitrate'],str) or not re.fullmatch(r'[1-9]\d*(?:\.\d+)?[kKmM]?',render['bitrate']):
        raise ApiError('码率需要有效数值，例如 6M')
    for axis in ('horizontal','vertical'):
        dims=render[axis]
        if not isinstance(dims,list) or len(dims)!=2 or any(type(n) is not int or not 16<=n<=7680 or n%2 for n in dims):
            raise ApiError('分辨率需要两个 16～7680 的偶数')
    export['render']=render
    export.setdefault('output_dir', cfg['output_dir'])
    source = value.get('source') or {'minimum_minutes': 0, 'require_audio': True}
    if set(source) - {'minimum_minutes','require_audio'} or type(source.get('require_audio',True)) is not bool:
        raise ApiError('来源要求无效')
    minimum = source.get('minimum_minutes', 0)
    if isinstance(minimum,bool) or not isinstance(minimum,(float,int)) or not 0 <= minimum <= 1440:
        raise ApiError('来源最短时长无效')
    name = str(value.get('name', '')).strip()
    if not 1 <= len(name) <= 100:
        raise ApiError('模板名称需要 1～100 字符')
    return dict(name=name, settings=settings, packaging=package, export=export, source=source)


class FactoryService:
    def __init__(self, cfg, allowed_roots=(), profile='gpu'):
        self.cfg = cfg
        self.profile = profile
        self.allowed_roots = [Path(p).resolve() for p in allowed_roots]
        self.lock = threading.RLock()
        with ProductionStore(cfg['database']) as db:
            if not db.connection.execute('SELECT 1 FROM production_templates LIMIT 1').fetchone():
                for identity, name, content, form in [('match-shorts','比赛短片','match','shorts'),
                    ('conversation-shorts','对话短片','conversation','shorts'),('match-long','比赛连续长片','match','continuous')]:
                    doc = normalize_template({'name':name,'settings':{'form':form,'editing_options':{'content':content}},
                        'source':{'minimum_minutes':90 if content=='match' else 0,'require_audio':True}},cfg)
                    db.connection.execute('INSERT INTO production_templates VALUES(?,?,?)',(identity,1,_json(doc)))
                db.connection.commit()

    def templates(self):
        with ProductionStore(self.cfg['database']) as db:
            return [dict(json.loads(r['document_json']),id=r['id'],revision=r['revision']) for r in db.connection.execute(
                'SELECT * FROM production_templates t WHERE revision=(SELECT MAX(revision) FROM production_templates WHERE id=t.id) ORDER BY id')]

    def save_template(self, data):
        doc = normalize_template(data, self.cfg)
        identity = data.get('id') or uuid.uuid4().hex
        with ProductionStore(self.cfg['database']) as db:
            db.connection.execute('BEGIN IMMEDIATE')
            previous = db.connection.execute('SELECT max(revision) FROM production_templates WHERE id=?',(identity,)).fetchone()[0] or 0
            revision = previous + 1
            db.connection.execute('INSERT INTO production_templates VALUES(?,?,?)',(identity,revision,_json(doc)))
            db.connection.commit()
        return dict(doc,id=identity,revision=revision)

    def create(self, data):
        from core.video_input import probe_video
        from core.utils import file_signature
        key = data.get('idempotency_key')
        if not isinstance(key,str) or not 1 <= len(key) <= 200:
            raise ApiError('缺少幂等键')
        signature = fingerprint(data)
        with ProductionStore(self.cfg['database']) as db:
            row = db.connection.execute('SELECT id,signature FROM productions WHERE request_key=?',(key,)).fetchone()
            if row:
                if row['signature'] != signature: raise ApiError('幂等键已用于不同的制作参数',409)
                return self.view(row['id'])
        artifact = data.get('artifact') or {}
        path = Path(artifact.get('path','')).resolve()
        if not artifact.get('verified') or not artifact.get('source_identity') or not path.is_file():
            raise ApiError('仅接收带来源身份的已校验素材')
        if self.allowed_roots and not any(path.is_relative_to(p) for p in self.allowed_roots):
            raise ApiError('素材不在已配对的下载目录中')
        if artifact.get('bytes') != path.stat().st_size:
            raise ApiError('交付后文件大小已改变，请重新校验')
        template = normalize_template(data.get('template') or {}, self.cfg)
        meta = probe_video(path,self.cfg,require_audio=template['source'].get('require_audio',True))
        if abs(meta['duration']-float(artifact.get('duration',0))) > max(2.,meta['duration']*.01):
            raise ApiError('交付时长不一致')
        if meta['duration']+.1<template['source']['minimum_minutes']*60:
            raise ApiError('素材未达到模板要求的最短时长')
        meta.pop('raw_probe',None)
        meta['source_signature'] = file_signature(path)
        identity = uuid.uuid5(uuid.NAMESPACE_URL,'production:'+key).hex
        workspace_id = 'auto-'+identity
        asset = dict(id=fingerprint(str(path)),path=str(path),name=path.name,duration=meta['duration'],meta=meta)
        settings = template['settings']
        settings.update(asset_ids=[asset['id']],name=template['name'])
        doc = dict(id=identity,workspace_id=workspace_id,flow_id=data.get('flow_id'),status='preparing',
                   settings=settings,template=template,prepare_revision=0,prepare_job=None,previews={},confirmations=[],error=None)
        w = dict(id=workspace_id,name=str(data.get('name') or path.stem)[:200],step=2,assets=[asset],variants=[],active_variant=None,
            jobs=[],packaging=template['packaging'],source_options=settings,status='自动生成候选中',
            automation={'production_id':identity,'flow_id':data.get('flow_id'),'profile':self.profile},
            export_settings={**template['export'],'resolution':'原配置'})
        with ProductionStore(self.cfg['database']) as db:
            db.connection.execute('BEGIN IMMEDIATE')
            db.connection.execute('INSERT OR IGNORE INTO workspaces VALUES(?,?,0,?)',(workspace_id,_json(w),_now()))
            db.connection.execute('INSERT INTO productions VALUES(?,?,?,?)',(identity,key,signature,_json(doc)))
            db.connection.commit()
        return self.view(identity)

    def view(self, identity):
        with ProductionStore(self.cfg['database']) as db:
            doc = db.production(identity)
            w = db.get_workspace(doc['workspace_id'])
            media = db.list_media(w['id'],False)
        variant = active_variant(w)
        return {k:doc[k] for k in ('id','workspace_id','flow_id','status','error','prepare_job','previews','confirmations')} | {
            'profile':self.profile,'candidate_count':len(variant['items']) if variant else 0,
            'outputs':[r['result'] for r in media], 'step':w['step']}

    def confirm(self, identity, data):
        with self.lock, ProductionStore(self.cfg['database']) as db:
            doc = db.production(identity)
            if doc['status'] not in ('awaiting_review','exporting','completed','failed'):
                raise ApiError('候选与预览尚未准备好')
            w = db.get_workspace(doc['workspace_id'])
            supplied = data.get('snapshot')
            if supplied:
                validate_snapshot(supplied)
                if supplied['workspace_id'] != w['id'] or not snapshot_is_current(w, supplied, self.cfg):
                    raise ApiError('审核内容已变更，请刷新后重新确认',409)
                # Preserve the exact text reviewed by the caller. Rebuilding a
                # snapshot here could silently substitute a later cache result.
                snapshot = copy.deepcopy(supplied)
                if snapshot['packaging'].get('subtitles', True) and 'frozen_subtitles' not in snapshot:
                    snapshot['frozen_subtitles'] = freeze_subtitles(snapshot['items'], snapshot['packaging'], self.cfg)
            else:
                export = doc['template']['export']
                snapshot = export_snapshot(w,self.cfg,export['orientations'],export['render'],export['output_dir'])
            digest = fingerprint(snapshot)
            job = submit_job('workflow_export',{'workspace_id':w['id'],'snapshot':snapshot},self.cfg,
                             idempotency_key=f'production:{identity}:export:{digest}')
            if not any(c['digest']==digest for c in doc['confirmations']):
                doc['confirmations'].append({'digest':digest,'job_id':job['id'],'confirmed_at':_now()})
            doc.update(status='exporting',error=None)
            db.save_production(doc)
        return self.view(identity)

    def advance(self):
        with self.lock:
            ensure_worker(self.cfg)
            with ProductionStore(self.cfg['database']) as db:
                ids=[r[0] for r in db.connection.execute('SELECT id FROM productions')]
            for identity in ids:
                try: self._advance_one(identity)
                except Exception as exc:
                    with ProductionStore(self.cfg['database']) as db:
                        doc=db.production(identity);doc.update(status='failed',error=str(exc));db.save_production(doc)
        # Delivery errors never turn a successful export into a render failure.
        # The local outbox survives service restarts and publisher downtime.
        try:
            from core.deliveries import dispatch_delivery_outbox
            from core.publisher_client import PublisherClient
            dispatch_delivery_outbox(self.cfg, lambda d, p: PublisherClient(self.cfg, timeout=2).submit_job(d, p), self.profile, limit=2)
        except (ImportError, OSError, ValueError):
            pass

    def _advance_one(self, identity):
        with ProductionStore(self.cfg['database']) as db:
            doc=db.production(identity)
            if doc['status'] not in ('preparing','previewing','exporting'):return
            output=Path(self.cfg['output_dir']);output.mkdir(parents=True,exist_ok=True)
            if shutil.disk_usage(output).free<1024**3:
                doc.update(status='needs_assistance',error='工厂输出磁盘可用空间不足 1GB，请清理后恢复此任务')
                db.save_production(doc);return
            if doc['status']=='preparing':
                job=submit_job('workflow_prepare',{'workspace_id':doc['workspace_id'],'settings':doc['settings'],
                    'expected_revision':doc['prepare_revision']},self.cfg,idempotency_key=f'production:{identity}:prepare')
                doc['prepare_job']=job['id']
                if job['status'] in ('failed','interrupted','cancelled'):doc.update(status='failed',error=job['error'])
                elif job['status']=='completed':doc['status']='previewing'
            if doc['status']=='previewing':
                w=db.get_workspace(doc['workspace_id']);variant=active_variant(w)
                if not variant or not variant['items']:
                    doc.update(status='awaiting_review',error='没有找到候选，请调整规则后在工作台重试')
                else:
                    ready=True
                    for item in variant['items']:
                        job=submit_job('workflow_preview',{'workspace_id':w['id'],'item_id':item['id']},self.cfg,
                            idempotency_key=f'production:{identity}:preview:{item["id"]}:{item["revision"]}')
                        doc['previews'][item['id']]=job['id']
                        if job['status'] in ('failed','interrupted','cancelled'):
                            doc.update(status='failed',error=job['error']);break
                        ready=ready and job['status']=='completed'
                    if ready and doc['status']!='failed':doc['status']='awaiting_review'
            if doc['status']=='exporting' and doc['confirmations']:
                job=get_job(doc['confirmations'][-1]['job_id'],self.cfg)
                if job['status']=='completed':doc.update(status='completed',error=None)
                elif job['status'] in ('failed','interrupted','cancelled'):doc.update(status='failed',error=job['error'])
            db.save_production(doc)

    def retry(self, identity):
        with self.lock, ProductionStore(self.cfg['database']) as db:
            doc=db.production(identity)
            for job_id in [doc['prepare_job'],*doc['previews'].values(),*[c['job_id'] for c in doc['confirmations'][-1:]]]:
                if not job_id:continue
                job=get_job(job_id,self.cfg)
                if job and job['status'] in ('failed','interrupted','cancelled'):retry_job(job_id,self.cfg)
            doc.update(status='exporting' if doc['confirmations'] else 'preparing',error=None)
            db.save_production(doc)
        return self.view(identity)

    def dispatch(self, method, path, data):
        with self.lock:
            if path.startswith('/api/v1/agent/'):
                from core.export_center import agent_call
                return agent_call(method,path[len('/api/v1/agent/'):],data,self.cfg)
            if path == '/api/v1/deliveries' and method == 'GET':
                profile = data.get('profile', self.profile)
                if profile != self.profile:
                    raise ApiError('交付配置不匹配', 409)
                try:
                    cursor = int(data.get('cursor', 0))
                    limit = int(data.get('limit', 100))
                except (TypeError, ValueError):
                    raise ApiError('交付游标或数量无效')
                with ProductionStore(self.cfg['database']) as db:
                    if data.get('pending') in ('true', '1', True):
                        from core.deliveries import list_pending_deliveries
                        entries = list_pending_deliveries(self.cfg, profile, limit)
                        return {'deliveries': entries, 'next_cursor': str(cursor)}
                    return db.list_deliveries(cursor, profile, limit)
            if path.startswith('/api/v1/deliveries/') and method == 'GET':
                identity = path[len('/api/v1/deliveries/'):]
                with ProductionStore(self.cfg['database']) as db:
                    delivery = db.get_delivery(identity, self.profile)
                if delivery is None:
                    raise ApiError('找不到交付', 404)
                return delivery
            if path=='/api/v1/health' and method=='GET':
                return {'service':'football-factory','api_version':1,'version':VERSION,'profile':self.profile,
                    'instance':fingerprint(self.cfg['database']),'asr_device':self.cfg['asr']['device'],
                    'compute_type':self.cfg['asr']['compute_type'],'encoder':self.cfg['render']['encoder'],
                    'capabilities':['productions','approval-snapshots','templates','idempotency','deliveries','delivery-outbox','agent-v1','cross-project-export','subtitle-quality-repair']}
            if path=='/api/v1/templates':
                return {'templates':self.templates()} if method=='GET' else self.save_template(data)
            if path=='/api/v1/productions' and method=='POST':return self.create(data)
            parts=path.strip('/').split('/')
            if len(parts)>=4 and parts[:3]==['api','v1','productions']:
                if method=='GET' and len(parts)==4:return self.view(parts[3])
                if method=='POST' and parts[4:]==['confirm']:return self.confirm(parts[3],data)
                if method=='POST' and parts[4:]==['retry']:return self.retry(parts[3])
            raise ApiError('接口不存在',404)
