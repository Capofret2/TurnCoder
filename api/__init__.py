"""ChatApp API core - assembles all mixins into the main Api class."""
import json
import os
import time
import queue
import threading

# 线程本地存储：每个 HTTP 请求线程有自己的 active_sid，互不覆盖
_tls = threading.local()

from .terminal import TerminalProcess  # noqa: F401
from .anthropic_helpers import AnthropicMixin
from .arc3 import Arc3Mixin
from .autopilot import AutopilotMixin
from .tool_accept import ToolAcceptMixin


from .code_helpers import CodeHelpersMixin
from .code_ops import CodeOpsMixin
from .context import ContextMixin
from .message_edit import MessageEditMixin
from .message_toggle import MessageToggleMixin
from .messages import MessageMixin
from .response import ResponseMixin
from .response_helpers import ResponseHelpersMixin
from .sessions import SessionMixin
from .state import StateMixin
from .subagent import SubagentMixin
from .terminal import TerminalMixin
from .worker import WorkerMixin
from .worker_engine import WorkerEngineMixin

SUBAGENT_MAGIC = "Jxjxhi?yla,xfcy"


class Api(AnthropicMixin, Arc3Mixin, AutopilotMixin, ToolAcceptMixin, CodeHelpersMixin, CodeOpsMixin, ContextMixin, MessageEditMixin, MessageToggleMixin, MessageMixin, ResponseMixin, ResponseHelpersMixin, SessionMixin, StateMixin, SubagentMixin, TerminalMixin, WorkerMixin, WorkerEngineMixin):
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.sessions = {}
        self.config = {}
        self.global_id_counter = 1
        self.payloads = {}
        self.model_stats = {}
        self.code_config = {}
        self.terminals = {}
        self.term_output_buffer = {}
        self._lock = threading.RLock()  # Thread-safety for session data serialization
        self.cc_tool_queues = {}     # cc_session_id → queue.Queue()
        self.cc_connections = {}     # cc_session_id → {active, model, msg_count, pending_tool, last_seen, is_direct}
        self.subagent_queues = {}    # subagent_id → queue.Queue()
        self.subagent_responses = {} # response_msg_id → raw_sse_string
        self.file_read_registry = {}  # file_path → {'sessions': set(sids), 'last_access': timestamp} — 跨会话文件读取引用追踪
        self._running_processes = {}  # tool_use_id → subprocess.Popen — 正在执行的 Bash 子进程注册表，供 abort_tool 真正杀进程
        self.session_groups = {}  # group_id → {'name': str, 'session_ids': [], 'order': float, 'collapsed': bool}
        self._child_session_events = {}  # child_sid → {'event': Event, 'parent_sid': str, 'result': dict}
        self.global_settings = {
            'enable_correction': False, 'enable_queue': False, 'enable_steps': False, 'enable_starred': False,
            'enable_autopilot': True, 'enable_deep_think_ui': False, 'enable_pure_mode': False,
            'enable_arc3': False, 'enable_stream': True, 'force_no_stream': False, 'auto_hide_env_obs': False,
            'enable_anthropic_protocol': True, 'enable_tool_inject': True, 'enable_descriptor_tool_calls': True, 'enable_reverse_context': False, 'enable_routing_token': False, 'starred_messages': [],
            'developer_mode': False,
            'enable_bulk_logging': False,
            'enable_webfetch_file_mode': True,
            'enable_custom_websearch': True,
            'enable_custom_webfetch': True,
            'enable_custom_webfetch_jina': True,
            'enable_webfetch_headless': True,
            'webfetch_proxy': '',
            'enable_show_all_autoread': False,
            'enable_thinking_retry': False,
            'websearch_total_timeout_s': 300,
            'webfetch_total_timeout_s': 600
        }
        self.spending = {"total": 0, "by_model": {}}

        self.data_dir = os.environ.get('CHATAPP_DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        self.sessions_dir = os.path.join(self.data_dir, "sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)

        self.load_sessions()
        if not self.sessions:
            self.create_session("\u9ed8\u8ba4\u5bf9\u8bdd")

        # 提示词的实际路径在每次读取时解析：data/ 下的用户覆写优先于随仓库分发的
        # prompts/ 默认值。见 ContextMixin._resolve_prompt_path。
        self.cached_prompt = ""
        self.prompt_mtime = 0
        self._prompt_src = None

        # 回填历史气泡的 created_at 时间戳（仅对 user/assistant 按 600 秒间隔递增，其他类型跟随前一个主气泡的时间）
        for _sid, _sess in self.sessions.items():
            _hist = _sess.get('conversation_history', [])
            _needs_backfill = any(not m.get('created_at') for m in _hist)
            if _needs_backfill and _hist:
                _main_count = sum(1 for m in _hist if m.get('role') in ('user', 'assistant'))
                _base_time = _sess.get('order', time.time() - _main_count * 30)
                _current_time = _base_time
                for _m in _hist:
                    if not _m.get('created_at'):
                        if _m.get('role') in ('user', 'assistant'):
                            _current_time += 30
                        _m['created_at'] = _current_time

        # 数据迁移：将旧代码生成的 autopilot 续接「继续」用户消息内容清为空字符串
        # 旧版 _make_autopilot_prompt 返回 "继续"，新版返回 ""，此迁移对齐旧数据
        for _sid, _sess in self.sessions.items():
            for _m in _sess.get('conversation_history', []):
                if _m.get('role') == 'user' and _m.get('content', '').strip() == '\u7ee7\u7eed':
                    _m['content'] = ''

        # 从磁盘恢复跨会话文件读取引用注册表
        self._load_file_registry()



        # 冷启动：仅在 registry 为空时扫描历史（首次启用该功能时执行一次，之后从磁盘加载即可）
        _cold_start_count = 0
        if not self.file_read_registry:
            for _sid, _sess in self.sessions.items():
                for _m in _sess.get('conversation_history', []):
                    _ar_file = _m.get('auto_read_file')
                    if _ar_file and not _m.get('is_outdated_read') and not _m.get('is_hidden'):
                        _entry = self.file_read_registry.setdefault(_ar_file, {'sessions': set(), 'last_access': 0})
                        if isinstance(_entry, set):
                            _entry = {'sessions': _entry, 'last_access': time.time()}
                            self.file_read_registry[_ar_file] = _entry
                        if _sid not in _entry['sessions']:
                            _entry['sessions'].add(_sid)
                            _cold_start_count += 1
            if _cold_start_count > 0:
                print(f'[FILE REGISTRY] Cold start: registered {_cold_start_count} new file-session subscriptions from history', flush=True)
                self._save_file_registry()

    @property
    def _active_sid(self):
        """当前请求线程的目标会话标识。线程安全，并发请求互不干扰。"""
        return getattr(_tls, 'active_sid', None)

    @property
    def _session(self):
        """Shorthand for the current session dict. Uses thread-local active_sid for thread safety."""
        return self.sessions.get(self._active_sid, {})

    def _next_id(self):
        """Generate next unique message ID. Centralizes all ID generation."""
        val = self.global_id_counter
        self.global_id_counter += 1
        return val

    def _require_real_session(self):
        """Guard: return True if current session is virtual (caller should return early)."""
        if self._active_sid == "starred_session_virtual":
            if self.socketio:
                self.socketio.emit('show_toast', {'message': '\u65e0\u6cd5\u5728\u5168\u5c40\u6536\u85cf\u4e2d\u64cd\u4f5c', 'type': 'error'})
            return True
        return False

    def _make_msg(self, role, content, summary="", is_collapsed=False, msg_id=None, **extra):
        """创建标准化消息字典。替代了原先散布在各模块中的35+处重复字典字面量构造。

        Args:
            role: 'user' | 'assistant' | 'system'
            content: 消息正文内容
            summary: 概括文本（用于概括模式）
            is_collapsed: 是否在UI上默认折叠
            msg_id: 自定义ID，默认使用_next_id()自动生成
            **extra: 额外字段（如content_parts, model, tool_use_part等）
        """
        msg = {
            "id": msg_id if msg_id is not None else self._next_id(),
            "role": role, "content": content,
            "summary": summary, "is_omitted": False, "is_collapsed": is_collapsed,
            "created_at": time.time(),
        }
        msg.update(extra)
        return msg

    def _build_tool_use_map(self, session):
        """Return tool_use_id -> (tool_name, file_path) for every tool part in a session."""
        _tui = {}
        for _m in session.get('conversation_history', []):
            for _p in (_m.get('content_parts') or []):
                if _p.get('type') != 'tool_use_part':
                    continue
                _tid = _p.get('tool_id') or ''
                _tname = _p.get('tool_name') or ''
                _tinput = _p.get('tool_input') or {}
                if not _tid and _p.get('content'):
                    try:
                        _td = json.loads(_p['content'])
                        _tid = _td.get('id') or _tid
                        _tname = _td.get('name') or _tname
                        _tinput = _td.get('input') or _tinput
                    except Exception:
                        pass
                if _tid:
                    _fp = _tinput.get('file_path', '') if isinstance(_tinput, dict) else ''
                    _tui[_tid] = (_tname, _fp)
        return _tui

    def _mark_old_reads_outdated(self, session, file_path):
        """Mark every non-outdated read result for file_path as outdated.

        This replaces five near-identical in-place loops spread across
        tool_executors and tool_accept. The marker text mirrors the old bubbles'
        content so context assembly sees exactly the same replacement.
        """
        _tui = self._build_tool_use_map(session)
        _marker = f"（已省略，概括为：{os.path.basename(file_path)} 的旧版本读取结果，已被更新的读取替代）"
        for _m in session.get('conversation_history', []):
            if not _m.get('is_tool_result') or _m.get('is_outdated_read'):
                continue
            if _m.get('is_auto_read') and _m.get('auto_read_file') == file_path:
                _m['is_outdated_read'] = True
                _m['content'] = _marker
            elif not _m.get('is_auto_read'):
                _info = _tui.get(_m.get('tool_use_id', ''))
                if _info and _info[0] == 'Read' and _info[1] == file_path:
                    _m['is_outdated_read'] = True
                    _m['content'] = _marker

    def _make_autopilot_prompt(self, session, append=""):
        """Build autopilot continuation prompt. Consolidates identical code from response.py and cc_core.py."""
        step = session.get('autopilot_total_steps', 0) + 1
        session['autopilot_total_steps'] = step
        # 空字符串基础文本，避免 AI 将自动续接误解为用户同意
        return "" + (f"\n\n{append}" if append else "")

    def send_message(self, text, models=None, is_early=False, max_steps=1, is_parallel=False, is_offline=False, is_deep_think=False, sid=None):
        """Override to inject file change detection before message processing.

        Checks all registered files for external modifications (sed -i, echo >, cp, etc.)
        and injects autoread bubbles BEFORE the message is processed or queued.
        This ensures the detection runs synchronously in the HTTP request context,
        regardless of whether api_worker_thread is subsequently called.
        """
        _check_sid = sid or self._active_sid
        if _check_sid and _check_sid != 'starred_session_virtual':
            try:
                self._check_registered_file_changes(_check_sid)
            except Exception:
                pass
        return super().send_message(text, models, is_early, max_steps, is_parallel, is_offline, is_deep_think, sid)

