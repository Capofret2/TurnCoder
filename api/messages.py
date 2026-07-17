"""Api mixin: Message sending and API thread management."""
import threading
import time



class MessageMixin:
    """Api mixin: Message sending and API thread management."""

    def add_image_message(self, image_data, mime_type="image/png"):
        """添加一个图片气泡到当前会话。image_data 是纯 base64 字符串，保存为磁盘文件。"""
        if self._require_real_session():
            return
        import os, base64
        img_dir = os.path.join(getattr(self, 'data_dir', 'data'), 'images')
        os.makedirs(img_dir, exist_ok=True)
        ext_map = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}
        ext = ext_map.get(mime_type, '.png')
        fname = f"user_upload_{self._next_id()}{ext}"
        with open(os.path.join(img_dir, fname), 'wb') as f:
            f.write(base64.b64decode(image_data))
        session = self._session
        new_msg = self._make_msg("user", "[用户上传了一张图片]", summary="图片",
                                      image={"path": fname, "mime_type": mime_type})

        session['conversation_history'].append(new_msg)
        self.save_sessions()


    def add_only_message(self, text, role='user'):
        if self._require_real_session():
            return
        session = self._session
        new_msg = self._make_msg(role, text, summary="尚未概括",
                                      is_collapsed=(len(text) / 3) > 5000)
        # For assistant messages, parse content_parts so tool calls are functional
        if role == 'assistant' and hasattr(self, '_parse_content_parts'):
            parts = self._parse_content_parts(new_msg)
            if parts:
                new_msg['content_parts'] = parts
        session['conversation_history'].append(new_msg)
        # 未闭合工具调用检测：存储为 assistant 消息的元数据字段
        if role == 'assistant':
            from .response import _detect_unclosed_tools
            _unclosed = _detect_unclosed_tools(text)
            if _unclosed:
                _warn_parts = []
                for _uc_name, _uc_desc in _unclosed:
                    _warn_parts.append(f'[{_uc_name}开始]{_uc_desc}')
                new_msg['_unclosed_warning'] = '<system-reminder>\n警告：检测到未闭合的工具调用开始：\n' + '\n'.join(_warn_parts) + '\n</system-reminder>'
        self.save_sessions()


    def send_message(self, text, models=None, is_early=False, max_steps=1, is_parallel=False, is_offline=False, is_deep_think=False, sid=None):
        if sid is None:
            if self._require_real_session():
                return
            sid = self._active_sid
        if not models: models = [self.config.get("MODEL_NAME", "")]
        session = self.sessions[sid]
        settings = getattr(self, 'global_settings', {})
        if not settings.get('enable_steps', True):
            max_steps = 1
        session['max_steps'] = max_steps
        session['current_chain_steps'] = 0

        new_msg = None
        if text.strip() != "":
            new_msg = self._make_msg("user", text,
                summary="正在生成...",
                target_models=models)


        is_multi = is_parallel or len(models) > 1
        is_queue_enabled = getattr(self, 'global_settings', {}).get('enable_queue', True)
        if is_queue_enabled and not is_multi and not is_early and not is_offline and (session['is_processing'] or session['is_paused']):
            if new_msg:
                if is_deep_think:
                    new_msg['_deep_think'] = is_deep_think
                session['message_queue'].append(new_msg)
            else:
                _q_msg = self._make_msg("user", "", summary="空白触发",
                                   target_models=models)
                if is_deep_think:
                    _q_msg['_deep_think'] = is_deep_think
                session['message_queue'].append(_q_msg)

            self.save_sessions()
            return

        if new_msg:
            new_msg['is_collapsed'] = (len(text) / 3) > 5000
            session['conversation_history'].append(new_msg)
        self.save_sessions()

        if is_multi:
            session['is_processing'] = True
            for model_name in models:
                steps = max_steps if is_parallel else 1
                for i in range(steps):
                    session['active_threads'] += 1
                    target_id = self._next_id()
                    display_name = "离线" if is_offline else model_name
                    _now_p = time.time()
                    session['conversation_history'].append({"id": target_id, "role": "assistant", "content": "", "summary": "等待中", "start_time": _now_p, "created_at": _now_p, "is_omitted": False, "is_collapsed": False, "model_name": display_name, "_autopilot_gen": session.get('_autopilot_gen', 0)})
                    self.payloads[target_id] = "系统正在后台进行重度文件 I/O 扫描与多分支并行组装，尚未生成最终 Payload，请等待数秒后再次尝试复制..."

                    threading.Thread(target=self.api_worker_thread, args=(sid, i+1, steps, True, target_id, is_offline, model_name, is_deep_think)).start()
            self.save_sessions()
        else:
            self.start_api_thread(sid, is_offline=is_offline, model_name=models[0], is_deep_think=is_deep_think)


    def start_api_thread(self, sid=None, target_id=None, is_offline=False, model_name=None, is_deep_think=False):
        if sid is None: sid = self._active_sid
        session = self.sessions[sid]
        session['is_processing'] = True
        display_name = "离线" if is_offline else (model_name or self.config.get("MODEL_NAME", ""))
        if target_id is None:
            target_id = self._next_id()
            _now = time.time()
            session['conversation_history'].append({"id": target_id, "role": "assistant", "content": "", "summary": "等待中", "start_time": _now, "created_at": _now, "is_omitted": False, "is_collapsed": False, "model_name": display_name, "_autopilot_gen": session.get('_autopilot_gen', 0)})
            self.payloads[target_id] = "系统正在后台进行重度文件 I/O 扫描与上下文组装，尚未生成最终 Payload，请等待数秒后再次尝试复制..."

            self.save_sessions()
        threading.Thread(target=self.api_worker_thread, args=(sid, None, None, False, target_id, is_offline, model_name, is_deep_think)).start()


