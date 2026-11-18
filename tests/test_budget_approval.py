import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.budget_approval import approve_and_retry, budget_context, budget_request
from core.config import load_config
from core.jobs import get_job, run_worker, submit_job
from core.llm_client import BudgetExceeded, BudgetLedger, HTTPStatusError, LLMClient

SCHEMA = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}


class BudgetApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config()
        self.cfg.update(database=str(self.root/'db.sqlite3'), cache_dir=str(self.root/'cache'), output_dir=str(self.root/'out'))
        self.cfg['openai'].update(provider='api',model='test-model',api_key='fixture',budget_usd=.1,
                                  input_cost_per_million=1000.,output_cost_per_million=0.,max_output_tokens=10,max_retries=0)
        self.path = self.root/'cache/project/selection/api_budget.json'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'version':1,'attempts':[{'id':0,'status':'success','cost_usd':.08,'model':'test-model'}]}))
        self.calls=[]
        self.patches=[patch('core.jobs.ensure_worker',return_value=False), patch('core.config.load_config',side_effect=lambda *a:copy.deepcopy(self.cfg))]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def ledger(self):
        return json.loads(self.path.read_text())

    def transport(self, payload):
        self.calls.append(payload)
        return {'choices':[{'message':{'content':'{"ok":true}'}}], 'usage':{'prompt_tokens':10,'completion_tokens':1}}

    def request(self, content='JSON', transport=None, options=None):
        return LLMClient(options or self.cfg['openai'],self.path,transport or self.transport,sleeper=lambda _:None).complete(
            [{'role':'user','content':content}],SCHEMA,response_key='ok')

    def blocked(self):
        job=submit_job('preview',{'project_id':'p','clip_id':'c'},self.cfg)
        with patch('core.jobs._execute',side_effect=lambda *a:self.request()):
            run_worker(self.cfg)
        job=get_job(job['id'],self.cfg)
        self.assertEqual(job['status'],'failed')
        request=budget_request(job,self.cfg)
        self.assertIsNotNone(request)
        return job,request

    def test_no_payment_before_approval_then_resume_once_with_cap_unchanged(self):
        job,request=self.blocked()
        self.assertEqual(self.calls,[])
        before=copy.deepcopy(self.ledger()['attempts'])
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.ledger()['attempts'],before)
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'queued')
        with patch('core.jobs._execute',side_effect=lambda *a:self.request()):run_worker(self.cfg)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'completed')
        self.assertEqual(self.cfg['openai']['budget_usd'],.1)
        self.assertIsNotNone(self.ledger()['budget_approvals'][0]['consumed_at'])

    def test_following_over_budget_request_requires_another_approval(self):
        job,request=self.blocked()
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        def two(*args):
            self.request()
            return self.request()
        with patch('core.jobs._execute',side_effect=two):run_worker(self.cfg)
        job=get_job(job['id'],self.cfg)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(job['status'],'failed')
        self.assertNotEqual(budget_request(job,self.cfg)['request_id'],request['request_id'])

    def test_concurrent_requests_cannot_consume_same_approval_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        job,request=self.blocked()
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        def attempt(_):
            with budget_context(job):
                try:
                    self.request()
                    return 'sent'
                except BudgetExceeded:
                    return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['blocked','sent'])
        self.assertEqual(len(self.calls),1)

    def test_approval_cannot_be_used_by_another_job_or_changed_request(self):
        job,request=self.blocked()
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        other={**job,'id':'other-job','log_path':str(self.root/'other.log')}
        with budget_context(other), self.assertRaises(BudgetExceeded):self.request()
        self.assertIsNone(self.ledger()['budget_approvals'][0]['consumed_at'])
        with budget_context(job), self.assertRaises(BudgetExceeded):self.request('changed request')
        self.assertIsNone(self.ledger()['budget_approvals'][0]['consumed_at'])
        self.assertEqual(self.calls,[])

    def test_failed_network_attempt_consumes_approval_and_retry_needs_new_one(self):
        job,request=self.blocked()
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        def fail(payload):
            self.calls.append(payload)
            raise HTTPStatusError(500,'fixture')
        options={**self.cfg['openai'],'max_retries':1}
        with patch('core.jobs._execute',side_effect=lambda *a:self.request(transport=fail,options=options)):run_worker(self.cfg)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.ledger()['attempts'][-1]['status'],'failed')
        self.assertIsNotNone(budget_request(get_job(job['id'],self.cfg),self.cfg))

    def test_call_count_limit_and_changed_spending_are_not_bypassed(self):
        job,request=self.blocked()
        options={**self.cfg['openai'],'max_calls':1}
        with budget_context(job),self.assertRaises(BudgetExceeded):self.request(options=options)
        ledger=self.ledger()
        ledger['attempts'][0]['cost_usd']=.09
        self.path.write_text(json.dumps(ledger))
        with self.assertRaises(ValueError):approve_and_retry(job['id'],request['request_id'],self.cfg)
        self.assertNotIn('budget_approvals',self.ledger())

    def test_optional_feature_catching_budget_error_still_exposes_approval(self):
        job=submit_job('preview',{'project_id':'p','clip_id':'c'},self.cfg)
        def optional_feature(*args):
            try:
                self.request()
            except Exception:
                return {'warnings':['optional title or visual analysis not complete']}
        with patch('core.jobs._execute',side_effect=optional_feature):run_worker(self.cfg)
        job=get_job(job['id'],self.cfg)
        self.assertEqual(job['status'],'failed')
        self.assertIsNotNone(budget_request(job,self.cfg))
        self.assertEqual(self.calls,[])

    def test_legacy_failed_task_can_be_approved_without_resetting_spending(self):
        from storage.workspaces import WorkspaceStore
        with WorkspaceStore(self.cfg['database']) as db:
            db.upsert_project(dict(id='p',source=str(self.root/'video.mp4'),name='fixture',duration=10,
                                   project_dir=str(self.path.parents[1]),status='failed',metadata={}))
        job=submit_job('preview',{'project_id':'p','clip_id':'c'},self.cfg)
        from core.jobs import _connection
        with _connection(self.cfg['database']) as conn:
            conn.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",('本次请求预计超过项目预算：已计 $0.0800，预留 $0.8000，上限 $0.1000',job['id']))
        job=get_job(job['id'],self.cfg)
        request=budget_request(job,self.cfg)
        self.assertTrue(request['legacy'])
        approve_and_retry(job['id'],request['request_id'],self.cfg)
        self.assertEqual(len(self.ledger()['attempts']),1)
        self.assertEqual(self.ledger()['attempts'][0]['cost_usd'],.08)

    def test_dialog_cancel_is_read_only_and_approve_queues_task(self):
        from streamlit.testing.v1 import AppTest
        job,request=self.blocked()
        code='''
from core.jobs import get_job
from ui.budget_approval import budget_action
cfg=CONFIG
budget_action(get_job(JOB,cfg),cfg,'open-budget')
'''.replace('CONFIG',repr(self.cfg)).replace('JOB',repr(job['id']))
        app=AppTest.from_string(code,default_timeout=15).run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        app.button(key='open-budget').click().run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        self.assertEqual(len(app.metric),4)
        app.button(key='budget-decline-'+job['id']).click().run()
        self.assertNotIn('budget_approvals',self.ledger())
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'failed')
        app.button(key='open-budget').click().run()
        app.button(key='budget-approve-'+job['id']).click().run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        self.assertEqual(len(app.error),0,[x.value for x in app.error])
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'queued')
        self.assertEqual(self.calls,[])

    def test_changed_provider_resumes_without_granting_original_api_budget(self):
        from core.budget_approval import resume_with_current
        job,request=self.blocked()
        self.cfg['openai'].update(provider='codex_cli', model='')
        with self.assertRaises(ValueError):approve_and_retry(job['id'],request['request_id'],self.cfg)
        resume_with_current(job['id'],self.cfg)
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'queued')
        self.assertNotIn('budget_approvals',self.ledger())
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
