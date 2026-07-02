import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_ROOT = Path(__file__).resolve().parent.parent

import streamlit as st

st.set_page_config(
    page_title="Migration Pipeline Console",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from pages.dashboard import render as render_dashboard
from pages.agent_metrics import render as render_metrics
from pages.correct_sql import render as render_correct_sql
from pages.fail_analysis import render as render_fail_analysis
from pages.mig_monitor import render as render_mig
from pages.rag_manager_page import render as render_rag
from pages.settings_page import render as render_settings
from pages.sql_monitor import render as render_sql
from pages.system_health import render as render_health
from pages.xml_export import render as render_xml
from utils.agent_control import get_status, pause, resume, start, stop
from utils.env_manager import read_env, write_env_key

_AGENT_CONTROL_ACTIONS = {
    "start": ("Agent 시작 중...", start),
    "pause": ("Agent 일시정지 요청 중...", pause),
    "resume": ("Agent 재개 요청 중...", resume),
    "stop": ("Agent 중지 요청 중...", stop),
}


def _queue_agent_control(action: str) -> None:
    st.session_state["agent_control_pending"] = action
    st.rerun()


def _run_pending_agent_control() -> None:
    action = st.session_state.get("agent_control_pending")
    if not action:
        return

    label, handler = _AGENT_CONTROL_ACTIONS.get(action, ("Agent 제어 중...", None))
    with st.spinner(label):
        try:
            message = handler() if handler else f"알 수 없는 Agent 제어 요청입니다: {action}"
        except Exception as exc:
            message = f"Agent 제어 실패: {exc}"

    st.session_state.pop("agent_control_pending", None)
    st.toast(message)
    st.rerun()


_MENU = {
    "📊 Dashboard": render_dashboard,
    "🔎 Fail Analysis": render_fail_analysis,
    "🗄️ Mig Agent Monitor": render_mig,
    "🧾 SQL Agent Monitor": render_sql,
    "✅ Correct SQL Manager": render_correct_sql,
    "📚 Tuning Rule Manager": render_rag,
    "🩺 System Health": render_health,
    "⚙️ Settings": render_settings,
    "📦 XML Export": render_xml,
    "📈 Agent Metrics": render_metrics,
}

st.markdown(
    """
<style>
[data-testid="stSidebarNav"],
[data-testid="stSidebarNavItems"],
[data-testid="stSidebarNavSeparator"],
section[data-testid="stSidebar"] ul { display: none !important; }
</style>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.image("https://img.icons8.com/color/96/database.png", width=60)
    st.markdown("## Migration Console")

    st.markdown("---")
    st.markdown("#### MENU")
    menu_items = list(_MENU.keys())
    requested_page = st.query_params.get("page")
    default_idx = menu_items.index(requested_page) if requested_page in menu_items else 0
    selected = st.radio("MENU", menu_items, index=default_idx, label_visibility="collapsed")

    st.markdown("---")
    st.markdown("#### 🧭 Agent 선택")
    env = read_env()
    db_only = env.get("DB_MIGRATION_ONLY", "false").lower() == "true"
    sql_only = env.get("SQL_CONVERSION_ONLY", "false").lower() == "true"
    tuning_only = env.get("SQL_TUNING_ONLY", "false").lower() == "true"
    formatting_only = env.get("SQL_FORMATTING_ONLY", "false").lower() == "true"
    supervisor_mode = env.get("SUPERVISOR_MODE", "false").lower() == "true"

    new_supervisor_mode = st.toggle("Supervisor", value=supervisor_mode, help="Supervisor 모드: AI가 실패 원인 분석 및 특정 작업 재실행을 지원합니다.")
    new_db_only = st.toggle("DB Migration", value=db_only)
    new_sql_only = st.toggle("SQL Conversion", value=sql_only)
    new_tuning_only = st.toggle("SQL Tuning", value=tuning_only)
    new_formatting_only = st.toggle("SQL Formatting", value=formatting_only)

    if (new_db_only, new_sql_only, new_tuning_only, new_formatting_only, new_supervisor_mode) != (
        db_only,
        sql_only,
        tuning_only,
        formatting_only,
        supervisor_mode,
    ):
        write_env_key("DB_MIGRATION_ONLY", str(new_db_only).lower())
        write_env_key("SQL_CONVERSION_ONLY", str(new_sql_only).lower())
        write_env_key("SQL_TUNING_ONLY", str(new_tuning_only).lower())
        write_env_key("SQL_FORMATTING_ONLY", str(new_formatting_only).lower())
        write_env_key("SUPERVISOR_MODE", str(new_supervisor_mode).lower())
        st.toast("Agent 선택 설정을 저장했습니다. 실행 중인 Agent에는 재시작 후 적용됩니다.")
        st.rerun()

    if not any((new_db_only, new_sql_only, new_tuning_only, new_formatting_only)):
        st.caption("전체 실행: 모든 Agent를 실행합니다.")
    else:
        selected_agents = []
        if new_db_only:
            selected_agents.append("DB")
        if new_sql_only:
            selected_agents.append("SQL")
        if new_tuning_only:
            selected_agents.append("Tuning")
        if new_formatting_only:
            selected_agents.append("Formatting")
        st.caption("선택 실행: " + ", ".join(selected_agents))
    if new_supervisor_mode:
        st.caption("🤖 Supervisor 모드 활성화")

    st.markdown("---")
    st.markdown("#### ⚙️ Agent 제어")
    _run_pending_agent_control()

    status = get_status()
    st.markdown(f"**{status['label']}**" + (f"  `PID {status['pid']}`" if status["pid"] else ""))

    if not status["running"]:
        if st.button("▶️ 시작", width="stretch", type="primary"):
            _queue_agent_control("start")
    else:
        c1, c2 = st.columns(2)
        if status["paused"]:
            with c1:
                if st.button("▶️ 재개", width="stretch", type="primary"):
                    _queue_agent_control("resume")
        else:
            with c1:
                if st.button("⏸️ 일시정지", width="stretch"):
                    _queue_agent_control("pause")
        with c2:
            if st.button("⏹️ 중지", width="stretch", type="secondary"):
                _queue_agent_control("stop")

    st.markdown("---")
    st.caption("Unified Multi-Agent Pipeline")

_MENU[selected]()
