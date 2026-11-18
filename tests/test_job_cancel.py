import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.config import load_config
from core.jobs import (_connection, _execute_isolated, _pid_alive, cancel_job, get_job,
                       list_jobs, retry_job, run_worker, submit_job)


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config()
        self.cfg.update(database=str(self.root/'db.sqlite3'), output_dir=str(self.root/'out'), cache_dir=str(self.root/'cache'))
        self.worker = patch('core.jobs.ensure_worker', return_value=False)
        self.worker.start()

    def tearDown(self):
        self.worker.stop()
        self.temp.cleanup()

    def submit(self, identity='p'):
        return submit_job('preview', {'project_id':identity, 'clip_id':'c'}, self.cfg)

    def test_queued_cancel_is_durable_idempotent_and_skipped(self):
        job = self.submit()
        self.assertEqual(cancel_job(job['id'], self.cfg)['status'], 'cancelled')
        self.assertEqual(cancel_job(job['id'], self.cfg)['status'], 'cancelled')
        self.assertEqual(list_jobs(self.cfg)[0]['status'], 'cancelled')
        with patch('core.jobs._execute') as execute:
            run_worker(self.cfg)
        execute.assert_not_called()
        self.assertEqual(get_job(job['id'], self.cfg)['attempt'], 0)
        self.assertEqual(retry_job(job['id'], self.cfg)['status'], 'queued')

    def test_completed_task_cannot_be_cancelled(self):
        job = self.submit()
        with patch('core.jobs._execute', return_value={'done':True}):
            run_worker(self.cfg)
        with self.assertRaises(ValueError):
            cancel_job(job['id'], self.cfg)
        self.assertEqual(get_job(job['id'], self.cfg)['status'], 'completed')

    def test_cancel_wins_finish_race_and_preserves_completed_result(self):
        job = self.submit()
        def execute(*args):
            self.assertEqual(cancel_job(job['id'], self.cfg)['status'], 'cancelling')
            return {'exports':[{'video':'already-finished.mp4'}]}
        with patch('core.jobs._execute', side_effect=execute):
            run_worker(self.cfg)
        result = get_job(job['id'], self.cfg)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['result']['exports'][0]['video'], 'already-finished.mp4')

    def test_running_cancellation_stops_real_task_tree_then_runs_next_job(self):
        first, second = self.submit('first'), self.submit('second')
        sentinel = self.root/'completed-cache.json'
        sentinel.write_text('preserve')
        heartbeat, pidfile = self.root/'heartbeat', self.root/'child-pid'
        child = self.root/'child.py'
        child.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.1)\n')
        task = self.root/'task.py'
        task.write_text('import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\npathlib.Path(sys.argv[3]).write_text(str(p.pid))\ntime.sleep(60)\n')
        real_popen = subprocess.Popen
        def popen(command, **kwargs):
            if len(command)>1 and Path(command[1]).name=='job_task.py':
                command = [sys.executable, str(task), str(child), str(heartbeat), str(pidfile)]
            return real_popen(command, **kwargs)
        errors=[]
        def cancel():
            try:
                deadline=time.monotonic()+10
                while not heartbeat.exists() and time.monotonic()<deadline:
                    time.sleep(.05)
                if not heartbeat.exists():
                    raise AssertionError('Task child did not start')
                cancel_job(first['id'],self.cfg)
            except Exception as exc:
                errors.append(exc)
        thread=threading.Thread(target=cancel)
        thread.start()
        def isolated(job,cfg):
            return _execute_isolated(job,cfg) if job['id']==first['id'] else {'done':True}
        with patch('core.jobs.subprocess.Popen', side_effect=popen), patch('core.jobs._execute_isolated', side_effect=isolated):
            run_worker(self.cfg,isolate=True)
        thread.join(timeout=12)
        self.assertFalse(errors,errors)
        self.assertEqual(get_job(first['id'],self.cfg)['status'],'cancelled')
        self.assertEqual(get_job(second['id'],self.cfg)['status'],'completed')
        self.assertFalse(_pid_alive(int(pidfile.read_text())))
        self.assertEqual(sentinel.read_text(),'preserve')

    def test_queue_list_has_per_row_cancel_action(self):
        from streamlit.testing.v1 import AppTest
        job = self.submit()
        paths = {key:self.cfg[key] for key in ('database', 'cache_dir', 'output_dir')}
        code = 'from ui.app import jobs_page\njobs_page('+repr(paths)+')'
        with patch('ui.app.ensure_worker',return_value=False):
            app = AppTest.from_string(code,default_timeout=15).run()
            self.assertEqual(len(app.exception),0,str(app.exception))
            app.button(key='cancel-'+job['id']).click().run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        self.assertEqual(get_job(job['id'],self.cfg)['status'],'cancelled')
        self.assertTrue(any(x.label=='重新执行此任务' for x in app.button))


if __name__=='__main__':
    unittest.main()
