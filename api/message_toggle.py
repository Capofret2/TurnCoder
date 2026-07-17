"""Api mixin: Message toggling, rating, starring, queue management."""
import time



class MessageToggleMixin:
    """Api mixin: Message toggling, rating, starring, queue management."""

    def retry_message(self, index):
        if self._require_real_session():
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            if msg.get('role') != 'assistant':
                return
            
            if session.get('code_backups'):
                part_ids_in_msg = {p.get('id') for p in msg.get('content_parts', []) if p.get('type') == 'code'}
                for p_id in part_ids_in_msg:
                    session['code_backups'].pop(p_id, None)
                    
            msg['content'] = ""
            msg.pop('content_parts', None)
            msg['summary'] = "等待中"
            msg['is_error'] = False
            msg['start_time'] = time.time()
            msg.pop('rating', None)
            msg.pop('timing', None)

            # 清除与该消息相关的已采纳工具 ID，使重试时工具可以重新入队
            _accepted = session.get('_accepted_tool_use_ids', [])
            _msg_prefix = f"toolu_{msg['id']}_"
            session['_accepted_tool_use_ids'] = [tid for tid in _accepted if not tid.startswith(_msg_prefix)]
            
            self.payloads[msg['id']] = "系统正在重组重试上下文并准备发起新的请求，尚未完成 Payload 封装，请稍候..."
            self.save_sessions()
            self.start_api_thread(sid=self._active_sid, target_id=msg['id'], model_name=msg.get('model_name'))


    def delete_message(self, index):
        if self._active_sid == "starred_session_virtual":
            self.remove_starred(index)
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg_to_delete = session['conversation_history'][index]

            # 软删除保护机制：首次删除操作将其状态切换为隐藏，再次删除时才执行物理销毁
            if not msg_to_delete.get('is_hidden', False):
                msg_to_delete['is_hidden'] = True
                self.save_sessions(push_update=True)
                return

            # 清理与此消息关联的文件备份
            if session.get('code_backups') and msg_to_delete.get('role') == 'assistant':
                part_ids_in_msg = {p.get('id') for p in msg_to_delete.get('content_parts', []) if p.get('type') == 'code'}
                for p_id in part_ids_in_msg:
                    if p_id in session['code_backups']:
                        del session['code_backups'][p_id]
            
            session.setdefault('deleted_msg_ids', [])
            if msg_to_delete['id'] not in session['deleted_msg_ids']:
                session['deleted_msg_ids'].append(msg_to_delete['id'])
                if len(session['deleted_msg_ids']) > 500:
                    session['deleted_msg_ids'] = session['deleted_msg_ids'][-200:]
            
            # Actually delete the message from the list
            del session['conversation_history'][index]

            # Check if the deleted message was a pending assistant bubble
            is_pending_assistant = (
                msg_to_delete.get('role') == 'assistant' and
                not msg_to_delete.get('content') and
                'start_time' in msg_to_delete
            )

            if is_pending_assistant:
                # After deletion, check if any *other* pending assistant bubbles are left
                any_pending_left = any(
                    m.get('role') == 'assistant' and not m.get('content') and 'start_time' in m
                    for m in session['conversation_history']
                )
                
                # If no pending bubbles remain, we can safely reset the processing state
                if not any_pending_left:
                    session['is_processing'] = False
                    session['active_threads'] = 0
            
            self.save_sessions(push_update=True)


    def toggle_mode(self, index, mode_type, value=None):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            if mode_type == 'omit':
                field = 'is_omitted'
            elif mode_type == 'hide':
                field = 'is_hidden'
            else:
                field = 'is_collapsed'
            msg = session['conversation_history'][index]
            if value is not None:
                msg[field] = bool(value)
            else:
                msg[field] = not msg.get(field, False)
            if msg.get('is_unread'):
                msg['is_unread'] = False
            self.save_sessions(push_update=True)


    def omit_all_large(self):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if not session:
            return
        changed = False
        for m in session['conversation_history']:
            if m.get('is_hidden'):
                continue
            originalC = len(m.get('content', ''))
            if m.get('role') == 'user' and (originalC / 3) < 300:
                originalC *= 3
            originalK = originalC / 3000
            if originalK > 0.1 and not m.get('is_omitted'):
                m['is_omitted'] = True
                changed = True
                if changed:
                    self.save_sessions()


    def expand_all(self):
        """取消所有气泡的概括模式"""
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if not session:
            return
        changed = False
        for m in session['conversation_history']:
            if m.get('is_omitted'):
                m['is_omitted'] = False
                changed = True
        if changed:
            self.save_sessions()


    def toggle_thinking_visible(self):
        """切换思维链默认可见状态（仅影响新产生的思维链气泡，不回溯修改已有气泡）"""
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if not session:
            return
        current = session.get('thinking_visible', True)
        session['thinking_visible'] = not current
        self.save_sessions()

    def batch_context_manage(self, filters, batch_action, sid=None):
        """对匹配筛选条件的气泡执行批量操作（概括/展开/隐藏/取消隐藏）。"""
        _target_sid = sid or self._active_sid
        if _target_sid == 'starred_session_virtual':
            return {'count': 0}
        session = self.sessions.get(_target_sid, {})
        types = set(filters.get('types', []))
        _want_image = 'has_image' in types
        _filter_types = types - {'has_image'}
        id_min = filters.get('id_min')
        id_max = filters.get('id_max')
        size_min_k = filters.get('size_min_k', 0)
        size_max_k = filters.get('size_max_k')
        count = 0
        _purge_indices = []
        for _idx, m in enumerate(session.get('conversation_history', [])):
            is_thinking = (m.get('model_name', '').endswith('(思考过程)') or m.get('cc_type') == 'thinking')
            _content = m.get('content', '') or ''
            is_tool = (m.get('is_tool_result', False)
                       or bool(m.get('tool_use_id'))
                       or (m.get('role') == 'user' and (
                           _content.startswith('**Tool Result**') or _content.startswith('**Tool Error**')
                           or _content.startswith('**审稿 [') or _content.startswith('**规划 [')
                           or _content.startswith('**聚合规划 ['))))
            has_img = bool(m.get('image') or m.get('multimodal_blocks'))
            if is_thinking:
                mtype = 'thinking'
            elif m.get('role') == 'assistant':
                mtype = 'assistant'
            elif is_tool:
                mtype = 'tool_result'
            else:
                mtype = 'user'
            if types:
                type_match = (mtype in _filter_types) if _filter_types else False
                image_match = (_want_image and has_img)
                if not type_match and not image_match:
                    continue
            if id_min is not None and m.get('id', 0) < id_min:
                continue
            if id_max is not None and m.get('id', 0) > id_max:
                continue
            content_len = len(m.get('content', '') or '')
            if size_min_k > 0 and (content_len / 3000) < size_min_k:
                continue
            if size_max_k is not None and (content_len / 3000) > size_max_k:
                continue
            if batch_action == 'omit' and not m.get('is_omitted'):
                m['is_omitted'] = True
                count += 1
            elif batch_action == 'expand' and m.get('is_omitted'):
                m['is_omitted'] = False
                count += 1
            elif batch_action == 'hide' and not m.get('is_hidden'):
                m['is_hidden'] = True
                count += 1
            elif batch_action == 'unhide' and m.get('is_hidden'):
                m['is_hidden'] = False
                count += 1
            elif batch_action == 'purge':
                if m.get('is_hidden'):
                    _purge_indices.append(_idx)
                    count += 1
        # Execute purge: physically remove messages and clean up associated image files
        if _purge_indices:
            import os as _pos
            _img_dir = _pos.path.join(getattr(self, 'data_dir', 'data'), 'images')
            for _pi in reversed(_purge_indices):
                _pm = session['conversation_history'][_pi]
                # Clean up image file
                if _pm.get('image') and _pm['image'].get('path'):
                    _fp = _pos.path.join(_img_dir, _pm['image']['path'])
                    try:
                        if _pos.path.exists(_fp): _pos.remove(_fp)
                    except Exception:
                        pass
                # Clean up multimodal block files
                for _blk in (_pm.get('multimodal_blocks') or []):
                    _src = _blk.get('source', {})
                    if _src.get('path'):
                        _bp = _pos.path.join(_img_dir, _src['path'])
                        try:
                            if _pos.path.exists(_bp): _pos.remove(_bp)
                        except Exception:
                            pass
                del session['conversation_history'][_pi]
        # 内联思维链块级操作：当 thinking 类型被选中时，同时处理 assistant 消息中的内联思维链
        import re as _re_toggle
        _want_inline_thinking = 'thinking' in types
        if _want_inline_thinking and batch_action in ('hide', 'omit'):
            _it_pat = _re_toggle.compile(r'((?:^|\n)\[思考开始\]\n)([\s\S]*?)(\n\[思考结束\](?:\n|$))')
            for m in session.get('conversation_history', []):
                if m.get('role') != 'assistant':
                    continue
                if m.get('is_hidden'):
                    continue
                is_thinking_bubble = (m.get('model_name', '').endswith('(思考过程)') or m.get('cc_type') == 'thinking')
                if is_thinking_bubble:
                    continue
                _content = m.get('content', '') or ''
                if '[思考开始]' not in _content:
                    continue
                if id_min is not None and m.get('id', 0) < id_min:
                    continue
                if id_max is not None and m.get('id', 0) > id_max:
                    continue
                _originals = m.get('_inline_thinking_originals')
                if _originals:
                    continue
                _originals_map = {}
                _idx_counter = [0]
                def _replace_fn(match):
                    _idx_counter[0] += 1
                    _originals_map[str(_idx_counter[0])] = match.group(2)
                    return match.group(1) + '已隐藏' + match.group(3)
                _new_content = _it_pat.sub(_replace_fn, _content)
                if _new_content != _content:
                    m['_inline_thinking_originals'] = _originals_map
                    m['content'] = _new_content
                    if m.get('content_parts'):
                        _cp_hide_pat = _re_toggle.compile(r'((?:^|\n)\[思考开始\]\n)([\s\S]*?)(\n\[思考结束\](?:\n|$))')
                        for _cp in m['content_parts']:
                            if _cp.get('type') == 'text' and '[思考开始]' in (_cp.get('content', '') or ''):
                                _cp['content'] = _cp_hide_pat.sub(lambda mm: mm.group(1) + '已隐藏' + mm.group(3), _cp['content'])
                    count += 1
        elif _want_inline_thinking and batch_action in ('unhide', 'expand'):
            for m in session.get('conversation_history', []):
                if m.get('role') != 'assistant':
                    continue
                is_thinking_bubble = (m.get('model_name', '').endswith('(思考过程)') or m.get('cc_type') == 'thinking')
                if is_thinking_bubble:
                    continue
                _originals_map = m.get('_inline_thinking_originals')
                if not _originals_map:
                    continue
                if id_min is not None and m.get('id', 0) < id_min:
                    continue
                if id_max is not None and m.get('id', 0) > id_max:
                    continue
                _content = m.get('content', '') or ''
                _it_pat = _re_toggle.compile(r'((?:^|\n)\[思考开始\]\n)(已隐藏)(\n\[思考结束\](?:\n|$))')
                _restore_idx = [0]
                def _restore_fn(match):
                    _restore_idx[0] += 1
                    _key = str(_restore_idx[0])
                    return match.group(1) + _originals_map.get(_key, match.group(2)) + match.group(3)
                _restored = _it_pat.sub(_restore_fn, _content)
                m['content'] = _restored
                if m.get('content_parts'):
                    _cp_restore_idx = [0]
                    for _cp in m['content_parts']:
                        if _cp.get('type') == 'text' and '已隐藏' in (_cp.get('content', '') or '') and '[思考开始]' in (_cp.get('content', '') or ''):
                            _cp_restore_idx[0] = 0
                            def _cp_restore_fn(match):
                                _cp_restore_idx[0] += 1
                                _key = str(_cp_restore_idx[0])
                                return match.group(1) + _originals_map.get(_key, match.group(2)) + match.group(3)
                            _cp['content'] = _it_pat.sub(_cp_restore_fn, _cp['content'])
                del m['_inline_thinking_originals']
                count += 1
        if count > 0:
            self.save_sessions()
        return {'count': count}


    def toggle_star(self, index):
        if self._active_sid == "starred_session_virtual":
            self.remove_starred(index)
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            starred = self.global_settings.setdefault('starred_messages', [])
            exists = False
            for s in starred:
                if s.get('content') == msg.get('content') and s.get('role') == msg.get('role'):
                    starred.remove(s)
                    exists = True
                    break
            if not exists:
                entry = {"role": msg.get('role'), "content": msg.get('content')}
                if msg.get('diff_content'):
                    entry['diff_content'] = msg['diff_content']
                starred.append(entry)
            self.save_sessions()


    def remove_starred(self, index):
        starred = self.global_settings.get('starred_messages', [])
        if 0 <= index < len(starred):
            starred.pop(index)
            self.save_sessions()


    def rate_message(self, index, rating):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            if msg['role'] != 'assistant': return
            m_name = msg.get('model_name', '默认模型')
            if m_name not in self.model_stats:
                self.model_stats[m_name] = {"total": 0, "up": 0, "down": 0}
            old_rating = msg.get('rating')
            if old_rating == rating:
                self.model_stats[m_name][rating] -= 1
                msg['rating'] = None
            else:
                if old_rating in ['up', 'down']: self.model_stats[m_name][old_rating] -= 1
                self.model_stats[m_name][rating] += 1
                msg['rating'] = rating
            self.save_sessions()


    def toggle_pause(self):
        session = self._session
        session['is_paused'] = not session['is_paused']
        self.save_sessions()
        if not session['is_paused'] and not session['is_processing']: 
            self.finalize_process(self._active_sid)


    def manage_queue(self, action, index, new_text=None):
        session = self._session
        q = session['message_queue']
        if 0 <= index < len(q):
            if action == 'del': q.pop(index)
            elif action == 'up' and index > 0: q[index], q[index-1] = q[index-1], q[index]
            elif action == 'down' and index < len(q)-1: q[index], q[index+1] = q[index+1], q[index]
            elif action == 'edit': q[index]['content'] = new_text
            elif action == 'early':
                msg = q.pop(index)
                self.send_message(msg['content'], models=msg.get('target_models'), is_early=True, is_deep_think=msg.get('_deep_think', False))
        self.save_sessions()


