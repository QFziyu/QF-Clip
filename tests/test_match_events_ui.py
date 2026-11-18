"""Exercise the new one-field form and submitted background-job options."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class CompactEventUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fcf-clock-ui-')
        self.env = patch.dict(os.environ, {'DATABASE_PATH': str(Path(self.temp.name) / 'db.sqlite')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def app(self, initial=None, form='montage'):
        from streamlit.testing.v1 import AppTest
        code = '''
from core.config import load_config
from ui.plans_page import editorial_controls
import streamlit as st
options = editorial_controls(load_config(), 'test-', forced_format=FORM, initial_options=INITIAL)
st.session_state['result'] = options
st.button('生成方案', disabled=options is None)
'''.replace('INITIAL', repr(initial)).replace('FORM', repr(form))
        return AppTest.from_string(code, default_timeout=15).run()

    @staticmethod
    def item(items, label):
        return next(x for x in items if x.label == label)

    def test_single_field_auto_defaults_half_parsing_and_no_manual_video_inputs(self):
        app = self.app()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(self.item(app.checkbox, '自动识别记分牌并对轴').value)
        self.assertEqual(len(app.dataframe), 0)
        self.item(app.text_input, '必剪事件（一行填写）').input('G23:10 40R / G61 V90+3:20').run()
        self.assertEqual(len(app.exception), 0)
        options = app.session_state['result']
        self.assertEqual(options['clock_mode'], 'auto')
        self.assertEqual(len(options['events']), 4)
        self.assertEqual(options['events'][2]['period'], 'second')
        self.assertEqual(options['anchors'], [])
        self.assertEqual(options['event_before'], 20)
        self.assertEqual(options['event_after'], 40)
        self.assertFalse(self.item(app.button, '生成方案').disabled)

    def test_bad_token_blocks_submission_and_correction_recovers(self):
        app = self.app()
        self.item(app.text_input, '必剪事件（一行填写）').input('G23 ??? / G61').run()
        self.assertTrue(self.item(app.button, '生成方案').disabled)
        self.assertIsNone(app.session_state['result'])
        self.assertTrue(len(app.warning))
        self.item(app.text_input, '必剪事件（一行填写）').input('/ V90+3:20').run()
        self.assertFalse(self.item(app.button, '生成方案').disabled)

    def test_same_compact_events_in_all_three_formats(self):
        from core.editing_options import FORMAT_LABELS
        for form in ('shorts', 'continuous', 'montage'):
            with self.subTest(form=form):
                app = self.app(form=form)
                self.item(app.text_input, '必剪事件（一行填写）').input('G23:10 R40 / G61 V90+3:20').run()
                self.assertEqual(len(app.exception), 0)
                options = app.session_state['result']
                self.assertEqual(options['format'], form)
                self.assertEqual(len(options['events']), 4)
                self.assertEqual(options['events'][2]['period'], 'second')
                self.assertTrue(any(FORMAT_LABELS[form] in c.value for c in app.caption))

    def test_changing_format_preserves_event_input(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string('''
from core.config import load_config
from ui.plans_page import editorial_controls
import streamlit as st
st.session_state['result'] = editorial_controls(load_config(), 'switch-')
''', default_timeout=15).run()
        self.item(app.text_input, '必剪事件（一行填写）').input('G23 / G61').run()
        for form in ('continuous', 'montage', 'shorts'):
            app.selectbox(key='switch-format').set_value(form).run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state['result']['format'], form)
            self.assertEqual(len(app.session_state['result']['events']), 2)
            self.assertEqual(self.item(app.text_input, '必剪事件（一行填写）').value, 'G23 / G61')

    def test_old_manual_anchors_and_event_specific_buffers_survive(self):
        initial = {'anchors': [{'period': 'first', 'match_time': '10:00', 'video_time': 900}],
            'events': [{'period': 'first', 'match_time': '23:10', 'label': '进球后反应', 'before': 10, 'after': 90}]}
        app = self.app(initial)
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(self.item(app.checkbox, '自动识别记分牌并对轴').value)
        options = app.session_state['result']
        self.assertEqual(options['events'][0]['before'], 10)
        self.assertEqual(options['events'][0]['after'], 90)
        self.assertEqual(options['anchors'][0]['video_time'], 900)
        self.item(app.checkbox, '自动识别记分牌并对轴').check().run()
        self.assertEqual(app.session_state['result']['clock_mode'], 'auto')
        self.assertEqual(app.session_state['result']['anchors'], [])


if __name__ == '__main__':
    unittest.main()
