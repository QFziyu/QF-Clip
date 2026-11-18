"""SRT cut boundaries must not duplicate a caption as a zero millisecond cue."""
import unittest
from core.subtitle import cues_srt,parse_srt
from core.workflow_renderer import remap_manual

class SubtitleBoundaryTests(unittest.TestCase):
    def test_real_source_cut_boundaries_do_not_create_zero_duration_fragments(self):
        cases=[[(283.18,338.2),(852.81,899.983)],[(679.26,733.8),(832.38,887.98)],
               [(156.99,209.07),(336.02,348.8)],[(303.76,333.62),(348.8,393.1)]]
        for case in cases:
            with self.subTest(case=case):
                segments=[dict(start=a,end=b) for a,b in case]
                lengths=[b-a for a,b in case];cut=round(lengths[0],3)
                text=cues_srt([dict(start=cut-1,end=cut,text='Ending sentence'),dict(start=cut,end=cut+1,text='Next sentence')])
                cues=parse_srt(remap_manual(text,segments,lengths),sum(lengths))
                self.assertEqual([c['text'] for c in cues],['Ending sentence','Next sentence'])
                self.assertTrue(all(c['end']>c['start'] for c in cues))

    def test_measured_frame_duration_rebases_without_a_ghost_caption(self):
        segments=[dict(start=283.18,end=338.2),dict(start=852.81,end=899.983)]
        lengths=[55.066667,47.2];cut=55.02
        text=cues_srt([dict(start=54,end=cut,text='Ending'),dict(start=cut,end=56,text='Next')])
        cues=parse_srt(remap_manual(text,segments,lengths),sum(lengths))
        self.assertEqual(len(cues),2)
        self.assertAlmostEqual(cues[1]['start'],55.067,places=3)
        self.assertEqual(cues[1]['text'],'Next')

    def test_a_valid_manual_caption_crossing_a_cut_keeps_its_text_on_both_parts(self):
        segments=[dict(start=0,end=2),dict(start=10,end=12)]
        text=cues_srt([dict(start=1.5,end=2.5,text='Human wording')])
        cues=parse_srt(remap_manual(text,segments,[2.04,2.04]),4.08)
        self.assertEqual([c['text'] for c in cues],['Human wording','Human wording'])
        self.assertEqual([(c['start'],c['end']) for c in cues],[(1.5,2.),(2.04,2.54)])
