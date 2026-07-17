"""Api mixin: Message editing, annotation, summary, and content retrieval."""
import difflib
import re



class MessageEditMixin:
    """Api mixin: Message editing, annotation, summary, and content retrieval."""

    def edit_message(self, index, content):
        if self._active_sid == "starred_session_virtual":
            starred = self.global_settings.setdefault('starred_messages', [])
            if 0 <= index < len(starred):
                starred[index]['content'] = content
                self.save_sessions()
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            msg['content'] = content

            #清除旧部件，重新解析（所有工具获得全新时间戳 ID）
            msg.pop('content_parts', None)
            if hasattr(self, '_parse_content_parts'):
                new_parts = self._parse_content_parts(msg)
                if new_parts:
                    msg['content_parts'] = new_parts

            # 从所有 CC 的等待队列中清除该气泡旧工具的残留
            import json
            _msg_id_str = str(msg['id'])
            for cc_id, q in getattr(self, 'cc_tool_queues', {}).items():
                with q.mutex:
                    new_queue = [item for item in list(q.queue) if not item.get('id', '').startswith(f"toolu_{_msg_id_str}_")]
                    q.queue.clear()
                    q.queue.extend(new_queue)

            # CC 消息同步更新 Anthropic 格式的 cc_content 并标记已编辑
            if 'cc_content' in msg:
                old_cc = msg['cc_content']
                if isinstance(old_cc, list):
                    # 仅保留 thinking 块（tool_use/tool_result 已拆分到独立气泡），只替换 text 块
                    new_blocks = [b for b in old_cc if isinstance(b, dict) and b.get('type') == 'thinking']
                    # 将编辑后的文本作为单个 text 块插入到第一个非 text 块之前
                    if content.strip():
                        text_block = {"type": "text", "text": content}
                        # 找到原始 text 块的位置，保持插入位置一致
                        first_non_text_idx = 0
                        for i, b in enumerate(old_cc):
                            if isinstance(b, dict) and b.get('type') == 'text':
                                first_non_text_idx = i
                                break
                        new_blocks.insert(first_non_text_idx, text_block)
                    msg['cc_content'] = new_blocks if new_blocks else content
                else:
                    msg['cc_content'] = content
                # 渲染时排除 thinking blocks（它们有独立气泡），避免编辑后思维链泄漏到主气泡
                if isinstance(msg['cc_content'], list):
                    display_cc = [b for b in msg['cc_content'] if not (isinstance(b, dict) and b.get('type') == 'thinking')]
                    msg['content'] = self._render_anthropic_content(display_cc) if display_cc else content
                else:
                    msg['content'] = self._render_anthropic_content(msg['cc_content'])
                msg['_edited'] = True
            self.save_sessions()


    def get_annotation_content(self, index):
        if self._active_sid == "starred_session_virtual":
            return ""
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            return msg.get('annotated_content', msg.get('content', ''))
        return ""


    def edit_annotation(self, index, content):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            if content == msg.get('content'):
                msg.pop('annotated_content', None)
                msg.pop('diff_content', None)
            else:
                msg['annotated_content'] = content
                import re
                import difflib
                # 词级分割：中文字符单拆，连续英文/数字捆绑，空白符和标点单拆
                tokens1 = re.findall(r'[a-zA-Z0-9]+|\s+|[\u4e00-\u9fa5]|.', msg.get('content', ''))
                tokens2 = re.findall(r'[a-zA-Z0-9]+|\s+|[\u4e00-\u9fa5]|.', content)
                
                d = difflib.SequenceMatcher(None, tokens1, tokens2)
                res = []
                for tag, i1, i2, j1, j2 in d.get_opcodes():
                    if tag == 'equal':
                        res.append("".join(tokens1[i1:i2]))
                    elif tag == 'delete':
                        res.append(f"<del>{''.join(tokens1[i1:i2])}</del>")
                    elif tag == 'insert':
                        res.append(f"<ins>{''.join(tokens2[j1:j2])}</ins>")
                    elif tag == 'replace':
                        res.append(f"<del>{''.join(tokens1[i1:i2])}</del><ins>{''.join(tokens2[j1:j2])}</ins>")
                msg['diff_content'] = "".join(res)
            self.save_sessions()


    def edit_summary(self, index, summary):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            session['conversation_history'][index]['summary'] = summary
            self.save_sessions()


    def edit_correction(self, index, part_id, content):
        if self._active_sid == "starred_session_virtual":
            return
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            for p in msg.get('content_parts', []):
                if p.get('id') == part_id:
                    p['content'] = content
                    break
            self.save_sessions()


    def get_message_content(self, index):
        if self._active_sid == "starred_session_virtual":
            starred = self.global_settings.get('starred_messages', [])
            return starred[index]['content'] if 0 <= index < len(starred) else ""
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            cc = msg.get('cc_content')
            if cc is not None:
                if isinstance(cc, str):
                    return cc
                if isinstance(cc, list):
                    # 仅提取 text 块的纯文本，忽略 thinking/tool_use/tool_result
                    parts = []
                    for block in cc:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            parts.append(block.get('text', ''))
                    return '\n\n'.join(parts) if parts else ''
                return str(cc)
            return msg.get('content', '')
        return ""


    def get_payload(self, index):
        if self._active_sid == "starred_session_virtual":
            return "全局收藏无 Payload"
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg_id = session['conversation_history'][index].get('id')
            payload = self.payloads.get(msg_id)
            if payload:
                return payload
            # Fallback: fake provider retries may store payload at a different target_id
            # Search for the most recent payload belonging to this session
            if self.payloads:
                session_ids = {m.get('id') for m in session.get('conversation_history', [])}
                for pid in sorted(self.payloads.keys(), reverse=True):
                    if pid in session_ids and self.payloads[pid] and not self.payloads[pid].startswith('系统正在'):
                        return self.payloads[pid]
            return "无相关的上下文记录（由于性能优化，旧会话或重启后的 Payload 不再持久化保存）"
        return ""

    def _parse_content_parts(self, msg):
        import uuid, json, re, time
        content = msg.get('content', '')
        parts = []
        last_idx = 0
        _cc_tc_seq = 0
        _is_cc = getattr(self, 'global_settings', {}).get('enable_tool_inject', False)

        _bare_tc_map = {}
        _tc_ph_counter = 0
        if _is_cc:
            # Pass D: 描述符格式工具调用解析 [ToolName开始]...[ToolName结束]
            _enable_descriptor = getattr(self, 'global_settings', {}).get('enable_descriptor_tool_calls', False)
            if _enable_descriptor:
                _KNOWN_DESC_TOOLS = {'Read', 'Write', 'Edit', 'Bash', 'WebSearch', 'WebFetch', '自动审稿', '压缩', '触发器', '单关卡审稿', '展开气泡', '命名会话', '创建子会话', '结束子会话', '申请审批', '缩减读取'}
                _NUM_PARAMS = {'offset', 'limit', 'timeout', 'count', 'interval_minutes', 'checkpoint_index', 'max_steps'}
                _BOOL_PARAMS_D = {'replace_all', 'run_in_background', 'force_full', 'enable_baseline', 'cancel'}
                _ARR_PARAMS = {'allowed_domains', 'blocked_domains', 'message_ids', 'checkpoint_indices'}
                _dlines = content.split('\n')
                _dline_starts = []
                _dpos = 0
                for _dl in _dlines:
                    _dline_starts.append(_dpos)
                    _dpos += len(_dl) + 1
                _desc_hits = []
                _di = 0
                while _di < len(_dlines):
                    _ds = _dlines[_di].strip()
                    _dtool = None
                    if _ds.startswith('[') and _ds.endswith('开始]') and len(_ds) > 4:
                        _candidate = _ds[1:-3]
                        if _candidate in _KNOWN_DESC_TOOLS:
                            _dtool = _candidate
                    if not _dtool:
                        _di += 1
                        continue
                    _dend_marker = f'[{_dtool}结束]'
                    _ddesc_lines = []
                    _dparams = {}
                    _dcur_param = None
                    _dcur_lines = []
                    _dfound = False
                    _dj = _di + 1
                    while _dj < len(_dlines):
                        _djs = _dlines[_dj].strip()
                        if _djs == _dend_marker:
                            if _dcur_param is not None:
                                _dpv = '\n'.join(_dcur_lines)
                                if _dpv.startswith('\n'): _dpv = _dpv[1:]
                                if _dpv.endswith('\n'): _dpv = _dpv[:-1]
                                _dparams[_dcur_param] = _dpv
                            _dfound = True
                            break
                        _dpm = re.match(r'^\[([a-zA-Z_][a-zA-Z0-9_]*)参数\]$', _djs)
                        if _dpm:
                            if _dcur_param is not None:
                                _dpv = '\n'.join(_dcur_lines)
                                if _dpv.startswith('\n'): _dpv = _dpv[1:]
                                if _dpv.endswith('\n'): _dpv = _dpv[:-1]
                                _dparams[_dcur_param] = _dpv
                            _dcur_param = _dpm.group(1)
                            _dcur_lines = []
                            _dj += 1
                            continue
                        if _dcur_param is None:
                            _ddesc_lines.append(_dlines[_dj])
                        else:
                            _dcur_lines.append(_dlines[_dj])
                        _dj += 1
                    if not _dfound:
                        _di += 1
                        continue
                    _dinput = {}
                    for _dk, _dv in _dparams.items():
                        if _dk in _NUM_PARAMS:
                            try: _dinput[_dk] = int(_dv.strip())
                            except ValueError: _dinput[_dk] = _dv
                        elif _dk in _BOOL_PARAMS_D:
                            _dvl = _dv.strip().lower()
                            if _dvl == 'true': _dinput[_dk] = True
                            elif _dvl == 'false': _dinput[_dk] = False
                            else: _dinput[_dk] = _dv
                        elif _dk in _ARR_PARAMS:
                            _dvs = _dv.strip()
                            if _dvs.startswith('[') and _dvs.endswith(']'):
                                try: _dinput[_dk] = json.loads(_dvs)
                                except (json.JSONDecodeError, ValueError): _dinput[_dk] = _dv
                            else: _dinput[_dk] = _dv
                        else:
                            _dinput[_dk] = _dv
                    _dstart = _dline_starts[_di]
                    _dend = _dline_starts[_dj] + len(_dlines[_dj])
                    _ddesc = '\n'.join(_ddesc_lines).strip()
                    _desc_hits.append((_dstart, _dend, {'name': _dtool, 'input': _dinput, '_descriptor': True}, _ddesc))
                    _di = _dj + 1
                for _ds, _de, _dobj, _ddesc in reversed(_desc_hits):
                    _ph = f"\x02TC{_tc_ph_counter}\x03"
                    _tc_ph_counter += 1
                    _bare_tc_map[_ph] = _dobj
                    _drepl = f"{_ddesc}\n{_ph}" if _ddesc else _ph
                    content = content[:_ds] + _drepl + content[_de:]

        for match in re.finditer(r'`{3}([^\n]*)\n(.*?)`{3}', content, re.DOTALL):
            start, end = match.span()
            if start > last_idx:
                text_part = content[last_idx:start].strip()
                if text_part:
                    parts.append({"id": str(uuid.uuid4()), "type": "text", "content": text_part})

            lang = match.group(1).strip()
            code_content = match.group(2).strip()

            if _is_cc and lang.lower() in ('correction', 'terminal'):
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            elif _is_cc and code_content.startswith('File:'):
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            elif lang.lower() == 'correction':
                parts.append({"id": str(uuid.uuid4()), "type": "correction", "content": code_content})
            elif lang.lower() == 'terminal':
                parts.append({"id": str(uuid.uuid4()), "type": "terminal", "content": code_content, "status": "pending"})
            elif code_content.startswith("File:"):
                parts.append({"id": str(uuid.uuid4()), "type": "code", "content": code_content, "status": "pending"})
            else:
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            last_idx = end

        if last_idx < len(content):
            text_part = content[last_idx:].strip()
            if text_part:
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": text_part})

        if _bare_tc_map:
            _ph_pat = re.compile("(" + "|".join(re.escape(p) for p in _bare_tc_map) + ")")
            _expanded = []
            for part in parts:
                if part["type"] == "text" and _ph_pat.search(part["content"]):
                    for _seg in _ph_pat.split(part["content"]):
                        if _seg in _bare_tc_map:
                            _obj = _bare_tc_map[_seg]
                            _cc_tc_seq += 1
                            _tc_id = f"toolu_{msg.get('id', 0)}_{_cc_tc_seq}_{time.strftime('%Y%m%d_%H%M%S')}"
                            _tc_block = {"type": "tool_use", "id": _tc_id, "name": _obj.get("name", "unknown"), "input": _obj.get("input", {})}
                            _expanded.append({"id": str(uuid.uuid4()), "type": "tool_use_part", "content": json.dumps(_tc_block, ensure_ascii=False, indent=2), "tool_name": _obj.get("name", "unknown"), "tool_id": _tc_id, "tool_input": _obj.get("input", {}), "status": "pending"})
                        elif _seg.strip():
                            _expanded.append({"id": str(uuid.uuid4()), "type": "text", "content": _seg.strip()})
                else:
                    _expanded.append(part)
            parts = _expanded

        return parts


