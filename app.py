import sys
import subprocess
import importlib
import os

# 绕过 ClashVerge 等代理工具设置的系统级代理，防止 requests 库自动拾取 SOCKS 代理导致连接失败
for _proxy_key in ['ALL_PROXY', 'all_proxy', 'SOCKS_PROXY', 'socks_proxy', 'HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy']:
    os.environ.pop(_proxy_key, None)

# Pre-flight dependency check and auto-install
REQUIRED_PACKAGES = {
    "flask": "Flask",
    "flask_socketio": "Flask-SocketIO",
    "requests": "requests",
    "eventlet": "eventlet",
    "dotenv": "python-dotenv",
    "simple_websocket": "simple-websocket",
}

_missing_pkgs = []
for module_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        importlib.import_module(module_name)
    except ImportError:
        _missing_pkgs.append((module_name, pip_name))

for module_name, pip_name in _missing_pkgs:
    print(f"Missing module '{module_name}', installing: {pip_name}", flush=True)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        importlib.invalidate_caches()
    except Exception as _pip_err:
        # 装不上就继续，不让异常冲出模块作用域。这段的职责只是省掉用户一次手工
        # pip install，而不是充当依赖守卫——真正的守卫是下面那几行 import，它的
        # ImportError 会准确报出缺哪个模块。
        # 原先是裸 check_call：在没有 pip 的环境（embeddable / Microsoft Store 版
        # Python、uv venv）里它必然失败，于是一个为防启动失败而写的机制成了启动
        # 失败的唯一原因，而且报的是 pip 的错误码，看不出缺哪个包。
        print(f"  自动安装失败: {_pip_err}", flush=True)
        print(f"  若启动失败请手动执行: \"{sys.executable}\" -m pip install {pip_name}", flush=True)
# 不要怀疑用户服务器忘记重启，用户总是会记得在合适的时候重启服务器以应用修改
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from api import Api
from config import CONFIG
import os
import json
from datetime import datetime
import threading
import time
import py_compile

app = Flask(__name__, template_folder='.', static_folder='libs', static_url_path='/libs')
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*",
                    ping_timeout=10, ping_interval=10)
backend = Api(socketio)
backend.config = CONFIG
backend._reschedule_triggers()

# Start billing sync daemon
from api.billing_sync import BillingSyncDaemon
_billing_daemon = BillingSyncDaemon(backend)
_billing_daemon.start()

# Debug: dump all incoming HTTP requests
DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'raw_dumps')
os.makedirs(DUMP_DIR, exist_ok=True)

@app.before_request
def dump_incoming_request():
    """拦截所有非静态资源的HTTP请求，将请求元数据(方法/路径/头/体)序列化为JSON转储到raw_dumps/目录，用于调试和审计。"""
    if request.path.startswith('/libs/') or request.path == '/' or request.path == '/favicon.ico':
        return None
    # Only dump when developer mode is ON
    try:
        gs = getattr(backend, 'global_settings', None)
        if not gs or not gs.get('developer_mode', False) or not gs.get('enable_bulk_logging', False):
            return None
    except Exception:
        return None
    try:
        now = datetime.now()
        timestamp = now.strftime('%Y%m%d_%H%M%S') + f'_{now.microsecond // 1000:03d}'
        body = None
        raw_body = None
        try:
            body = request.get_json(silent=True, force=True)
        except Exception:
            pass
        if body is None:
            try:
                raw_body = request.get_data(as_text=True)
            except Exception:
                raw_body = '<unable to read>'
        dump = {
            'timestamp': now.isoformat(),
            'method': request.method,
            'path': request.path,
            'url': request.url,
            'query_string': request.query_string.decode('utf-8', errors='replace'),
            'headers': dict(request.headers),
            'body_json': body,
            'body_raw': raw_body,
            'remote_addr': request.remote_addr
        }
        safe_path = request.path.strip('/').replace('/', '_') or 'root'
        filename = f'req_{timestamp}_{safe_path}.json'
        filepath = os.path.join(DUMP_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
        print(f'[DUMP] {request.method} {request.path} -> {filename}')
    except Exception as e:
        print(f'[DUMP ERROR] {e}')
    return None

@app.route('/')
def index():
    return render_template('frontend.html')

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    from api import _tls
    # WebSocket 事件不在 HTTP 请求上下文中，_tls.active_sid 为 None
    # 选择第一个可用会话作为初始推送的焦点，确保前端首次加载时有数据可渲染
    if not getattr(_tls, 'active_sid', None) and backend.sessions:
        _tls.active_sid = next(iter(backend.sessions))
    emit('state_update', backend.get_full_state())

@socketio.on('perf_log')
def handle_perf_log(data):
    label = data.get('label', 'unknown')
    duration = data.get('duration', 0)
    print(f"[Perf] {label}: {duration:.2f}ms")

@app.route('/api/action', methods=['POST'])
def handle_action():
    data = request.json
    action = data.get('action')

    client_sid = data.get('client_sid')
    # 设置线程本地的 active_sid，替代全局 current_session_id 覆写
    # 每个请求线程有自己的 active_sid，并发请求互不干扰
    from api import _tls
    if client_sid and client_sid in backend.sessions:
        _tls.active_sid = client_sid

    for key in ['index', 'msg_index']:
        if data.get(key) is not None:
            try:
                data[key] = int(data[key])
            except (ValueError, TypeError):
                pass

    # Handle explicit outgoing state save for switch_session
    # Frontend passes _outgoing_sid + _outgoing_state to tell server WHERE to save
    # the departing session's UI state (draft, think level, models)
    _outgoing_sid = data.get('_outgoing_sid')
    _outgoing_state = data.get('_outgoing_state')
    if _outgoing_sid and _outgoing_state and _outgoing_sid in backend.sessions:
        _os = backend.sessions[_outgoing_sid]
        if '_draft_text' in _outgoing_state:
            _os['_draft_text'] = str(_outgoing_state['_draft_text'])
        if '_deep_think_level' in _outgoing_state:
            _os['_deep_think_level'] = int(_outgoing_state['_deep_think_level'])
        if '_selected_models' in _outgoing_state:
            _os['_selected_models'] = _outgoing_state['_selected_models']

    # Sync frontend model selection for trigger use
    _sel_models = data.get('_selected_models')
    if _sel_models and isinstance(_sel_models, list):
        _sync_sid = client_sid
        if _sync_sid in backend.sessions:
            backend.sessions[_sync_sid]['_selected_models'] = _sel_models

    # Sync frontend deep think level per-session
    _deep_think = data.get('_deep_think_level')
    if _deep_think is not None:
        _sync_sid_dt = client_sid
        if _sync_sid_dt in backend.sessions:
            backend.sessions[_sync_sid_dt]['_deep_think_level'] = int(_deep_think)

    # Sync frontend draft text per-session
    _draft = data.get('_draft_text')
    if _draft is not None:
        _sync_sid_dr = client_sid
        if _sync_sid_dr in backend.sessions:
            backend.sessions[_sync_sid_dr]['_draft_text'] = str(_draft)



    try:
        if action == 'create_session':
            new_sid = backend.create_session(data.get('name', ''))
            return jsonify({"status": "ok", "new_sid": new_sid, "state": backend.get_full_state()})
        elif action == 'switch_session':
            backend.switch_session(data['sid'])
        elif action == 'delete_session':
            backend.delete_session(data['sid'])
        elif action == 'rename_session':
            backend.rename_session(data['sid'], data['name'])
        elif action == 'duplicate_session':
            new_sid = backend.duplicate_session(data['sid'])
            return jsonify({"status": "ok", "new_sid": new_sid, "state": backend.get_full_state()})
        elif action == 'archive_session':
            backend.archive_session(data['sid'])
        elif action == 'unarchive_session':
            backend.unarchive_session(data['sid'])
        elif action == 'restore_session':
            backend.restore_session(data['sid'])
        elif action == 'reorder_session':
            backend.reorder_session(data['sid'], data.get('direction'), data.get('new_order'))
        elif action == 'add_image':
            backend.add_image_message(data['image_data'], data.get('mime_type', 'image/png'))
        elif action == 'add_only':
            backend.add_only_message(data['text'], role=data.get('role', 'user'))
        elif action == 'start_autopilot':
            backend.start_autopilot(data.get('text', ''), data.get('models', []),
                                   data.get('turns', 3), data.get('max_k', 8.0),
                                   data.get('is_deep_think', False))
        elif action == 'stop_autopilot':
            backend.stop_autopilot()
        elif action == 'update_autopilot_turns':
            _new_turns = data.get('turns')
            try:
                _new_turns = int(_new_turns)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "invalid turns value"})
            if _new_turns < 0:
                return jsonify({"status": "error", "message": "turns must be >= 0"})
            backend.update_autopilot_turns(_new_turns)
        elif action == 'send_message':
            backend.send_message(data['text'], data.get('models', []),
                               data.get('is_early', False), data.get('steps', 1),
                               data.get('parallel', False), data.get('is_offline', False),
                               data.get('is_deep_think', False))
        elif action == 'retry_message':
            backend.retry_message(data['index'])
        elif action == 'delete_message':
            backend.delete_message(data['index'])
        elif action == 'process_offline_response':
            backend.process_offline_response(data['text'])
        elif action == 'edit_message':
            backend.edit_message(data['index'], data['content'])
        elif action == 'update_global_settings':
            _gs = data.get('settings', {})
            if 'enable_billing_sync_button' not in _gs:
                _gs['enable_billing_sync_button'] = getattr(backend, 'global_settings', {}).get('enable_billing_sync_button', False)
            backend.update_global_settings(_gs)
        elif action == 'manual_billing_sync':
            _pages = int(data.get('pages', 5))
            _bd = globals().get('_billing_daemon')
            if _bd is None:
                return jsonify({"status": "error", "message": "billing daemon not initialized"})
            _matched = _bd.manual_sync(_pages)
            return jsonify({"status": "ok", "matched": _matched})
        elif action == 'edit_summary':
            backend.edit_summary(data['index'], data['summary'])
        elif action == 'edit_correction':
            backend.edit_correction(data['msg_index'], data['part_id'], data['content'])
        elif action == 'toggle_mode':
            backend.toggle_mode(data['index'], data['mode_type'], value=data.get('value'))
            return jsonify({"status": "ok"})
        elif action == 'omit_all_large':
            backend.omit_all_large()
        elif action == 'expand_all':
            backend.expand_all()
        elif action == 'toggle_thinking_visible':
            backend.toggle_thinking_visible()
        elif action == 'batch_context_manage':
            _batch_sid = data.get('client_sid')
            res = backend.batch_context_manage(data.get('filters', {}), data.get('batch_action', ''), sid=_batch_sid)
            return jsonify({'status': 'ok', 'count': res.get('count', 0), 'state': backend.get_full_state()})
        elif action == 'subagent_send':
            from api.provider_routes import get_subagent_providers
            providers = get_subagent_providers()
            pidx = data.get('provider_idx', 0)
            if 0 <= pidx < len(providers):
                res = backend.subagent_send(data['subagent_id'], providers[pidx])
                return jsonify(res)
            return jsonify({"error": "Invalid provider index"}), 400
        elif action == 'subagent_adopt':
            backend.subagent_adopt(data['subagent_id'], data['response_msg_id'])
        elif action == 'toggle_pause':
            backend.toggle_pause()
        elif action == 'toggle_star':
            backend.toggle_star(data['index'])
        elif action == 'remove_starred':
            backend.remove_starred(data['index'])
        elif action == 'ping':
            if backend.socketio:
                backend.socketio.emit('state_update', backend.get_full_state())
            return jsonify({"status": "ok"})
        elif action == 'manage_queue':
            backend.manage_queue(data['manage_action'], data['index'], data.get('text'))
        elif action == 'get_session_data':
            sid = data.get('sid')
            if sid in backend.sessions:
                return jsonify({"status": "ok", "session_data": backend.sessions[sid]})
            return jsonify({"status": "error", "message": "Session not found"})
        elif action == 'fetch_bubble_content':
            _fbc_mid = data.get('message_id')
            _fbc_sid = data.get('client_sid') or client_sid
            _fbc_sess = backend.sessions.get(_fbc_sid)
            if _fbc_sess:
                _fbc_skip = frozenset(('cc_content', '_cached_payload'))
                for _fbc_m in _fbc_sess.get('conversation_history', []):
                    if _fbc_m.get('id') == _fbc_mid:
                        _fbc_clean = {k: v for k, v in _fbc_m.items() if k not in _fbc_skip}
                        # Partial read filtering: apply visible_lines filter to tool_result content on demand
                        _fbc_pr_enabled = backend.global_settings.get('enable_partial_read', False)
                        if _fbc_pr_enabled and _fbc_m.get('is_tool_result') and _fbc_clean.get('content'):
                            _fbc_pr_file = _fbc_m.get('_read_file_path') or _fbc_m.get('auto_read_file')
                            if _fbc_pr_file:
                                _fbc_pr_state = _fbc_sess.get('_partial_read_state', {}).get(_fbc_pr_file)
                                if _fbc_pr_state:
                                    _fbc_vl = _fbc_pr_state.get('visible_lines', [])
                                    if isinstance(_fbc_vl, list):
                                        _fbc_vl = set(_fbc_vl)
                                    if _fbc_vl:
                                        _fbc_c = _fbc_clean['content']
                                        _fbc_in_cb = False
                                        _fbc_code = []
                                        for _fbc_l in _fbc_c.split('\n'):
                                            if _fbc_l.startswith('```') and not _fbc_in_cb:
                                                _fbc_in_cb = True
                                                continue
                                            elif _fbc_l.startswith('```') and _fbc_in_cb:
                                                _fbc_in_cb = False
                                                continue
                                            if _fbc_in_cb:
                                                _fbc_code.append(_fbc_l)
                                        if _fbc_code:
                                            _fbc_max_ln = 0
                                            for _fbc_cl in _fbc_code:
                                                _tp = _fbc_cl.find('\t')
                                                if _tp > 0:
                                                    try:
                                                        _ln = int(_fbc_cl[:_tp])
                                                        if _ln > _fbc_max_ln:
                                                            _fbc_max_ln = _ln
                                                    except ValueError:
                                                        pass
                                            if _fbc_max_ln > 0:
                                                _fbc_vl.update(range(1, min(11, _fbc_max_ln + 1)))
                                                _fbc_vl.update(range(max(1, _fbc_max_ln - 9), _fbc_max_ln + 1))
                                            _fbc_filt = []
                                            _fbc_gap = False
                                            for _fbc_cl in _fbc_code:
                                                _tp = _fbc_cl.find('\t')
                                                _ln = 0
                                                if _tp > 0:
                                                    try:
                                                        _ln = int(_fbc_cl[:_tp])
                                                    except ValueError:
                                                        pass
                                                if _ln > 0 and _ln in _fbc_vl:
                                                    if _fbc_gap:
                                                        _fbc_filt.append('')
                                                        _fbc_filt.append('...')
                                                        _fbc_filt.append('')
                                                        _fbc_gap = False
                                                    _fbc_filt.append(_fbc_cl)
                                                else:
                                                    if not _fbc_gap:
                                                        _fbc_gap = True
                                            _hdr_end = _fbc_c.find('```\n')
                                            if _hdr_end >= 0:
                                                _hdr = _fbc_c[:_hdr_end + 4]
                                                _ftr_start = _fbc_c.rfind('\n```')
                                                _ftr = _fbc_c[_ftr_start:] if _ftr_start > _hdr_end else '\n```'
                                                _fbc_clean['content'] = _hdr + '\n'.join(_fbc_filt) + _ftr
                        return jsonify({"status": "ok", "message": _fbc_clean})
            return jsonify({"status": "error", "message": "消息不存在"})
        elif action == 'get_clipboard':
            return jsonify({"status": "ok", "text": backend.get_message_content(data['index'])})
        elif action == 'get_annotation':
            return jsonify({"status": "ok", "text": backend.get_annotation_content(data['index'])})
        elif action == 'edit_annotation':
            backend.edit_annotation(data['index'], data['content'])
        elif action == 'get_payload':
            return jsonify({"status": "ok", "text": backend.get_payload(data['index'])})
        elif action == 'rate_message':
            backend.rate_message(data['index'], data['rating'])
            return jsonify({"status": "ok"})
        elif action == 'scan_files':
            return jsonify(backend.scan_files(data.get('paths', []), data.get('extensions', [])))
        elif action == 'update_code_config':
            backend.update_code_config(data.get('config', {}))
        elif action == 'get_code_context':
            text = backend.get_code_context(sid=data.get('client_sid'))
            return jsonify({"status": "ok", "text": text, "tokens": len(text)/3000})
        elif action == 'cc_accept_tool':
            backend.accept_tool(data.get('tool_json'), msg_index=data.get('index'), part_id=data.get('part_id'), is_retry=data.get('is_retry', False))
        elif action == 'cc_accept_all':
            backend.accept_all_tools(data.get('index', -1), target_sid=data.get('client_sid'))
        elif action == 'cc_reject_approval':
            _rej_idx = data.get('index')
            _rej_part_id = data.get('part_id')
            _rej_reason = data.get('reason', '')
            _rej_sid = data.get('client_sid')
            _rej_session = backend.sessions.get(_rej_sid)
            if _rej_session and _rej_idx is not None:
                _rej_idx = int(_rej_idx)
                if 0 <= _rej_idx < len(_rej_session.get('conversation_history', [])):
                    _rej_msg = _rej_session['conversation_history'][_rej_idx]
                    _rej_tool_use_id = None
                    for _rp in _rej_msg.get('content_parts', []):
                        if _rp.get('id') == _rej_part_id:
                            try:
                                _rtd = json.loads(_rp['content'])
                                _rej_tool_use_id = _rtd.get('id')
                            except Exception:
                                pass
                            break
                    if _rej_tool_use_id:
                        from api.tool_executors import ToolResult
                        _rej_content = f"用户拒绝了审批：{_rej_reason}" if _rej_reason else "用户拒绝了审批"
                        _rej_result = ToolResult(_rej_content, "审批被拒绝", is_error=True, part_status="rejected")
                        backend._create_tool_result_bubble(_rej_session, _rej_tool_use_id, _rej_result, _rej_idx, _rej_part_id)
                        backend._continue_autopilot_tool_queue(_rej_session, _rej_sid)
        elif action == 'cc_abort_tool':
            # Defaults to 'all', not 'one': before this change the endpoint had only
            # the batch meaning, so any caller omitting the field must keep it.
            # Silently downgrading would look like "I pressed abort and autopilot
            # kept running", which is far harder to diagnose than an error.
            _abt = backend.abort_tool(data.get('index'), data.get('part_id'),
                                      target_sid=data.get('client_sid'),
                                      scope=data.get('scope', 'all'))
            if not _abt or _abt.get('status') != 'ok':
                # 400, not a 200 carrying an error: postAction only raises a toast on a
                # non-OK response, so a 200 here would swallow the message entirely and
                # the user would see a greyed-out button and nothing else.
                return jsonify(_abt or {'status': 'error', 'message': '中止失败'}), 400
            _abt = dict(_abt)
            _abt['state'] = backend.get_full_state()
            return jsonify(_abt)
        elif action == 'cc_edit_message':
            backend.edit_cc_message(data['index'], data['content_json'])
        elif action == 'apply_block':
            res = backend.apply_block(data['block_text'], data.get('index'), data.get('part_id'))
            return jsonify(res)
        elif action == 'undo_code_block':
            backend.undo_code_block(data['index'], data['part_id'])
            return jsonify({"status": "ok"})
        elif action == 'update_block_status':
            backend.update_block_status(data['index'], data['part_id'], data['status'])
            return jsonify({"status": "ok"})
        elif action == 'restart_server':
            try:
                import py_compile, glob
                errors = []
                for py_file in glob.glob("**/*.py", recursive=True):
                    if any(skip in py_file for skip in ["venv", ".venv", ".git"]): continue
                    try: py_compile.compile(py_file, doraise=True)
                    except py_compile.PyCompileError as ce: errors.append(str(ce))
                if errors:
                    return jsonify({"status": "error", "message": "语法检查失败:\n" + "\n".join(errors)})
                return jsonify({"status": "ok", "message": "语法检查通过。请在终端中 Ctrl+C 后重新运行 python app.py 来重启。"})
            except Exception as e:
                return jsonify({"status": "error", "message": f"语法检查异常: {e}"})
        elif action == 'create_session_group':
            import uuid as _sg_uuid
            _sg_id = str(_sg_uuid.uuid4())[:8]
            backend.session_groups[_sg_id] = {
                'name': data.get('name', '新分组'),
                'session_ids': data.get('session_ids', []),
                'order': time.time(),
                'collapsed': False
            }
            backend.save_sessions(push_update=True)
            return jsonify({'status': 'ok', 'group_id': _sg_id})
        elif action == 'update_session_group':
            _sg_id = data.get('group_id')
            if _sg_id in backend.session_groups:
                _sg = backend.session_groups[_sg_id]
                if 'name' in data: _sg['name'] = data['name']
                if 'session_ids' in data: _sg['session_ids'] = data['session_ids']
                if 'collapsed' in data: _sg['collapsed'] = data['collapsed']
                if 'order' in data: _sg['order'] = data['order']
                backend.save_sessions(push_update=True)
            return jsonify({'status': 'ok'})
        elif action == 'delete_session_group':
            _sg_id = data.get('group_id')
            if _sg_id in backend.session_groups:
                del backend.session_groups[_sg_id]
                backend.save_sessions(push_update=True)
            return jsonify({'status': 'ok'})
        elif action == 'add_session_to_group':
            _sg_id = data.get('group_id')
            _sid = data.get('sid')
            if _sg_id in backend.session_groups and _sid:
                # 从其他组中移除（一个会话只能属于一个组）
                for _g in backend.session_groups.values():
                    if _sid in _g.get('session_ids', []):
                        _g['session_ids'].remove(_sid)
                backend.session_groups[_sg_id].setdefault('session_ids', []).append(_sid)
                backend.save_sessions(push_update=True)
            return jsonify({'status': 'ok'})
        elif action == 'remove_session_from_group':
            _sg_id = data.get('group_id')
            _sid = data.get('sid')
            if _sg_id in backend.session_groups and _sid:
                _sids = backend.session_groups[_sg_id].get('session_ids', [])
                if _sid in _sids:
                    _sids.remove(_sid)
                backend.save_sessions(push_update=True)
            return jsonify({'status': 'ok'})
        elif action == 'save_kanban_presence':
            event = data.get('event')
            if event:
                if not hasattr(backend, '_kanban_presence_log'):
                    backend._kanban_presence_log = []
                backend._kanban_presence_log.append(event)
                # Keep last 500 entries
                if len(backend._kanban_presence_log) > 500:
                    backend._kanban_presence_log = backend._kanban_presence_log[-500:]
            return jsonify({"status": "ok"})
        elif action == 'get_kanban_data':
            presence_log = getattr(backend, '_kanban_presence_log', [])
            return jsonify({"status": "ok", "presence_log": presence_log})
        elif action == 'get_group_waterfall':
            # 返回瀑布流可视化所需的轻量数据
            _sg_id = data.get('group_id')
            _sg = backend.session_groups.get(_sg_id, {})
            _wf_data = []
            for _sid in _sg.get('session_ids', []):
                _sess = backend.sessions.get(_sid)
                if not _sess or _sess.get('soft_deleted'):
                    continue
                _bubbles = []
                for _m in _sess.get('conversation_history', []):
                    if (_m.get('role') in ('user', 'assistant') and not _m.get('is_hidden')
                        and not _m.get('is_tool_result') and not _m.get('is_auto_read')
                        and _m.get('cc_type') != 'thinking'
                        and not (_m.get('model_name', '') or '').endswith('(思考过程)')
                        and _m.get('created_at', 0) > 0):
                        _bubbles.append({
                            'id': _m.get('id'),
                            'role': _m.get('role'),
                            'summary': (_m.get('summary') or '')[:60],
                            'token_k': round(len(_m.get('content', '') or '') / 3000, 1),
                            'created_at': _m.get('created_at', 0)
                        })
                _wf_data.append({
                    'sid': _sid,
                    'name': _sess.get('name', ''),
                    'order': _sess.get('order', 0),
                    'bubble_count': len(_bubbles),
                    'bubbles': _bubbles
                })
            _wf_data.sort(key=lambda x: x['order'])
            return jsonify({'status': 'ok', 'waterfall': _wf_data, 'group_name': _sg.get('name', '')})



        elif action == 'get_providers':
            _gp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'providers.json')
            try:
                with open(_gp_path, 'r', encoding='utf-8') as _gp_f:
                    _gp_data = json.load(_gp_f)
                return jsonify({"status": "ok", "providers": _gp_data})
            except FileNotFoundError:
                return jsonify({"status": "ok", "providers": []})
            except Exception as _gp_e:
                return jsonify({"status": "error", "message": f"读取 providers.json 失败: {_gp_e}"})
        elif action == 'save_providers':
            _sp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'providers.json')
            _sp_data = data.get('providers', [])
            try:
                _sp_tmp = _sp_path + '.tmp'
                with open(_sp_tmp, 'w', encoding='utf-8') as _sp_f:
                    json.dump(_sp_data, _sp_f, ensure_ascii=False, indent=2)
                os.replace(_sp_tmp, _sp_path)
                from api.provider_routes import reload_providers
                reload_providers()
                return jsonify({"status": "ok", "state": backend.get_full_state()})
            except Exception as _sp_e:
                return jsonify({"status": "error", "message": f"保存失败: {_sp_e}"})
        elif action == 'test_provider':
            import requests as _tp_requests
            _tp_url = (data.get('api_url') or '').strip()
            _tp_key = (data.get('api_key') or '').strip()
            if not _tp_url:
                return jsonify({"reachable": False, "message": "URL 为空"})
            try:
                _tp_headers = {}
                if _tp_key:
                    _tp_headers['Authorization'] = f'Bearer {_tp_key}'
                _tp_base = _tp_url.replace('/chat/completions', '').replace('/messages', '').rstrip('/')
                _tp_test_url = _tp_base + '/models'
                _tp_resp = _tp_requests.get(_tp_test_url, headers=_tp_headers, timeout=10,
                                           proxies={'http': None, 'https': None})
                if _tp_resp.status_code == 200:
                    return jsonify({"reachable": True, "message": f"连接成功"})
                elif _tp_resp.status_code in (401, 403):
                    return jsonify({"reachable": True, "message": f"服务器可达，鉴权失败 (HTTP {_tp_resp.status_code})"})
                else:
                    return jsonify({"reachable": True, "message": f"服务器响应 HTTP {_tp_resp.status_code}"})
            except _tp_requests.exceptions.Timeout:
                return jsonify({"reachable": False, "message": "连接超时"})
            except _tp_requests.exceptions.ConnectionError:
                return jsonify({"reachable": False, "message": "无法连接到服务器"})
            except Exception as _tp_e:
                return jsonify({"reachable": False, "message": f"测试异常: {str(_tp_e)[:100]}"})
    except Exception as e:
        print("Action Error:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "ok", "state": backend.get_full_state()})

@app.route('/data/images/<path:filename>')
def serve_image(filename):
    """Serve multimodal images saved from CC tool results."""
    from flask import send_from_directory, abort
    img_dir = os.path.join(backend.data_dir, 'images')
    img_path = os.path.join(img_dir, filename)
    if not os.path.exists(img_path):
        abort(404)
    return send_from_directory(img_dir, filename)

# 注册进程退出钩子：Ctrl+C 或正常退出时强制同步写入会话数据，防止 Timer 延迟导致数据丢失
import atexit
import signal

def _shutdown_save():
    try:
        backend.force_save_now()
    except Exception as e:
        print(f"[ATEXIT] 退出保存异常: {e}", flush=True)

atexit.register(_shutdown_save)

def _sigint_handler(signum, frame):
    print("\n[SIGINT] 收到中断信号，正在保存会话...", flush=True)
    try:
        backend.force_save_now()
    except Exception:
        pass
    raise SystemExit(0)

signal.signal(signal.SIGINT, _sigint_handler)

if __name__ == '__main__':
    import argparse
    _parser = argparse.ArgumentParser()
    _parser.add_argument('--port', type=int, default=5001)
    _args = _parser.parse_args()

    print(f"Starting Flask-SocketIO server... Visit http://127.0.0.1:{_args.port}")
    socketio.run(app, host='0.0.0.0', port=_args.port, debug=False, allow_unsafe_werkzeug=True)
