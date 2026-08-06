"""Api mixin: Session CRUD operations and persistence."""
import copy
import json
import os
import random
import time
import uuid



class SessionMixin:
    """Api mixin: Session CRUD operations and persistence."""

    def load_sessions(self):
        self.global_settings = {
            'enable_correction': False, 'enable_queue': False, 'enable_steps': False, 'enable_starred': True,
            'enable_autopilot': True, 'enable_deep_think_ui': False, 'enable_pure_mode': False,
            'enable_arc3': False, 'enable_stream': True, 'auto_hide_env_obs': False,
            # 这两个键必须是 True，与前端 applySettingsUI 的非开发者模式强制值及下面
            # _dev_only_defaults 表中的取值一致。初值任何一处不同步，就会在「从未保存过
            # 设置、data/global.json 不存在」的窗口期里让模型拿不到工具定义注入。
            'enable_anthropic_protocol': True, 'enable_tool_inject': True, 'starred_messages': [],
            'developer_mode': False,
            'enable_bulk_logging': False,
            'enable_tool_lower_bound': False,
            'enable_descriptor_tool_calls': True,
            'enable_webfetch_file_mode': True,
            'enable_custom_websearch': True,
            'enable_custom_webfetch': True,
            'enable_custom_webfetch_jina': True,
            'enable_webfetch_headless': True,
            'enable_bottom_tabs': False,
            # 新思维链气泡的默认可见性。刻意放在这里而不是 create_session 的初值里：
            # 会话自己的 thinking_visible 一旦被显式切换过就以会话值为准，没切换过的
            # 会话在 worker_engine 的消费点回落到这一项。因此改这个开关对既有会话立即
            # 生效。若「顺手」改成建会话时写死初值，症状是改了开关对现有会话毫无反应，
            # 而那看起来像是没保存成功。
            'default_thinking_visible': True,
            # 默认关闭不是保守起见：申请审批会让托管停下来等人，而托管的全部价值是无人
            # 值守连续推进几十步。给不给模型这个能力属于用户决定，不该由模型自行判断。
            'enable_approval_tool': False
        }

        # 兼容并迁移旧版单一文件
        if os.path.exists("sessions.json"):
            try:
                with open("sessions.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    migrated_sessions = data.get("sessions", {})
                    for s in migrated_sessions.values():
                        for m in s.get('conversation_history', []):
                            m.pop('payload', None)
                    
                    with open(os.path.join(self.data_dir, "global.json"), "w", encoding="utf-8") as gf:
                        json.dump({
                            "current_session_id": data.get("current_session_id"),
                            "global_id_counter": data.get("global_id_counter", 1),
                            "model_stats": data.get("model_stats", {}),
                            "global_settings": data.get("global_settings", self.global_settings)
                        }, gf, ensure_ascii=False, indent=2)
                        
                    for sid, s_data in migrated_sessions.items():
                        with open(os.path.join(self.sessions_dir, f"{sid}.json"), "w", encoding="utf-8") as sf:
                            json.dump(s_data, sf, ensure_ascii=False, indent=2)
                            
                os.rename("sessions.json", "sessions.json.bak")
            except Exception as e:
                print("迁移旧版 sessions.json 失败:", e)

        global_path = os.path.join(self.data_dir, "global.json")
        if os.path.exists(global_path):
            try:
                with open(global_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "global_settings" in data:
                        self.global_settings.update(data["global_settings"])
                    # 加载会话组
                    if "session_groups" in data:
                        self.session_groups = data["session_groups"]
                    # 迁移旧设置键名到新键名
                    # 只剩一条不是残缺，enable_cc_simulate → enable_tool_simulate 那一
                    # 半随该开关删除。它是活代码：持有旧键的 global.json 会被它 pop 出
                    # 旧键并写入新键，于是一个已经没有任何消费者的键在每次启动时复活。
                    _key_migrations = {'enable_cc_inject': 'enable_tool_inject'}
                    for _old_k, _new_k in _key_migrations.items():
                        if _old_k in self.global_settings:
                            self.global_settings[_new_k] = self.global_settings.pop(_old_k)
                    # 开发者模式关闭时，强制开发者专属开关恢复默认值
                    if not self.global_settings.get('developer_mode', False):
                        _dev_only_defaults = {
                            'enable_correction': False, 'enable_queue': False, 'enable_steps': False,
                            'enable_reverse_context': False,
                            'enable_arc3': False, 'auto_hide_env_obs': False,
                            'enable_deep_think_ui': True,
                            'enable_autopilot': True,
                            'enable_anthropic_protocol': True, 'enable_tool_inject': True,
                'enable_tool_lower_bound': False,
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
                    self.global_id_counter = data.get("global_id_counter", 1)
                    self.model_stats = data.get("model_stats", {})
                    self.spending = data.get("spending", {"total": 0, "by_model": {}})
            except Exception:
                pass

        self.sessions = {}
        self._failed_session_files = set()  # 记录加载失败的文件，防止清理步骤误删
        if os.path.exists(self.sessions_dir):
            for fname in os.listdir(self.sessions_dir):
                if fname.endswith(".json") and not fname.endswith('.tmp'):
                    sid = fname[:-5]
                    try:
                        with open(os.path.join(self.sessions_dir, fname), "r", encoding="utf-8") as f:
                            s_data = json.load(f)
                            for m in s_data.get('conversation_history', []):
                                m.pop('payload', None)
                            s_data.setdefault('code_config', {})
                            s_data.setdefault('bound_cc_id', None)            # 兼容旧数据：使用文件修改时间作为初始排序基准
                            s_data.setdefault('order', os.path.getmtime(os.path.join(self.sessions_dir, fname)))
                            self.sessions[sid] = s_data
                    except Exception as _le:
                        self._failed_session_files.add(fname)
                        print(f"[LOAD WARNING] 会话文件 {fname} 加载失败（可能因上次异常退出损坏）: {_le}", flush=True)

        max_id_found = 0
        for s in self.sessions.values():
            s.setdefault('code_config', {})
            s.setdefault('bound_cc_id', None)
            s.setdefault('spending', {"total": 0, "by_model": {}})
            s.setdefault('_theme_hue', random.randint(0, 359))
            s.setdefault('_deep_think_level', 0)
            s.setdefault('_selected_models', [])
            s.setdefault('_draft_text', '')
            for m in s.get('conversation_history', []):
                if isinstance(m.get('id'), int) and m['id'] > max_id_found:
                    max_id_found = m['id']
            for q in s.get('message_queue', []):
                if isinstance(q.get('id'), int) and q['id'] > max_id_found:
                    max_id_found = q['id']
            for d_id in s.get('deleted_msg_ids', []):
                if isinstance(d_id, int) and d_id > max_id_found:
                    max_id_found = d_id
                    
        if getattr(self, 'global_id_counter', 1) <= max_id_found:
            self.global_id_counter = max_id_found + 1

        self.code_config = {}

        # 清除僵尸活跃状态：重启后没有工作线程在运行，所有 is_processing/active_threads 都是上次异常退出遗留的
        # 不清除会导致 get_full_state 每次都包含这些会话的完整历史，payload 膨胀到 100+MB
        _zombie_count = 0
        for _sid, _sess in self.sessions.items():
            if _sess.get('is_processing') or _sess.get('active_threads', 0) > 0:
                _sess['is_processing'] = False
                _sess['active_threads'] = 0
                _zombie_count += 1
        if _zombie_count > 0:
            print(f'[STARTUP] Cleared {_zombie_count} zombie is_processing/active_threads flags', flush=True)

        # === One-time migration v1: cc_tool_use → tool_use_part ===
        # Old versions stored tool content_parts with type "cc_tool_use".
        # Current code expects "tool_use_part". This migration runs once on first
        # startup after update, converting all old parts and persisting to disk.
        # Version flag stored in global_settings (persisted by save_sessions automatically).
        if self.global_settings.get('_migration_version', 0) < 1:
            _migrated_sids = []
            _migrated_count = 0
            for _sid, _sess in self.sessions.items():
                _any_changed = False
                for _msg in _sess.get('conversation_history', []):
                    for _part in _msg.get('content_parts', []):
                        if _part.get('type') == 'cc_tool_use':
                            _part['type'] = 'tool_use_part'
                            _any_changed = True
                            _migrated_count += 1
                            # Old format lacks tool_name/tool_id/tool_input fields
                            # Extract from content JSON for accept_all_tools and ordering engine
                            if 'tool_name' not in _part or 'tool_id' not in _part or 'tool_input' not in _part:
                                try:
                                    _tc = json.loads(_part.get('content', '{}'))
                                    _part.setdefault('tool_name', _tc.get('name', 'unknown'))
                                    _part.setdefault('tool_id', _tc.get('id', ''))
                                    _part.setdefault('tool_input', _tc.get('input', {}))
                                except (json.JSONDecodeError, ValueError, TypeError):
                                    _part.setdefault('tool_name', 'unknown')
                                    _part.setdefault('tool_id', '')
                                    _part.setdefault('tool_input', {})
                if _any_changed:
                    _migrated_sids.append(_sid)
            # Persist migrated sessions to disk immediately (atomic writes)
            if _migrated_sids:
                for _msid in _migrated_sids:
                    _sp = os.path.join(self.sessions_dir, f"{_msid}.json")
                    _st = _sp + '.tmp'
                    with open(_st, "w", encoding="utf-8") as _mf:
                        json.dump(self.sessions[_msid], _mf, ensure_ascii=False, indent=2,
                                 default=lambda o: list(o) if isinstance(o, set) else str(o))
                    os.replace(_st, _sp)
                print(f'[MIGRATION v1] cc_tool_use -> tool_use_part: {_migrated_count} parts in {len(_migrated_sids)} sessions', flush=True)
            else:
                print('[MIGRATION v1] No cc_tool_use parts found, marked as complete', flush=True)
            # Set version in global_settings — automatically persisted by save_sessions
            self.global_settings['_migration_version'] = 1
            # Also write global.json directly for immediate persistence
            # (save_sessions Timer may not have fired yet at this point)
            _gp = os.path.join(self.data_dir, "global.json")
            try:
                with open(_gp, "r", encoding="utf-8") as _gf:
                    _gd = json.load(_gf)
            except Exception:
                _gd = {}
            if 'global_settings' not in _gd:
                _gd['global_settings'] = {}
            _gd['global_settings']['_migration_version'] = 1
            _gt = _gp + '.tmp'
            with open(_gt, "w", encoding="utf-8") as _gf:
                json.dump(_gd, _gf, ensure_ascii=False, indent=2,
                         default=lambda o: list(o) if isinstance(o, set) else str(o))
            os.replace(_gt, _gp)


    def create_session(self, name="新对话"):
        sid = str(uuid.uuid4())
        import copy
        self.sessions[sid] = {
            "name": f"{name} {len(self.sessions)+1}",
            "order": time.time(),
            "conversation_history": [],
            "message_queue": [],
            "is_paused": False,
            "is_processing": False,
            "active_threads": 0,
            "current_chain_steps": 0,
            "max_steps": 1,
            "autopilot_active": False,
            "autopilot_turns_left": 0,
            "autopilot_model": None,
            "code_config": copy.deepcopy(getattr(self, 'code_config', {})),
            "code_backups": {},
            "deleted_msg_ids": [],
            "bound_cc_id": None,
            "spending": {"total": 0, "by_model": {}},
            "_theme_hue": random.randint(0, 359),
            "_deep_think_level": self.sessions.get(self._active_sid, {}).get('_deep_think_level', 0),
            "_selected_models": list(self.sessions.get(self._active_sid, {}).get('_selected_models', [])),
            "_draft_text": ""
        }
        self.save_sessions(push_update=False)
        return sid


    def switch_session(self, sid):
        """No-op: session routing is per-request via _tls.active_sid."""
        pass


    def delete_session(self, sid):
        if sid == "starred_session_virtual":
            return
        if sid in self.sessions and len(self.sessions) > 1:
            # 从所有会话组中移除该会话
            for _g in getattr(self, 'session_groups', {}).values():
                if sid in _g.get('session_ids', []):
                    _g['session_ids'].remove(sid)
            # Soft delete: mark as hidden in frontend, session stays fully functional in backend
            self.sessions[sid]['soft_deleted'] = True
            # Timestamped so the recycle bin can order by recency. A bin sorted any
            # other way buries the one entry the user is most likely reaching for —
            # the session they deleted by mistake a moment ago.
            self.sessions[sid]['deleted_at'] = time.time()
            self.save_sessions(push_update=True)


    def restore_session(self, sid):
        """Bring a session back out of the recycle bin.

        Group membership is deliberately not restored: delete_session strips the
        sid from every group and records nothing about which one it came from, so
        any guess here would be wrong some of the time. Dragging it back is one
        gesture, and a wrong guess costs two.
        """
        if sid == "starred_session_virtual" or sid not in self.sessions:
            return
        self.sessions[sid].pop('soft_deleted', None)
        self.sessions[sid].pop('deleted_at', None)
        self.save_sessions(push_update=True)

    # No purge counterpart, by design. Dropping a sid from self.sessions is what
    # makes save_sessions' orphan sweep unlink the file, so such a method would be
    # the only path in the application that destroys a transcript. Those are user
    # assets; reclaiming the space is done outside the app.

    def rename_session(self, sid, new_name):
        if sid == "starred_session_virtual":
            return
        if sid in self.sessions and new_name.strip():
            self.sessions[sid]['name'] = new_name.strip()
            self.save_sessions()


    def duplicate_session(self, sid):
        if sid == "starred_session_virtual":
            return
        if sid in self.sessions:
            import copy
            new_sid = str(uuid.uuid4())
            new_session = copy.deepcopy(self.sessions[sid])
            new_session['name'] = f"{new_session['name']} (副本)"
            new_session['order'] = time.time()
            new_session['is_processing'] = False
            new_session['active_threads'] = 0
            new_session['code_backups'] = {} # 复制会话时不保留文件备份
            new_session['bound_cc_id'] = None  # 复制会话时清除 CC 绑定
            self.sessions[new_sid] = new_session
            # 复制文件订阅关系：新会话继承原会话的所有文件监控
            for _fp, _entry in getattr(self, 'file_read_registry', {}).items():
                if isinstance(_entry, dict) and sid in _entry.get('sessions', set()):
                    _entry['sessions'].add(new_sid)
            self.save_sessions()
            return new_sid


    def archive_session(self, sid):
        """归档会话：从侧边栏隐藏但保留数据，可恢复。"""
        if sid == 'starred_session_virtual' or sid not in self.sessions:
            return
        self.sessions[sid]['is_archived'] = True
        self.save_sessions(push_update=True)

    def unarchive_session(self, sid):
        """取消归档：恢复到侧边栏。"""
        if sid in self.sessions:
            self.sessions[sid].pop('is_archived', None)
            self.save_sessions(push_update=True)

    def reorder_session(self, sid, direction=None, new_order=None):
        if sid == "starred_session_virtual" or sid not in self.sessions:
            return
        if new_order is not None:
            self.sessions[sid]['order'] = new_order
            self.save_sessions()
            return
        sids = sorted(self.sessions.keys(), key=lambda k: self.sessions[k].get('order', 0))
        idx = sids.index(sid)
        if direction == 'up' and idx > 0:
            prev_sid = sids[idx - 1]
            self.sessions[sid]['order'], self.sessions[prev_sid]['order'] = self.sessions[prev_sid].get('order', 0), self.sessions[sid].get('order', 0)
            self.save_sessions()
        elif direction == 'down' and idx < len(sids) - 1:
            next_sid = sids[idx + 1]
            self.sessions[sid]['order'], self.sessions[next_sid]['order'] = self.sessions[next_sid].get('order', 0), self.sessions[sid].get('order', 0)
            self.save_sessions()


