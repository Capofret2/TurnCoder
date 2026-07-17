"""Api mixin: Code block undo and status update helpers."""


class CodeHelpersMixin:
    """Api mixin: Code block undo and status update helpers."""

    def update_block_status(self, index, part_id, status):
        session = self._session
        if 0 <= index < len(session['conversation_history']):
            msg = session['conversation_history'][index]
            for p in msg.get('content_parts', []):
                if p.get('id') == part_id:
                    p['status'] = status
                    # tool_use_part 被拒绝时，生成假的 tool_result 气泡（和拦截器一致的行为）
                    if status == 'rejected' and p.get('type') == 'tool_use_part':
                        import json
                        try:
                            _tc_data = json.loads(p.get('content', '{}'))
                            _tc_id = _tc_data.get('id', '')
                            _tc_name = _tc_data.get('name', 'unknown')
                        except Exception:
                            _tc_id = ''
                            _tc_name = 'unknown'
                        bt = chr(96) * 3
                        fake_result = {
                            "id": self._next_id(),
                            "role": "user",
                            "content": f"**Tool Error** (tool: {_tc_id})\n\n{bt}\n<system-reminder>\nThe user has rejected this tool call ({_tc_name}). Do not retry this exact operation.\n</system-reminder>\n{bt}",
                            "summary": f"User rejected: {_tc_name}",
                            "is_omitted": False,
                            "is_collapsed": True,
                            "is_tool_result": True,
                            "tool_use_id": _tc_id,
                        }
                        session['conversation_history'].append(fake_result)
                        # 用户拒绝工具调用后，推进排序引擎释放下一个已批准的工具
                        for _s in self.sessions.values():
                            if _s is session:
                                _sid = next((k for k, v in self.sessions.items() if v is session), None)
                                if _sid:
                                    self._continue_autopilot_tool_queue(session, _sid)
                                break
        self.save_sessions(push_update=True)


    def undo_code_block(self, index, part_id):
        session = self._session
        if not session:
            return

        backup = session.get('code_backups', {}).pop(part_id, None)

        if not backup:
            print(f"撤销失败：未找到 part_id {part_id} 的备份")
            return

        try:
            with open(backup['path'], 'w', encoding='utf-8') as f:
                f.write(backup['content'])

            if 0 <= index < len(session['conversation_history']):
                msg = session['conversation_history'][index]
                for p in msg.get('content_parts', []):
                    if p.get('id') == part_id:
                        p['status'] = 'pending'
                        break
            
            print(f"成功撤销对 {backup['path']} 的修改")
        except Exception as e:
            print(f"撤销 part_id {part_id} 期间发生错误: {str(e)}")
            session.get('code_backups', {})[part_id] = backup # 恢复失败时，将备份放回去
        finally:
            self.save_sessions()


