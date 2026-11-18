"""Scoreboard OCR, event notation and real timeline/coverage boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.editing_options import normalize_editing_options, locate_events
from core.match_events import parse_event_text, format_event_text
from core.match_clock import (clock_value, validate_readings, verified_pair, scan_match_clock,
                              locate_verified_events, read_clock_frames, auto_calibrate_options)


class EventTextTests(unittest.TestCase):
    def test_both_notations_case_halves_extra_time_and_unicode(self):
        events = parse_event_text('g23：10 45＋2G ／ 61R v90+3:20 / G100 / 119V')
        self.assertEqual([e['label'] for e in events], ['进球', '进球', '红牌', 'VAR', '进球', 'VAR'])
        self.assertEqual([e['period'] for e in events], ['first', 'first', 'second', 'second', 'extra_first', 'extra_second'])
        self.assertEqual(parse_event_text('/ G61')[0]['period'], 'second')
        self.assertEqual(parse_event_text('   '), [])

    def test_invalid_event_is_never_silently_dropped(self):
        for value in ['G23 rubbish / G61', 'X23', 'G12:99', 'G61', '/ R10', 'G35+2', 'G2////R100', 'G2G3', 'G2, G3']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_event_text(value)

    def test_existing_descriptions_round_trip(self):
        events = [{'period': 'second', 'match_time': '61:00', 'label': '红牌 / 保留完整反应'}]
        self.assertEqual(parse_event_text(format_event_text(events)), events)
        self.assertEqual(parse_event_text('GOAL23 VAR40')[1]['label'], 'VAR')


class ClockValidationTests(unittest.TestCase):
    def test_split_and_continuous_stoppage_clocks(self):
        for clock, period, extra, expected in [
                ('23:10', 'first', '', ('23:10', 1390)),
                ('47:10', 'first', '', ('45+2:10', 2830)),
                ('45:00', 'first', '02:10', ('45+2:10', 2830)),
                ('90:00', 'second', '03:20', ('90+3:20', 5600)),
                ('93:20', 'second', '', ('90+3:20', 5600)),
                ('45+2:10', 'first', '', ('45+2:10', 2830)),
                ('107:01', 'extra_first', '', ('105+2:01', 6421))]:
            with self.subTest(clock=clock):
                self.assertEqual(clock_value(clock, period, extra), expected)
        for clock, period, extra in [('45:00', 'first', '+4'), ('47:10', 'first', '02:10'),
                                     ('23', 'first', ''), ('76:00', 'first', ''),
                                     ('90:00', 'unknown', ''), ('93:99', 'second', '')]:
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                clock_value(clock, period, extra)

    def test_ids_confidence_unknown_phase_and_static_clock(self):
        frames = [{'id': 'a', 'time': 100}, {'id': 'b', 'time': 112}]
        rows = [{'id': name, 'clock': clock, 'stoppage_clock': '', 'period': 'first', 'live': True, 'confidence': .95}
                for name, clock in [('a', '01:30'), ('b', '01:42')]]
        value = {'readings': rows}
        a, b = validate_readings(value, frames)
        self.assertEqual(verified_pair(a, b), 10)
        self.assertIsNone(verified_pair(a, {**b, 'match_seconds': a['match_seconds']}))
        self.assertIsNone(verified_pair(a, {**b, 'period': 'second'}))
        self.assertFalse(validate_readings({'readings': [rows[0], {**rows[1], 'confidence': .7}]}, frames)[1]['usable'])
        self.assertFalse(validate_readings({'readings': [rows[0], {**rows[1], 'period': 'unknown'}]}, frames)[1]['usable'])
        for invalid in [{'readings': [rows[0]]}, {'readings': [rows[0], rows[0]]},
                        {'readings': [rows[0], {**rows[1], 'id': 'invented'}]},
                        {'readings': [rows[0], {**rows[1], 'confidence': float('nan')}]}]:
            with self.assertRaises(ValueError):
                validate_readings(invalid, frames)


class ClockScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fcf-clock-test-')
        self.root = Path(self.temp.name)
        source = self.root / '比赛.mkv'; source.touch()
        self.meta = {'source': str(source), 'duration': 7200., 'timeline_origin': 50., 'format_start': 48., 'video_stream': 1}
        self.cfg = {'openai': {'api_key': 'fixture', 'model': 'fixture-model'}}

    def tearDown(self):
        self.temp.cleanup()

    def reader(self, frames, cfg, root, ledger):
        rows = []
        for f in frames:
            t = f['time']; phase = 'unknown'; clock = ''
            if 120 <= t < 3060:
                phase, seconds = 'first', int(t - 120)
                clock = f'{seconds // 60}:{seconds % 60:02d}'
            elif t >= 3900:
                phase, seconds = 'second', int(t - 3900 + 2700)
                clock = f'{seconds // 60}:{seconds % 60:02d}'
            rows.append({'id': f['id'], 'clock': clock, 'stoppage_clock': '', 'period': phase,
                         'live': bool(clock), 'confidence': .99})
        return validate_readings({'readings': rows}, frames)

    @staticmethod
    def frame(meta, t, root, cfg):
        return {'id': str(round(t, 3)), 'time': round(t, 3), 'path': 'not-sent.jpg'}

    def scan(self, events, meta=None, reader=None):
        with patch('core.match_clock._frame', side_effect=self.frame), patch('core.match_clock.read_clock_frames', side_effect=reader or self.reader):
            return scan_match_clock(meta or self.meta, self.cfg, self.root, events)

    def test_halftime_stoppage_and_buffers_are_automatically_located(self):
        options = normalize_editing_options({'format': 'montage', 'count': 1,
            'events': parse_event_text('G12 G45+2 / R61 V90+2')})
        report = self.scan(options['events'])
        self.assertEqual(report['warnings'], [])
        options['anchors'] = report['anchors']
        events = locate_verified_events(options, self.meta['duration'], report)
        self.assertEqual([e['start'] for e in events], [820, 2920, 4840, 6700])
        self.assertTrue(all(e['status'] == 'located' and not e['confirmed'] for e in events))
        self.assertTrue(all(abs(e['end'] - e['start'] - 60) < .01 for e in events))

    def test_frozen_clock_cannot_establish_calibration(self):
        def paused(frames, cfg, root, ledger):
            value = {'readings': [{'id': f['id'], 'clock': '23:10', 'stoppage_clock': '', 'period': 'first',
                                   'live': True, 'confidence': .99} for f in frames]}
            return validate_readings(value, frames)
        report = self.scan(parse_event_text('G23'), reader=paused)
        self.assertEqual(report['anchors'], [])
        self.assertTrue(report['warnings'])

    def test_unverified_nearby_event_cannot_borrow_a_neighbor_anchor(self):
        options = normalize_editing_options({'events': parse_event_text('G12 R12:10'),
            'anchors': [{'period': 'first', 'match_time': '12:00', 'video_time': 840, 'video_start': 800, 'video_end': 900}]})
        events = locate_verified_events(options, 7200, {'verified_events': [('first', '12')]})
        self.assertIsNotNone(events[0]['start'])
        self.assertIsNone(events[1]['start'])

    def test_repeated_footage_keeps_multiple_positions_ambiguous(self):
        def repeated(frames, cfg, root, ledger):
            rows = []
            for f in frames:
                seconds = int(f['time'] % 600)
                rows.append({'id': f['id'], 'clock': f'{seconds // 60}:{seconds % 60:02d}',
                             'stoppage_clock': '', 'period': 'first', 'live': True, 'confidence': .99})
            return validate_readings({'readings': rows}, frames)
        options = normalize_editing_options({'events': parse_event_text('G3')})
        report = self.scan(options['events'], {**self.meta, 'duration': 1300.}, reader=repeated)
        options['anchors'] = report['anchors']
        events = locate_verified_events(options, 1300, report)
        self.assertEqual(events[0]['status'], 'ambiguous')
        self.assertIsNone(events[0]['start'])

    def test_virtual_batch_shifts_recording_positions_once(self):
        part = {**self.meta, 'sequence_start': 500, 'sequence_end': 7700}
        meta = {'virtual_timeline': True, 'parts': [part]}
        report = self.scan(parse_event_text('G12 / R61'), meta)
        options = normalize_editing_options({'events': parse_event_text('G12 / R61'), 'anchors': report['anchors']})
        self.assertEqual([e['start'] for e in locate_verified_events(options, 7700, report)], [1320, 5340])
        self.assertEqual(report['warnings'], [])

    def test_spliced_source_does_not_fill_a_missing_match_interval(self):
        def spliced(frames, cfg, root, ledger):
            rows = []
            for f in frames:
                t = f['time']
                seconds = int(t - 120 if t < 1800 else t + 600)
                rows.append({'id': f['id'], 'clock': f'{seconds // 60}:{seconds % 60:02d}' if seconds >= 0 else '',
                             'stoppage_clock': '', 'period': 'first', 'live': seconds >= 0, 'confidence': .99})
            return validate_readings({'readings': rows}, frames)
        options = normalize_editing_options({'events': parse_event_text('G12 R35 V42')})
        report = self.scan(options['events'], {**self.meta, 'duration': 2200.}, reader=spliced)
        options['anchors'] = report['anchors']
        events = locate_verified_events(options, 2200, report)
        self.assertAlmostEqual(events[0]['start'], 820, delta=1.)
        self.assertIsNone(events[1]['start'])
        self.assertAlmostEqual(events[2]['start'], 1900, delta=1.)
        self.assertEqual(len(report['warnings']), 1)

    def test_selected_profile_and_cached_response_are_used(self):
        image = self.root / 'frame.jpg'; image.write_bytes(b'fixture-not-a-real-image')
        frames = [{'id': 'a', 'time': 100., 'path': str(image)}]
        response = {'readings': [{'id': 'a', 'clock': '23:10', 'stoppage_clock': '', 'period': 'first', 'live': True, 'confidence': .99}]}
        cfg = {'openai': {'provider': 'codex_cli'}, '_llm_api_profile': {'api_key': 'secret', 'model': 'vision', 'provider': 'api'}}
        with patch('core.match_clock.LLMClient') as client:
            client.return_value.complete.return_value = response
            first = read_clock_frames(frames, cfg, self.root, self.root / 'ledger.json')
            second = read_clock_frames(frames, cfg, self.root, self.root / 'ledger.json')
            self.assertEqual(first, second)
            self.assertEqual(client.call_count, 1)
            self.assertEqual(client.call_args.args[0]['provider'], 'codex_cli')
        cache_text = ''.join(p.read_text() for p in self.root.glob('readings_*.json'))
        self.assertNotIn('secret', cache_text)

    def test_real_plan_creation_applies_calibration_before_required_coverage(self):
        from core.config import DEFAULTS
        from core.editing import create_editing_plan
        from core.utils import atomic_json, fingerprint
        from storage.plans import PlanStore
        cfg = copy.deepcopy(DEFAULTS)
        cfg.update(database=str(self.root / 'db.sqlite'), prompt_path=str(Path(__file__).resolve().parents[1] / 'prompts/clip_selection.md'))
        project_root = self.root / 'project'
        transcript = {'segments': [{'start': 10., 'end': 40., 'text': '足球比赛解说'}]}
        key = fingerprint(transcript)
        atomic_json(project_root / 'transcripts' / (key + '.json'), transcript)
        with PlanStore(cfg['database']) as db:
            db.upsert_project({'id': 'p', 'name': 'fixture', 'source': self.meta['source'], 'duration': 7200.,
                               'project_dir': str(project_root), 'status': 'transcribed',
                               'metadata': {**self.meta, 'transcript_key': key}})
        options = normalize_editing_options({'clock_mode': 'auto', 'format': 'montage', 'count': 1,
            'min_seconds': 1, 'target_seconds': 120, 'max_seconds': 1000, 'events': parse_event_text('G12 / R61')})
        report = self.scan(options['events'])
        with patch('core.match_clock.scan_match_clock', return_value=report) as scanner, patch('core.clip_selector.select_clips', return_value=[]):
            result = create_editing_plan('p', cfg, options)
            self.assertEqual(scanner.call_count, 1)
            self.assertEqual(scanner.call_args.args[-1], project_root / 'selection/api_budget.json')
        with PlanStore(cfg['database']) as db:
            plan = db.get_plan(result['plan_id'])
        self.assertEqual([e['start'] for e in plan['events']], [820, 4840])
        self.assertEqual(plan['options']['anchors'], report['anchors'])
        self.assertEqual(plan['clock_report']['verified_events'], [['first', '12'], ['second', '61']])
        self.assertTrue(all(any(s['start'] <= e['start'] and s['end'] >= e['end'] for s in plan['drafts'][0]['segments']) for e in plan['events']))


if __name__ == '__main__':
    unittest.main()
