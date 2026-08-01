"""Api mixin: State assembly, save/load, and WebSocket push."""
import copy
import json
import os
import threading
import time

# 全局 JSON 序列化安全补丁：set → list
# 防止 session 数据中的 set 类型字段导致 jsonify/socketio.emit 崩溃
_original_json_default = json.JSONEncoder.default
def _json_set_safe_default(self, obj):
    if isinstance(obj, set):
        return list(obj)
    return _original_json_default(self, obj)
json.JSONEncoder.default = _json_set_safe_default



class StateMixin:
    """Api mixin: State assembly, save/load, and WebSocket push."""

    def get_full_state(self):
        """组装并返回当前对前端有用的完整状态（含多模态脱水）。"""
        version_stamp = getattr(self, 'state_version', time.time())
        opt_sessions = {sid: {"name": s["name"], "order": s.get("order", 0), "soft_deleted": s.get("soft_deleted", False), "is_archived": s.get("is_archived", False), "_theme_hue": s.get("_theme_hue")} for sid, s in self.sessions.items()}
        opt_sessions["starred_session_virtual"] = {"name": "全局收藏", "order": float('-999999')}
        
        if self._active_sid == "starred_session_virtual":
            starred = self.global_settings.get("starred_messages", [])
            history = []
            for i, sm in enumerate(starred):
                history.append({
                    "id": i + 9990000,
                    "role": sm.get("role", "user"),
                    "content": sm.get("content", ""),
                    "summary": "全局收藏",
                    "is_omitted": False,
                    "is_collapsed": False
                })
            opt_sessions["starred_session_virtual"] = {
                "name": "全局收藏",
                "conversation_history": history,
                "message_queue": [],
                "is_paused": False,
                "is_processing": False,
                "active_threads": 0,
                "current_chain_steps": 0,
                "max_steps": 1,
                "code_config": {}
            }
        elif self._active_sid in self.sessions:
            # 对于当前会话以及所有活跃处理中的会话，发送完整的会话数据以支持多窗口并行查阅
            # 引入余晖机制 (Afterglow)：若会话在刚结束生成的 5 秒内，强制包含全量数据，防止瞬间状态切换导致的精简包覆盖导致前端漏渲
            current_time = time.time()

            _active = self._active_sid
            for s_id, sess in self.sessions.items():
                is_active = sess.get('is_processing') or sess.get('active_threads', 0) > 0 or sess.get('message_queue')
                is_afterglow = (current_time - sess.get('last_updated', 0)) < 5.0
                if s_id == _active or is_active or is_afterglow:
                    # 轻量拷贝：避免 copy.deepcopy 的 50MB 临时分配，只浅拷贝需要的字段
                    _SKIP_MSG_KEYS = frozenset(('cc_content', '_cached_payload'))
                    _sess_copy = {k: (v if k != 'conversation_history' else None) for k, v in sess.items()}
                    _sess_copy['code_backups'] = {}  # 不推送文件备份内容
                    # 安全转换：set 类型字段无法被 JSON 序列化，转为 list
                    for _sk in list(_sess_copy.keys()):
                        if isinstance(_sess_copy[_sk], set):
                            _sess_copy[_sk] = list(_sess_copy[_sk])
                    # 部分读入：动态过滤 tool_result 消息的 content，实现热插拔
                    _pr_state_data = sess.get('_partial_read_state', {})
                    _pr_enabled = getattr(self, 'global_settings', {}).get('enable_partial_read', False)
                    _sess_copy.pop('_partial_read_state', None)  # 内部状态，前端不需要
                    # 同上。中止名单只被 accept_tool 的 is_retry 分支和执行器线程读取，
                    # 前端无消费者。它与 _approved_tool_queue 不同：后者随工具执行完毕
                    # 自然收缩，前者单调增长到 200 上限后长期满载（约 5KB），而托管期间
                    # 状态推送是高频的。pop 而非 del：绝大多数会话没有这个键。
                    _sess_copy.pop('_aborted_tool_ids', None)
                    _DEHYDRATE_STRIP_KEYS = frozenset(('content', 'content_parts', 'multimodal_blocks', 'thinking', 'diff_content', 'cc_content', '_cached_payload'))
                    _lightweight_hist = []
                    for _m in sess.get('conversation_history', []):
                        if _m.get('is_omitted'):
                            _mc = {
                                'id': _m.get('id'),
                                'role': _m.get('role', 'user'),
                                'content': '',
                                'summary': _m.get('summary', ''),
                                'is_omitted': True,
                                'is_collapsed': _m.get('is_collapsed', False),
                                'is_hidden': _m.get('is_hidden', False),
                                'is_unread': _m.get('is_unread', False),
                                'rating': _m.get('rating'),
                                'model_name': _m.get('model_name', ''),
                                'timing': _m.get('timing'),
                                'created_at': _m.get('created_at', 0),
                            }
                        elif _m.get('is_tool_result'):
                            if _m.get('is_auto_read'):
                                # Autoread: dehydrate content but preserve routing metadata
                                # Frontend needs these fields to identify and attach below Edit blocks
                                _mc = {
                                    'id': _m.get('id'),
                                    'role': _m.get('role', 'user'),
                                    'summary': _m.get('summary', ''),
                                    'is_tool_result': True,
                                    'is_hidden': _m.get('is_hidden', False),
                                    'tool_use_id': _m.get('tool_use_id', ''),
                                    'created_at': _m.get('created_at', 0),
                                    '_content_len': _m.get('_content_len') or len((_m.get('content') or '')),
                                    'execution_time_s': _m.get('execution_time_s'),
                                    '_dehydrated': True,
                                    'is_auto_read': True,
                                    'auto_read_trigger_id': _m.get('auto_read_trigger_id'),
                                    'auto_read_file': _m.get('auto_read_file'),
                                    'is_outdated_read': _m.get('is_outdated_read', False),
                                }
                            else:
                                # Dehydrate non-read tool_result for performance (content fetched on demand)
                                _mc = {
                                    'id': _m.get('id'),
                                    'role': _m.get('role', 'user'),
                                    'summary': _m.get('summary', ''),
                                    'is_tool_result': True,
                                    'is_hidden': _m.get('is_hidden', False),
                                    'tool_use_id': _m.get('tool_use_id', ''),
                                    'created_at': _m.get('created_at', 0),
                                    '_content_len': _m.get('_content_len') or len((_m.get('content') or '')),
                                    'execution_time_s': _m.get('execution_time_s'),
                                    '_dehydrated': True,
                                }
                        elif _m.get('is_hidden') or (_m.get('is_collapsed') and not (_m.get('model_name', '').endswith('\u601d\u8003\u8fc7\u7a0b)') or _m.get('cc_type') == 'thinking')):
                            _mc = {
                                'id': _m.get('id'),
                                'role': _m.get('role', 'user'),
                                'summary': _m.get('summary', ''),
                                'is_collapsed': _m.get('is_collapsed', False),
                                'is_omitted': _m.get('is_omitted', False),
                                'is_hidden': _m.get('is_hidden', False),
                                'is_tool_result': _m.get('is_tool_result', False),
                                'is_auto_read': _m.get('is_auto_read', False),
                                'is_outdated_read': _m.get('is_outdated_read', False),
                                'created_at': _m.get('created_at', 0),
                                'tool_use_id': _m.get('tool_use_id', ''),
                                'model_name': _m.get('model_name', ''),
                                '_dehydrated': True,
                            }

                        else:
                            _mc = {k: v for k, v in _m.items() if k not in _SKIP_MSG_KEYS}
                            # Strip residual base64 data from multimodal_blocks (keep file paths for rendering)
                            if _mc.get('multimodal_blocks'):
                                _stripped = []
                                for _blk in _mc['multimodal_blocks']:
                                    if isinstance(_blk, dict) and isinstance(_blk.get('source'), dict) and 'data' in _blk['source']:
                                        _stripped.append({**_blk, 'source': {_sk: _sv for _sk, _sv in _blk['source'].items() if _sk != 'data'}})
                                    else:
                                        _stripped.append(_blk)
                                _mc['multimodal_blocks'] = _stripped
                        _mc['_content_len'] = len((_m.get('content', '') or ''))
                        _mc['_has_image'] = bool(_m.get('image') or _m.get('multimodal_blocks'))
                        _lightweight_hist.append(_mc)
                    _sess_copy['conversation_history'] = _lightweight_hist
                    opt_sessions[s_id] = _sess_copy
        
        from .provider_routes import get_all_models, get_subagent_providers, get_model_providers
        return {
            "sessions": opt_sessions,
            "state_version": version_stamp,
            "current_session_id": self._active_sid,
            "available_models": get_all_models(),
            "model_providers": get_model_providers(),
            "subagent_providers": [{"name": p["name"], "index": i} for i, p in enumerate(get_subagent_providers())],
            "spending": self.sessions[self._active_sid].get('spending', {"total": 0, "by_model": {}}) if self._active_sid in self.sessions else {"total": 0, "by_model": {}},
            "global_spending": getattr(self, 'spending', {"total": 0, "by_model": {}}),
            "model_stats": self.model_stats,
            "session_groups": getattr(self, 'session_groups', {}),
            "global_settings": getattr(self, 'global_settings', {
                'enable_correction': False, 'enable_queue': False, 'enable_steps': False, 'enable_starred': False,
                'enable_autopilot': True, 'enable_deep_think_ui': False, 'enable_pure_mode': False,
                'enable_arc3': False, 'enable_stream': True, 'auto_hide_env_obs': False, 
                'enable_anthropic_protocol': False, 'enable_tool_inject': False, 'starred_messages': [],
                'enable_bulk_logging': False,
                'enable_tool_lower_bound': False,
                'enable_tool_simulate': False,
                'enable_webfetch_file_mode': True,
                'enable_custom_websearch': True,
                'enable_custom_webfetch': True,
                'enable_custom_webfetch_jina': True,
                'enable_webfetch_headless': True,
                'enable_bottom_tabs': False,
                'enable_tool_description_enforcement': False,
                'enable_truncation_detection': False,
                'enable_partial_read': False,
                'enable_auto_update': False
            })
        }


    def update_global_settings(self, settings):
        _old_sf = getattr(self, 'global_settings', {}).get('enable_style_filter', False)
        self.global_settings = settings
        # 开发者模式关闭时，强制开发者专属开关恢复默认值
        if not settings.get('developer_mode', False):
            _dev_only_defaults = {
                'enable_correction': False, 'enable_queue': False, 'enable_steps': False,
                'enable_reverse_context': False,
                'enable_arc3': False, 'auto_hide_env_obs': False,
                'enable_deep_think_ui': True,
                'enable_autopilot': True,
                'enable_anthropic_protocol': True, 'enable_tool_inject': True,
                'enable_tool_lower_bound': False,
                'enable_tool_simulate': True,
                'enable_webfetch_file_mode': True,
                'enable_custom_websearch': True,
                'enable_custom_webfetch': True,
                'enable_custom_webfetch_jina': True,
                'enable_webfetch_headless': True,
                'enable_routing_token': False,
                'enable_descriptor_tool_calls': True,
            }
            for _dk, _dv in _dev_only_defaults.items():
                self.global_settings[_dk] = _dv
        # 语言风格过滤器开关变化时，批量应用/还原所有会话的历史气泡
        _new_sf = self.global_settings.get('enable_style_filter', False)
        if _new_sf != _old_sf:
            from .style_filter import apply_style_filter
            for _sf_sid, _sf_sess in self.sessions.items():
                for _sf_msg in _sf_sess.get('conversation_history', []):
                    if _sf_msg.get('role') != 'assistant' or _sf_msg.get('cc_type') == 'thinking':
                        continue
                    if _new_sf and not _sf_msg.get('_style_filter_original'):
                        _sf_orig = {}
                        for _sf_p in _sf_msg.get('content_parts', []):
                            if _sf_p.get('type') == 'text':
                                _sf_new_t, _sf_ch = apply_style_filter(_sf_p['content'], _sf_msg['id'])
                                if _sf_ch:
                                    _sf_orig[_sf_p['id']] = _sf_p['content']
                                    _sf_p['content'] = _sf_new_t
                        if _sf_msg.get('summary') and _sf_msg['summary'] not in ('暂无', '纯净模式'):
                            _sf_new_s, _sf_ch_s = apply_style_filter(_sf_msg['summary'], _sf_msg['id'])
                            if _sf_ch_s:
                                _sf_orig['_summary'] = _sf_msg['summary']
                                _sf_msg['summary'] = _sf_new_s
                        if _sf_orig:
                            _sf_msg['_style_filter_original'] = _sf_orig
                    elif not _new_sf and _sf_msg.get('_style_filter_original'):
                        _sf_orig = _sf_msg['_style_filter_original']
                        for _sf_p in _sf_msg.get('content_parts', []):
                            if _sf_p.get('type') == 'text' and _sf_p.get('id') in _sf_orig:
                                _sf_p['content'] = _sf_orig[_sf_p['id']]
                        if '_summary' in _sf_orig:
                            _sf_msg['summary'] = _sf_orig['_summary']
                        del _sf_msg['_style_filter_original']
            self.save_sessions(push_update=True)
        else:
            self.save_sessions()


    def force_save_now(self):
        """强制立即同步写入所有会话数据到磁盘。用于进程退出前的紧急保存。"""
        import copy
        def _json_safe(obj):
            if isinstance(obj, set):
                return list(obj)
            return str(obj)
        try:
            if hasattr(self, '_save_timer') and self._save_timer.is_alive():
                self._save_timer.cancel()
            snap_global = {
                "global_id_counter": self.global_id_counter,
                "model_stats": copy.deepcopy(self.model_stats),
                "global_settings": copy.deepcopy(getattr(self, 'global_settings', {})),
                "spending": copy.deepcopy(getattr(self, 'spending', {})),
                "session_groups": copy.deepcopy(getattr(self, 'session_groups', {}))
            }
            import json
            _gp = os.path.join(self.data_dir, "global.json")
            _tmp = _gp + '.tmp'
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(snap_global, f, ensure_ascii=False, indent=2, default=_json_safe)
            os.replace(_tmp, _gp)
            for sid in list(self.sessions.keys()):
                try:
                    s_data = copy.deepcopy(self.sessions[sid])
                    # Strip _cached_payload from messages before disk write
                    for _m in s_data.get('conversation_history', []):
                        _m.pop('_cached_payload', None)
                    _sp = os.path.join(self.sessions_dir, f"{sid}.json")
                    _st = _sp + '.tmp'
                    with open(_st, "w", encoding="utf-8") as f:
                        json.dump(s_data, f, ensure_ascii=False, indent=2, default=_json_safe)
                    os.replace(_st, _sp)
                except Exception:
                    pass
            # 持久化文件读取引用注册表
            if hasattr(self, '_save_file_registry'):
                self._save_file_registry()
            print("[SHUTDOWN] 会话数据已强制同步写入磁盘", flush=True)
        except Exception as e:
            print(f"[SHUTDOWN] 强制保存失败: {e}", flush=True)

    def save_sessions(self, push_update=False):
        """
        极大缓解前端卡顿。
        """
        # 标记当前操作的会话为 dirty，_write 只保存 dirty 会话避免 800MB 全量 deepcopy
        _active = getattr(self, '_active_sid', None)
        if _active and _active in self.sessions:
            if not hasattr(self, '_dirty_sessions'):
                self._dirty_sessions = set()
            self._dirty_sessions.add(_active)

        # 更新全局的时间戳，作为防止幽灵倒退包的版本凭证
        if not hasattr(self, 'state_version'):
            self.state_version = 0
        self.state_version = time.time()
        
        # 全局推送节流：无论多少线程同时请求推送，最多每 0.35 秒向前端广播一次完整状态
        if push_update and self.socketio:
            now = time.time()
            last_push = getattr(self, '_last_ws_push', 0)
            if now - last_push >= 0.35:
                self._last_ws_push = now
                try:
                    _state = self.get_full_state()
                    self.socketio.emit('state_update', _state)
                except Exception as _push_err:
                    print(f"WebSocket push failed: {_push_err}")
            elif not getattr(self, '_ws_trailing_timer', None) or not self._ws_trailing_timer.is_alive():
                from . import _tls as _tls_ref
                _captured_sid = getattr(_tls_ref, 'active_sid', None)
                def _trailing_push():
                    # 恢复触发请求的 active_sid 到定时器线程
                    _tls_ref.active_sid = _captured_sid
                    self._last_ws_push = time.time()
                    self.state_version = time.time()
                    if self.socketio:
                        try:
                            self.socketio.emit('state_update', self.get_full_state())
                        except Exception as _push_err:
                            print(f"WebSocket trailing push failed: {_push_err}")
                self._ws_trailing_timer = threading.Timer(0.35, _trailing_push)
                self._ws_trailing_timer.start()

        if hasattr(self, '_save_timer') and self._save_timer.is_alive():
            self._save_timer.cancel()
        
        def _json_safe(obj):
            """Handle non-JSON-serializable types (set, etc.)."""
            if isinstance(obj, set):
                return list(obj)
            return str(obj)

        def _write():
            # 互斥锁防止多个 Timer 线程并发写入导致 .tmp 文件竞争
            if not hasattr(self, '_write_lock'):
                self._write_lock = threading.Lock()
            if not self._write_lock.acquire(blocking=False):
                return  # 另一个 _write 正在执行，跳过本次（下次 timer 会再写）
            try:
                # 只保存 dirty 会话，避免对 800MB+ 全量数据 deepcopy 导致 GIL 阻塞 15 秒
                _to_save = set()
                if hasattr(self, '_dirty_sessions'):
                    _to_save = self._dirty_sessions.copy()
                    self._dirty_sessions.clear()

                # Global.json 始终保存（很小，<10KB）
                snap_global = {
                    "global_id_counter": self.global_id_counter,
                    "model_stats": copy.deepcopy(self.model_stats),
                    "global_settings": copy.deepcopy(getattr(self, 'global_settings', {})),
                    "spending": copy.deepcopy(getattr(self, 'spending', {})),
                    "session_groups": copy.deepcopy(getattr(self, 'session_groups', {}))
                }

                # 只 deepcopy 被修改的会话
                snap_sessions = {}
                for sid in _to_save:
                    if sid in self.sessions:
                        try:
                            _snap = copy.deepcopy(self.sessions[sid])
                            # Strip _cached_payload from messages before disk write (pure memory cache)
                            for _m in _snap.get('conversation_history', []):
                                _m.pop('_cached_payload', None)
                            snap_sessions[sid] = _snap
                        except Exception:
                            pass

                # 原子写入 global.json
                _gp = os.path.join(self.data_dir, "global.json")
                _gt = _gp + '.tmp'
                with open(_gt, "w", encoding="utf-8") as f:
                    json.dump(snap_global, f, ensure_ascii=False, indent=2, default=_json_safe)
                os.replace(_gt, _gp)

                # 只写入被修改的会话文件
                for sid, s_data in snap_sessions.items():
                    _sp = os.path.join(self.sessions_dir, f"{sid}.json")
                    _st = _sp + '.tmp'
                    with open(_st, "w", encoding="utf-8") as f:
                        json.dump(s_data, f, ensure_ascii=False, indent=2, default=_json_safe)
                    os.replace(_st, _sp)

                # 持久化文件读取引用注册表
                if hasattr(self, '_save_file_registry'):
                    self._save_file_registry()

                # 清理孤儿会话文件：只删除确认不在 self.sessions 中且不在加载失败列表中的文件
                _protected = getattr(self, '_failed_session_files', set())
                existing_files = set(f"{sid}.json" for sid in self.sessions.keys())
                for fname in os.listdir(self.sessions_dir):
                    if fname.endswith(".json") and fname not in existing_files and fname not in _protected:
                        try:
                            os.remove(os.path.join(self.sessions_dir, fname))
                        except FileNotFoundError:
                            pass  # 另一个线程已删除
            except Exception as e:
                print("保存对话失败:", e)
                import traceback
                traceback.print_exc()
            finally:
                self._write_lock.release()
        
        self._save_timer = threading.Timer(0.5, _write) # 稍微缩短延迟
        self._save_timer.start()


