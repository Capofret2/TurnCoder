"""Api mixin: Subagent handling and CC message editing."""
import json
import os
import queue
import requests
import threading
import time
import uuid



class SubagentMixin:
    """Api mixin: Subagent handling and CC message editing."""

    def _handle_subagent_request(self, data, cc_session_id, model):
        """处理 subagent 请求：创建 UI 气泡，等待用户驱动的转发和采纳"""
        subagent_id = str(uuid.uuid4())[:12]
        _bound_sid = self._find_chatapp_for_cc(cc_session_id)
        target_sid = _bound_sid or self._active_sid
        if not target_sid or target_sid not in self.sessions:
            err_sse = self._construct_anthropic_sse([{"type": "text", "text": "Subagent error: no chatapp session bound"}], model)
            if isinstance(err_sse, str): yield err_sse.encode('utf-8')
            else: yield err_sse
            return
        session = self.sessions[target_sid]
        # 查找触发此 subagent 的父 Agent tool_use，建立双向关联
        parent_tool_id = None
        for _m in reversed(session.get('conversation_history', [])):
            if _m.get('role') == 'assistant' and _m.get('content_parts'):
                for _p in _m.get('content_parts', []):
                    if _p.get('type') == 'tool_use_part':
                        try:
                            _td = json.loads(_p['content'])
                            if _td.get('name') == 'Agent' and _td.get('id') and not _p.get('linked_subagent_id'):
                                parent_tool_id = _td['id']
                                _p['linked_subagent_id'] = subagent_id
                                break
                        except Exception:
                            pass
                if parent_tool_id:
                    break
        query_text = ""
        for msg in data.get("messages", []):
            c = msg.get("content", "")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text": query_text = b.get("text", "")
            elif isinstance(c, str): query_text = c
        tools_desc = ", ".join(t.get("name", t.get("type", "?")) for t in data.get("tools", []))
        bubble = self._make_msg("assistant",
            f"**Subagent 请求** (model: {model}, tools: {tools_desc})\n\n{query_text}",
            summary=f"Subagent: {query_text[:50]}", is_hidden=True,
            is_subagent_request=True, subagent_id=subagent_id,
            subagent_data=data, model_name=f"Subagent ({model})",
            subagent_parent_tool_id=parent_tool_id)
        session['conversation_history'].append(bubble)
        self.subagent_queues[subagent_id] = queue.Queue()
        self.save_sessions(push_update=True)
        # 托管模式下自动发送 subagent 请求
        print(f"[SUBAGENT AUTO-CHECK] autopilot_active={session.get('autopilot_active')}, target_sid={target_sid}, parent_tool_id={parent_tool_id}, subagent_id={subagent_id}", flush=True)
        if session.get('autopilot_active'):
            from .provider_routes import get_subagent_providers
            _providers = get_subagent_providers()
            print(f"[SUBAGENT AUTO-CHECK] providers_count={len(_providers)}, providers={[p.get('name','?') for p in _providers]}", flush=True)
            if _providers:
                threading.Thread(target=self._auto_subagent_forward,
                               args=(subagent_id, _providers[0], target_sid),
                               daemon=True).start()
        # 保存 subagent 请求到专用目录（受 enable_bulk_logging 开关控制）
        if getattr(self, 'global_settings', {}).get('enable_bulk_logging', False):
            try:
                _sa_dump_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'subagent_dumps')
                os.makedirs(_sa_dump_dir, exist_ok=True)
                import time as _sa_time
                _sa_ts = _sa_time.strftime('%Y%m%d_%H%M%S')
                _sa_dump = {'subagent_id': subagent_id, 'cc_session_id': cc_session_id, 'model': model, 'query': query_text, 'full_request': data}
                _sa_path = os.path.join(_sa_dump_dir, f'subagent_{_sa_ts}_{subagent_id}.json')
                with open(_sa_path, 'w', encoding='utf-8') as _sf:
                    json.dump(_sa_dump, _sf, ensure_ascii=False, indent=2)
                print(f'[SUBAGENT DUMP] Saved to {_sa_path}')
            except Exception as _se:
                print(f'[SUBAGENT DUMP ERROR] {_se}')
        yield b": subagent_waiting\n\n"
        print(f"[SUBAGENT] Created {subagent_id} for CC {cc_session_id}, query: {query_text[:60]}", flush=True)
        _cc_q = self.cc_tool_queues.get(cc_session_id)
        _hb_counter = 0
        try:
            while True:
                # 检查 subagent 队列（用户通过 subagent_send 转发的响应）
                try:
                    raw_sse = self.subagent_queues[subagent_id].get(timeout=0.5)
                    if isinstance(raw_sse, str): yield raw_sse.encode('utf-8')
                    elif isinstance(raw_sse, bytes): yield raw_sse
                    print(f"[SUBAGENT] Sent adopted response for {subagent_id}", flush=True)
                    return
                except queue.Empty:
                    pass
                # 同时检查 cc_tool_queues（用户在 UI 中直接采纳 tool_use 时推入此队列）
                if _cc_q:
                    try:
                        tool_block = _cc_q.get(timeout=0.5)
                        _conn = self.cc_connections.get(cc_session_id)
                        if _conn: _conn["pending_tool"] = tool_block
                        sse = self._construct_anthropic_sse([tool_block], model, stop_reason="tool_use")
                        if isinstance(sse, str): yield sse.encode("utf-8")
                        else: yield sse
                        if _conn: _conn["pending_tool"] = None
                        print(f"[SUBAGENT→CC] Sent tool_use via subagent channel: {tool_block.get('name', '?')}", flush=True)
                        return
                    except queue.Empty:
                        pass
                _hb_counter += 1
                if _hb_counter >= 3:
                    _hb_counter = 0
                    yield b": heartbeat\n\n"
                    _conn = self.cc_connections.get(cc_session_id)
                    if _conn: _conn["last_seen"] = time.time()
        finally:
            self.subagent_queues.pop(subagent_id, None)
            _conn = self.cc_connections.get(cc_session_id)
            if _conn:
                _conn["active"] = False
                _conn["last_seen"] = time.time()


    def subagent_send(self, subagent_id, provider_config, target_sid=None):
        """转发 subagent 请求到上游提供商并创建响应气泡"""
        if target_sid and target_sid in self.sessions:
            session = self.sessions[target_sid]
        else:
            session = self._session
        if not session: return {"error": "No session"}
        request_bubble = next((m for m in session['conversation_history'] if m.get('subagent_id') == subagent_id and m.get('is_subagent_request')), None)
        if not request_bubble: return {"error": "Subagent request not found"}
        response_id = self._next_id()
        _parent_tid = request_bubble.get('subagent_parent_tool_id')
        response_bubble = self._make_msg("assistant", "",
            summary=f"Subagent 响应中... ({provider_config['name']})",
            msg_id=response_id, start_time=time.time(), is_hidden=True,
            is_subagent_response=True, subagent_id=subagent_id,
            has_subagent_sse=False, model_name=f"Subagent ({provider_config['name']})",
            subagent_parent_tool_id=_parent_tid)
        session['conversation_history'].append(response_bubble)
        self.save_sessions(push_update=True)
        def _forward():
            data = request_bubble.get('subagent_data', {})
            try:
                resp = requests.post(
                    provider_config['url'],
                    headers={"Content-Type": "application/json", "x-api-key": provider_config['api_key'],
                             "anthropic-version": "2023-06-01", "anthropic-beta": "interleaved-thinking-2025-05-14"},
                    json=data, timeout=600, stream=True, proxies={"http": None, "https": None}
                )
                raw_sse_bytes = resp.content
                all_text = ""
                for ls in raw_sse_bytes.decode('utf-8', errors='ignore').split('\n'):
                    ls = ls.strip()
                    if not ls:
                        continue
                    if 'text_delta' in ls or 'web_search_result' in ls:
                            try:
                                p = ls[6:] if ls.startswith('data: ') else ls
                                d = json.loads(p)
                                if d.get('delta', {}).get('type') == 'text_delta':
                                    all_text += d['delta'].get('text', '')
                                elif d.get('content_block', {}).get('type') == 'web_search_tool_result':
                                    results = d['content_block'].get('content', [])
                                    if isinstance(results, list):
                                        for r in results[:3]:
                                            all_text += f"\n- [{r.get('title', '?')}]({r.get('url', '')})\n  {r.get('encrypted_content', '')[:100]}...\n"
                            except: pass
                rmsg = next((m for m in session['conversation_history'] if m['id'] == response_id), None)
                if rmsg:
                    rmsg['content'] = all_text or "(无文本内容)"
                    rmsg['summary'] = f"Subagent 结果 ({provider_config['name']})"
                    rmsg.pop('start_time', None)
                    rmsg['has_subagent_sse'] = True
                    rmsg['timing'] = {"prep": 0, "ttfb": resp.elapsed.total_seconds(), "download": 0}
                    self.subagent_responses[response_id] = raw_sse_bytes
                self.save_sessions(push_update=True)
            except Exception as e:
                rmsg = next((m for m in session['conversation_history'] if m['id'] == response_id), None)
                if rmsg:
                    rmsg['content'] = f"Subagent 请求失败: {str(e)}"
                    rmsg['summary'] = "Subagent 失败"
                    rmsg['is_error'] = True
                    rmsg.pop('start_time', None)
                self.save_sessions(push_update=True)
        threading.Thread(target=_forward, daemon=True).start()
        return {"status": "ok", "response_id": response_id}


    def _auto_subagent_forward(self, subagent_id, provider_config, target_sid):
        """托管模式下自动转发 subagent 请求到提供商，等待响应后自动采纳返回给CC。"""
        try:
            result = self.subagent_send(subagent_id, provider_config, target_sid=target_sid)
            if result.get('error'):
                print(f"[SUBAGENT AUTO] 自动发送失败: {result['error']}", flush=True)
                return
            response_id = result.get('response_id')
            if not response_id:
                return
            # 等待响应完成（轮询 has_subagent_sse 标志）
            for _ in range(600):  # 最多等待 5 分钟
                time.sleep(0.5)
                session = self.sessions.get(target_sid)
                if not session:
                    return
                rmsg = next((m for m in session['conversation_history'] if m.get('id') == response_id), None)
                if rmsg and rmsg.get('has_subagent_sse'):
                    self.subagent_adopt(subagent_id, response_id)
                    print(f"[SUBAGENT AUTO] 自动采纳成功: {subagent_id} -> {response_id}", flush=True)
                    return
                if rmsg and rmsg.get('is_error'):
                    print(f"[SUBAGENT AUTO] 响应出错，跳过自动采纳", flush=True)
                    return
            print(f"[SUBAGENT AUTO] 等待响应超时: {subagent_id}", flush=True)
        except Exception as e:
            print(f"[SUBAGENT AUTO] 异常: {e}", flush=True)

    def subagent_adopt(self, subagent_id, response_msg_id):
        """采纳 subagent 响应并返回给CC"""
        raw_sse = self.subagent_responses.get(response_msg_id)
        if not raw_sse:
            if self.socketio: self.socketio.emit('show_toast', {'message': 'Subagent 响应数据不存在', 'type': 'error'})
            return
        q = self.subagent_queues.get(subagent_id)
        if q:
            q.put(raw_sse)
            print(f"[SUBAGENT] Adopted response {response_msg_id} for {subagent_id}", flush=True)
        else:
            if self.socketio: self.socketio.emit('show_toast', {'message': f'Subagent 队列 {subagent_id} 已过期', 'type': 'error'})


    def _collect_edited_cc_messages(self, session):
        """收集所有 CC 消息的 cc_content 构成完整的 Anthropic messages 数组，按 cc_msg_group 合并"""
        raw_entries = []
        has_hidden = False
        for m in session.get('conversation_history', []):
            if m.get('cc_content') is not None and not m.get('cc_type'):
                cc = m['cc_content']
                if m.get('is_hidden'):
                    cc = self._redact_cc_content(cc)
                    has_hidden = True
                raw_entries.append({"role": m['role'], "content": cc, "group": m.get('cc_msg_group')})
        
        # 按 cc_msg_group 合并属于同一原始消息的气泡
        messages = []
        for entry in raw_entries:
            group = entry['group']
            role = entry['role']
            cc = entry['content']
            
            # 尝试合并到上一条同角色同组消息
            if group and messages and messages[-1].get('_group') == group and messages[-1]['role'] == role:
                prev = messages[-1]['content']
                if isinstance(prev, list) and isinstance(cc, list):
                    prev.extend(cc)
                elif isinstance(prev, list):
                    prev.append({"type": "text", "text": str(cc)})
                else:
                    messages[-1]['content'] = [{"type": "text", "text": str(prev)}] + (cc if isinstance(cc, list) else [{"type": "text", "text": str(cc)}])
            else:
                messages.append({"role": role, "content": cc, "_group": group})
        
        # 清理内部标记
        for msg in messages:
            msg.pop('_group', None)
        
        has_edits = any(m.get('_edited') for m in session.get('conversation_history', []))
        if has_edits or has_hidden:
            for m in session.get('conversation_history', []):
                m.pop('_edited', None)
            return messages
        return None


    def edit_cc_message(self, index, content_json):
        """编辑 CC 会话中的消息（直接修改 cc_content）"""
        session = self._session
        if not session or not session.get('is_cc_session'):
            return
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            try:
                new_cc_content = json.loads(content_json)
                msg['cc_content'] = new_cc_content
                msg['content'] = self._render_anthropic_content(new_cc_content)
                msg['_edited'] = True
                self.save_sessions(push_update=True)
            except json.JSONDecodeError:
                msg['cc_content'] = content_json
                msg['content'] = content_json
                msg['_edited'] = True
                self.save_sessions(push_update=True)



