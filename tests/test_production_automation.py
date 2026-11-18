"""Durable API boundaries, approval gating and coordinator recovery."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request,error

from core.config import load_config
from core.jobs import submit_job, _connection
from core.local_api import make_server,ApiError
from core.production import FactoryService,ProductionStore,normalize_template
from core.workflow import active_variant,edit_item,export_snapshot,_item

HUB_ROOT=Path(__file__).resolve().parents[2]/'FootballWorkflow'
if (HUB_ROOT/'flowhub/service.py').is_file():
    sys.path.insert(0,str(HUB_ROOT))
    from flowhub.service import Hub,FlowError,source_url
else:
    Hub = FlowError = source_url = None


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='自动生产-');self.root=Path(self.temp.name)
        self.cfg=load_config();self.cfg.update(database=str(self.root/'factory.sqlite3'),output_dir=str(self.root/'output'),cache_dir=str(self.root/'cache'))
        self.source=self.root/'中文 原片.mp4';self.source.write_bytes(b'fixture')
        self.workers=[patch('core.jobs.ensure_worker',return_value=False),patch('core.production.ensure_worker',return_value=False)]
        for worker in self.workers:worker.start()
        self.service=FactoryService(self.cfg,[self.root])
        self.artifact={'verified':True,'path':str(self.source),'bytes':7,'duration':90.,'source_identity':{'platform':'haixing','resource_id':'1'}}
        self.meta={'source':str(self.source),'duration':90.,'width':320,'height':180,'audio_streams':[{'index':1}]}
    def tearDown(self):
        for worker in self.workers:worker.stop()
        self.temp.cleanup()
    def create(self):
        data={'idempotency_key':'source-one','flow_id':'flow-one','artifact':self.artifact,'template':self.service.templates()[0]}
        with patch('core.video_input.probe_video',return_value=self.meta):return self.service.create(data)
    def complete(self,identity):
        with _connection(self.cfg['database']) as db:db.execute("UPDATE jobs SET status='completed',result_json='{}' WHERE id=?",(identity,))
    def review_ready(self):
        result=self.create();self.service.advance()
        with ProductionStore(self.cfg['database']) as db:
            doc=db.production(result['id']);w=db.get_workspace(doc['workspace_id'])
            item=_item('i1','原始标题',[{'start':1.,'end':21.}],{'id':'project','source':str(self.source),'duration':90.,'metadata':{},'project_dir':str(self.root)},subtitle='1\n00:00:00,000 --> 00:00:01,000\n人工字幕\n')
            w.update(variants=[{'id':'v','form':'shorts','name':'一批','items':[item],'events':[],'options':{}}],active_variant='v',step=3)
            db.save_workspace(w)
        self.complete(doc['prepare_job']);self.service.advance()
        doc=self.service.view(result['id'])
        for job in doc['previews'].values():self.complete(job)
        self.service.advance()
        return result['id'],result['workspace_id']
    def test_template_snapshots_and_public_schema_reject_credentials(self):
        old=self.service.templates()[0]
        self.assertEqual(normalize_template(old,self.cfg)['settings']['organization'],'single')
        saved=self.service.save_template({**old,'name':'新修订'})
        self.assertEqual(saved['revision'],2);self.assertEqual(old['revision'],1)
        with self.assertRaises(ApiError):normalize_template({'name':'x','settings':{'api_key':'secret'}},self.cfg)
    def test_factory_create_is_idempotent_and_validates_receipt(self):
        first=self.create();second=self.create();self.assertEqual(first['id'],second['id'])
        with ProductionStore(self.cfg['database']) as db:self.assertEqual(len(db.list_workspaces()),1)
        data={'idempotency_key':'wrong','artifact':{**self.artifact,'bytes':8},'template':self.service.templates()[0]}
        with self.assertRaisesRegex(Exception,'大小'):self.service.create(data)
    def test_render_template_rejects_unknown_fields_and_bad_values(self):
        base=self.service.templates()[0]
        for change in ({'api_key':'secret'},{'fps':0},{'horizontal':[321,180]},{'encoder':'unknown'},{'bitrate':'bad'}):
            bad=copy.deepcopy(base);bad['export']['render'].update(change)
            with self.assertRaises(ApiError):normalize_template(bad,self.cfg)
    def test_factory_rechecks_minimum_duration_before_import(self):
        data={'idempotency_key':'short-match','artifact':self.artifact,
            'template':next(t for t in self.service.templates() if t['id']=='match-shorts')}
        with patch('core.video_input.probe_video',return_value=self.meta),self.assertRaisesRegex(ApiError,'最短时长'):
            self.service.create(data)
    def test_disk_pressure_blocks_new_preparation_and_retry_reuses_id(self):
        created=self.create()
        with patch('core.production.shutil.disk_usage',return_value=type('Disk',(),{'free':1})()):self.service.advance()
        self.assertEqual(self.service.view(created['id'])['status'],'needs_assistance')
        self.assertIsNone(self.service.view(created['id'])['prepare_job'])
        self.service.retry(created['id']);self.service.advance()
        job=self.service.view(created['id'])['prepare_job'];self.service.retry(created['id']);self.service.advance()
        self.assertEqual(self.service.view(created['id'])['prepare_job'],job)
    def test_completed_job_idempotency_survives_worker_restart(self):
        payload={'path':str(self.source),'skip_render':True}
        first=submit_job('process',payload,self.cfg,idempotency_key='operation')
        self.complete(first['id'])
        self.assertEqual(submit_job('process',payload,self.cfg,idempotency_key='operation')['id'],first['id'])
        with self.assertRaisesRegex(ValueError,'幂等键'):submit_job('process',{**payload,'skip_render':False},self.cfg,idempotency_key='operation')
    def test_previews_ready_does_not_export_without_explicit_batch_confirmation(self):
        identity,workspace=self.review_ready()
        self.assertEqual(self.service.view(identity)['status'],'awaiting_review')
        with _connection(self.cfg['database']) as db:self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='workflow_export'").fetchone()[0],0)
        with self.assertRaisesRegex(ValueError,'通过'):self.service.confirm(identity,{})
        edit_item(workspace,'i1',{'title':'人工标题','status':'approved'},self.cfg)
        confirmed=self.service.confirm(identity,{})
        again=self.service.confirm(identity,{})
        self.assertEqual(len(again['confirmations']),1)
        self.assertEqual(confirmed['confirmations'],again['confirmations'])
        edit_item(workspace,'i1',{'title':'第二修订'},self.cfg)
        revised=self.service.confirm(identity,{})
        self.assertEqual(len(revised['confirmations']),2)
    def test_outdated_manual_subtitles_block_confirmation(self):
        identity,workspace=self.review_ready()
        edit_item(workspace,'i1',{'status':'approved','segments':[{'start':2.,'end':21.}]},self.cfg)
        with self.assertRaisesRegex(ValueError,'字幕'):self.service.confirm(identity,{})
    def test_http_rejects_unpaired_origin_and_unknown_routes(self):
        server=make_server(0,'token',self.service.dispatch,'factory-test')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        opener=request.build_opener(request.ProxyHandler({}))
        try:
            for headers,code in [({},401),({'X-Workflow-Token':'token','Origin':'https://example.com'},403)]:
                with self.assertRaises(error.HTTPError) as caught:opener.open(request.Request(base+'/api/v1/health',headers=headers))
                self.assertEqual(caught.exception.code,code)
            with opener.open(request.Request(base+'/api/v1/health',headers={'X-Workflow-Token':'token'})) as response:
                self.assertEqual(json.load(response)['service'],'football-factory')
        finally:server.shutdown();server.server_close();thread.join()


class RemoteFixture:
    def __init__(self):
        self.downloads={};self.productions={};self.lost_response=False
        self.template={'id':'match','revision':1,'name':'比赛短片','source':{'minimum_minutes':0,'require_audio':True},'settings':{},'packaging':{},'export':{}}
    def health(self,service):return {'api_version':1}
    def call(self,service,path,data=None):
        if path=='/api/v1/templates':return {'templates':[self.template]}
        if service=='lightboat':
            if data is not None and path=='/api/v1/jobs':
                return self.downloads.setdefault(data['idempotency_key'],{'id':'download','status':'completed','artifact':{'verified':True,'path':'not-read-by-hub'}})
            if path.endswith('/retry') or path.endswith('/choose'):return {}
            return next(iter(self.downloads.values()))
        if data is not None and path=='/api/v1/productions':
            result=self.productions.setdefault(data['idempotency_key'],{'id':'production','workspace_id':'workspace','status':'awaiting_review','outputs':[]})
            if self.lost_response:self.lost_response=False;raise FlowError('模拟受理后响应丢失',503)
            return result
        result=next(iter(self.productions.values()))
        if path.endswith('/confirm'):result['status']='exporting'
        return result


@unittest.skipIf(Hub is None, 'Optional sibling FootballWorkflow is not installed')
class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.database=Path(self.temp.name)/'hub.sqlite3'
        self.remote=RemoteFixture();self.config={'profile':'gpu'};self.hub=Hub(self.database,self.config,self.remote)
        self.data={'idempotency_key':'one','source_url':'https://ymx168.top/live/1?recordId=2','template_id':'match','template_revision':1}
    def tearDown(self):self.temp.cleanup()
    def test_end_to_end_waits_for_human_then_tracks_output(self):
        flow=self.hub.create(self.data);self.hub.tick();flow=self.hub.get(flow['id'])
        self.assertEqual(flow['status'],'awaiting_review')
        self.assertEqual(len(self.remote.productions),1)
        self.hub.action(flow['id'],'confirm',{})
        self.assertEqual(self.hub.get(flow['id'])['status'],'exporting')
        production=next(iter(self.remote.productions.values()));production.update(status='completed',outputs=[{'video':'result.mp4'}])
        flow=self.hub.get(flow['id']);flow['next_poll']=0;self.hub.save(flow);self.hub.tick()
        self.assertEqual(self.hub.get(flow['id'])['outputs'],[{'video':'result.mp4'}])
    def test_response_lost_restart_reconciles_same_child_operation(self):
        self.remote.lost_response=True
        flow=self.hub.create(self.data);self.hub.tick()
        recovered=Hub(self.database,self.config,self.remote);flow=recovered.get(flow['id']);flow['next_poll']=0;recovered.save(flow);recovered.tick()
        self.assertEqual(recovered.get(flow['id'])['status'],'awaiting_review')
        self.assertEqual(len(self.remote.downloads),1);self.assertEqual(len(self.remote.productions),1)
    def test_source_template_freezing_and_request_deduplication(self):
        flow=self.hub.create(self.data);self.assertEqual(self.hub.create(self.data)['id'],flow['id'])
        self.remote.template=copy.deepcopy(self.remote.template);self.remote.template['revision']=2
        self.assertEqual(self.hub.get(flow['id'])['template']['revision'],1)
        with self.assertRaises(FlowError):self.hub.create({**self.data,'source_url':'https://ymx168.top/live/1?recordId=3'})
    def test_source_validation_strips_page_secrets_and_rejects_unsupported(self):
        self.assertNotIn('token',source_url(self.data['source_url']+'&token=private'))
        for bad in ('https://example.com/video','http://ymx168.top/live/1?recordId=2','https://ymx168.top/live/1'):
            with self.assertRaises(FlowError):source_url(bad)
    def test_cancel_stops_pipeline_before_import(self):
        flow=self.hub.create(self.data);self.hub.action(flow['id'],'cancel',{});self.hub.tick()
        self.assertFalse(self.remote.downloads);self.assertFalse(self.remote.productions)
        for action in ('confirm','retry','choose'):
            with self.assertRaisesRegex(FlowError,'已取消'):self.hub.action(flow['id'],action,{})
    def test_cancel_reports_already_submitted_exports_honestly(self):
        flow=self.hub.create(self.data);self.hub.tick();self.hub.action(flow['id'],'confirm',{})
        cancelled=self.hub.action(flow['id'],'cancel',{})
        self.assertIn('已确认提交的导出仍会执行',cancelled['message'])
        self.assertEqual(next(iter(self.remote.productions.values()))['status'],'exporting')
    def test_network_retry_is_bounded_and_recovers_without_duplicate_children(self):
        flow=self.hub.create(self.data)
        with patch.object(self.remote,'health',side_effect=FlowError('service unavailable',503)):
            for _ in range(4):
                doc=self.hub.get(flow['id']);doc['next_poll']=0;self.hub.save(doc);self.hub.tick()
        self.assertEqual(self.hub.get(flow['id'])['status'],'failed')
        self.assertFalse(self.remote.downloads)
        self.hub.action(flow['id'],'retry',{});self.hub.tick()
        self.assertEqual(self.hub.get(flow['id'])['status'],'awaiting_review')


if __name__=='__main__':unittest.main()
