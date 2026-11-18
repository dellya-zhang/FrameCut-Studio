"""Local Streamlit workstation for FrameCut."""
import json
import shutil
import tempfile
from pathlib import Path

import streamlit as st

from framecut import chapters, export, index_video, plan, search


def save_upload(upload, root, stem):
    target = root / (stem + Path(upload.name).suffix)
    upload.seek(0)
    with target.open("wb") as file:
        shutil.copyfileobj(upload, file)
    upload.seek(0)
    return target


def timestamp(time):
    minutes, seconds = divmod(int(time), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


st.set_page_config(page_title="FrameCut Studio", page_icon="🎬", layout="wide")
st.title("FrameCut Studio")
st.caption("长视频检索与主题剪辑")

with st.sidebar:
    st.header("素材")
    video = st.file_uploader("视频", type=["mp4", "mov", "mkv", "webm"])
    captions = st.file_uploader("字幕（可选）", type=["srt", "vtt"])
    model = st.selectbox("本地转写模型", ["tiny", "base", "small"], disabled=captions is not None)
    ocr = st.selectbox("画面文字识别", ["关闭", "eng", "chi_sim+eng"])
    build = st.button("建立索引", type="primary", disabled=video is None)

if not video:
    st.session_state.pop("analysis", None)
    st.session_state.pop("render", None)
    st.stop()

identity = (video.file_id, captions.file_id if captions else None, model, ocr)
if build:
    try:
        with st.spinner("正在读取素材并建立时间戳索引…"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video_path = save_upload(video, root, "video")
            caption_path = save_upload(captions, root, "captions") if captions else None
            index = index_video(video_path, caption_path, model, None if ocr == "关闭" else ocr)
        st.session_state.analysis = (identity, index)
        st.session_state.pop("render", None)
    except (OSError, RuntimeError, ValueError, ImportError, KeyError) as error:
        st.error(str(error))

analysis = st.session_state.get("analysis")
if not analysis or analysis[0] != identity:
    st.info("建立索引后即可查看章节、搜索内容和导出剪辑。")
    st.stop()

index = analysis[1]
if not index["cues"]:
    st.warning("没有识别到内容。可导入字幕，或打开画面文字识别后重新建立索引。")

player, workspace = st.columns([1, 1])
with workspace:
    outline, finder, editor = st.tabs(["章节", "证据检索", "智能剪辑"])
    with outline:
        for chapter in chapters(index):
            st.markdown(f"**{timestamp(chapter['start'])} – {timestamp(chapter['end'])}**")
            st.text(chapter["excerpt"])
        st.download_button("下载时间戳索引", json.dumps(index, ensure_ascii=False, indent=2),
                           "index.json", mime="application/json")
    with finder:
        query = st.text_input("关键词或主题", key="search_query")
        hits = search(index, query)
        if query and not hits:
            st.info("没有匹配的证据")
        for number, hit in enumerate(hits):
            if st.button(f"{timestamp(hit['start'])} · {hit['source']}", key=f"hit-{number}"):
                st.session_state.seek = (video.file_id, hit["start"])
            st.text(hit["text"])
    with editor:
        topic = st.text_input("剪辑主题", placeholder="例如：太阳能发电")
        count = st.slider("最多保留片段数", 1, 10, 3)
        seconds = st.slider("单片段最长秒数", 5, 90, 30)
        vertical = st.checkbox("9:16 竖屏")
        clips = plan(index, topic, count, seconds)
        if topic and not clips:
            st.info("没有匹配的片段")
        selected = []
        for number, clip in enumerate(clips):
            label = f"{timestamp(clip['start'])} – {timestamp(clip['end'])} · {clip['text']}"
            if st.checkbox(label, value=True, key=f"clip-{number}-{label}"):
                selected.append(clip)
        settings = (identity, selected, vertical)
        if st.button("导出 MP4", type="primary", disabled=not selected):
            try:
                with st.spinner("正在渲染剪辑…"), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    result = root / "highlights.mp4"
                    export(save_upload(video, root, "video"), selected, result, vertical)
                    st.session_state.render = (settings, result.read_bytes())
            except (OSError, RuntimeError, ValueError) as error:
                st.error(str(error))
        rendered = st.session_state.get("render")
        if rendered and rendered[0] == settings:
            st.download_button("下载剪辑", rendered[1], "highlights.mp4", mime="video/mp4")

with player:
    target, start = st.session_state.get("seek", (None, 0))
    st.video(video, format=video.type, start_time=int(start) if target == video.file_id else 0)
