"""Approval text freezing, immutable packages, and crash-safe delivery handoff."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.deliveries import (dispatch_delivery_outbox, list_pending_deliveries,
                             verify_delivery, verify_render_package, file_record)
from core.production import FactoryService
from core.subtitle_review import proofread_cues
from core.utils import read_json
from core.workflow import (_item, active_variant, edit_item, export_snapshot, run_workflow_export,
                           validate_snapshot)
from core.workflow_renderer import render_workflow_item
from storage.workspaces import WorkspaceStore


SRT = '1\n00:00:00,000 --> 00:00:01,000\n人工最终字幕\n'


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='交付-')
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(database=str(self.root/'factory.sqlite3'), cache_dir=str(self.root/'cache'),
                        output_dir=str(self.root/'out'))
        self.cfg['render'].update(horizontal=[320, 180], vertical=[180, 320], fps=10)
        self.source = self.root/'原片.mp4'
        self.source.write_bytes(b'unchanged source fixture')
        self.item = _item('i1', '人工保留标题', [{'start': 0., 'end': 2.}],
            {'id': 'p', 'source': str(self.source), 'duration': 2., 'metadata': {}, 'project_dir': str(self.root/'project')},
            SRT, 'approved')
        self.item['description'] = '人工保留发布说明'
        self.intent = dict(platform='douyin', account_id='local-account', mode='auto', orientation='horizontal', hashtags=['足球'])
        with WorkspaceStore(self.cfg['database']) as db:
            w = db.create_workspace('交付项目', 'w')
            w.update(variants=[{'id': 'v', 'items': [self.item], 'events': [], 'options': {}, 'form': 'full'}],
                     active_variant='v', packaging={'subtitles': True})
            db.save_workspace(w)
        # Encoding and FFprobe are isolated here. These tests exercise actual
        # package writes, full byte hashes, SQLite transactions, and the outbox.
        def cut(meta, segment, destination, cfg, **kwargs):
            Path(destination).write_bytes(b'encoded selected frames')
        def compose(parts, destination, cfg, subtitles=None):
            Path(destination).write_bytes(b'finished video bytes with selected frames')
            return {'duration': 2.}
        def probe(path, cfg, **kwargs):
            vertical = 'vertical_' in str(path)
            return {'duration': 2., 'width': 180 if vertical else 320, 'height': 320 if vertical else 180}
        self.patches = [patch('core.workflow_renderer.render_clip', side_effect=cut),
            patch('core.workflow_renderer.compose_parts', side_effect=compose),
            patch('core.workflow_renderer.probe_intro', return_value={'duration': 2.}),
            patch('core.workflow_renderer.restore_inherited_access'),
            patch('core.video_input.probe_video', side_effect=probe),
            patch('core.jobs.ensure_worker', return_value=False), patch('core.production.ensure_worker', return_value=False)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def workspace(self):
        with WorkspaceStore(self.cfg['database']) as db:
            return db.get_workspace('w')

    def snapshot(self, orientations=None, publication=True):
        return export_snapshot(self.workspace(), self.cfg, orientations or ['horizontal'], {}, self.cfg['output_dir'],
                               publication=self.intent if publication else None)

    def export(self, snapshot=None):
        return run_workflow_export('w', snapshot or self.snapshot(), self.cfg)

    def test_completed_package_registers_once_and_preserves_manual_publication(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot['frozen_subtitles']['i1']['text'], SRT)
        self.assertEqual(snapshot['items'][0], self.item)
        first, second = self.export(snapshot), self.export(snapshot)
        self.assertEqual(first['errors'], [])
        self.assertEqual(first['deliveries'], second['deliveries'])
        delivery = first['deliveries'][0]
        self.assertEqual(len(delivery['media']['sha256']), 64)
        self.assertEqual(delivery['publication'], {'title': self.item['title'], 'description': self.item['description']})
        with WorkspaceStore(self.cfg['database']) as db:
            self.assertEqual(len(db.list_deliveries()['deliveries']), 1)
            self.assertEqual(len(db.pending_deliveries()), 1)
            self.assertEqual(len(db.list_media('w', False)), 1)

    def test_bilibili_delivery_freezes_the_bilibili_title_and_leaves_local_title(self):
        with WorkspaceStore(self.cfg['database']) as db:
            w=db.get_workspace('w');w['variants'][0]['items'][0]['platform_titles']={'douyin':'抖音标题','bilibili':'B站详细议题标题'};db.save_workspace(w)
        self.intent.update(platform='bilibili',account_id='bilibili:gpu',copyright=2,source='https://example.invalid/source',category_path=['运动','足球'])
        snapshot=self.snapshot();result=self.export(snapshot)
        self.assertFalse(result['errors'])
        self.assertEqual(result['deliveries'][0]['publication']['title'],'B站详细议题标题')
        self.assertEqual(result['exports'][0]['title'],'人工保留标题')

    def test_full_file_replacement_and_sidecar_tampering_are_detected(self):
        result = self.export()
        delivery = result['deliveries'][0]
        video = Path(delivery['media']['path'])
        original = video.read_bytes()
        video.write_bytes(b'X' * len(original))
        with self.assertRaisesRegex(ValueError, '不符|替换|校验'):
            verify_delivery(delivery, self.cfg)
        # A failed cache verification must never overwrite the old package.
        self.assertTrue(self.export()['errors'])
        self.assertEqual(video.read_bytes(), b'X' * len(original))
        video.write_bytes(original)
        manifest = read_json(result['exports'][0]['verification_path'])
        publication = next(f for f in manifest['files'] if Path(f['path']).name == 'publication.txt')
        Path(publication['path']).write_text('changed title', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '替换|修改'):
            verify_render_package(result['exports'][0], self.cfg)

    def test_incomplete_proofreading_blocks_before_queue_without_api_calls(self):
        with WorkspaceStore(self.cfg['database']) as db:
            w = db.get_workspace('w')
            item = active_variant(w)['items'][0]
            item.update(subtitle_text=None, transcript_key='t')
            w['packaging']['proofread_subtitles'] = True
            db.save_workspace(w)
        self.cfg['openai'].update(model='test-model', api_key='never-used')
        transcript = {'segments': [{'start': 0., 'end': 1., 'text': '格一出球'}]}
        with patch('core.workflow.item_transcript', return_value=transcript), \
             patch('core.subtitle_review.LLMClient', side_effect=AssertionError('paid API forbidden')):
            with self.assertRaisesRegex(ValueError, '第四步'):
                self.snapshot()
        self.assertFalse((self.root/'project'/'subtitle_review').exists())

    def test_automatic_text_is_frozen_and_final_render_never_looks_up_llm(self):
        transcript = {'segments': [{'start': 0., 'end': 1., 'text': '格一出球'}]}
        with WorkspaceStore(self.cfg['database']) as db:
            w = db.get_workspace('w')
            active_variant(w)['items'][0].update(subtitle_text=None, transcript_key='t')
            w['packaging']['proofread_subtitles'] = True
            db.save_workspace(w)
        with patch('core.workflow.item_transcript', return_value=transcript), \
             patch('core.subtitle_review.item_cues', return_value=([{'start': 0., 'end': 1., 'text': '格伊出球'}], {'status': 'complete'})) as lookup:
            snapshot = self.snapshot()
        self.assertFalse(lookup.call_args.kwargs['request'])
        with patch('core.subtitle_review.item_cues', side_effect=AssertionError('frozen text required')), \
             patch('core.workflow.item_transcript', side_effect=AssertionError('frozen original text required')), \
             patch('core.subtitle_review.LLMClient', side_effect=AssertionError('paid API forbidden')):
            result = self.export(snapshot)
        self.assertEqual(result['errors'], [])
        self.assertIn('格伊出球', Path(result['exports'][0]['subtitles']['srt']).read_text(encoding='utf-8'))
        broken = copy.deepcopy(snapshot)
        broken['frozen_subtitles']['i1']['text'] += 'tampered'
        with self.assertRaisesRegex(ValueError, '校验'):
            validate_snapshot(broken)

    def test_read_only_corrupt_proofreading_cache_is_never_quarantined(self):
        self.cfg['openai'].update(model='test-model', api_key='never-used')
        self.cfg['subtitle_review']['enabled'] = True
        cues = [{'start': 0., 'end': 1., 'text': '原始字幕'}]
        _, report = proofread_cues(cues, self.cfg, self.root/'project')
        path = Path(report['cache_dir'])/'chunk_0000.json'
        path.parent.mkdir(parents=True)
        path.write_text('{bad json', encoding='utf-8')
        with patch('core.subtitle_review.LLMClient', side_effect=AssertionError('read only')):
            _, report = proofread_cues(cues, self.cfg, self.root/'project')
        self.assertEqual(report['status'], 'pending')
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(path.read_text(encoding='utf-8'), '{bad json')

    def test_cover_is_frozen_at_approval_and_same_path_replacement_blocks_export(self):
        cover = self.root/'人工选择的封面.png'
        cover.write_bytes(b'approved-cover-bytes')
        self.intent['cover_path'] = str(cover.resolve())
        snapshot = self.snapshot()
        proof = snapshot['publication']['cover_proof']
        self.assertEqual(proof, file_record(cover))
        self.assertNotIn('cover_proof', self.intent)
        cover.write_bytes(b'X' * proof['bytes'])
        with patch('core.workflow_renderer.render_workflow_item', side_effect=AssertionError('must fail before rendering')):
            with self.assertRaisesRegex(ValueError, '封面已变化'):
                self.export(snapshot)
        # A caller replaying the original frozen intention cannot refresh its bytes.
        with self.assertRaisesRegex(ValueError, '封面已变化'):
            export_snapshot(self.workspace(), self.cfg, ['horizontal'], {}, self.cfg['output_dir'],
                            publication=snapshot['publication'])

    def test_cover_changed_after_export_cannot_cross_publisher_boundary(self):
        cover = self.root/'封面.png'
        cover.write_bytes(b'approved image')
        self.intent['cover_path'] = str(cover.resolve())
        snapshot = self.snapshot()
        result = self.export(snapshot)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(list_pending_deliveries(self.cfg)), 1)
        cover.write_bytes(b'X' * cover.stat().st_size)
        calls = []
        dispatched = dispatch_delivery_outbox(self.cfg, lambda *args: calls.append(args) or {'id':'unexpected'})
        self.assertEqual(calls, [])
        self.assertEqual(dispatched['sent'], [])
        self.assertEqual(list_pending_deliveries(self.cfg), [])
        with self.assertRaisesRegex(ValueError, '封面已变化'):
            validate_snapshot(snapshot)

    def test_cover_proof_shape_and_digest_must_match_approved_image(self):
        cover = self.root/'封面.png'
        cover.write_bytes(b'approved image')
        self.intent['cover_path'] = str(cover.resolve())
        snapshot = self.snapshot()
        for change in ({'bytes':True}, {'sha256':'0'*64}, {'path':str(self.root/'other.png')}):
            broken = copy.deepcopy(snapshot)
            broken['publication']['cover_proof'].update(change)
            with self.assertRaisesRegex(ValueError, '封面'):
                validate_snapshot(broken)
        missing = copy.deepcopy(snapshot)
        missing['publication'].pop('cover_proof')
        with self.assertRaisesRegex(ValueError, '封面'):
            validate_snapshot(missing)

    def test_historical_output_during_render_and_later_edits_never_dispatch(self):
        actual = render_workflow_item
        def changed(*args, **kwargs):
            result = actual(*args, **kwargs)
            edit_item('w', 'i1', {'title': '并发修订'}, self.cfg)
            return result
        with patch('core.workflow_renderer.render_workflow_item', side_effect=changed):
            result = self.export()
        self.assertTrue(result['exports'][0]['historical'])
        self.assertEqual(result['deliveries'], [])
        self.assertEqual(list_pending_deliveries(self.cfg), [])
        result = self.export()
        self.assertEqual(len(result['deliveries']), 1)
        edit_item('w', 'i1', {'title': '导出之后修订'}, self.cfg)
        calls = []
        dispatch_delivery_outbox(self.cfg, lambda *args: calls.append(args) or {'id': 'job'})
        self.assertEqual(calls, [])
        self.assertEqual(list_pending_deliveries(self.cfg), [])

    def test_partial_batch_has_no_delivery_then_retry_recovers_without_duplicates(self):
        snapshot = self.snapshot(['horizontal', 'vertical'])
        actual = render_workflow_item
        def partial(*args, **kwargs):
            if args[3] == 'vertical':
                raise RuntimeError('simulated encoding failure')
            return actual(*args, **kwargs)
        with patch('core.workflow_renderer.render_workflow_item', side_effect=partial):
            partial_result = self.export(snapshot)
        self.assertEqual(len(partial_result['exports']), 1)
        self.assertEqual(partial_result['deliveries'], [])
        self.assertEqual(list_pending_deliveries(self.cfg), [])
        recovered = self.export(snapshot)
        self.assertEqual(recovered['errors'], [])
        self.assertEqual(len(recovered['deliveries']), 2)
        self.assertEqual(len(list_pending_deliveries(self.cfg)), 1)
        with WorkspaceStore(self.cfg['database']) as db:
            self.assertEqual(len(db.list_media('w', False)), 2)

    def test_outbox_restart_lost_response_and_ack_use_one_stable_delivery_id(self):
        self.export()
        ids = []
        def disconnected(delivery, publication):
            ids.append(delivery['delivery_id'])
            raise ConnectionError('accepted remotely but response lost')
        self.assertEqual(len(dispatch_delivery_outbox(self.cfg, disconnected)['errors']), 1)
        pending = list_pending_deliveries(self.cfg)
        self.assertEqual(pending[0]['attempts'], 1)
        def recovered(delivery, publication):
            ids.append(delivery['delivery_id'])
            self.assertEqual(publication, self.intent)
            return {'id': 'same-idempotent-job'}
        self.assertEqual(len(dispatch_delivery_outbox(self.cfg, recovered)['sent']), 1)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(list_pending_deliveries(self.cfg), [])
        self.export()
        self.assertEqual(list_pending_deliveries(self.cfg), [])

    def test_plain_exports_and_legacy_history_have_no_publication_authorization(self):
        with WorkspaceStore(self.cfg['database']) as db:
            db.record_workflow_media('w', 'i1', {}, {'video': 'old.mp4', 'legacy_outputs': ['old.mp4']})
        snapshot = self.snapshot(publication=False)
        legacy = copy.deepcopy(snapshot)
        legacy.pop('frozen_subtitles')
        validate_snapshot(legacy)
        result = self.export(snapshot)
        self.assertEqual(len(result['deliveries']), 1)
        self.assertEqual(list_pending_deliveries(self.cfg), [])

    def test_delivery_api_cursor_profile_and_authorized_pending_wrappers(self):
        result = self.export()
        service = FactoryService(self.cfg, profile='gpu')
        page = service.dispatch('GET', '/api/v1/deliveries', {'cursor': '0', 'profile': 'gpu'})
        self.assertEqual(page['deliveries'], result['deliveries'])
        self.assertEqual(service.dispatch('GET', '/api/v1/deliveries', {'cursor': page['next_cursor']})['deliveries'], [])
        pending = service.dispatch('GET', '/api/v1/deliveries', {'pending': 'true'})
        self.assertEqual(pending['deliveries'][0]['publication'], self.intent)
        self.assertEqual(service.dispatch('GET', '/api/v1/deliveries/'+result['deliveries'][0]['delivery_id'], {}), result['deliveries'][0])
        with self.assertRaisesRegex(Exception, '配置不匹配'):
            service.dispatch('GET', '/api/v1/deliveries', {'profile': 'cpu'})


if __name__ == '__main__':
    unittest.main()
