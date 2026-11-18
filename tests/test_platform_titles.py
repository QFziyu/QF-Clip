import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.titles import generate_item_titles, cached_item_titles, publication_title, validate_titles
from core.subtitle import write_subtitles


class PlatformTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cfg=load_config();self.cfg['cache_dir']=self.temp.name
        self.cfg['openai'].update(provider='api',api_key='fixture',model='fixture')
        self.items=[dict(id='one',title='精选集锦 1',segments=[dict(start=1,end=120)],revision=0)]
        self.body='嘉宾讨论阿森纳的防守。球队不怕快节奏，但也担心利物浦的主场攻势。'

    def answer(self,messages,schema,validator=None,response_key=None):
        rows=[]
        for row in json.loads(messages[1]['content']):
            rows.append(dict(id=row['id'],douyin='阿森纳真不怕快节奏？',bilibili='【足球杂谈】阿森纳如何应对利物浦主场攻势',description='嘉宾讨论阿森纳的防守与主场压力。',hashtags=['足球','阿森纳'],evidence=['嘉宾讨论阿森纳的防守']))
        value={'items':rows};validator(value);return value

    def test_both_platforms_are_cached_and_manual_titles_survive_regeneration(self):
        with patch('core.titles._body',return_value=self.body),patch('core.titles.LLMClient.complete',side_effect=self.answer) as model:
            result,warnings=generate_item_titles(self.items,self.cfg,'workspace')
            self.assertFalse(warnings);self.assertEqual(result[0]['title'],'阿森纳真不怕快节奏？')
            self.assertIn('利物浦',publication_title(result[0],'bilibili'))
            again,_=generate_item_titles(result,self.cfg,'workspace')
            self.assertEqual(model.call_count,1)
            again[0].update(title='人工作品名',title_source='manual',manual_platform_titles=['douyin'])
            again[0]['platform_titles']['douyin']='人工抖音标题'
            final,_=generate_item_titles(again,self.cfg,'workspace',force=True)
            self.assertEqual(final[0]['title'],'人工作品名')
            self.assertEqual(final[0]['platform_titles']['douyin'],'人工抖音标题')
            self.assertEqual(final[0]['title_generation']['status'],'complete')
            self.assertEqual(self.items[0]['title'],'精选集锦 1')

    def test_evidence_outside_selected_clip_is_rejected_and_old_title_retained(self):
        def bad(messages,schema,validator=None,**kwargs):
            value=self.answer(messages,schema,lambda x:None)
            value['items'][0]['evidence']=['输入中没有出现的所谓进球']
            validator(value)
        with patch('core.titles._body',return_value=self.body),patch('core.titles.LLMClient.complete',side_effect=bad):
            result,warnings=generate_item_titles(self.items,self.cfg,'other')
        self.assertTrue(warnings);self.assertEqual(result[0]['title'],'精选集锦 1')
        self.assertEqual(result[0]['title_generation']['status'],'failed')

    def test_manual_platform_title_validation_and_no_platform_leak(self):
        self.assertEqual(publication_title({'title':'文件名','platform_titles':{'bilibili':'B站专用'}},'bilibili'),'B站专用')
        self.assertEqual(publication_title({'title':'文件名','platform_titles':{'bilibili':'B站专用'}},'douyin'),'文件名')
        for value in ({'bilibili':'x'*81},{'douyin':'两行\n标题'},{'unknown':'wrong'}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_titles(value)

    def test_saved_topic_index_restores_titles_without_any_model_call(self):
        root=Path(self.temp.name);path=root/'plans/one/selection/decisions.json';path.parent.mkdir(parents=True)
        source=str(root/'source.mp4')
        path.write_text(json.dumps({'source':source,'clips':[dict(start=1,end=120,title='阿森纳如何应对利物浦主场攻势',evidence=['嘉宾讨论阿森纳的防守'],tags=['足球'])]},ensure_ascii=False),encoding='utf-8')
        item=copy.deepcopy(self.items[0]);item['source']={'project_dir':str(root),'meta':{'source':source}}
        with patch('core.titles._body',return_value=self.body),patch('core.titles.LLMClient.complete') as model:
            result,warnings=cached_item_titles([item],self.cfg)
        model.assert_not_called();self.assertFalse(warnings)
        self.assertEqual(result[0]['title_generation']['status'],'cached')
        self.assertIn('阿森纳',result[0]['platform_titles']['douyin'])
        self.assertTrue(result[0]['platform_titles']['bilibili'].startswith('【足球杂谈】'))

    def test_subtitles_are_exactly_75_percent_and_shift_by_output_width_with_safe_clamp(self):
        root=Path(self.temp.name)
        for orientation,width,height,expected in [('horizontal',1920,1080,4),('vertical',1080,1920,51),('horizontal',640,360,4)]:
            cfg=copy.deepcopy(self.cfg);cfg['render'][orientation]=[width,height]
            result=write_subtitles({},dict(start=0,end=2),root/'a.srt',root/'a.ass',cfg,orientation,edited_srt='1\n00:00:00,000 --> 00:00:01,000\n测试字幕')
            style=next(x for x in Path(result['ass']).read_text(encoding='utf-8').splitlines() if x.startswith('Style: Default,')).split(',')
            self.assertEqual(float(style[2]),25.5);self.assertEqual(int(style[-2]),expected)

