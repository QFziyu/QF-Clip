"""LLM settings using the workbench's existing Streamlit controls."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import streamlit as st

from core.llm_settings import EFFORTS, LABELS, save_settings

PROVIDERS = {"api":"API 密钥接入", "codex_cli":"官方登录（Codex / ChatGPT）"}


def panel(cfg):
    active = cfg["openai"]
    if st.session_state.pop('_llm_clear_key', False):
        st.session_state.pop('llm-api-key', None)
    identity = hashlib.sha256(json.dumps({k: active.get(k, '') for k in
        ('provider','base_url','model','reasoning_effort','api_key','codex_home','codex_cli_path')}, sort_keys=True).encode()).hexdigest()
    checked = st.session_state.get('_llm_connection_result', {})
    st.subheader("API 设置")
    st.caption("在这里填写并保存即可，无需修改配置文件。支持 OpenAI 兼容 API，也可切换到已登录的 Codex。")
    st.caption("当前使用："+PROVIDERS[active.get("provider", "api")]+" · "+
               (active.get("model") or ("Codex 默认模型" if active.get("provider")=="codex_cli" else "尚未选择模型"))+" · 思考等级："+active.get("reasoning_effort", "default"))
    if checked.get('identity') == identity:
        (st.success if checked.get('ok') else st.error)(checked['message'])
    else:
        st.caption('连接状态：尚未测试当前配置')
    notice = st.session_state.pop("_llm_notice", None)
    if notice:
        st.success(notice)
    provider = st.radio("调用来源", list(PROVIDERS), format_func=PROVIDERS.get,
                        index=0 if active.get("provider", "api") == "api" else 1,
                        horizontal=True, key="llm-provider")
    profile = cfg["_llm_api_profile"] if provider == "api" else cfg["_llm_codex_profile"]
    prefix = "llm-"+provider+"-"
    with st.form(prefix+"form"):
        if provider == "api":
            st.markdown("**填写服务商提供的三项信息**")
            base = st.text_input("API 地址（Base URL）", value=profile.get("base_url", ""), key=prefix+"base", placeholder="https://api.openai.com/v1",
                                 help="填写服务商提供的地址，例如 https://api.openai.com/v1；也支持完整的 /chat/completions 地址。")
            key = st.text_input("API Key", type="password", key=prefix+"key",
                                help="密钥以密码形式显示。已配置时留空即可保留原密钥；只保存在本机。")
            st.caption("密钥状态："+("已配置" if profile.get("api_key") else "未配置"))
        model = st.text_input("模型名称", value=profile.get("model", ""), key=prefix+"model",
                              placeholder="复制服务商控制台中的模型 ID", help="填写该账户/服务商支持的准确模型名。Codex 留空使用 CLI 内置默认模型。")
        choices = list(EFFORTS if provider == "codex_cli" else EFFORTS[:-1])
        effort = st.selectbox("思考等级", choices, index=choices.index(profile.get("reasoning_effort", "default")),
                              format_func=LABELS.get, key=prefix+"effort")
        st.caption("可用等级取决于模型与服务商；默认不发送该参数。更高等级可能增加耗时和额度消耗。")
        if provider == "codex_cli":
            st.info("使用 Codex CLI 的 ChatGPT 登录与订阅额度，仍受账户限额约束。项目中的 API Key 不会传给 CLI。")
            from core.codex_cli import detected_home
            st.caption("登录目录：" + (detected_home(profile) or "系统默认目录"))
            with st.expander("Codex 高级设置"):
                cli = st.text_input("Codex CLI 路径", value=profile.get("codex_cli_path", ""), key=prefix+"cli",
                                    help="留空自动检测。填写可执行文件路径，不添加命令行参数。")
                home = st.text_input("Codex 登录目录（CODEX_HOME）", value=profile.get("codex_home", ""), key=prefix+"home",
                                     help="留空继承当前系统环境与 CLI 默认目录；选择你已登录 ChatGPT 的目录。")
        saved = st.form_submit_button("保存并使用此来源", type="primary")
    if saved:
        fields = ({"OPENAI_BASE_URL":base, "OPENAI_API_KEY":key, "OPENAI_MODEL":model,
                   "OPENAI_REASONING_EFFORT":effort} if provider == "api" else
                  {"CODEX_MODEL":model, "CODEX_REASONING_EFFORT":effort, "CODEX_CLI_PATH":cli, "FCF_CODEX_HOME":home})
        try:
            save_settings(cfg, provider, fields)
            st.session_state["_llm_clear_key"] = True
            st.session_state["_llm_notice"] = "已保存。后续开始执行的任务将使用此来源、模型与思考等级；当前运行的任务按原配置完成。"
            st.rerun()
        except (ValueError, OSError) as error:
            st.error(str(error))
    if provider == "codex_cli":
        if st.button("检查 Codex 登录状态"):
            from core.codex_cli import login_status
            draft = dict(profile, codex_cli_path=cli, codex_home=home)
            try:
                status = login_status(draft)
                (st.success if status["ready"] else st.warning)(status["message"])
                st.caption(status["executable"])
            except RuntimeError as error:
                st.error(str(error))
        with st.expander("登录方法"):
            st.write("在 PowerShell 中运行下面的命令，选择 ChatGPT 登录，使用你的 Plus 账户。完成后回来检查状态。")
            try:
                from core.codex_cli import executable
                from core.codex_cli import detected_home
                command = executable(dict(profile, codex_cli_path=cli))
                home = detected_home(dict(profile, codex_home=home))
                invocation = "& "+" ".join("'"+arg.replace("'", "''")+"'" for arg in command)+" login"
                if home:
                    invocation = "$env:CODEX_HOME = '"+home.replace("'", "''")+"'\n"+invocation
                st.code(invocation, language="powershell")
            except RuntimeError:
                st.code("npm install -g @openai/codex\ncodex login", language="powershell")
    st.caption("配置仅保存在本机，后续任务自动使用。API 未配置时仍可打开工作台；需要大模型的功能再配置即可。")
    with st.expander("高级：配置保存位置"):
        st.code(cfg["_llm_settings_path"], language=None)
        st.caption("程序自动管理此文件，无需手动编辑；不要将它发给其他人或提交到 GitHub。")
    st.caption("先保存，再测试连接。测试使用已保存的配置，会产生一次模型调用，可能计费。")
    if st.button("测试已保存配置", key="llm-test"):
        try:
            from core.llm_client import LLMClient
            options = copy.deepcopy(active)
            options.update(max_retries=0, max_output_tokens=1024)
            schema = {"type":"object", "additionalProperties":False, "required":["ok"],
                      "properties":{"ok":{"type":"boolean"}}}
            with st.spinner("正在测试已保存的模型配置…"):
                value = LLMClient(options, Path(cfg["cache_dir"])/"llm_connection_budget.json").complete(
                    [{"role":"user", "content":'仅返回 JSON {"ok":true}'}], schema, response_key="ok")
            if value.get("ok") is not True:
                raise ValueError("模型返回内容未通过连接测试")
            st.session_state['_llm_connection_result'] = {'identity':identity,'ok':True,'message':'连接成功，模型配置可用。'}
            st.rerun()
        except Exception as error:
            detail = str(error)
            for secret in (cfg["_llm_api_profile"].get("api_key"), active.get("api_key")):
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            st.session_state['_llm_connection_result'] = {'identity':identity,'ok':False,'message':'连接测试失败：'+detail}
            st.rerun()
