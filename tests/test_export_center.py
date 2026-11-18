import copy
import unittest
from unittest.mock import patch
from tests.test_workflow import WorkflowTests
from core.export_center import catalog,plan_exports,submit_exports,run_export_batch
from core.workflow import active_variant,edit_item,preview_workflow
from storage.workspaces import WorkspaceStore


class ExportCenterTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        worker=patch('core.jobs.ensure_worker',return_value=False)
        worker.start();self.addCleanup(worker.stop)

    def test_all_projects_and_inactive_approved_variants_are_included(self):
        with WorkspaceStore(self.cfg['database']) as store:
            base=store.get_workspace('legacy-p')
            for index in range(2):
                doc=store.create_workspace('另一项目'+str(index),'project'+str(index))
                doc['variants']=copy.deepcopy(base['variants']);doc['active_variant']=base['active_variant']
                store.save_workspace(doc)
            base['variants'].append({**copy.deepcopy(base['variants'][0]),'id':'inactive','name':'备用方案'})
            store.save_workspace(base)
        rows=catalog(self.cfg);self.assertEqual(len(rows),4)
        plan=plan_exports(self.cfg,[r['key'] for r in rows])
        self.assertFalse(plan['issues']);self.assertEqual(plan['item_count'],4)
        self.assertEqual(submit_exports(plan,self.cfg)['id'],submit_exports(plan,self.cfg)['id'])
        edit_item('legacy-p','c1',{'title':'后来修改'},self.cfg)
        with self.assertRaisesRegex(ValueError,'修改'):submit_exports(plan,self.cfg)

    def test_invalid_item_is_reported_without_hiding_good_project(self):
        rows=catalog(self.cfg)
        plan=plan_exports(self.cfg,[rows[0]['key'],'missing/v/i'])
        self.assertEqual(plan['item_count'],1);self.assertEqual(plan['issues'][0]['code'],'NOT_FOUND')
        with self.assertRaises(ValueError):submit_exports(plan,self.cfg)
        self.assertFalse(plan_exports(self.cfg,[rows[0]['key']])['issues'])

    def test_legacy_media_does_not_crash_global_listing(self):
        with WorkspaceStore(self.cfg['database']) as store:
            store.record_workflow_media('legacy-p','c1',{'revision':1},{'video':str(self.source)})
        self.assertEqual(catalog(self.cfg)[0]['export_status'],'changed')

    def test_legacy_export_stays_visible_without_default_rerender(self):
        with WorkspaceStore(self.cfg['database']) as store:
            w=store.get_workspace('legacy-p')
            active_variant(w)['items'][0]['legacy_outputs']=[{'path':str(self.source)}]
            store.save_workspace(w)
        row=catalog(self.cfg)[0]
        self.assertEqual(row['export_status'],'legacy');self.assertFalse(row['default_selected'])
        self.assertEqual(row['outputs'][0]['video'],str(self.source))

    def test_completed_batch_is_verified_and_not_rendered_twice(self):
        plan=plan_exports(self.cfg,[catalog(self.cfg)[0]['key']])
        result={'workspace_id':'legacy-p','exports':[{'video':'fixture.mp4'}],'errors':[]}
        with patch('core.export_center.run_workflow_export',return_value=result) as renderer,patch('core.deliveries.verify_render_package'):
            self.assertFalse(run_export_batch(plan,self.cfg)['errors'])
            self.assertFalse(run_export_batch(plan,self.cfg)['errors'])
            self.assertEqual(renderer.call_count,1)

    def test_failed_review_never_becomes_successful_preview(self):
        self.cfg['subtitle_review']['enabled']=True
        with patch('core.workflow.review_workflow_subtitles',return_value={'needs_retry':True}),patch('core.workflow_renderer.render_workflow_item') as renderer:
            with self.assertRaisesRegex(ValueError,'复核未完成'):preview_workflow('legacy-p','c1',self.cfg)
            renderer.assert_not_called()

    def test_repair_retains_manual_subtitle_title_approval_and_rating(self):
        from core.workflow_quality import repair_previews
        self.cfg['subtitle_review']['enabled']=True
        before=active_variant(self.workspace())['items'][0]
        with patch('core.workflow_quality._scores',return_value={}),patch('core.workflow.preview_workflow',return_value={'video':'fixture'}):
            result=repair_previews('legacy-p',self.cfg)
        self.assertEqual(result['preview_count'],1)
        self.assertEqual(before,active_variant(self.workspace())['items'][0])


if __name__=='__main__':unittest.main()
