"""Shared budget approval dialog for the workbench and task queue."""
import streamlit as st

from core.budget_approval import approve_and_retry, budget_request, effective_options, resume_with_current
from core.jobs import get_job


def _close_dialog():
    st.session_state.pop('_budget_dialog', None)


@st.dialog('批准本次超预算请求', on_dismiss=_close_dialog)
def approval_dialog(job_id, request_id, cfg):
    job = get_job(job_id, cfg)
    request = budget_request(job, cfg) if job else None
    if not request or request['request_id'] != request_id:
        st.warning('预算申请已变化，请关闭窗口并刷新任务。')
        return
    spent, estimate, limit = (request[k] for k in ('spent_usd', 'estimate_usd', 'limit_usd'))
    st.write('任务 ' + job_id[:8] + ' · 模型 ' + request['model'])
    a, b = st.columns(2)
    a.metric('项目已计金额', f'${spent:.4f}')
    b.metric('项目默认上限', f'${limit:.4f}')
    a.metric('本次请求预留', f'${estimate:.4f}')
    b.metric('预计累计金额', f'${spent+estimate:.4f}')
    st.warning(f'本次预计超过默认上限 ${max(0, spent+estimate-limit):.4f}。')
    st.caption('金额按项目配置单价估算，实际费用以服务商账单为准；请求失败也可能消耗额度。')
    if request.get('legacy'):
        st.caption('这是一条旧版拦截记录，本次预留已包含原报错金额的舍入余量。')
    options = effective_options(job, cfg)
    changed = options.get('provider', 'api') != request['provider'] or options.get('model', '') != request['model']
    if changed:
        source = '官方登录（Codex）' if options.get('provider') == 'codex_cli' else 'API · '+options.get('model', '')
        st.info('当前来源／模型已改为 '+source+'。以上是原请求的预算记录；按当前配置继续不会批准原 API 的超预算请求，新请求仍按当前来源的预算规则执行。')
    else:
        st.write('批准后仅放行这一次请求，并从已有缓存继续任务。默认预算不变，后续超预算请求需要再次批准。')
    a, b = st.columns(2)
    if a.button('按当前配置继续任务' if changed else '批准这一次并继续', type='primary', key='budget-approve-'+job_id):
        try:
            if changed:
                resume_with_current(job_id, cfg)
            else:
                approve_and_retry(job_id, request_id, cfg)
            _close_dialog()
            st.rerun()
        except (ValueError, OSError) as error:
            st.error(str(error))
    if b.button('暂不批准', key='budget-decline-'+job_id):
        _close_dialog()
        st.rerun()


def budget_action(job, cfg, key, compact=False):
    request = budget_request(job, cfg)
    if not request:
        return False
    if not compact:
        st.caption('本次超预算请求正在等待你的批准，尚未发送。')
    if st.button('预算审批' if compact else '审批本次超预算请求', key=key):
        st.session_state['_budget_dialog'] = (job['id'], key)
    if st.session_state.get('_budget_dialog') == (job['id'], key):
        approval_dialog(job['id'], request['request_id'], cfg)
    return True
