"""Api 混入：CC 工具采纳与会话绑定。

处理用户在 UI 上采纳/拒绝 tool_use 块的操作，
并将采纳的工具推入对应 CC 连接的执行队列。
包含命令拦截（长命令、禁用命令）和前置文本长度检查。
"""
import hashlib
import json
import os
import queue
import time


from .tool_diag import tool_diag



class ToolAcceptMixin:
    """Api 混入：CC 工具采纳与会话绑定。"""

    def _is_tool_settled(self, session, tool_use_id):
        """判断工具调用是否已确定（已收到 tool_result 返回值，或被用户拒绝/系统拦截）。

        一个工具调用的生命周期: pending → approved → queued → settled
        settled 的两种途径:
          1. CC 返回了 tool_result（成功或错误都算）
          2. 用户在 UI 拒绝 / 系统拦截（禁用命令/过长等）生成了假 tool_result
        """
        for m in session.get('conversation_history', []):
            if m.get('is_tool_result') and m.get('tool_use_id') == tool_use_id:
                return True
        for m in session.get('conversation_history', []):
            for p in m.get('content_parts', []):
                if p.get('type') == 'tool_use_part':
                    _tid = p.get('tool_id') or ''
                    if _tid == tool_use_id and p.get('status') in ('rejected', 'failed'):
                        return True
        return False

    def _get_sibling_tool_ids(self, session, tool_use_id):
        """获取与 tool_use_id 同属一个 assistant 消息的所有工具 ID（按出现顺序）。

        排序约束只在同一消息内执行——同一个 AI 回复中的多个工具必须按序执行，
        但不同回复之间的工具互不阻塞。
        """
        for m in session.get('conversation_history', []):
            ids_in_msg = []
            for p in m.get('content_parts', []):
                if p.get('type') == 'tool_use_part':
                    _tid = p.get('tool_id') or ''
                    if _tid:
                        ids_in_msg.append(_tid)
            if tool_use_id in ids_in_msg:
                return ids_in_msg
        return []

    def _can_queue_tool(self, session, tool_use_id):
        """检查同一消息内所有前序工具是否已确定。跨消息的工具不受此约束。"""
        sibling_ids = self._get_sibling_tool_ids(session, tool_use_id)
        for tid in sibling_ids:
            if tid == tool_use_id:
                return True  # 所有同消息前序都已确定
            if not self._is_tool_settled(session, tid):
                # 申请审批工具不阻塞后续工具执行
                _is_approval = False
                for _m in session.get('conversation_history', []):
                    for _p in (_m.get('content_parts') or []):
                        if _p.get('type') == 'tool_use_part':
                            _ptid = _p.get('tool_id') or ''
                            if _ptid == tid and (_p.get('tool_name') or 'unknown') == '申请审批':
                                _is_approval = True
                        if _is_approval:
                            break
                    if _is_approval:
                        break
                if _is_approval:
                    continue
                return False  # 同消息内存在未确定的前序工具
        return True  # tool_use_id 未找到（安全放行）

    def _advance_approved_queue(self, session, target_sid):
        """释放已批准队列中满足前序约束的工具。返回是否成功释放了至少一个。

        Planned mode: 释放所有满足条件的工具（并行释放），不仅仅是第一个。
        Standard mode: 只释放第一个满足条件的。
        """
        _planned_mode = getattr(self, 'global_settings', {}).get('enable_planned_tools', False)
        pending = session.get('_approved_tool_queue', [])
        if not pending:
            return False

        # Single pass: collect items to release and items to keep
        _release_items = []
        _keep_items = []
        for item in pending:
            # Skip settled tools
            if self._is_tool_settled(session, item['tool_use_id']):
                continue
            # Skip ghost tools (message deleted)
            _ghost = True
            for _m in session.get('conversation_history', []):
                if _m.get('content_parts'):
                    for _p in _m['content_parts']:
                        if _p.get('type') == 'tool_use_part' and (_p.get('tool_id') or '') == item['tool_use_id']:
                            _ghost = False
                            break
                    if not _ghost:
                        break
            if _ghost:
                continue
            # Decide: release or keep
            if _planned_mode:
                # Release ALL non-settled non-ghost tools; accept_tool handles dep checks
                _release_items.append(item)
            else:
                if self._can_queue_tool(session, item['tool_use_id']):
                    _release_items.append(item)
                    # Standard mode: only release first releasable, keep rest
                    break
                else:
                    _keep_items.append(item)
                    break

        if not _release_items:
            return False

        # Update queue: remove released items
        _released_ids = {it['tool_use_id'] for it in _release_items}
        session['_approved_tool_queue'] = [
            it for it in pending
            if it['tool_use_id'] not in _released_ids
            and not self._is_tool_settled(session, it['tool_use_id'])
        ]

        # Execute released tools
        for item in _release_items:
            self.accept_tool(item['tool_data'], target_sid=item.get('target_sid', target_sid),
                             msg_index=item.get('msg_index'), part_id=item.get('part_id'))
        return True

    def _update_part_status(self, session, msg_index, part_id, status):
        """Update the status field of a specific content_part in a message.

        Centralizes the try/except + loop pattern that was previously repeated ~8 times.
        """
        try:
            idx = int(msg_index)
        except (ValueError, TypeError):
            return
        if 0 <= idx < len(session.get('conversation_history', [])):
            for p in session['conversation_history'][idx].get('content_parts', []):
                if p.get('id') == part_id:
                    p['status'] = status

    def _create_tool_result_bubble(self, session, tool_use_id, result, msg_index=None, part_id=None):
        """Create a standardized tool_result bubble from a ToolResult.

        Handles:
        - Bubble creation with proper formatting (Tool Result / Tool Error prefix)
        - Part status update (auto: 'adopted' for success, 'failed' for error; overridable via result.part_status)
        - Session save + push
        """
        bt = chr(96) * 3
        prefix = "Tool Error" if result.is_error else "Tool Result"
        bubble = {
            "id": self._next_id(),
            "role": "user",
            "content": f"**{prefix}** (tool: {tool_use_id})\n\n{bt}\n{result.content}\n{bt}",
            "summary": result.summary,
            "is_omitted": False,
            "is_collapsed": True,
            "is_tool_result": True,
            "tool_use_id": tool_use_id,
        }
        # 执行耗时：如果 ToolResult 携带了 execution_time_s，写入 bubble
        if hasattr(result, 'execution_time_s') and result.execution_time_s is not None:
            bubble['execution_time_s'] = result.execution_time_s
        # Read 文件路径标记：如果 ToolResult 携带了 _read_file_path，设置到 bubble 上
        if hasattr(result, '_read_file_path') and result._read_file_path:
            bubble['_read_file_path'] = result._read_file_path
        # 图片读取支持：如果 ToolResult 携带了图片数据，注入 multimodal_blocks
        if hasattr(result, '_image_data') and result._image_data:
            _img_dir = os.path.join(getattr(self, 'data_dir', 'data'), 'images')
            os.makedirs(_img_dir, exist_ok=True)
            import base64 as _tb64, uuid as _tuuid
            _img_fname = f'read_{_tuuid.uuid4().hex[:8]}{os.path.splitext(result._image_path)[1]}'
            with open(os.path.join(_img_dir, _img_fname), 'wb') as _tif:
                _tif.write(_tb64.b64decode(result._image_data))
            bubble['multimodal_blocks'] = [{
                'type': 'image',
                'source': {'type': 'file', 'media_type': result._image_mime, 'path': _img_fname}
            }]
        session['conversation_history'].append(bubble)
        if msg_index is not None and part_id is not None:
            # 本地执行器的错误返回仍标记为 adopted（工具确实被执行了，只是结果是错误）
            # 只有 part_status 被显式设为 'failed' 时才显示为拦截
            status = result.part_status if result.part_status is not None else 'adopted'
            self._update_part_status(session, msg_index, part_id, status)
        self.save_sessions(push_update=True)

    def _resolve_part_tool_id(self, session, msg_index, part_id):
        """Return (message, part, tool_use_id) for one content_part.

        Two sources for the id and both are genuinely in use. worker_engine writes
        the flat tool_id/tool_name/tool_input alongside the JSON in content — every
        one of the 1101 parts in the persisted sessions carries both — while a part
        synthesised by the frontend descriptor pass has only content. Reading the
        flat field first and falling back to a parse covers either shape; assuming
        one of them would make this silently fail on the other.
        """
        try:
            idx = int(msg_index)
        except (ValueError, TypeError):
            return None, None, ''
        history = session.get('conversation_history', [])
        if not (0 <= idx < len(history)):
            return None, None, ''
        msg = history[idx]
        for p in msg.get('content_parts', []):
            if p.get('id') != part_id:
                continue
            tid = p.get('tool_id') or ''
            if not tid:
                try:
                    tid = (json.loads(p.get('content') or '{}') or {}).get('id') or ''
                except Exception:
                    tid = ''
            return msg, p, tid
        return msg, None, ''

    def _purge_tool_results(self, session, tool_use_id):
        """Drop the recorded result for one tool call so it can be run again.

        The autoread an Edit triggered goes too. That bubble is a snapshot of the
        file as of that run, and keeping a stale copy while the edit re-executes
        puts two contradictory versions of the same file in the context.

        Sliced in place rather than rebound: a background executor thread holds a
        reference to the session dict and appends to this list, and keeping one
        list object means there is never a window where the two disagree.
        """
        history = session.get('conversation_history', [])
        keep = [m for m in history
                if not (m.get('is_tool_result') and m.get('tool_use_id') == tool_use_id)
                and m.get('auto_read_trigger_id') != tool_use_id]
        removed = len(history) - len(keep)
        if removed:
            history[:] = keep
        return removed

    def accept_all_tools(self, msg_index, target_sid=None):
        """批量批准一个气泡内的所有待定工具调用。统一入口，一键采纳和自动托管共用。"""
        sid = target_sid or self._active_sid
        session = self.sessions.get(sid)
        if not session:
            return
        if 0 <= msg_index < len(session.get('conversation_history', [])):
            msg = session['conversation_history'][msg_index]
            for part in msg.get('content_parts', []):
                if part.get('type') == 'tool_use_part' and part.get('status') == 'pending':
                    try:
                        # 申请审批工具不自动采纳，需要用户手动通过或拒绝
                        if (part.get('tool_name') or '').strip() == '申请审批':
                            continue
                        tool_data = {
                            "type": "tool_use",
                            "id": part.get('tool_id', ''),
                            "name": part.get('tool_name', 'unknown'),
                            "input": part.get('tool_input', {})
                        }
                        self.accept_tool(tool_data, target_sid=sid,
                                           msg_index=msg_index, part_id=part['id'])
                    except Exception as e:
                        print(f"[CC ACCEPT ALL] 工具调用构建失败: {e}", flush=True)

    def accept_tool(self, tool_json, target_sid=None, msg_index=None, part_id=None, is_retry=False):
        """将 UI 上采纳的 tool_use 块推入绑定的 CC 会话队列，由对应的 CC 连接拾取并执行（含自动读取触发支持）"""
        try:
            tool_block = json.loads(tool_json) if isinstance(tool_json, str) else tool_json
            if not isinstance(tool_block, dict) or tool_block.get("type") != "tool_use":
                print(f"[CC EXECUTOR] Invalid tool block: {str(tool_block)[:100]}")
                return
        except Exception as e:
            print(f"[CC EXECUTOR] accept_tool parse error: {e}")
            return

        _active_sid = target_sid or self._active_sid
        session = self.sessions.get(_active_sid)
        if not session or _active_sid == "starred_session_virtual":
            if self.socketio:
                self.socketio.emit('show_toast', {'message': '当前会话不支持 CC 工具调用', 'type': 'error'})
            return

        # Retry. is_retry has been on this signature and forwarded from app.py all
        # along without the body ever reading it; wiring it up here is what makes
        # the retry button possible at all.
        #
        # It has to run before the dedup guard below, which returns immediately on
        # any call that already has a result — a plain re-dispatch was a silent
        # no-op that also advanced the queue a second time. The part goes back to
        # pending too, or the UI keeps reading 已采纳 while the tool re-executes,
        # and the id comes off the abort list so abort-then-retry works.
        if is_retry:
            _rt_id = tool_block.get('id', '')
            if _rt_id:
                _rt_removed = self._purge_tool_results(session, _rt_id)
                _rt_ab = session.get('_aborted_tool_ids')
                if isinstance(_rt_ab, list) and _rt_id in _rt_ab:
                    _rt_ab.remove(_rt_id)
                if msg_index is not None and part_id is not None:
                    self._update_part_status(session, msg_index, part_id, 'pending')
                self.save_sessions(push_update=True)
                print(f'[CC RETRY] {tool_block.get("name", "?")} ({_rt_id[:25]}): '
                      f'清除 {_rt_removed} 条旧结果后重新执行', flush=True)

        # 防重机制：如果对话历史中已存在该 tool_use_id 的 tool_result 气泡，说明工具已执行过，坚决拒绝重复入队。
        _tool_use_id = tool_block.get('id', '')
        if _tool_use_id:
            _has_result = any(
                m.get('tool_use_id') == _tool_use_id and m.get('is_tool_result')
                for m in session.get('conversation_history', [])
            )
            if _has_result:
                # 去重生效：阻止重复执行，但必须继续推进队列
                # 否则从 _advance_approved_queue 调用时会导致后续工具永远卡死
                self._continue_autopilot_tool_queue(session, _active_sid)
                return

        # JSON 解析失败的工具调用：直接返回错误信息，不发给 CC
        if tool_block.get('name') == 'parse_error':
            bt = chr(96) * 3
            _raw_content = tool_block.get('input', {}).get('raw', '')
            fake_result = {
                "id": self._next_id(),
                "role": "user",
                "content": f"**Tool Error** (tool: {_tool_use_id})\n\n{bt}\n<system-reminder>\nThis tool call could not be parsed because the JSON was malformed. The raw content was:\n{_raw_content[:300]}\n\nPlease fix the JSON syntax and retry. Common issues: unescaped quotes, missing commas, truncated content.\n</system-reminder>\n{bt}",
                "summary": f"JSON 解析失败: parse_error",
                "is_omitted": False,
                "is_collapsed": True,
                "is_tool_result": True,
                "tool_use_id": _tool_use_id,
            }
            session['conversation_history'].append(fake_result)
            if msg_index is not None and part_id is not None:
                try:
                    _pi = int(msg_index)
                except (ValueError, TypeError):
                    _pi = -1
                if 0 <= _pi < len(session.get('conversation_history', [])):
                    for p in session['conversation_history'][_pi].get('content_parts', []):
                        if p.get('id') == part_id:
                            p['status'] = 'failed'
            self.save_sessions(push_update=True)
            self._continue_autopilot_tool_queue(session, _active_sid)
            return

        # Bash 命令长度拦截：超过约 1k 字符的命令直接生成假返回，不发给 CC（CC模拟模式跳过）
        if tool_block.get('name') == 'Bash' and not getattr(self, 'global_settings', {}).get('enable_tool_simulate', False):
            cmd = tool_block.get('input', {}).get('command', '')
            if len(cmd) > 1000:
                bt = chr(96) * 3
                char_count = len(cmd)
                token_est = char_count // 3
                fake_result = {
                    "id": self._next_id(),
                    "role": "user",
                    "content": f"**Tool Error** (tool: {_tool_use_id})\n\n{bt}\n<system-reminder>\nYour Bash command was intercepted by ChatApp because it exceeds the safety limit ({char_count} characters 1k limit).\nThis almost always means you are trying to use Bash to edit files (sed/awk/echo/heredoc/python -c with file writes), which violates rule S25.\n\nDo NOT panic or give up just because an Edit tool call failed. Edit failures are completely normal and harmless — they only mean old_string did not match the current file content. The correct recovery is:\n1. Use Read to re-read the file and get its current content\n2. Copy the EXACT text from the Read output as your new old_string\n3. Retry the Edit\n4. NEVER attempt to work around Edit by writing a Bash script to modify files — this corrupts the Edit tool's internal file snapshot and causes ALL subsequent Edits to fail\n\nEdit is safe. Bash file modification is dangerous. Stay calm and use Edit.\n\nIMPORTANT — Take a deep breath. Do NOT panic if an Edit tool call fails. Edit failures are completely normal and harmless — they only mean your old_string did not exactly match the current file content.\nBefore retrying an Edit, you MUST:\n1. Carefully copy-paste the EXACT surrounding code from the Read output (do not type from memory)\n2. Double-check every character, space, and newline in your old_string against the file\n3. Only then submit the retry\nDo NOT attempt to bypass the Edit tool by using Bash commands (sed, awk, echo, python -c, heredocs, etc.) to modify files. This will always be intercepted and rejected. The Edit tool is safe; Bash file modification is dangerous and prohibited.\nStay calm. Use the proper tools. You have unlimited retries at zero cost.\n</system-reminder>\n{bt}",
                    "summary": f"Bash 命令过长被拦截 ({token_est} tokens)",
                    "is_omitted": False,
                    "is_collapsed": True,
                    "is_tool_result": True,
                    "tool_use_id": _tool_use_id,
                }
                session['conversation_history'].append(fake_result)
                if msg_index is not None and part_id is not None:
                    try:
                        msg_index = int(msg_index)
                    except (ValueError, TypeError):
                        msg_index = -1
                    if 0 <= msg_index < len(session.get('conversation_history', [])):
                        for p in session['conversation_history'][msg_index].get('content_parts', []):
                            if p.get('id') == part_id:
                                p['status'] = 'adopted'
                self.save_sessions(push_update=True)
                print(f"[CC INTERCEPTED] Bash command too long ({char_count} chars), generated fake tool_result for {_tool_use_id}", flush=True)
                self._continue_autopilot_tool_queue(session, _active_sid)
                return

        # Bash 禁用命令拦截：仅在非CC模拟模式下生效
        if tool_block.get('name') == 'Bash' and not getattr(self, 'global_settings', {}).get('enable_tool_simulate', False):
            cmd = tool_block.get('input', {}).get('command', '')
            _banned = {'find', 'grep', 'rg', 'cat', 'head', 'tail', 'sed', 'awk'}
            # 仅检查第一个管道符 '|' 之前的词 — 管道后面的命令可以合法使用这些被禁用的工具
            _pipe_idx = cmd.find('|')
            _pre_pipe = cmd[:_pipe_idx] if _pipe_idx >= 0 else cmd
            _cmd_words = _pre_pipe.split()
            _hit = [w for w in _cmd_words if w in _banned]
            if _hit:
                bt = chr(96) * 3
                _hit_str = ', '.join(set(_hit))
                fake_result = {
                    "id": self._next_id(),
                    "role": "user",
                    "content": f"**Tool Error** (tool: {_tool_use_id})\n\n{bt}\n<system-reminder>\nYour Bash command was rejected because it contains banned command(s): {_hit_str}\nPer rule S25, you MUST use the dedicated tools instead:\n- find → use Glob tool\n- grep/rg → use Grep tool (Grep is based on ripgrep)\n- cat/head/tail → use Read tool\n- sed/awk → use Edit tool\nThese dedicated tools provide better user experience and are optimized for the correct permissions.\nPlease rewrite your operation using the appropriate tool(s).\n\nIMPORTANT — Take a deep breath. Do NOT panic if an Edit tool call fails. Edit failures are completely normal and harmless — they only mean your old_string did not exactly match the current file content.\nBefore retrying an Edit, you MUST:\n1. Carefully copy-paste the EXACT surrounding code from the Read output (do not type from memory)\n2. Double-check every character, space, and newline in your old_string against the file\n3. Only then submit the retry\nDo NOT attempt to bypass the Edit tool by using Bash commands (sed, awk, echo, python -c, heredocs, etc.) to modify files. This will be intercepted and rejected. The Edit tool is safe; Bash file modification is dangerous and prohibited.\nStay calm. Use the proper tools. You have unlimited retries at zero cost.\n</system-reminder>\n{bt}",
                    "summary": f"Bash 禁用命令被拦截: {_hit_str}",
                    "is_omitted": False,
                    "is_collapsed": True,
                    "is_tool_result": True,
                    "tool_use_id": _tool_use_id,
                }
                session['conversation_history'].append(fake_result)
                if msg_index is not None and part_id is not None:
                    try:
                        _bi = int(msg_index)
                    except (ValueError, TypeError):
                        _bi = -1
                    if 0 <= _bi < len(session.get('conversation_history', [])):
                        for p in session['conversation_history'][_bi].get('content_parts', []):
                            if p.get('id') == part_id:
                                p['status'] = 'failed'
                self.save_sessions(push_update=True)
                print(f"[CC INTERCEPTED] Bash 禁用命令: {_hit_str} in: {cmd[:100]}", flush=True)
                self._continue_autopilot_tool_queue(session, _active_sid)
                return

        # 工具块前置文本长度检查：每个工具块前必须有至少 15 个字符的描述文本
        if msg_index is not None and part_id is not None:
            try:
                _ci = int(msg_index)
            except (ValueError, TypeError):
                _ci = -1
            if 0 <= _ci < len(session.get('conversation_history', [])):
                _cm = session['conversation_history'][_ci]
                _cparts = _cm.get('content_parts', [])
                _ppos = -1
                for _pi, _pp in enumerate(_cparts):
                    if _pp.get('id') == part_id:
                        _ppos = _pi
                        break
                if _ppos >= 0:
                    _ptxt = 0
                    for _pi in range(_ppos - 1, -1, -1):
                        _pp = _cparts[_pi]
                        if _pp.get('type') == 'tool_use_part':
                            break
                        if _pp.get('type') == 'text':
                            _ptxt += len(_pp.get('content', '').strip())
                    if _ptxt < 15:
                        # 不拦截，只记录——等工具结果回来后追加威慑性警告
                        _sdw = session.get('_short_desc_warnings')
                        if not isinstance(_sdw, dict):
                            _sdw = {}
                            session['_short_desc_warnings'] = _sdw
                        _sdw[_tool_use_id] = _ptxt
                        print(f"[CC WARNING] 工具前置文本过短 ({_ptxt} < 15): {tool_block.get('name', '?')}，已放行但将追加警告", flush=True)

        # === 排序约束：前序工具未确定时暂存到批准队列，确定后自动释放 ===
        # 注意：此检查必须在 UI 状态更新之前，否则被暂存的工具会被前端误判为 adopted
        _planned_mode = getattr(self, 'global_settings', {}).get('enable_planned_tools', False)
        _this_is_planned = False
        _this_wait_list = []
        if _planned_mode and msg_index is not None and part_id is not None:
            try:
                _pi = int(msg_index)
            except (ValueError, TypeError):
                _pi = -1
            if 0 <= _pi < len(session.get('conversation_history', [])):
                for _p in session['conversation_history'][_pi].get('content_parts', []):
                    if _p.get('id') == part_id:
                        _this_is_planned = _p.get('_is_planned', False)
                        _this_wait_list = _p.get('_wait_list', [])
                        break
        if _planned_mode and _this_is_planned:
            # Planned tool: check if all dependencies are settled AND successful
            _all_deps_ok = True
            _any_dep_failed = False
            if _this_wait_list:
                # Build seq->tool_use_id mapping from the same message
                _seq_to_tid = {}
                try:
                    _pi2 = int(msg_index)
                except (ValueError, TypeError):
                    _pi2 = -1
                if 0 <= _pi2 < len(session.get('conversation_history', [])):
                    _seq_n = 0
                    for _p in session['conversation_history'][_pi2].get('content_parts', []):
                        if _p.get('type') == 'tool_use_part':
                            _seq_n += 1
                            _seq_to_tid[_seq_n] = _p.get('tool_id', '')
                for _dep_seq in _this_wait_list:
                    _dep_tid = _seq_to_tid.get(_dep_seq, '')
                    if not _dep_tid:
                        continue
                    if not self._is_tool_settled(session, _dep_tid):
                        _all_deps_ok = False
                        break
                    # Check if dependency failed (is_error or rejected)
                    _dep_failed = False
                    for _m in session.get('conversation_history', []):
                        if _m.get('is_tool_result') and _m.get('tool_use_id') == _dep_tid:
                            if _m.get('is_error') or 'Tool Error' in (_m.get('content') or ''):
                                _dep_failed = True
                            break
                    if not _dep_failed:
                        for _m in session.get('conversation_history', []):
                            for _p in (_m.get('content_parts') or []):
                                if _p.get('type') == 'tool_use_part' and _p.get('tool_id') == _dep_tid:
                                    if _p.get('status') in ('rejected', 'failed'):
                                        _dep_failed = True
                                    break
                            if _dep_failed:
                                break
                    if _dep_failed:
                        _any_dep_failed = True
                        break
            if _any_dep_failed:
                # Auto-reject: dependency failed, cascade rejection
                from .tool_executors import ToolResult
                _rej_result = ToolResult(
                    'This planned tool was automatically rejected because one of its dependencies failed.',
                    'Dependency failure cascade',
                    is_error=True
                )
                self._create_tool_result_bubble(session, _tool_use_id, _rej_result, msg_index, part_id)
                self._continue_autopilot_tool_queue(session, _active_sid)
                return
            if not _all_deps_ok:
                # Not all deps settled yet, queue it
                _aq = session.setdefault('_approved_tool_queue', [])
                if not any(_item['tool_use_id'] == _tool_use_id for _item in _aq):
                    _aq.append({
                        'tool_use_id': _tool_use_id,
                        'tool_data': tool_block,
                        'target_sid': _active_sid,
                        'msg_index': msg_index,
                        'part_id': part_id,
                    })
                return
            # All deps settled and successful - fall through to execute
        elif _planned_mode and not _this_is_planned:
            # Immediate tool in planned mode: skip ordering constraints entirely
            pass
        else:
            # Standard mode: check all predecessors settled
            if not self._can_queue_tool(session, _tool_use_id):
                _aq = session.setdefault('_approved_tool_queue', [])
                # 去重：如果该 tool_use_id 已在队列中，不重复添加
                if not any(_item['tool_use_id'] == _tool_use_id for _item in _aq):
                    _aq.append({
                        'tool_use_id': _tool_use_id,
                        'tool_data': tool_block,
                        'target_sid': _active_sid,
                        'msg_index': msg_index,
                        'part_id': part_id,
                    })
                    print(f"[CC EXECUTOR] 工具 {tool_block.get('name', '?')} ({_tool_use_id[:25]}) 已批准但暂存：前序工具尚未确定", flush=True)
                else:
                    print(f"[CC EXECUTOR] 工具 {tool_block.get('name', '?')} ({_tool_use_id[:25]}) 已在批准队列中，跳过重复入队", flush=True)
                return

        # UI 状态更新为 adopted（仅通过排序检查的工具才会到达这里）
        if msg_index is not None and part_id is not None:
            try:
                msg_index = int(msg_index)
            except (ValueError, TypeError):
                msg_index = -1
            if 0 <= msg_index < len(session.get('conversation_history', [])):
                for p in session['conversation_history'][msg_index].get('content_parts', []):
                    if p.get('id') == part_id:
                        p['status'] = 'adopted'
            self.save_sessions(push_update=True)

        # === 本地执行器路由（在排序约束和 UI 状态更新之后执行） ===
        from .tool_executors import LOCAL_EXECUTORS, EXECUTOR_SETTINGS, ToolResult, ToolReject, _prepare_autoread_content
        _exec_name = tool_block.get('name', '')
        _executor = LOCAL_EXECUTORS.get(_exec_name)
        if _executor:
            _setting_check = EXECUTOR_SETTINGS.get(_exec_name)
            _use_local = False
            if _setting_check is None:
                _use_local = True
            elif isinstance(_setting_check, str):
                _use_local = bool(getattr(self, 'global_settings', {}).get(_setting_check, False))
            elif callable(_setting_check):
                _use_local = bool(_setting_check(getattr(self, 'global_settings', {})))
            if _use_local:
                _cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'webfetch_cache')
                # Planned mode: ALL tools run in background thread for true parallelism
                if _planned_mode:
                    import threading
                    def _run_immediate_executor(_exec=_executor, _input=tool_block.get('input', {}), _settings=getattr(self, 'global_settings', {}), _cd=_cache_dir, _sid=_active_sid, _tuid=_tool_use_id, _mi=msg_index, _pi=part_id, _sess=session, _tn=_exec_name):
                        # Set thread-local active_sid so save_sessions/get_full_state know which session to push
                        if hasattr(self, '_tls'):
                            self._tls.active_sid = _sid
                        try:
                            _t0 = time.time()
                            _res = _exec(_input, _settings, _cd, api=self, target_sid=_sid, tool_use_id=_tuid)
                            # Aborted while this thread was inside the executor. The
                            # abort already wrote a synthetic result and settled the
                            # call, so injecting this one would leave two results under
                            # one tool_use_id and advance a queue that was emptied on
                            # purpose. This thread could not be interrupted, but its
                            # output can be dropped.
                            if _tuid in (_sess.get('_aborted_tool_ids') or []):
                                print(f'[CC ABORT] 丢弃迟到的结果: {_tn} ({_tuid[:25]})', flush=True)
                                return
                            if _res is not None:
                                _res.execution_time_s = time.time() - _t0
                                self._create_tool_result_bubble(_sess, _tuid, _res, _mi, _pi)
                                # Edit autoread and cross-session broadcast
                                if _tn == 'Edit' and not _res.is_error:
                                    _efp = _input.get('file_path')
                                    if _efp:
                                        try:
                                            _en, _en_tl, _en_tr = _prepare_autoread_content(_efp)
                                            if _en:
                                                bt = chr(96) * 3
                                                _ar_id = f'toolu_autoread_{self._next_id()}_0'
                                                _ar_bubble = {'id': self._next_id(), 'role': 'user', 'content': f'**Tool Result** (tool: {_ar_id})\n\n{bt}\n{_en}\n{bt}\n\n<system-reminder>\nThis file was modified by your Edit. The content above is the latest version.\n</system-reminder>', 'summary': f'自动读取 (Edit): {_efp.split("/")[-1]}', 'is_omitted': False, 'is_collapsed': False, 'is_tool_result': True, 'is_auto_read': True, 'auto_read_file': _efp, 'tool_use_id': _ar_id, 'auto_read_trigger_id': _tuid, 'created_at': time.time()}
                                                _sess['conversation_history'].append(_ar_bubble)
                                                self.save_sessions(push_update=True)
                                        except Exception:
                                            pass
                                        if hasattr(self, '_register_file_read'):
                                            self._register_file_read(_efp, _sid)
                                self._continue_autopilot_tool_queue(_sess, _sid)
                        except Exception as _ex:
                            import traceback
                            traceback.print_exc()
                            _err_r = ToolResult(f'{_tn} execution failed: {str(_ex)}', f'{_tn} 失败', is_error=True)
                            self._create_tool_result_bubble(_sess, _tuid, _err_r, _mi, _pi)
                            self._continue_autopilot_tool_queue(_sess, _sid)
                    _t = threading.Thread(target=_run_immediate_executor, daemon=True)
                    _t.start()
                    self._update_part_status(session, msg_index, part_id, 'executing')
                    self.save_sessions(push_update=True)
                    return
                try:
                    _exec_start = time.time()
                    _local_result = _executor(tool_block.get('input', {}), getattr(self, 'global_settings', {}), _cache_dir,
                                              api=self, target_sid=_active_sid, tool_use_id=_tool_use_id)
                    if _local_result is not None:
                        _local_result.execution_time_s = time.time() - _exec_start
                        self._create_tool_result_bubble(session, _tool_use_id, _local_result, msg_index, part_id)
                        # Edit 成功后：为当前会话创建 autoread 并注册引用
                        if _exec_name == 'Edit' and not _local_result.is_error:
                            _edit_fp = tool_block.get('input', {}).get('file_path')
                            if _edit_fp:
                                try:
                                    _en, _en_total_lines, _en_truncated = _prepare_autoread_content(_edit_fp)
                                    if _en is None:
                                        raise Exception('Failed to read file for autoread')
                                    bt = chr(96) * 3
                                    _ar_id = f'toolu_autoread_{self._next_id()}_0'
                                    _ar_bubble = {
                                        'id': self._next_id(),
                                        'role': 'user',
                                        'content': f'**Tool Result** (tool: {_ar_id})\n\n{bt}\n{_en}\n{bt}\n\n<system-reminder>\nThis file was modified by your Edit. The content above is the latest version.\n</system-reminder>',
                                        'summary': f'自动读取 (Edit): {_edit_fp.split("/")[-1]}',
                                        'is_omitted': False,
                                        'is_collapsed': False,
                                        'is_tool_result': True,
                                        'is_auto_read': True,
                                        'auto_read_file': _edit_fp,
                                        'tool_use_id': _ar_id,
                                        'auto_read_trigger_id': _tool_use_id,
                                        'created_at': time.time(),
                                    }
                                    # 构建 tool_use_id → (tool_name, file_path) 映射表
                                    _ar_tui = {}
                                    for _atm in session.get('conversation_history', []):
                                        for _atp in (_atm.get('content_parts') or []):
                                            if _atp.get('type') == 'tool_use_part':
                                                try:
                                                    _atd = json.loads(_atp['content'])
                                                    if _atd.get('id'):
                                                        _ar_tui[_atd['id']] = (_atd.get('name', ''), _atd.get('input', {}).get('file_path', ''))
                                                except Exception:
                                                    pass
                                    for _m in session.get('conversation_history', []):
                                        if _m.get('is_tool_result') and not _m.get('is_outdated_read'):
                                            if _m.get('is_auto_read') and _m.get('auto_read_file') == _edit_fp:
                                                _m['is_outdated_read'] = True
                                                _m['content'] = f"（已省略，概括为：{_edit_fp.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                                            elif not _m.get('is_auto_read'):
                                                _ar_info = _ar_tui.get(_m.get('tool_use_id', ''))
                                                if _ar_info and _ar_info[0] == 'Read' and _ar_info[1] == _edit_fp:
                                                    _m['is_outdated_read'] = True
                                                    _m['content'] = f"（已省略，概括为：{_edit_fp.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                                    session['conversation_history'].append(_ar_bubble)
                                    self.save_sessions(push_update=True)
                                except Exception:
                                    pass
                                if hasattr(self, '_register_file_read'):
                                    self._register_file_read(_edit_fp, _active_sid)
                            # 跨会话广播：向其他持有引用的会话创建 autoread
                            if _edit_fp:
                                _entry = self.file_read_registry.get(_edit_fp)
                                if _entry and isinstance(_entry, dict):
                                    _other_sids = _entry.get('sessions', set()) - {_active_sid}
                                    if not _other_sids:
                                        print(f'[CROSS-SESSION AR] No other sessions for {_edit_fp.split("/")[-1]} (registered: {len(_entry.get("sessions", set()))} sessions, active: {_active_sid[:8]})', flush=True)
                                    if _other_sids:
                                        try:
                                            _bn, _bn_total_lines, _bn_truncated = _prepare_autoread_content(_edit_fp)
                                            if _bn is None:
                                                raise Exception('Failed to read file for broadcast autoread')
                                            bt = chr(96) * 3
                                            for _osid in list(_other_sids):
                                                _os = self.sessions.get(_osid)
                                                if not _os or _os.get('soft_deleted'):
                                                    continue
                                                _bar_id = f'toolu_autoread_{self._next_id()}_0'
                                                _bar_bubble = {
                                                    'id': self._next_id(),
                                                    'role': 'user',
                                                    'content': f'**Tool Result** (tool: {_bar_id})\n\n{bt}\n{_bn}\n{bt}\n\n<system-reminder>\nThis file was modified. The content above is the latest version. Your previous read of this file is now outdated.\n</system-reminder>',
                                                    'summary': f'自动读取 (跨会话修改): {_edit_fp.split("/")[-1]}',
                                                    'is_omitted': False,
                                                    'is_collapsed': False,
                                                    'is_tool_result': True,
                                                    'is_auto_read': True,
                                                    'auto_read_file': _edit_fp,
                                                    'tool_use_id': _bar_id,
                                                    'created_at': time.time(),
                                                }
                                                # 标记旧读取为过时（autoread 和普通 Read 都标记）
                                                _bcast_tui = {}
                                                for _btm in _os.get('conversation_history', []):
                                                    for _btp in (_btm.get('content_parts') or []):
                                                        if _btp.get('type') == 'tool_use_part':
                                                            try:
                                                                _btd = json.loads(_btp['content'])
                                                                if _btd.get('id'):
                                                                    _bcast_tui[_btd['id']] = (_btd.get('name', ''), _btd.get('input', {}).get('file_path', ''))
                                                            except Exception:
                                                                pass
                                                for _bm in _os.get('conversation_history', []):
                                                    if _bm.get('is_tool_result') and not _bm.get('is_outdated_read'):
                                                        if _bm.get('is_auto_read') and _bm.get('auto_read_file') == _edit_fp:
                                                            _bm['is_outdated_read'] = True
                                                            _bm['content'] = f"（已省略，概括为：{_edit_fp.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                                                        elif not _bm.get('is_auto_read'):
                                                            _bm_info = _bcast_tui.get(_bm.get('tool_use_id', ''))
                                                            if _bm_info and _bm_info[0] == 'Read' and _bm_info[1] == _edit_fp:
                                                                _bm['is_outdated_read'] = True
                                                                _bm['content'] = f"（已省略，概括为：{_edit_fp.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                                                _os['conversation_history'].append(_bar_bubble)
                                            self.save_sessions(push_update=True)
                                            print(f'[CROSS-SESSION AR] Broadcast OK: {_edit_fp.split("/")[-1]} → {len(_other_sids)} sessions', flush=True)
                                        except Exception as _be:
                                            print(f'[CROSS-SESSION AR] Broadcast failed: {_be}', flush=True)
                        self._continue_autopilot_tool_queue(session, _active_sid)
                        return
                    # _local_result is None: check if this is an async executor (handles its own result injection)
                    _ASYNC_LOCAL_EXECUTORS = {'创建子会话'}
                    if _exec_name in _ASYNC_LOCAL_EXECUTORS:
                        # Async executor: background thread will inject tool_result later
                        self._update_part_status(session, msg_index, part_id, 'executing')
                        self.save_sessions(push_update=True)
                        return
                    # 如果没有被异步处理，那就失败（外部CC已移除）
                except ToolReject as _tr:
                    self._update_part_status(session, msg_index, part_id, 'pending')
                    if self.socketio:
                        self.socketio.emit('show_toast', {'message': _tr.message, 'type': 'error'})
                    print(f'[TOOL EXECUTOR] ToolReject: {_tr.message[:100]}', flush=True)
                    self.save_sessions(push_update=True)
                    return
                except Exception as _ex:
                    import traceback
                    traceback.print_exc()
                    _err_result = ToolResult(f'{_exec_name} execution failed: {str(_ex)}', f'{_exec_name} 失败', is_error=True)
                    self._create_tool_result_bubble(session, _tool_use_id, _err_result, msg_index, part_id)
                    self._continue_autopilot_tool_queue(session, _active_sid)
                    return

        # 移除外部CC后，如果没有被本地执行器处理，直接报错
        _err_result = ToolResult(f'Tool {_exec_name} is not supported locally and external CC is removed.', f'不支持的工具: {_exec_name}', is_error=True)
        self._create_tool_result_bubble(session, _tool_use_id, _err_result, msg_index, part_id)
        self._continue_autopilot_tool_queue(session, _active_sid)

    def _continue_autopilot_tool_queue(self, session, target_sid):
        """工具确定后推进队列或自动托管。每次工具返回结果后调用。逻辑：
        1. 有排队等待的工具 → 释放下一个 → return 等它完成
        2. 没有排队的了 → 检查是否所有工具都已返回 → 有未返回的 → return 继续等
        3. 全部返回了 → 推进自动托管下一轮（或 finalize）
        """
        # 第 1 步：释放下一个排队的工具
        if self._advance_approved_queue(session, target_sid):
            self.save_sessions(push_update=True)
            return

        # 第 2 步：只检查最近一条含工具调用的 assistant 消息的工具是否全部返回
        # 不检查全部历史，避免老消息中的残留工具阻塞推进
        _latest_tool_msg = None
        for _m in reversed(session.get('conversation_history', [])):
            if _m.get('role') == 'assistant' and _m.get('content_parts'):
                if any(_p.get('type') == 'tool_use_part' for _p in _m.get('content_parts', [])):
                    _latest_tool_msg = _m
                    break
        if _latest_tool_msg:
            for _p in _latest_tool_msg.get('content_parts', []):
                if _p.get('type') == 'tool_use_part' and _p.get('status') in ('pending', 'adopted', 'executing'):
                    _tid = _p.get('tool_id') or ''
                    if _tid and not self._is_tool_settled(session, _tid):
                        _tname = _p.get('tool_name') or '?'
                        print(f"[CONTINUE QUEUE] 等待工具返回: {_tname} ({_tid[:25]}), status={_p.get('status')}", flush=True)
                        return  # 还有工具未返回，继续等
            print(f"[CONTINUE QUEUE] 最近工具消息 (msg_id={_latest_tool_msg.get('id')}) 的所有工具已全部返回", flush=True)

        # 第 3 步：所有工具已返回，原子性地抢占推进权
        # session.pop 在 CPython 中由 GIL 保证原子性——多线程同时到达时，只有一个线程能 pop 到非 None 值
        _ap_sid = session.pop('_ap_tool_sid', None)
        if _ap_sid is None:
            return  # 非托管模式，或另一个线程已经抢先推进了
        # 代际守卫：检查最近工具消息的代际是否为当前代际
        if _latest_tool_msg:
            _ltm_gen = _latest_tool_msg.get('_autopilot_gen', -1)
            _sess_gen = session.get('_autopilot_gen', 0)
            if _ltm_gen != _sess_gen:
                print(f"[CONTINUE QUEUE GEN] 过期轮次跳过续接: msg_gen={_ltm_gen}, sess_gen={_sess_gen}", flush=True)
                self.finalize_process(_ap_sid)
                return
        _ap_auto_text = session.pop('_ap_tool_auto_text', '')
        if session.get('autopilot_active') and session.get('autopilot_turns_left', 0) > 0:
            self._apply_sliding_window(session)
            session['autopilot_turns_left'] -= 1
            session['autopilot_total_steps'] = session.get('autopilot_total_steps', 0) + 1
            if session['autopilot_turns_left'] <= 0:
                session['autopilot_active'] = False
                self.finalize_process(_ap_sid)
            else:
                session['_autopilot_gen'] = session.get('_autopilot_gen', 0) + 1
                _ap_text = self._make_autopilot_prompt(session, append=_ap_auto_text)
                self.send_message(_ap_text, [session.get('autopilot_model')], is_early=True, is_deep_think=session.get('_deep_think_active', False), sid=_ap_sid)
        else:
            self.finalize_process(_ap_sid)

    def abort_tool(self, msg_index, part_id, target_sid=None, scope='all'):
        """Abort a tool call. scope='one' settles just that one; 'all' also stops
        autopilot and drops the queue behind it.

        scope='one' is genuinely per-tool, not a cosmetic distinction. Recording
        the id and writing a rejected synthetic result makes _is_tool_settled
        accept the call as decided, which lets _continue_autopilot_tool_queue
        release the siblings behind it — calling it here is correct, and exactly
        the opposite of the 'all' path below.

        What neither scope can do is isolate a thread already inside an executor,
        so that limit is shared and the granularity difference is real.

        Cooperative, and the wording matters because the limit is real: Python
        cannot safely kill a thread and the local executors run in bare daemon
        threads, so a step already inside an executor will finish. What is
        guaranteed is that its result is discarded on arrival and that nothing
        else gets scheduled.

        Five steps, none of them optional:

        1. Record the id, so a late result from a still-running thread is dropped.
        2. Write a synthetic tool_result. This is the only way the call becomes
           settled — _is_tool_settled looks for a result or a rejected status —
           and an unsettled call blocks every sibling behind it indefinitely.
        3. Reject and clear _approved_tool_queue. stop_autopilot does not touch
           it, so anything left there is released by the next
           _advance_approved_queue: exactly the "stop the tools behind it" this
           is supposed to prevent.
        4. Pop _ap_tool_sid. Also untouched by stop_autopilot, and while it
           survives _continue_autopilot_tool_queue can still claim the right to
           advance and start another turn.
        5. finalize_process, which is what clears is_processing. Without it the
           composer stays disabled by setUIEnabled(!is_processing) and the abort
           reads as having failed.

        _continue_autopilot_tool_queue is deliberately not called: releasing the
        next tool is its entire job.
        """
        sid = target_sid or self._active_sid
        session = self.sessions.get(sid)
        if not session:
            return {'status': 'error', 'message': '会话不存在'}
        _msg, part, tool_use_id = self._resolve_part_tool_id(session, msg_index, part_id)
        if not part:
            return {'status': 'error', 'message': '找不到该工具调用'}
        if not tool_use_id:
            return {'status': 'error', 'message': '该工具调用缺少 tool_id，无法中止'}

        _ab = session.setdefault('_aborted_tool_ids', [])
        if tool_use_id not in _ab:
            _ab.append(tool_use_id)
        # Bounded: this field persists on a session that may run for weeks, while
        # only a result arriving shortly after the abort is ever tested against it.
        if len(_ab) > 200:
            del _ab[:-200]

        from .tool_executors import ToolResult

        if scope == 'one':
            # Autopilot, the approved queue and _ap_tool_sid are all left alone.
            # Those belong to "stop the batch", and folding them in here would mean
            # aborting one tool silently halted everything — the granularity lie
            # this scope exists to remove.
            _one = ('用户中止了该工具调用。同一回复中的其他工具调用不受影响。'
                    '\n<system-reminder>\nThe user aborted this single tool call. The other '
                    'tool calls in this turn are unaffected and will proceed. Do not retry '
                    'this one on your own.\n</system-reminder>')
            _res1 = ToolResult(_one, '工具调用被用户中止', is_error=True, part_status='rejected')
            self._create_tool_result_bubble(session, tool_use_id, _res1, msg_index, part_id)
            # Settled as rejected, so releasing the siblings is the right move —
            # the opposite of the 'all' path, which must not advance anything.
            self._continue_autopilot_tool_queue(session, sid)
            print(f'[CC ABORT ONE] {part.get("tool_name") or "?"} ({tool_use_id[:25]}) 已中止，'
                  f'其余工具继续', flush=True)
            return {'status': 'ok', 'scope': 'one', 'dropped': 0, 'autopilot_stopped': False}

        _was_autopilot = bool(session.get('autopilot_active'))
        _dropped = 0
        for _item in (session.get('_approved_tool_queue') or []):
            _qid = _item.get('tool_use_id')
            if _qid and _qid not in _ab:
                _ab.append(_qid)
            self._update_part_status(session, _item.get('msg_index'),
                                     _item.get('part_id'), 'rejected')
            _dropped += 1
        session['_approved_tool_queue'] = []
        session.pop('_ap_tool_sid', None)
        session.pop('_ap_tool_auto_text', None)
        # The same field set stop_autopilot writes, so the button, the badge and
        # the kanban section all agree about the state.
        session['autopilot_active'] = False
        session['autopilot_turns_left'] = 0
        session['_deep_think_active'] = False
        session['trigger_autopilot'] = False

        _note = '用户中止了该工具调用。'
        if _was_autopilot:
            _note += '托管已停止。'
        if _dropped:
            _note += f'另有 {_dropped} 个排队中的工具调用被一并取消。'
        _note += ('\n<system-reminder>\nThe user aborted this tool call. Do not retry it '
                  'on your own and do not start the next step; wait for the user to say '
                  'what to do next.\n</system-reminder>')
        _res = ToolResult(_note, '工具调用被用户中止', is_error=True, part_status='rejected')
        self._create_tool_result_bubble(session, tool_use_id, _res, msg_index, part_id)
        self.finalize_process(sid)
        print(f'[CC ABORT] {part.get("tool_name") or "?"} ({tool_use_id[:25]}) 已中止；'
              f'清空队列 {_dropped} 个；托管='
              f'{"已停止" if _was_autopilot else "本未运行"}', flush=True)
        return {'status': 'ok', 'scope': 'all', 'dropped': _dropped,
                'autopilot_stopped': _was_autopilot}

    def _register_file_read(self, file_path, sid):
        """Register that a session holds a read reference to a file. Thread-safe via GIL.
        Also updates last_access timestamp and ensures watchdog monitors the file's directory."""
        if not file_path or not sid:
            return
        entry = self.file_read_registry.setdefault(file_path, {'sessions': set(), 'last_access': 0})
        # 兼容旧格式（set → dict 迁移）
        if isinstance(entry, set):
            entry = {'sessions': entry, 'last_access': time.time()}
            self.file_read_registry[file_path] = entry
        entry['sessions'].add(sid)
        entry['last_access'] = time.time()
        # Cache mtime and content hash for polling-based change detection
        try:
            _stat = os.stat(file_path)
            entry['mtime'] = _stat.st_mtime
            with open(file_path, 'rb') as _f:
                entry['content_hash'] = hashlib.md5(_f.read()).hexdigest()
        except (OSError, IOError):
            pass


    def _check_registered_file_changes(self, sid, trigger_tool_id=None, insert_before_id=None):
        """Check registered files for external modifications before context assembly.

        Uses mtime as fast pre-check, then content hash for confirmation.
        If a file has changed since last cached state, injects autoread into the current session.
        Called at the start of api_worker_thread to detect external edits (sed -i, echo >, cp, etc.).
        Performance: 100 files with no changes → ~10ms (stat-only).

        Args:
            sid: Target session ID to inject autoread into.
            trigger_tool_id: If provided, sets auto_read_trigger_id on injected bubbles
                            for frontend absorption below the triggering Edit block.
            insert_before_id: If provided, insert autoread bubbles before the message
                            with this ID instead of appending at the end.
        """
        changed_files = []
        for file_path, entry in list(self.file_read_registry.items()):
            if isinstance(entry, set):
                continue  # Old format without hash cache, skip
            if sid not in entry.get('sessions', set()):
                continue  # This session doesn't hold a reference to this file
            if not os.path.exists(file_path):
                continue
            # Fast pre-check: mtime comparison (avoids hash computation for unchanged files)
            try:
                current_mtime = os.stat(file_path).st_mtime
            except (OSError, IOError):
                continue
            cached_mtime = entry.get('mtime')
            if cached_mtime is not None and current_mtime == cached_mtime:
                continue  # mtime unchanged → file definitely not modified
            # mtime changed → compute content hash to confirm actual content change
            try:
                with open(file_path, 'rb') as _f:
                    current_hash = hashlib.md5(_f.read()).hexdigest()
            except (OSError, IOError):
                continue
            cached_hash = entry.get('content_hash')
            if cached_hash is None:
                # No cached hash (registration bypassed _register_file_read, or legacy entry)
                # Treat as potential change and broadcast — this only fires once per file
                # since we set content_hash here for subsequent checks
                entry['mtime'] = current_mtime
                entry['content_hash'] = current_hash
                changed_files.append(file_path)
                continue
            if current_hash == cached_hash:
                # mtime changed but content identical (e.g., touch) → update mtime cache only
                entry['mtime'] = current_mtime
                continue
            # Content genuinely changed → update cache BEFORE broadcast to prevent duplicate detection
            entry['mtime'] = current_mtime
            entry['content_hash'] = current_hash
            changed_files.append(file_path)
        # Inject autoread into ALL registered sessions for each changed file
        if not changed_files:
            return
        bt = chr(96) * 3
        _injected = 0
        for file_path in changed_files:
            from .tool_executors import _prepare_autoread_content as _pac
            _numbered, _chk_total_lines, _chk_truncated = _pac(file_path)
            if _numbered is None:
                continue
            # Broadcast to ALL sessions registered for this file
            _file_entry = self.file_read_registry.get(file_path, {})
            _all_file_sids = _file_entry.get('sessions', set()) if isinstance(_file_entry, dict) else set()
            for _target_sid in list(_all_file_sids):
                _target_session = self.sessions.get(_target_sid)
                if not _target_session or _target_session.get('soft_deleted'):
                    continue
                _ar_id = f'toolu_autoread_{self._next_id()}_0'
                _ar_bubble = {
                    'id': self._next_id(),
                    'role': 'user',
                    'content': f'**Tool Result** (tool: {_ar_id})\n\n{bt}\n{_numbered}\n{bt}\n\n<system-reminder>\nThis file was modified. The content above is the latest version. Your previous read of this file is now outdated.\n</system-reminder>',
                    'summary': f'自动读取 (外部修改): {file_path.split("/")[-1]}',
                    'is_omitted': False,
                    'is_collapsed': False,
                    'is_tool_result': True,
                    'is_auto_read': True,
                    'auto_read_file': file_path,
                    'tool_use_id': _ar_id,
                    'created_at': time.time(),
                }
                # For the triggering session: use trigger_tool_id and insert_before_id
                if _target_sid == sid:
                    _effective_trigger = trigger_tool_id
                    if not _effective_trigger:
                        for _m in reversed(_target_session.get('conversation_history', [])):
                            if _m.get('content_parts'):
                                for _p in reversed(_m.get('content_parts', [])):
                                    if _p.get('type') == 'tool_use_part':
                                        try:
                                            _td = json.loads(_p['content'])
                                            if _td.get('name') in ('Read', 'Edit') and _td.get('input', {}).get('file_path') == file_path:
                                                _effective_trigger = _td.get('id')
                                        except Exception:
                                            pass
                                        if _effective_trigger:
                                            break
                            if _effective_trigger:
                                break
                    if _effective_trigger:
                        _ar_bubble['auto_read_trigger_id'] = _effective_trigger
                    if insert_before_id:
                        _insert_idx = None
                        for _idx, _m in enumerate(_target_session['conversation_history']):
                            if _m.get('id') == insert_before_id:
                                _insert_idx = _idx
                                break
                        if _insert_idx is not None:
                            _target_session['conversation_history'].insert(_insert_idx, _ar_bubble)
                        else:
                            _target_session['conversation_history'].append(_ar_bubble)
                    else:
                        _target_session['conversation_history'].append(_ar_bubble)
                else:
                    # For other sessions: just append at end
                    _target_session['conversation_history'].append(_ar_bubble)
                _injected += 1
                # Mark old reads as outdated, including normal Read tool_results
                try:
                    _chk_tui = {}
                    for _ctm in _target_session.get('conversation_history', []):
                        for _ctp in (_ctm.get('content_parts') or []):
                            if _ctp.get('type') == 'tool_use_part':
                                try:
                                    _ctd = json.loads(_ctp['content'])
                                    if _ctd.get('id'):
                                        _chk_tui[_ctd['id']] = (_ctd.get('name', ''), _ctd.get('input', {}).get('file_path', ''))
                                except Exception:
                                    pass
                    for _m in _target_session.get('conversation_history', []):
                        if _m is _ar_bubble:
                            continue
                        if _m.get('is_tool_result') and not _m.get('is_outdated_read'):
                            if _m.get('is_auto_read') and _m.get('auto_read_file') == file_path:
                                _m['is_outdated_read'] = True
                                _m['content'] = f"（已省略，概括为：{file_path.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                            elif not _m.get('is_auto_read'):
                                _chk_info = _chk_tui.get(_m.get('tool_use_id', ''))
                                if _chk_info and _chk_info[0] == 'Read' and _chk_info[1] == file_path:
                                    _m['is_outdated_read'] = True
                                    _m['content'] = f"（已省略，概括为：{file_path.split('/')[-1]} 的旧版本读取结果，已被更新的读取替代）"
                except Exception:
                    pass
        if _injected > 0:
            self.save_sessions(push_update=True)
            print(f'[FILE CHANGE] Injected autoread for {_injected} file-session pairs', flush=True)








    def _save_file_registry(self):
        """Persist file_read_registry to disk as JSON. Sets are converted to lists."""
        try:
            _path = os.path.join(getattr(self, 'data_dir', 'data'), 'file_registry.json')
            _data = {}
            for fp, entry in self.file_read_registry.items():
                if isinstance(entry, dict):
                    _saved = {'sessions': list(entry.get('sessions', set())), 'last_access': entry.get('last_access', 0)}
                    if entry.get('mtime') is not None:
                        _saved['mtime'] = entry['mtime']
                    if entry.get('content_hash') is not None:
                        _saved['content_hash'] = entry['content_hash']
                    _data[fp] = _saved
                elif isinstance(entry, set):
                    _data[fp] = {'sessions': list(entry), 'last_access': time.time()}
            _tmp = _path + '.tmp'
            with open(_tmp, 'w', encoding='utf-8') as _f:
                import json as _frj
                _frj.dump(_data, _f, ensure_ascii=False)
            os.replace(_tmp, _path)
        except Exception as _e:
            print(f'[FILE REGISTRY] Save failed: {_e}', flush=True)

    def _load_file_registry(self):
        """Load file_read_registry from disk. Lists are converted back to sets.
        After loading, starts watchdog and monitors all registered directories."""
        _path = os.path.join(getattr(self, 'data_dir', 'data'), 'file_registry.json')
        if not os.path.exists(_path):
            return
        try:
            import json as _frj
            with open(_path, 'r', encoding='utf-8') as _f:
                _data = _frj.load(_f)
            for fp, entry in _data.items():
                if isinstance(entry, dict):
                    _loaded_entry = {
                        'sessions': set(entry.get('sessions', [])),
                        'last_access': entry.get('last_access', 0)
                    }
                    # Restore saved mtime/hash (fast: no disk I/O during startup)
                    if entry.get('mtime') is not None:
                        _loaded_entry['mtime'] = entry['mtime']
                        _loaded_entry['content_hash'] = entry.get('content_hash')
                    self.file_read_registry[fp] = _loaded_entry
            print(f'[FILE REGISTRY] Loaded {len(self.file_read_registry)} entries from disk', flush=True)
        except Exception as _e:
            print(f'[FILE REGISTRY] Load failed: {_e}', flush=True)



 

