"""Api mixin: Autopilot/auto-pilot mode for continuous generation."""
from .provider_routes import strip_composite


class AutopilotMixin:
    """Api mixin: Autopilot/auto-pilot mode for continuous generation."""

    def _apply_sliding_window(self, session):
        # 仅在包含 ARC3 环境的双模型托管模式下应用滑动窗口，纯大模型托管保留无限上下文
        if not session.get('autopilot_env_model'): 
            return
            
        max_k = session.get('autopilot_max_k')
        if not max_k: return
        
        total_chars = 0
        non_hidden_indices = []
        for i, m in enumerate(session['conversation_history']):
            if m.get('is_hidden'): continue
            non_hidden_indices.append(i)
            c_len = len(m.get('diff_content') or m.get('content') or "")
            if m.get('role') == 'user' and (c_len / 3) < 300:
                c_len *= 3
            total_chars += c_len
            
        current_k = total_chars / 3000.0
        if current_k > max_k:
            for idx in non_hidden_indices:
                if current_k <= max_k:
                    break
                m = session['conversation_history'][idx]
                
                # 按照约束，滑动窗口清理时直接豁免环境组件生成的任何气泡
                if strip_composite(m.get('model_name', '')).startswith('[ARC3]'):
                    continue
                    
                m['is_hidden'] = True
                c_len = len(m.get('diff_content') or m.get('content') or "")
                if m.get('role') == 'user' and (c_len / 3) < 300:
                    c_len *= 3
                current_k -= (c_len / 3000.0)


    def start_autopilot(self, text, models, turns, max_k=8.0, is_deep_think=False):
        import time as _time
        self._session['_autopilot_started_at'] = _time.time()
        if self._require_real_session():
            return
        session = self._session
            
        llm_model = next((m for m in models if not strip_composite(m).startswith('[ARC3]')), None)
        env_model = next((m for m in models if strip_composite(m).startswith('[ARC3]')), None)
            
        session['autopilot_active'] = True
        session['_autopilot_gen'] = session.get('_autopilot_gen', 0) + 1
        session['autopilot_turns_left'] = turns
        session['autopilot_total_steps'] = 0
        session['autopilot_max_k'] = float(max_k)
        session['autopilot_model'] = llm_model or env_model or self.config.get("MODEL_NAME", "")
        session['autopilot_env_model'] = env_model
        session['_deep_think_active'] = is_deep_think

        self.save_sessions()
        
        if text.strip() != "":
            # 修复：只使用单一的 autopilot_model，避免多模型导致 is_multi=True 而跳过托管续接逻辑
            self.send_message(text, [session['autopilot_model']], is_early=False, max_steps=1, is_parallel=False, is_offline=False, is_deep_think=is_deep_think)
        else:
            # 判断起点是否处于 ARC3 语境
            if env_model:
                new_msg = self._make_msg("user", "", summary="拉取初始环境状态", is_hidden=True)
                session['conversation_history'].append(new_msg)
                self.save_sessions()
                # 绑定了环境模型时，第一棒优先交给环境去产生观测画面
                self.start_api_thread(self._active_sid, model_name=env_model)
                return

            is_arc3 = getattr(self, 'global_settings', {}).get('enable_arc3', False)
            if is_arc3:
                new_msg = self._make_msg("user", "", summary="托管自动推进", is_hidden=True)
                session['conversation_history'].append(new_msg)
                self.save_sessions()
                self.start_api_thread(self._active_sid, model_name=session['autopilot_model'])
            else:
                auto_text = self._make_autopilot_prompt(session)
                self.send_message(auto_text, [session['autopilot_model']], is_early=True, is_deep_think=is_deep_think)


    def update_autopilot_turns(self, turns):
        """在托管运行期间更新剩余步数。0 等同于停止托管，负数或非数字忽略。"""
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if not session.get('autopilot_active'):
            return
        if not isinstance(turns, int) or turns < 0:
            return
        session['autopilot_turns_left'] = turns
        if turns == 0:
            session['autopilot_active'] = False
        self.save_sessions()

    def stop_autopilot(self):
        import time as _time
        _started = self._session.pop('_autopilot_started_at', None)
        if _started:
            self._session.setdefault('_autopilot_history', []).append({'started': _started, 'ended': _time.time()})
            if len(self._session['_autopilot_history']) > 50:
                self._session['_autopilot_history'] = self._session['_autopilot_history'][-50:]
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        session['autopilot_active'] = False
        session['autopilot_turns_left'] = 0
        session['_deep_think_active'] = False
        session['trigger_autopilot'] = False
        self.save_sessions()
        # Notify parent if this is a child session whose autopilot just ended
        self._notify_child_ended(self._active_sid)

    def _notify_child_ended(self, sid):
        """If sid is a child session with a waiting parent, signal the parent to unblock."""
        if not hasattr(self, '_child_session_events'):
            return
        if sid not in self._child_session_events:
            return
        session = self.sessions.get(sid, {})
        entry = self._child_session_events[sid]
        if not entry['event'].is_set():
            entry['result'] = {
                'child_sid': sid,
                'steps': session.get('autopilot_total_steps', 0)
            }
            entry['event'].set()
            print(f'[CHILD SESSION] Auto-end signal for {sid[:8]} (autopilot stopped)', flush=True)


    def _add_trigger(self, sid, fire_time):
        """Add a single trigger that fires at the given datetime. Pure in-memory."""
        import threading, uuid, datetime
        if not hasattr(self, '_triggers'):
            self._triggers = []
        now = datetime.datetime.now()
        delay = max(0, (fire_time - now).total_seconds())
        trigger_id = str(uuid.uuid4())[:8]
        timer = threading.Timer(delay, self._fire_trigger, args=[sid])
        timer.daemon = True
        timer.start()
        self._triggers.append({'id': trigger_id, 'sid': sid, 'fire_time': fire_time, 'timer': timer})
        print(f'[TRIGGER] Scheduled {trigger_id} for session {sid[:8]} at {fire_time.strftime("%H:%M:%S")} ({delay/60:.1f} min)', flush=True)
        return trigger_id

    def _fire_trigger(self, sid):
        """Called when a trigger timer fires. Starts autopilot-like execution."""
        session = self.sessions.get(sid)
        if not session:
            return
        if session.get('is_processing') or session.get('autopilot_active'):
            print(f'[TRIGGER] Session {sid[:8]} is busy, skipping', flush=True)
            return
        # Get model from frontend-synced selection
        _models = session.get('_selected_models', [])
        from .provider_routes import strip_composite
        _model = next((m for m in _models if not strip_composite(m).startswith('[ARC3]')), None)
        if not _model:
            _model = _models[0] if _models else self.config.get('MODEL_NAME', '')
        session['trigger_autopilot'] = True
        session['_trigger_retries_left'] = 3
        session['autopilot_active'] = True
        session['_autopilot_gen'] = session.get('_autopilot_gen', 0) + 1
        session['autopilot_turns_left'] = 999
        session['autopilot_model'] = _model
        session['autopilot_env_model'] = None
        session['_deep_think_active'] = False
        trigger_prompt = self._make_trigger_prompt(session)
        new_msg = self._make_msg("user", trigger_prompt, summary="触发器自动触发")
        session['conversation_history'].append(new_msg)
        self.save_sessions()
        self.start_api_thread(sid, model_name=_model)
        print(f'[TRIGGER] Fired for session {sid[:8]}, model={_model}', flush=True)

    def _make_trigger_prompt(self, session):
        """Generate the prompt sent when a trigger fires."""
        return "[系统触发器已触发。用户不在线，请自行判断接下来的任务并继续。]"

    def _handle_trigger_completion(self, sid):
        """Called when a trigger cycle completes."""
        session = self.sessions.get(sid)
        if not session:
            return
        session['trigger_autopilot'] = False
        session['_trigger_retries_left'] = 0
        print(f'[TRIGGER] Cycle complete for session {sid[:8]}', flush=True)

    def _reschedule_triggers(self):
        """Clean up stale trigger flags after server restart. Triggers are not persisted."""
        if not hasattr(self, '_triggers'):
            self._triggers = []
        for sid, session in self.sessions.items():
            if session.get('trigger_autopilot'):
                session['trigger_autopilot'] = False
                session['autopilot_active'] = False
                session['autopilot_turns_left'] = 0
            session.pop('trigger_config', None)  # Clean up legacy field



