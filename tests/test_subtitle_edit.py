import copy
import unittest

from core.subtitle import cues_srt, parse_srt, transcript_cues
from core.subtitle_edit import replace_srt, clean_srt


class SubtitleEditTests(unittest.TestCase):
    def setUp(self):
        self.cues=[{'start':1.,'end':3.,'text':'洛桑传给洛桑'},
                   {'start':4.,'end':6.,'text':'曼城2比1，VAR确认'}]
        self.text=cues_srt(self.cues)

    def test_literal_replace_preserves_timeline_and_other_cues(self):
        result,count,cues=replace_srt(self.text,10,'洛桑','胡桑诺夫')
        self.assertEqual((count,cues),(2,1))
        changed=parse_srt(result,10)
        self.assertEqual(changed[0],{**self.cues[0],'text':'胡桑诺夫传给胡桑诺夫'})
        self.assertEqual(changed[1],self.cues[1])
        self.assertEqual(replace_srt(self.text,10,'00:00:01','bad'),(self.text,0,0))
        self.assertEqual(replace_srt(self.text,10,'.*','bad'),(self.text,0,0))

    def test_deletion_drops_empty_cue_and_errors_do_not_touch_text(self):
        result,_,_=replace_srt(self.text,10,'洛桑传给洛桑','')
        self.assertEqual(parse_srt(result,10),[self.cues[1]])
        for find,replacement in [('', 'x'),(' ', 'x'),('洛桑','坏\n\n字幕')]:
            with self.assertRaises(ValueError):replace_srt(self.text,10,find,replacement)
        with self.assertRaises(ValueError):replace_srt('broken SRT',10,'x','y')

    def test_cleanup_markers_split_across_cues_retains_scores_and_english(self):
        cues=[{'start':0.,'end':1.,'text':'unk> <un'},
              {'start':1.,'end':2.,'text':'k> 胡桑诺夫<|zh|>'},self.cues[1]]
        result,count=clean_srt(cues_srt(cues),10)
        cleaned=parse_srt(result,10)
        self.assertGreater(count,0)
        self.assertEqual(cleaned,[{**cues[1],'text':'胡桑诺夫'},self.cues[1]])
        self.assertEqual(clean_srt(result,10),(result,0))
        self.assertEqual(clean_srt(self.text,10),(self.text,0))

    def test_chinese_only_is_explicit(self):
        result,_=clean_srt(self.text,10,chinese_only=True)
        self.assertEqual(parse_srt(result,10)[1]['text'],'曼城比，确认')

    def test_auto_cleanup_precedes_wrapping_and_cut_preserves_raw_and_onsets(self):
        text='<unk> 胡桑诺夫传球 VAR 2比1'
        words=[{'word':c,'start':i*.2,'end':(i+1)*.2} for i,c in enumerate(text)]
        # A marker straddles two ASR segments, then the selected cut starts
        # inside the marker. Neither fragment may leak into the final captions.
        transcript={'segments':[{'start':0.,'end':.6,'text':text[:3],'words':words[:3]},
                                {'start':.6,'end':len(text)*.2,'text':text[3:],'words':words[3:]}]}
        original=copy.deepcopy(transcript)
        cues=transcript_cues(transcript,{'start':.4,'end':len(text)*.2},max_chars=6)
        body=''.join(c['text'].replace('\n','') for c in cues)
        self.assertNotIn('unk',body);self.assertNotIn('<',body);self.assertNotIn('>',body)
        self.assertIn('胡桑诺夫',body);self.assertIn('VAR',body);self.assertIn('2比1',body)
        self.assertAlmostEqual(cues[0]['start'],.8)
        self.assertEqual(transcript,original)
        marker_only={'segments':[{'start':0,'end':2,'text':'<unk><|zh|>'}]}
        self.assertEqual(transcript_cues(marker_only,{'start':0,'end':2}),[])


class SubtitleToolsUITests(unittest.TestCase):
    def test_replace_cleanup_undo_and_manual_edit_protection(self):
        from streamlit.testing.v1 import AppTest
        source="""
import streamlit as st
from ui.subtitle_tools import subtitle_tools
st.session_state.setdefault('captions','1\\n00:00:01,000 --> 00:00:03,000\\n<unk> 洛桑传给洛桑 2比1\\n')
subtitle_tools('captions',10)
st.text_area('字幕',key='captions')
"""
        app=AppTest.from_string(source).run()
        def button(label):return next(b for b in app.button if b.label==label)
        original=app.text_area[0].value
        button('查找与替换').click().run()
        next(t for t in app.text_input if t.label=='查找文字').input('洛桑')
        next(t for t in app.text_input if t.label=='替换为').input('胡桑诺夫').run()
        self.assertTrue(any('2 处匹配' in c.value for c in app.caption))
        button('全部替换').click().run()
        self.assertIn('胡桑诺夫传给胡桑诺夫',app.text_area[0].value)
        button('撤销上次批量操作').click().run()
        self.assertEqual(app.text_area[0].value,original)
        button('清理识别标记').click().run()
        self.assertNotIn('<unk>',app.text_area[0].value)
        self.assertIn('2比1',app.text_area[0].value)
        app.text_area[0].input(app.text_area[0].value.replace('传给','传球给')).run()
        self.assertTrue(button('撤销上次批量操作').disabled)
        self.assertEqual(len(app.exception),0)


if __name__=='__main__':unittest.main()
