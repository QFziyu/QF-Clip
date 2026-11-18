"""Publishing stays behind the existing final export and an exact batch approval."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.workflow import _item
from storage.workspaces import WorkspaceStore


class PublishingUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='publishing-ui-')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            'DATABASE_PATH': str(self.root / 'factory.sqlite3'),
            'CACHE_DIR': str(self.root / 'cache'), 'OUTPUT_DIR': str(self.root / 'out')})
        self.env.start()
        self.worker = patch('core.jobs.ensure_worker', return_value=False)
        self.worker.start()
        self.client_patch = patch('ui.publishing.PublisherClient')
        self.client_class = self.client_patch.start()
        self.client = self.client_class.return_value
        self.account = dict(id='douyin:gpu', label='已确认的测试账号', status='ready',
                            creator_id='creator-123', auto_submit_verified=False)
        self.client.health.return_value = {'service': 'football-publisher'}
        self.client.accounts.side_effect = lambda: {'accounts': [copy.deepcopy(self.account)]}
        self.client.jobs.return_value = {'jobs': []}

    def tearDown(self):
        self.client_patch.stop()
        self.worker.stop()
        self.env.stop()
        self.temp.cleanup()

    def seed(self, automation=False):
        cfg = load_config()
        source = self.root / '本地录像.mp4'
        source.touch()
        with WorkspaceStore(cfg['database']) as store:
            w = store.create_workspace('本批人工审核')
            approved = _item('current', '人工确认的标题', [{'start': 1., 'end': 12.}],
                dict(id='source', source=str(source), duration=20., metadata={}, project_dir=str(self.root)))
            approved.update(status='approved', description='人工保存的发布文案')
            pending = {**copy.deepcopy(approved), 'id': 'pending', 'title': '未通过审核', 'status': 'pending'}
            rejected = {**copy.deepcopy(approved), 'id': 'rejected', 'title': '已拒绝内容', 'status': 'rejected'}
            w.update(step=5, active_variant='variant', packaging={'subtitles': False},
                variants=[dict(id='variant', name='当前方案', form='shorts', items=[approved, pending, rejected], events=[], options={})])
            if automation:
                w['automation'] = {'flow_id': 'confirmed-flow'}
            store.save_workspace(w)
            store.preference('current_workspace', w['id'])
            return w['id']

    def app(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'ui/app.py'), default_timeout=20).run()
        next(r for r in app.radio if r.label=='导出范围').set_value('当前项目导出与发布').run()
        self.healthy(app)
        return app

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(len(app.error), 0, str(app.error))

    def element(self, collection, label):
        return next(element for element in collection if element.label == label)

    def publishing(self, app):
        next(r for r in app.radio if r.label=='导出范围').set_value('当前项目导出与发布').run()
        self.element(app.radio, '输出去向').set_value('douyin').run()
        self.healthy(app)
        return app

    def authorize(self, app):
        return next(checkbox for checkbox in app.checkbox if checkbox.label.startswith('我已核对以上'))

    def test_local_export_is_default_and_no_browser_opens_on_render(self):
        identity = self.seed()
        app = self.app()
        self.assertEqual(self.element(app.radio, '输出去向').value, 'local')
        self.assertFalse(self.element(app.button, '开始导出').disabled)
        self.assertFalse(any(c.label.startswith('我已核对以上') for c in app.checkbox))
        with patch('ui.workbench.submit_job', return_value={'id': 'local-export'}) as queue:
            self.element(app.button, '开始导出').click().run()
        self.healthy(app)
        snapshot = queue.call_args.args[1]['snapshot']
        self.assertNotIn('publication', snapshot)
        self.assertEqual([item['id'] for item in snapshot['items']], ['current'])
        self.client.connect_account.assert_not_called()
        self.client.refresh_account.assert_not_called()
        self.client.jobs.assert_called_with(workspace_id=identity)

    def test_bilibili_export_uses_its_own_title_account_and_required_metadata(self):
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);w['variants'][0]['items'][0]['platform_titles']={'douyin':'抖音专用标题','bilibili':'B站球队讨论与具体观点'};store.save_workspace(w)
        self.account.update(id='bilibili:gpu',platform='bilibili',creator_id='123456')
        self.client.health.return_value={'caps':{'platforms':['douyin','bilibili']}}
        app=self.app();self.element(app.radio,'输出去向').set_value('bilibili').run();self.healthy(app)
        self.element(app.text_input,'转载来源（转载必填）').input('https://example.invalid/original').run()
        self.element(app.text_input,'话题（以空格分隔）').input('足球 阿森纳').run();self.healthy(app)
        preview=next(frame.value for frame in app.dataframe if '发布文案' in frame.value.columns)
        self.assertEqual(list(preview['标题']),['B站球队讨论与具体观点'])
        self.authorize(app).check().run()
        with patch('ui.workbench.submit_job',return_value={'id':'bili-export'}) as queue:
            self.element(app.button,'导出并准备发布到B站').click().run()
        self.healthy(app);publication=queue.call_args.args[1]['snapshot']['publication']
        self.assertEqual(publication['platform'],'bilibili');self.assertEqual(publication['account_id'],'bilibili:gpu')
        self.assertEqual(publication['category_path'],['运动','足球']);self.assertEqual(publication['copyright'],2)

    def test_prepared_export_uses_one_selected_orientation_and_exact_approved_batch(self):
        self.seed()
        app = self.app()
        self.element(app.multiselect, '输出画幅').set_value(['horizontal', 'vertical']).run()
        self.publishing(app)
        mode = self.element(app.radio, '提交方式')
        self.assertEqual(mode.value, 'prepare')
        self.assertEqual(len(mode.options), 1)
        self.element(app.selectbox, '发布画幅（每条只发布一种）').set_value('vertical').run()
        self.element(app.text_input, '话题（以空格分隔）').input('#足球 #比赛集锦 #足球').run()
        preview = next(frame.value for frame in app.dataframe if '发布文案' in frame.value.columns)
        self.assertEqual(list(preview['标题']), ['人工确认的标题'])
        self.assertEqual(preview.iloc[0]['发布文案'], '人工保存的发布文案\n#足球 #比赛集锦')
        self.assertTrue(self.element(app.button, '导出并准备发布到抖音').disabled)
        self.authorize(app).check().run()
        self.assertFalse(self.element(app.button, '导出并准备发布到抖音').disabled)
        with patch('ui.workbench.submit_job', return_value={'id': 'published-export'}) as queue:
            self.element(app.button, '导出并准备发布到抖音').click().run()
        self.healthy(app)
        snapshot = queue.call_args.args[1]['snapshot']
        self.assertEqual(snapshot['publication'], dict(platform='douyin', account_id='douyin:gpu',
            mode='prepare', orientation='vertical', hashtags=['足球', '比赛集锦']))
        self.assertEqual([item['id'] for item in snapshot['items']], ['current'])
        self.assertEqual(snapshot['items'][0]['description'], '人工保存的发布文案')
        self.assertFalse(self.authorize(app).value)
        self.assertTrue(self.element(app.button, '导出并准备发布到抖音').disabled)

    def test_verified_auto_mode_requires_fresh_authorization_after_caption_change(self):
        self.seed()
        self.account['auto_submit_verified'] = True
        app = self.publishing(self.app())
        self.element(app.radio, '提交方式').set_value('auto').run()
        self.assertIn('上传并提交发布', self.authorize(app).label)
        self.authorize(app).check().run()
        self.assertFalse(self.element(app.button, '导出并自动提交到抖音').disabled)
        self.element(app.text_input, '话题（以空格分隔）').input('#新话题').run()
        self.healthy(app)
        self.assertFalse(self.authorize(app).value)
        self.assertTrue(self.element(app.button, '导出并自动提交到抖音').disabled)

    def test_automation_confirmation_receives_publication_snapshot(self):
        self.seed(automation=True)
        app = self.publishing(self.app())
        self.authorize(app).check().run()
        with patch('core.automation_client.AutomationClient') as client, patch('ui.workbench.submit_job') as queue:
            self.element(app.button, '导出并准备发布到抖音').click().run()
        self.healthy(app)
        path, data = client.return_value.call.call_args.args
        self.assertEqual(path, 'flows/confirmed-flow/confirm')
        self.assertEqual(data['snapshot']['publication']['mode'], 'prepare')
        self.assertEqual(data['snapshot']['items'][0]['title'], '人工确认的标题')
        queue.assert_not_called()

    def test_service_missing_has_explicit_start_and_preserves_local_route(self):
        self.seed()
        self.client_class.side_effect = RuntimeError('发布服务尚未启动')
        app = self.publishing(self.app())
        self.assertTrue(any('发布服务尚未启动' in info.value for info in app.info))
        self.assertTrue(any(button.label == '启动发布服务' for button in app.button))
        self.assertTrue(self.element(app.button, '导出并准备发布到抖音').disabled)
        with patch('core.publisher_client.start_publishing_services') as start:
            self.element(app.button, '启动发布服务').click().run()
        start.assert_called_once()
        self.element(app.radio, '输出去向').set_value('local').run()
        self.healthy(app)
        self.assertFalse(self.element(app.button, '开始导出').disabled)

    def test_account_binding_and_calibration_only_follow_explicit_actions(self):
        self.seed()
        self.account.update(status='unverified', creator_id=None,
            observed_identity={'creator_id': 'observed-456', 'label': '浏览器中的账号'})
        app = self.app()
        app.sidebar.radio[0].set_value('设置').run()
        self.healthy(app)
        self.client.connect_account.assert_not_called()
        self.client.refresh_account.assert_not_called()
        self.element(app.button, '确认此账号').click().run()
        self.client.refresh_account.assert_called_once_with('douyin:gpu', expected_creator_id='observed-456')
        self.account.update(status='ready', creator_id='observed-456')
        self.client.jobs.return_value = {'jobs': [dict(id='sample', state='prepared',
            publication={'account_id': 'douyin:gpu', 'title': '已准备的样片'})]}
        app.run()
        self.assertTrue(self.element(app.button, '确认样片并启用自动提交').disabled)
        self.client.calibrate_account.assert_not_called()
        next(c for c in app.checkbox if c.label.startswith('我已在 Edge')).check().run()
        self.element(app.button, '确认样片并启用自动提交').click().run()
        self.client.calibrate_account.assert_called_once_with('douyin:gpu', 'sample')

    def test_unknown_is_distinct_from_published_and_cannot_be_retried(self):
        self.seed()
        self.client.jobs.return_value = {'jobs': [
            dict(id='uncertain', state='submission_unknown', publication={'title': '结果不确定'}),
            dict(id='review', state='submitted', publication={'title': '平台审查'}),
            dict(id='done', state='published', publication={'title': '平台已确认'}),
            dict(id='prepared', state='prepared', publication={'title': '仅准备完成'}),
        ]}
        app = self.app()
        statuses = next(frame.value for frame in app.dataframe if '状态' in frame.value.columns)
        self.assertEqual(list(statuses['状态']), ['发布结果待核实', '已提交 · 平台审核中', '已发布', '已上传 · 等待你最终发布'])
        self.assertFalse(any(button.label == '重试发布任务' for button in app.button))
        self.assertTrue(any('避免重复发布' in info.value for info in app.info))
        self.assertTrue(app.button(key='publishing-verify-uncertain').disabled)
        app.text_input(key='publishing-remote-id-uncertain').input('123456789').run()
        app.button(key='publishing-verify-uncertain').click().run()
        self.healthy(app)
        self.client.verify_job.assert_called_once_with('uncertain', '123456789')

    def test_review_caption_persists_to_frozen_publication_preview(self):
        identity = self.seed()
        app = self.app()
        self.element(app.button, '03  预览审核').click().run()
        self.element(app.text_input, '标题').input('人工最终标题')
        self.element(app.text_area, '发布文案（可选）').input('人工最终文案')
        self.element(app.button, '保存审核').click().run()
        self.element(app.button, '05  统一导出').click().run()
        self.publishing(app)
        preview = next(frame.value for frame in app.dataframe if '发布文案' in frame.value.columns)
        self.assertEqual(list(preview['标题']), ['人工最终标题'])
        self.assertEqual(list(preview['发布文案']), ['人工最终文案'])
        with WorkspaceStore(load_config()['database']) as store:
            item = store.get_workspace(identity)['variants'][0]['items'][0]
        self.assertEqual(item['description'], '人工最终文案')

    def test_custom_cover_picker_uses_image_kind_and_cancel_preserves_choice(self):
        self.seed()
        cover = self.root / '封面 图片.png'
        from PIL import Image
        Image.new('RGB', (32, 18), 'green').save(cover)
        app = self.publishing(self.app())
        with patch('ui.file_picker.start_local_picker', return_value=object()) as picker, \
             patch('ui.file_picker.poll_local_picker', side_effect=[{'status':'selected','value':str(cover)},
                                                                   {'status':'cancelled','value':None}]):
            self.element(app.button, '选择封面图片…').click().run()
            self.element(app.button, '选择封面图片…').click().run()
        self.assertEqual(picker.call_args.args[:2], ('选择本批发布封面', 'image'))
        self.authorize(app).check().run()
        with patch('ui.workbench.submit_job', return_value={'id': 'cover-export'}) as queue:
            self.element(app.button, '导出并准备发布到抖音').click().run()
        self.assertEqual(queue.call_args.args[1]['snapshot']['publication']['cover_path'], str(cover))
        self.healthy(app)

    def test_published_receipt_is_visible_and_empty_caption_matches_submission(self):
        from ui.publishing import frozen_caption
        self.assertEqual(frozen_caption({'title':'只有标题','description':''}, ['足球']), '#足球')
        self.seed()
        self.client.jobs.return_value = {'jobs': [dict(id='published-one', state='published',
            delivery={'publication':{'title':'人工作品'}}, publication={'orientation':'horizontal'},
            result={'receipt':{'remote_id':'1234567890123'}})]}
        app = self.app()
        statuses = next(frame.value for frame in app.dataframe if '状态' in frame.value.columns)
        self.assertEqual(list(statuses['作品 ID / BVID']), ['1234567890123'])
        self.assertTrue(any('查看抖音作品' in link.label for link in app.get('link_button')))


class CoverPickerTests(unittest.TestCase):
    def test_native_image_filter_and_cancel_close_dialog(self):
        from scripts.pick_video import pick_video
        # Platform-native dispatch is checked without creating an interactive window.
        with patch('scripts.pick_video._native_windows_choice', return_value={'path':None}) as native:
            with patch('scripts.pick_video.os.name', 'nt'):
                self.assertEqual(pick_video('选择封面', kind='image'), {'path': None})
        self.assertEqual(native.call_args.args, ('选择封面', None, False, False, 'image'))


if __name__ == '__main__':
    unittest.main()
