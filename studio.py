"""FrameCut's local video workbench."""
import json
import os
from pathlib import Path

import streamlit as st

from framecut import chapters, search
from identity import tenant_workspace
from jobs import INDEX_VERSION, QuotaExceeded, Workspace
from worker import start
from uploads import media_url, mint
from retrieval import config as embedding_config, hybrid
from visual import shots

PHASES = {
    "": "排队", "probe": "读取媒体", "transcript": "语音与字幕", "ocr": "画面文字",
    "semantic": "建立语义索引", "planning": "规划剪辑", "answering": "检索与回答", "visual": "镜头分析", "result": "写入结果", "render": "渲染视频", "complete": "完成",
}
STATES = {"queued": "排队中", "running": "处理中", "failed": "失败", "done": "完成"}
KINDS = {"index": "视频分析", "plan": "剪辑规划", "export": "视频导出", "question": "证据分析"}


@st.cache_resource
def open_workspace(root):
    return Workspace(root)


@st.cache_resource
def start_workers(root, concurrency):
    start(root, concurrency)
    return True


def submit(store, kind, spec):
    try:
        return store.submit(kind, spec)
    except QuotaExceeded as error:
        st.error(str(error))
        return None


def timestamp(time):
    minutes, seconds = divmod(int(time), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


@st.fragment(run_every=1.5)
def progress(store, task_id, previous):
    job = store.get(task_id)
    state = job["state"]
    if state != previous:
        st.rerun()
    if state in {"queued", "running"}:
        st.info(f"{STATES[state]} · {PHASES[job['phase']]}")
    elif state == "failed":
        st.error(f"{PHASES[job['phase']]}失败 · {job['error']}")
        if st.button("重试任务", key=f"retry-{task_id}"):
            try:
                store.retry(task_id)
            except QuotaExceeded as error:
                st.error(str(error))
            else:
                st.rerun()


st.set_page_config(page_title="FrameCut Studio", page_icon="🎬", layout="wide")
root = os.getenv("FRAMECUT_WORKSPACE", ".framecut")
upload_url = os.getenv("FRAMECUT_UPLOAD_URL")
upload_secret = os.getenv("FRAMECUT_UPLOAD_SECRET", "")
media_enabled = bool(upload_url and len(upload_secret) >= 32)
require_login = os.getenv("FRAMECUT_REQUIRE_LOGIN") == "1"
if require_login:
    if not media_enabled:
        st.error("登录模式需要配置独立的上传与媒体服务。")
        st.stop()
    if not st.user.is_logged_in:
        st.title("FrameCut Studio")
        if st.button("登录"):
            st.login()
        st.stop()
    try:
        workspace_root = tenant_workspace(root, st.user)
    except ValueError as error:
        st.error(str(error))
        st.stop()
    if st.sidebar.button("退出登录"):
        st.logout()
else:
    workspace_root = root
tenant = Path(workspace_root).name if require_login else "local"
store = open_workspace(str(workspace_root))
start_workers(root, int(os.getenv("FRAMECUT_EMBEDDED_WORKERS", "1")))
st.title("FrameCut Studio")
st.caption("视频理解与智能剪辑工作台")

with st.sidebar:
    st.header("素材库")
    if media_enabled:
        st.link_button("续传大文件", f"{upload_url.rstrip('/')}/#{mint(tenant, upload_secret)}")
        if st.button("刷新素材库"):
            st.rerun()
    with st.expander("导入视频", expanded=not store.list("index")):
        with st.form("import_video"):
            upload = st.file_uploader("视频", type=["mp4", "mov", "mkv", "webm"])
            captions = st.file_uploader("字幕（可选）", type=["srt", "vtt"])
            visual_only = st.checkbox("仅分析画面", help="跳过自动语音转写。")
            model = st.selectbox("本地转写模型", ["tiny", "base", "small"])
            ocr = st.selectbox("画面文字识别", ["关闭", "eng", "chi_sim+eng"])
            build = st.form_submit_button("提交分析", type="primary")
        if build:
            if not upload:
                st.error("请选择视频文件。")
            else:
                try:
                    project = submit(store, "index", {
                        "video": store.ingest(upload), "name": upload.name,
                        "captions": store.ingest(captions) if captions else None,
                        "model": model, "ocr": None if ocr == "关闭" else ocr, "skip_speech": visual_only, "embedding": embedding_config(), "index_version": INDEX_VERSION,
                    })
                    if project:
                        st.session_state.project = project
                except QuotaExceeded as error:
                    st.error(str(error))
    library = {job["id"]: json.loads(job["spec"])["name"] for job in store.list("index")}
    if library:
        project = st.selectbox("打开素材", list(library), format_func=library.get, key="project")

with st.expander("任务记录"):
    jobs = store.list()
    if jobs:
        st.dataframe([{
            "任务": KINDS[j["kind"]], "状态": STATES[j["state"]], "阶段": PHASES[j["phase"]],
            "次数": j["attempts"], "编号": j["id"][:8],
        } for j in jobs], hide_index=True)
    if st.button("刷新任务记录"):
        st.rerun()

if not library:
    st.info("导入视频后，工作台会建立时间戳索引，并按你的目标生成剪辑方案。")
    st.stop()

task = store.get(project)
st.subheader(library[project])
if task["state"] != "done":
    progress(store, project, task["state"])
    st.stop()

index = store.result(project)
video = json.loads(task["spec"])["video"]
player, workspace = st.columns([1, 1])
with workspace:
    editor, outline, finder, discussion = st.tabs(["剪辑 Agent", "内容与镜头", "证据检索", "视频问答"])
    with outline:
        for chapter in chapters(index):
            st.markdown(f"**{timestamp(chapter['start'])} – {timestamp(chapter['end'])}**")
            st.text(chapter["excerpt"])
        boundaries = shots(index.get("visual", []), index["duration"])
        if boundaries:
            st.dataframe([{"开始": timestamp(start), "结束": timestamp(end)}
                          for start, end in boundaries], hide_index=True)
        if media_enabled:
            st.link_button("下载时间戳索引", media_url(upload_url, tenant, upload_secret,
                          store.folder(project) / "result.json", download=True))
        else:
            st.download_button("下载时间戳索引", json.dumps(index, ensure_ascii=False, indent=2),
                               "index.json", mime="application/json")
    with finder:
        with st.form("evidence_search"):
            query = st.text_input("关键词或追问", key="search_query")
            st.form_submit_button("检索证据")
        hits = hybrid(index, query, search(index, query), 8) if query.strip() else []
        if query and not hits:
            st.info("没有匹配的文字证据。")
        for number, hit in enumerate(hits):
            if st.button(f"{timestamp(hit['start'])} · {hit['source']}", key=f"hit-{number}"):
                st.session_state.seek = (project, hit["start"])
            st.text(hit["text"])
    with discussion:
        modes = {"general": "通用", "study": "学习", "review": "审查", "creative": "创作"}
        turns = [(item, json.loads(item["spec"])) for item in reversed(store.list("question"))]
        turns = [(item, spec) for item, spec in turns if spec["index"] == project]
        for item, spec in turns:
            st.markdown(f"**{modes[spec['mode']]}**")
            st.text(spec["question"])
            if item["state"] != "done":
                progress(store, item["id"], item["state"])
                continue
            answer = store.result(item["id"])
            st.text(answer["answer"])
            for number, cue in enumerate(answer["evidence"]):
                if st.button(timestamp(cue["start"]), key=f"question-{item['id']}-{number}"):
                    st.session_state.seek = (project, cue["start"])
                st.caption(cue["text"])
        with st.form("video_question", clear_on_submit=True):
            question = st.text_input("向这段视频提问", max_chars=2000)
            mode = st.selectbox("分析侧重", list(modes), format_func=modes.get)
            if st.form_submit_button("提交问题") and question.strip():
                task_id = submit(store, "question", {
                    "video": video, "index": project, "mode": mode, "question": question,
                    "history": [spec["question"] for _, spec in turns[-2:]],
                    "model": os.getenv("FRAMECUT_TEXT_MODEL"),
                    "endpoint": os.getenv("FRAMECUT_API_BASE") or "http://127.0.0.1:11434/v1",
                })
                if task_id:
                    st.rerun()
    with editor:
        with st.form("editing_brief"):
            goal = st.text_area("你想怎么剪？", max_chars=2000, placeholder="例如：剪一条节奏舒缓的旅行短片")
            count = st.slider("最多保留片段数", 1, 10, 3)
            seconds = st.slider("单片段最长秒数", 2, 90, 12)
            use_vision = bool(os.getenv("FRAMECUT_VISION_MODEL")) and st.checkbox(
                "让视觉模型理解候选画面", value=True,
                help="候选画面和相关文字会发送到配置的服务。")
            generate = st.form_submit_button("生成剪辑方案", type="primary")
        if generate:
            if goal.strip():
                new_plan = submit(store, "plan", {
                    "video": video, "index": project, "goal": goal, "count": count,
                    "seconds": seconds, "vision": use_vision, "plan_version": 2,
                    "model": os.getenv("FRAMECUT_VISION_MODEL") if use_vision else None,
                    "review_model": os.getenv("FRAMECUT_REVIEW_MODEL"),
                    "planner_model": os.getenv("FRAMECUT_PLANNER_MODEL"),
                    "endpoint": os.getenv("FRAMECUT_API_BASE") if use_vision else None,
                })
                if new_plan:
                    st.session_state[f"plan-{project}"] = new_plan
            else:
                st.error("请输入剪辑目标。")
        plans = {j["id"]: json.loads(j["spec"])["goal"] for j in store.list("plan")
                 if json.loads(j["spec"])["index"] == project}
        if plans:
            ids = list(plans)
            active = st.session_state.get(f"plan-{project}", ids[0])
            if generate and goal.strip():
                st.session_state.pop(f"plan-picker-{project}", None)
            plan_id = st.selectbox("剪辑方案", ids, index=ids.index(active), format_func=plans.get,
                                   key=f"plan-picker-{project}")
            plan_task = store.get(plan_id)
            if plan_task["state"] != "done":
                progress(store, plan_id, plan_task["state"])
            else:
                result = store.result(plan_id)
                st.text(result["overview"])
                if result.get("review"):
                    st.caption(result["review"]["note"])
                if result.get("observations"):
                    accepted = {clip["vision"]["id"] for clip in result["clips"] if clip.get("vision")}
                    with st.expander("画面证据"):
                        for item in result["observations"]:
                            label = {True: "审阅剔除", False: "未入选"}[item["match"]]
                            if item["id"] in accepted:
                                label = "已入选"
                            if st.button(f"{timestamp(item['frame'])} · {label}",
                                         key=f"vision-frame-{plan_id}-{item['id']}"):
                                st.session_state.seek = (project, item["frame"])
                            st.text(item["description"])
                if not result["clips"]:
                    st.info("没有找到符合目标的片段。")
                selected = []
                for number, clip in enumerate(result["clips"]):
                    label = f"{timestamp(clip['start'])} – {timestamp(clip['end'])} · {clip['reason']}"
                    if st.checkbox(label, value=True, key=f"clip-{plan_id}-{number}"):
                        selected.append(clip)
                    for cue in clip["evidence"][:3]:
                        st.caption(f"{timestamp(cue['start'])} · {cue['text']}")
                if selected and st.button("预览首个选中片段"):
                    st.session_state.seek = (project, selected[0]["start"])
                vertical = st.checkbox("9:16 竖屏")
                if st.button("导出 MP4", disabled=not selected, type="primary"):
                    submit(store, "export", {"video": video, "plan": plan_id, "clips": selected,
                                            "vertical": vertical})
                renders = [j for j in store.list("export")
                           if json.loads(j["spec"])["plan"] == plan_id]
                for render in renders:
                    if render["state"] == "done":
                        fmt = "9:16" if json.loads(render["spec"])["vertical"] else "原画幅"
                        output = Path(store.result(render["id"])["file"])
                        if media_enabled:
                            st.link_button(f"下载剪辑 · {fmt}",
                                           media_url(upload_url, tenant, upload_secret, output, download=True))
                        else:
                            st.download_button(f"下载剪辑 · {fmt}", output.read_bytes(), "highlights.mp4",
                                               mime="video/mp4", key=render["id"])
                    else:
                        progress(store, render["id"], render["state"])

with player:
    target, start = st.session_state.get("seek", (None, 0))
    source = media_url(upload_url, tenant, upload_secret, Path(video)) if media_enabled else video
    st.video(source, start_time=int(start) if target == project else 0)
