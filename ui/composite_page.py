"""Local dual-feed composition, durable settings and background jobs."""
from pathlib import Path

import streamlit as st

from core.composite_options import normalize_composite_options, overlap_range
from core.compositor import create_composite
from core.jobs import submit_job
from storage.composites import CompositeStore
from core.local_file_picker import choose_video, choose_videos, resolve_video_path


def _add_sequence(from_path=False):
    try:
        selected = [resolve_video_path(st.session_state.get('composite_commentator_path', ''))] if from_path else choose_videos()
        current = list(st.session_state.get('composite_sequence_paths', []))
        for path in selected:
            if path not in current:
                current.append(path)
        st.session_state['composite_sequence_paths'] = current
        st.session_state.pop('composite_picker_error', None)
    except (OSError, ValueError, RuntimeError) as exc:
        st.session_state['composite_picker_error'] = str(exc)


def _move_source(index, direction):
    paths = list(st.session_state.get('composite_sequence_paths', []))
    if direction == 0:
        paths.pop(index)
    else:
        other = index + direction
        if 0 <= other < len(paths):
            paths[index], paths[other] = paths[other], paths[index]
    st.session_state['composite_sequence_paths'] = paths


def _apply_offset(cfg, identity, options, offset):
    with CompositeStore(cfg['database']) as db:
        db.update_composite(identity, options={**options, 'offset_seconds': offset, 'sync_confirmed': False})


def _visual_controls(document, cfg, options):
    from core.visual_sync import EVENTS
    identity = document['id']
    with st.expander('开球 / 进球等画面事件辅助对轴', expanded=False):
        st.write('圈定同一事件在两路录像中的大概范围，先看对照图，再选择是否让 AI 匹配画面。每路最多搜索 10 分钟；缩小范围可提高精度。')
        st.caption('两路都需看得到比赛画面；仅主播人脸无法做画面对轴。AI 使用 .env 配置的模型，需支持图片输入，会发送所选范围的抽帧图并产生 API 费用。不会上传整段视频。')
        with st.form(f'visual_search_{identity}'):
            event = st.selectbox('画面事件', list(EVENTS), format_func=EVENTS.get)
            description = st.text_input('事件补充描述（可选）', max_chars=300, placeholder='例如：下半场第一次进球，白衣队射门，排除回放')
            a, b = st.columns(2)
            values = {}
            for col, name, label in ((a, 'match', '比赛'), (b, 'commentator', '主播')):
                duration = float(document[name]['duration'])
                values[f'{name}_start'] = col.number_input(f'{label}画面搜索起点（录像秒数）', min_value=0., max_value=duration, value=0., step=1.)
                values[f'{name}_end'] = col.number_input(f'{label}画面搜索终点（录像秒数）', min_value=0., max_value=duration, value=min(60., duration), step=1.)
            samples = st.slider('每路抽帧数量', 8, 32, 20)
            frames_only = st.form_submit_button('生成事件画面对照图（不调用 API）')
            analyze = st.form_submit_button('AI 匹配事件画面')
            if frames_only or analyze:
                try:
                    job = submit_job('compose_visual', {'composite_id': identity,
                        'search': {**values, 'event': event, 'description': description, 'samples': samples}, 'analyze': analyze}, cfg)
                    st.success(f"画面任务已排队：{job['id'][:8]}。完成后刷新对轴结果。")
                except (ValueError, OSError, RuntimeError) as exc:
                    st.error(str(exc))
        if st.button('刷新画面结果'):
            st.rerun()
        visual = document.get('visual_sync')
        if not visual:
            return
        st.caption(visual['note'])
        if visual.get('error'):
            st.warning(visual['error'])
        if visual.get('offset_seconds') is not None:
            st.write(f"画面建议偏移：{visual['offset_seconds']:+.3f} 秒 · 采样误差约 ±{visual['uncertainty_seconds']:.2f} 秒 · 待预览核对")
            if st.button('采用画面建议偏移（仍需预览）'):
                _apply_offset(cfg, identity, options, visual['offset_seconds'])
                st.rerun()
        if visual.get('anchors'):
            st.dataframe(visual['anchors'], hide_index=True)
        with st.expander('查看抽帧对照与手动选择同一画面'):
            a, b = st.columns(2)
            chosen = []
            for col, name, label in ((a, 'match', '比赛'), (b, 'commentator', '主播')):
                frames = visual['frames'][name]
                frame = col.selectbox(f'{label}事件帧', range(len(frames)), key=f'visual_frame_{identity}_{name}_{len(frames)}',
                    format_func=lambda i, fs=frames: f"{fs[i]['id']} · {fs[i]['time']:.3f} 秒")
                chosen.append(frames[frame])
                if Path(frames[frame]['path']).is_file():
                    col.image(frames[frame]['path'])
                for sheet in visual['sheets'][name]:
                    if Path(sheet).is_file():
                        col.image(sheet)
            st.caption('选择两路中同一瞬间的帧后应用，再生成短预览调到准确位置。主播时间以拼接后的总时间为准。')
            if st.button('使用这两个画面位置计算偏移'):
                _apply_offset(cfg, identity, options, chosen[1]['time'] - chosen[0]['time'])
                st.rerun()


def _pick_source(key, title):
    try:
        selected = choose_video(title, st.session_state.get(key, ''))
        st.session_state.pop('composite_picker_error', None)
        if selected:
            st.session_state[key] = selected
    except (OSError, ValueError, RuntimeError) as exc:
        st.session_state['composite_picker_error'] = str(exc)


def composite_page(cfg):
    st.title('双路合成')
    st.caption('比赛画面作主画面，主播画面放入角落。先对轴、检查短预览，再合成完整视频。')
    with st.expander('新建双路合成', expanded=False):
        a, b = st.columns(2)
        a.button('选择比赛录像', on_click=_pick_source, args=('composite_match_path', '选择比赛录像'))
        b.button('选择主播录像', on_click=_pick_source, args=('composite_commentator_path', '选择主播录像'))
        st.caption('点击按钮打开本机文件窗口，选中视频即可。直接读取原文件，无需上传或复制。也可粘贴本地完整路径。')
        if st.session_state.get('composite_picker_error'):
            st.warning(st.session_state['composite_picker_error'])
        match = a.text_input('比赛录像本地路径', placeholder='D:/videos/比赛.mp4', key='composite_match_path')
        commentator = b.text_input('主播录像本地路径', placeholder='D:/videos/主播.mp4', key='composite_commentator_path')
        if match:
            a.caption('已选：' + Path(match).stem)
        if commentator:
            b.caption('已选：' + Path(commentator).stem)
        multiple = st.checkbox('主播端有多个素材：按列表顺序首尾拼接')
        if multiple:
            a, b = st.columns(2)
            a.button('选择多个主播素材', on_click=_add_sequence)
            b.button('把上方主播路径加入列表', on_click=_add_sequence, args=(True,))
            st.caption('列表从上到下就是 1→2→3 的顺序，可上下移动。拼接工作副本保存在缓存，不修改原文件；不同分辨率/帧率会统一，无音轨的段补静音。')
            sequence = st.session_state.get('composite_sequence_paths', [])
            for i, path in enumerate(sequence):
                label, up, down, remove = st.columns([7, 1, 1, 1])
                label.write(f'{i + 1}. {Path(path).stem}')
                label.caption(path)
                up.button('↑', key=f'source_up_{i}', disabled=i == 0, on_click=_move_source, args=(i, -1))
                down.button('↓', key=f'source_down_{i}', disabled=i == len(sequence)-1, on_click=_move_source, args=(i, 1))
                remove.button('移除', key=f'source_remove_{i}', on_click=_move_source, args=(i, 0))
            st.warning('首尾拼接会直接接起来，不会补录制中断期间的真实时间。若两段之间漏录了比赛，应分项目分别对轴。标准化可能耗时并占用磁盘，已完成段可复用。')
        with st.form('composite_create'):
            a, b = st.columns(2)
            match_track = a.number_input('比赛音轨（从 0 开始）', min_value=0, max_value=30, step=1)
            commentator_track = b.number_input('主播音轨（从 0 开始）', min_value=0, max_value=30, step=1)
            if st.form_submit_button('创建合成项目'):
                try:
                    if multiple:
                        from core.source_sequence import source_paths
                        paths = source_paths(st.session_state.get('composite_sequence_paths', []))
                        job = submit_job('compose_prepare', {'match': resolve_video_path(match), 'commentator': paths,
                            'match_track': int(match_track), 'commentator_track': int(commentator_track)}, cfg)
                        st.success(f"顺序拼接已排队：{job['id'][:8]}。完成后刷新，选择新建的合成项目。")
                    else:
                        document = create_composite(resolve_video_path(match), resolve_video_path(commentator), cfg, int(match_track), int(commentator_track))
                        st.session_state['composite_selected'] = document['id']
                        st.rerun()
                except (OSError, ValueError, RuntimeError) as exc:
                    st.error(str(exc))
    if st.button('刷新合成项目列表'):
        st.rerun()
    with CompositeStore(cfg['database']) as db:
        documents = db.list_composites()
    if not documents:
        st.info('添加比赛录像和主播录像即可开始；支持中文路径，无需上传整个大文件。')
        return
    by_id = {d['id']: d for d in documents}
    identity = st.selectbox('合成项目', list(by_id), key='composite_selected',
        format_func=lambda k: f"{Path(by_id[k]['match']['source']).stem} + {Path(by_id[k]['commentator']['source']).stem} · {k[:8]}")
    document = by_id[identity]
    options = normalize_composite_options(document['options'])
    st.caption(f"比赛 {document['match']['duration']/60:.1f} 分钟 · 主播 {document['commentator']['duration']/60:.1f} 分钟")
    if document['commentator'].get('parts'):
        with st.expander('主播素材拼接顺序与时间映射'):
            st.dataframe([{'顺序': i+1, '素材': Path(p['source']).name, '总时间起点': p['sequence_start'],
                '总时间终点': p['sequence_end'], '原素材时长': p['duration']} for i,p in enumerate(document['commentator']['parts'])], hide_index=True)
            st.caption('以下主播时间均以拼接后的总时间为准。若录制有缺段，一个固定偏移可能无法覆盖所有素材，请检查接缝前后。')
    with st.expander('自动对轴', expanded=True):
        st.write('共同声音匹配会扫描整段录像，支持较大的开场时间差，不调用大模型。不同解说可选语义辅助，结果需要人工核对。')
        semantic = st.checkbox('共同声音不足时启用语音与 LLM 事件辅助（可能转录两路全片并产生 API 费用）')
        allow_cpu = st.checkbox('语音辅助允许 CPU 回退')
        if st.button('开始自动对轴', key=f'sync_{identity}'):
            job = submit_job('compose_sync', {'composite_id': identity, 'semantic': semantic, 'allow_cpu': allow_cpu}, cfg)
            st.success(f"已加入后台队列：{job['id'][:8]}。完成后点击刷新。")
        if st.button('刷新对轴和输出结果'):
            st.rerun()
        sync = document.get('sync')
        if sync:
            confidence = {'high': '较高', 'medium': '中等', 'low': '低 / 待核对'}[sync['confidence']]
            st.write(f"可信度：{confidence} · 建议偏移：{sync.get('offset_seconds')} 秒")
            st.caption(sync['note'])
            if sync.get('semantic_error'):
                st.warning('语义辅助未完成；共同声音结果已保留。' + sync['semantic_error'])
            with st.expander('匹配证据与候选'):
                st.json(sync)
            if sync.get('offset_seconds') is not None and st.button('采用建议偏移（仍需预览）'):
                with CompositeStore(cfg['database']) as db:
                    db.update_composite(identity, options={**options, 'offset_seconds': sync['offset_seconds'], 'sync_confirmed': False})
                st.rerun()
    _visual_controls(document, cfg, options)
    # Revision-specific keys clear stale widgets when synchronization settings change externally.
    from core.utils import fingerprint
    key = identity + fingerprint(options)
    with st.form(f'composite_options_{key}'):
        title = st.text_input('输出标题', value=options['title'])
        offset = st.number_input('同步偏移（秒，可正可负）', value=options['offset_seconds'], min_value=-86400., max_value=86400., step=.05, format='%.3f')
        st.caption('偏移 = 主播录像时间 − 比赛录像时间。例如同一事件：比赛 120 秒、主播 360 秒，偏移填 +240。')
        with st.expander('用同一事件的两个录像位置校准'):
            match_anchor = st.number_input('事件在比赛录像中的秒数', min_value=0., value=0.)
            commentator_anchor = st.number_input('事件在主播录像中的秒数', min_value=0., value=0.)
            use_anchors = st.checkbox('本次保存时使用两个事件位置计算偏移')
        a, b = st.columns(2)
        layout = a.selectbox('主播画面', ['fit', 'face'], index=['fit', 'face'].index(options['layout']),
                             format_func=lambda v: {'fit': '完整缩放（默认）', 'face': '人脸跟踪 · 16:9 裁剪'}[v])
        ratio = b.slider('主播窗口宽度占比（%）', 10, 50, round(options['width_ratio'] * 100))
        positions = ['bottom_left', 'bottom_right', 'top_left', 'top_right']
        position = a.selectbox('窗口位置', positions, index=positions.index(options['position']),
            format_func=lambda v: dict(zip(positions, ['左下', '右下', '左上', '右上']))[v])
        keep_commentator = b.checkbox('保留主播声音', value=options['audio'] in ('commentator', 'mix'))
        mute_match = a.checkbox('比赛录像静音', value=options['match_audio_mode'] == 'mute' or options['audio'] == 'commentator')
        processing = b.selectbox('比赛声音处理（未静音时生效）', ['original', 'remove_vocals'],
            index=1 if options['match_audio_mode'] == 'remove_vocals' else 0,
            format_func=lambda v: {'original':'保留原声', 'remove_vocals':'AI 去解说人声，尽量保留现场声'}[v])
        st.caption('比赛静音会完全去掉比赛音轨。AI 去人声只处理比赛声音，可能残留解说或削弱欢呼声；先生成短预览试听。主播录音中已有的串音不会被此开关去掉。')
        from core.vocal_remover import available
        if not available():
            st.info('AI 去人声为可选功能。尚未安装时可直接静音；启用 AI 前运行 python -m pip install -r requirements-vocals.txt。')
        devices = ['auto', 'cpu', 'cuda']
        vocal_device = b.selectbox('AI 去人声设备', devices, index=devices.index(options['vocal_device']),
                                  format_func=lambda v: {'auto':'自动（GPU 不可用时用 CPU）','cpu':'CPU','cuda':'GPU（PyTorch CUDA）'}[v])
        st.caption('AI 分离首次使用会下载预训练模型；只处理选定范围，已完成的 30 秒块会缓存。静音无需模型。两个声音都关闭时输出静音视频。')
        match_volume = a.slider('比赛音量', 0., 2., options['match_volume'], step=.05)
        commentator_volume = b.slider('主播音量', 0., 2., options['commentator_volume'], step=.05)
        confirmed = st.checkbox('已核对同步位置（整段导出前请检查前后段）', value=options['sync_confirmed'])
        if st.form_submit_button('保存合成设置'):
            value = normalize_composite_options({**options, 'title': title, 'offset_seconds': commentator_anchor-match_anchor if use_anchors else offset,
                'layout': layout, 'width_ratio': ratio/100, 'position': position,
                'audio': ('commentator' if mute_match else 'mix') if keep_commentator else 'match',
                'match_audio_mode': 'mute' if mute_match else processing, 'vocal_device': vocal_device,
                'match_volume': match_volume, 'commentator_volume': commentator_volume, 'sync_confirmed': confirmed})
            with CompositeStore(cfg['database']) as db:
                db.update_composite(identity, options=value)
            st.rerun()
    st.caption('渲染使用已保存的设置。25% 指窗口宽度；完整画面会等比缩放并补边。人脸模式未检测到人脸时保持完整画面。')
    try:
        overlap = overlap_range(document['match']['duration'], document['commentator']['duration'], options['offset_seconds'])
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.write(f"可合成的比赛录像范围：{overlap['start']:.2f}～{overlap['end']:.2f} 秒")
    a, b = st.columns(2)
    start = a.number_input('导出起点（比赛录像秒数）', min_value=overlap['start'], max_value=overlap['end'], value=overlap['start'], key=f'start_{key}')
    end = b.number_input('导出终点（比赛录像秒数）', min_value=overlap['start'], max_value=overlap['end'], value=overlap['end'], key=f'end_{key}')
    preview_start = st.number_input('短预览起点（比赛录像秒数，最多预览 20 秒）', min_value=overlap['start'], max_value=overlap['end'], value=overlap['start'], key=f'preview_{key}')
    a, b = st.columns(2)
    if a.button('生成短预览'):
        if preview_start >= overlap['end']:
            st.error('预览起点需要小于公共区间终点')
        else:
            job = submit_job('compose_render', {'composite_id': identity, 'options': options, 'preview': True, 'preview_start': preview_start}, cfg)
            st.success(f"预览任务已排队：{job['id'][:8]}")
    if b.button('导出合成视频', disabled=not options['sync_confirmed']):
        if end <= start:
            st.error('导出终点需要大于起点')
        else:
            job = submit_job('compose_render', {'composite_id': identity, 'options': options, 'segments': [{'start': start, 'end': end}], 'preview': False}, cfg)
            st.success(f"合成任务已排队：{job['id'][:8]}。可在任务队列查看进度。")
    outputs = document.get('outputs', [])
    if outputs:
        selected = st.selectbox('查看已生成的视频', list(range(len(outputs)-1, -1, -1)),
            format_func=lambda i: f"{'预览' if outputs[i]['preview'] else '成品'} · {outputs[i]['options']['title']} · {outputs[i]['duration']:.1f} 秒 · {outputs[i]['render_key'][:8]}")
        output = outputs[selected]
        if Path(output['video']).is_file():
            st.video(output['video'])
            st.code(output['video'], language=None)
            for warning in output.get('warnings', []):
                st.caption(warning)
            st.caption('需要继续 AI 切片时，在「项目」中导入这个合成文件即可；原始两路文件会保留。')
        else:
            st.warning('此输出文件已被移动或删除，可重新生成。')
