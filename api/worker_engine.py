"""Api mixin: API worker thread - context building, model routing, and HTTP execution."""
import copy
import json
import os
import re
import requests
import time
from .provider_routes import route_provider, strip_composite, is_model_segmented


class _TimedUpload:
    """File-like wrapper that records when the HTTP body is fully consumed by the transport layer.

    urllib3 reads the body in 8192-byte chunks via read(). When the final chunk is read,
    the preceding chunks have already been passed to socket.send() which blocks on TCP flow
    control for large payloads. So read_complete_time closely tracks actual wire upload completion.
    """
    __slots__ = ('_data', '_pos', 'read_complete_time')

    def __init__(self, data):
        self._data = data.encode('utf-8') if isinstance(data, str) else data
        self._pos = 0
        self.read_complete_time = None

    def read(self, size=-1):
        if self._pos >= len(self._data):
            if self.read_complete_time is None:
                self.read_complete_time = time.time()
            return b''
        if size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + size]
            self._pos += size
        if self._pos >= len(self._data) and self.read_complete_time is None:
            self.read_complete_time = time.time()
        return chunk

    def __len__(self):
        return len(self._data)


class WorkerEngineMixin:
    """Api mixin: API worker thread - context building, model routing, and HTTP execution."""

    @staticmethod
    def _apply_corrections(text):
        """Apply correction block diffs to the preceding text. Pure function."""
        corr_matches = list(re.finditer(r'`{3}correction\n(.*?)`{3}', text, re.DOTALL))
        for corr_match in corr_matches:
            corr_content = corr_match.group(1)
            search_regex = r'<{4}\n(.*?)\n={4}\n(.*?)\n>{4}'
            for diff in re.finditer(search_regex, corr_content, re.DOTALL):
                search_str = diff.group(1)
                replace_str = diff.group(2)
                if search_str.strip():
                    diff_text = f"<del>{search_str}</del><ins>{replace_str}</ins>"
                    idx = text.find(corr_match.group(0))
                    if idx != -1:
                        prefix = text[:idx]
                        suffix = text[idx:]
                        if search_str in prefix:
                            r_idx = prefix.rfind(search_str)
                            prefix = prefix[:r_idx] + diff_text + prefix[r_idx + len(search_str):]
                            text = prefix + suffix
                        else:
                            text = text.replace(search_str, diff_text, 1)
                    else:
                        text = text.replace(search_str, diff_text, 1)
        return text

    def api_worker_thread(self, sid, stage=None, total=None, is_parallel=False, target_id=None, is_offline=False, model_name=None, is_deep_think=False):
        session = self.sessions.get(sid)
        if not session:
            return

        # Abort if target bubble has been deleted (user deleted during retry cycle)
        if target_id is not None:
            _deleted_ids = session.get('deleted_msg_ids', [])
            _target_exists = any(m['id'] == target_id for m in session.get('conversation_history', []))
            if target_id in _deleted_ids or not _target_exists:
                print(f'[RETRY ABORT] target_id={target_id} no longer exists (deleted by user), aborting worker thread', flush=True)
                # Clean up retry counters
                if hasattr(self, '_request_retry_count'):
                    self._request_retry_count.pop(target_id, None)
                if hasattr(self, '_ttfb_retry_count'):
                    self._ttfb_retry_count.pop(target_id, None)
                if hasattr(self, '_rate_limit_retry_count'):
                    self._rate_limit_retry_count.pop(target_id, None)
                if hasattr(self, '_dt3_retry_count'):
                    self._dt3_retry_count.pop(target_id, None)
                return

        # Polling-based file change detection: check registered files before context assembly
        # Skip when prefix lock is enabled to avoid auto-read injections breaking cached prefix
        if not getattr(self, 'global_settings', {}).get('enable_prefix_lock', False):
            self._check_registered_file_changes(sid, insert_before_id=target_id)

        # ARC3 环境路由：完全绕过 LLM 上下文组装
        _arc3_check = model_name or self.config.get("MODEL_NAME", "")
        if strip_composite(_arc3_check).startswith("[ARC3]"):
            self._handle_arc3_step(sid, session, target_id, _arc3_check)
            return

        # 生图模型路由：完全绕过 LLM 上下文组装，走 /v1/images/generations 端点
        _img_model_check = strip_composite(model_name or self.config.get("MODEL_NAME", "")).lower()
        _is_image_gen = any(kw in _img_model_check for kw in ('gpt-image', 'dall-e', 'dalle', 'flux', 'midjourney', 'stable-diffusion'))
        if _is_image_gen:
            self._handle_image_generation(sid, session, target_id, model_name or self.config.get("MODEL_NAME", ""))
            return

        settings = getattr(self, 'global_settings', {})
        _context_start = time.time()  # 上下文组装计时起点
        
        if not settings.get('enable_pure_mode', False):
            full_context = self.get_system_prompt()

            # Planned tools mode: replace serial execution descriptions in system prompt
            if settings.get('enable_planned_tools', False):
                _sys_serial_replacements = [
                    ('工具调用是严格串行的，这意味着你可以提前写任意多的工具调用，批处理，它们会按计划执行。', '立即工具并行执行，计划工具按等待清单在依赖成功后自动执行。你应该大量使用计划调用来表达多步操作链。'),
                    ('比如如果你计划等安装某个库看到工具调用返回成功之后用这个库来进行下一步的工作，你应该直接在这一个气泡里写下一步的工具调用，不要等到你亲眼看到那个安装成功的返回。', '比如安装库后要使用它，写一个计划调用等待安装成功后自动执行使用步骤。永远不要说要等结果才写下一步。'),
                    ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。如果你需要执行耗时较长的多个任务并且希望并行以减少等待时间，在同一个消息中写一个python文件并且运行来科学且现代化地实现并行。', '多个立即工具同时并行执行。需要等待结果的操作使用计划调用声明依赖即可，不需要写python脚本来实现并行。'),
                    ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。', '多个立即工具同时并行执行，计划工具在依赖满足后自动执行。'),
                    ('也就是并行发出，串行执行。', '立即工具并行发出并行执行，计划工具按等待清单在依赖成功后执行。'),
                    ('把单一工具调用尽量拆分成多个按顺序的工具调用，只有当你必须亲自看到某个工具调用的返回值才能写后续的工具调用时允许你停下写更多的工具调用并结束这一次气泡的工具调用编写。', '尽量在一次回复中写尽可能多的工具调用。不需要等待结果，直接用计划调用声明依赖关系。只有当你完全无法预测下一步需要什么时才允许停下。'),
                ]
                for _old_sys, _new_sys in _sys_serial_replacements:
                    full_context = full_context.replace(_old_sys, _new_sys)

            # CC 注入模式下清洗系统提示词：移除 chatapp 自带工具的占位指令（规则9代码块、规则10代码监听、规则12终端操作），避免与 CC 工具冲突
            if settings.get('enable_tool_inject', False):
                full_context = full_context.replace('\r\n', '\n').replace('\r', '\n')  # Windows换行符归一化
                for _rn in [9, 10, 12]:
                    _pat = r'\n' + str(_rn) + r'[.．][\s\S]*?(?=\n\d{1,2}[.．]|\Z)'
                    full_context = re.sub(_pat, '', full_context)

            b36_token = self._to_base36(target_id)
            if settings.get('enable_routing_token', True):
                full_context = full_context.replace("[令牌xx]", f"[令牌{b36_token}]")
                full_context = full_context.replace("<<<xx>>>", f"<<<{b36_token}>>>")
            else:
                # 开关关闭时移除占位符，避免静态文本中残留令牌字样
                full_context = full_context.replace("[令牌xx]", "")
                full_context = full_context.replace("<<<xx>>>", "")
            
            if not settings.get('enable_tool_inject', False):
                code_ctx = self.get_code_context(sid)
                if code_ctx:
                    full_context += f"\n\n[实时读取的代码上下文]\n{code_ctx}\n[代码上下文结束]"
        else:
            full_context = ""
        _is_cc = settings.get('enable_tool_inject', False)
        if not _is_cc and settings.get('enable_correction', True):
            full_context += "\n12.纠错空间与文本自修正协议：\n    - 你必须在每次回复的较长文本中，将其自然划分为2到4个块，并在每个块结尾使用如下格式附带一个“纠错空间”。\n    - 格式必须严格为（注意包裹在markdown代码块中）：\n" + "`"*3 + "correction\n[你的自我反思、微型思维链，或留空]\n" + "<"*4 + "\n[需要被替换的原文（必须是该纠错空间之前、且在同一个气泡内的确切文本）]\n" + "="*4 + "\n[替换后的新文本]\n" + ">"*4 + "\n" + "`"*3 + "\n    - `" + "<"*4 + "`、`" + "="*4 + "`、`" + ">"*4 + "` 仅在需要修改前面的文本时才使用。查找内容必须精确匹配。\n    - 不要重写全文，只需在纠错空间输出局部替换指令即可，系统会自动将原文渲染为划线。"

        if not _is_cc and settings.get('enable_routing_token', True):
            b36_token = self._to_base36(target_id)
            msg_str = f"[系统指令：本轮回复匹配令牌为 {b36_token}。必须且只能在回复最开头输出精确的 [令牌{b36_token}] 作为凭证]"
            full_context += f"\n\n{msg_str}\n{msg_str}\n{msg_str}"
        if not _is_cc and is_parallel:
            full_context += f"\n\n[任务拆解规则：并行模式。你会知道自己处于第几个并行分支。独立判断并执行你该承担的子任务。]"
            full_context += f"\n[并行拆解监控：你当前处于第 {stage} 个并行任务分支，总计 {total} 个分支。请独立完成该阶段任务。]"
        elif not _is_cc and session['max_steps'] > 1:
            full_context += f"\n\n[任务拆解规则：顺序模式。需继续请输出信号：---NEXT_STEP---。]"
            full_context += f"\n[任务拆解监控：当前处于第 {session['current_chain_steps'] + 1} 步，最大允许拆解为 {session['max_steps']} 步]"
        
        is_reverse = settings.get('enable_reverse_context', False)
        if is_reverse:
            full_context = "[重要：本对话上下文采用时间倒序排列，最新的消息在最前面，最旧的消息在最后面。系统提示词和规则在末尾。请据此理解对话脉络。]\n\n" + full_context

        _msg_blocks = []
        _img_marker_counter = 0
        _img_marker_map = {}  # marker_string -> image_url_block (for interleaving images in context)
        last_user_idx = -1
        for i, m in enumerate(session['conversation_history']):
            if m['id'] == target_id:
                break
            if m.get('role') == 'user' and not m.get('is_hidden', False) and not m.get('is_error', False):
                last_user_idx = i

        if settings.get('enable_starred', True) and settings.get('starred_messages'):
            full_context += "\n\n[全局收藏的上下文]"
            for s_msg in settings.get('starred_messages', []):
                content = s_msg.get('diff_content', s_msg.get('content', ''))
                content = self._apply_corrections(content)
                token_k = len(content) / 3000
                role = s_msg.get('role', 'user').capitalize()
                full_context += f"\n[预估: {token_k:.2f}k Tokens] {role}: {content}"
            full_context += "\n[全局收藏上下文结束]"

        _system_portion_end = len(full_context)
        for i, msg in enumerate(session['conversation_history']):
            if msg['id'] == target_id:
                break

            if msg.get('is_hidden', False):
                continue

            if i > last_user_idx and msg.get('role') == 'assistant' and not msg.get('content') and not msg.get('content_parts'):
                continue

            if msg.get('is_error', False):
                continue

            content_to_render = ""
            total_len = 0
            if msg.get('is_outdated_read'):
                _orf = msg.get('auto_read_file', msg.get('read_file_path', ''))
                content_to_render = f"（此文件的读取结果已过时，已有更新的自动读取结果：{_orf}）"
                total_len = len(content_to_render)
            elif msg.get('is_omitted'):
                content_to_render = f"（已省略，概括为：{msg.get('summary', '无概括')}）"
                total_len = len(content_to_render)
            elif msg.get('diff_content'):
                content_to_render = msg['diff_content']
                content_to_render = self._apply_corrections(content_to_render)
                total_len = len(content_to_render)
            elif msg.get('content_parts'):
                _has_tool_parts = any(p.get('type') == 'tool_use_part' for p in msg['content_parts'])
                if _has_tool_parts:
                    content_to_render = msg.get('content', '')
                    content_to_render = self._apply_corrections(content_to_render)
                    total_len = len(content_to_render)
                else:
                    temp_parts = copy.deepcopy(msg['content_parts'])
                
                    # 提前将 correction 差异注入到对应的文本切片中
                    for p_idx, part in enumerate(temp_parts):
                        if part['type'] == 'correction':
                            search_regex = r'<{4}\n(.*?)\n={4}\n(.*?)\n>{4}'
                            matches = re.finditer(search_regex, part['content'], re.DOTALL)
                            for match in matches:
                                search_str = match.group(1)
                                replace_str = match.group(2)
                                if search_str.strip():
                                    for i in range(p_idx - 1, -1, -1):
                                        if temp_parts[i]['type'] == 'text' and search_str in temp_parts[i]['content']:
                                            diff_text = f"<del>{search_str}</del><ins>{replace_str}</ins>"
                                            temp_parts[i]['content'] = temp_parts[i]['content'].replace(search_str, diff_text, 1)
                                            break

                    parts = []
                    for part in temp_parts:
                        if part['type'] == 'text':
                            parts.append(part['content'])
                        elif part['type'] == 'code':
                            status_map = {'pending': '待处理', 'adopted': '已采用', 'rejected': '未采用', 'failed': '采用失败'}
                            status_text = status_map.get(part.get('status'), '未知')
                            parts.append(f"[代码块操作 - 状态: {status_text}]\n{'`'*3}\n{part['content']}\n{'`'*3}")
                        elif part['type'] == 'correction':
                            parts.append(f"\n{'`'*3}correction\n{part['content']}\n{'`'*3}\n")
                        elif part['type'] == 'terminal':
                            status_map = {'pending': '待处理', 'adopted': '已执行', 'rejected': '已跳过', 'failed': '执行失败'}
                            status_text = status_map.get(part.get('status'), '未知')
                            parts.append(f"[终端操作 - 状态: {status_text}]\n{'`'*3}terminal\n{part['content']}\n{'`'*3}")
                    content_to_render = "\n\n".join(parts)
                    total_len = sum(len(p['content']) for p in msg['content_parts'])
            else:
                content_to_render = msg['content']
                if msg['role'] == 'assistant' and not content_to_render:
                    content_to_render = "（已省略。上文的问题已解决。请勿在下文重复回答已解决的问题。）"
                elif not _is_cc and msg['role'] == 'user' and (len(content_to_render) / 3) < 300:
                    content_to_render = (content_to_render + "\n") * 3
                
                content_to_render = self._apply_corrections(content_to_render)
                total_len = len(msg['content'])
                
            # 部分读入过滤：enable_partial_read 开启时，对 Read 结果 bubble 按可见行集合过滤
            if settings.get('enable_partial_read', False) and msg.get('is_tool_result'):
                _pr_file = msg.get('_read_file_path') or msg.get('auto_read_file')
                # 回溯查找：如果 bubble 没有直接标记文件路径，通过 tool_use_id 关联到对应的 Read 工具调用
                if not _pr_file and msg.get('tool_use_id'):
                    _pr_tuid = msg['tool_use_id']
                    for _pr_cm in session.get('conversation_history', []):
                        if _pr_cm.get('content_parts'):
                            for _pr_cp in _pr_cm['content_parts']:
                                if _pr_cp.get('type') == 'tool_use_part' and _pr_cp.get('tool_id') == _pr_tuid:
                                    if _pr_cp.get('tool_name') == 'Read':
                                        _pr_file = (_pr_cp.get('tool_input') or {}).get('file_path')
                                    break
                            if _pr_file:
                                break
                if _pr_file:
                    _pr_state = session.get('_partial_read_state', {}).get(_pr_file)
                    if _pr_state:
                        _pr_visible = _pr_state.get('visible_lines', [])
                        if isinstance(_pr_visible, list):
                            _pr_visible = set(_pr_visible)
                        elif not isinstance(_pr_visible, set):
                            _pr_visible = set()
                        if _pr_visible:
                            # 行号偏移维护：如果 reference_lines 与当前 bubble 内容不同，用 difflib 映射
                            _pr_ref_lines = _pr_state.get('reference_lines', [])
                            # 从 content_to_render 中提取实际文件行（cat -n 格式: "行号\t内容"）
                            _pr_content_lines = []
                            _pr_in_code_block = False
                            _pr_code_lines = []
                            for _pr_line in content_to_render.split('\n'):
                                if _pr_line.startswith('```') and not _pr_in_code_block:
                                    _pr_in_code_block = True
                                    continue
                                elif _pr_line.startswith('```') and _pr_in_code_block:
                                    _pr_in_code_block = False
                                    continue
                                if _pr_in_code_block:
                                    _pr_code_lines.append(_pr_line)
                            # 解析代码块中的行号格式内容
                            _pr_parsed_lines = []
                            for _pr_cl in _pr_code_lines:
                                _pr_tab_pos = _pr_cl.find('\t')
                                if _pr_tab_pos > 0:
                                    try:
                                        _pr_lnum = int(_pr_cl[:_pr_tab_pos])
                                        _pr_parsed_lines.append((_pr_lnum, _pr_cl[_pr_tab_pos + 1:]))
                                    except ValueError:
                                        _pr_parsed_lines.append((0, _pr_cl))
                                else:
                                    _pr_parsed_lines.append((0, _pr_cl))
                            if _pr_parsed_lines:
                                # 提取当前文件内容行用于 difflib 比较（仅包含有效行号的行）
                                _pr_current_lines = [line_content for lnum, line_content in _pr_parsed_lines if lnum > 0]
                                if not _pr_current_lines:
                                    pass  # 没有有效行号 = 非文件内容（拦截消息等），跳过过滤不修改状态
                                else:
                                # 如果 reference_lines 存在且与当前内容不同，执行偏移映射
                                    _pr_ref_stripped = [l.rstrip('\n').rstrip('\r') for l in _pr_ref_lines] if _pr_ref_lines else []
                                if _pr_current_lines and _pr_ref_stripped and _pr_ref_stripped != _pr_current_lines:
                                    import difflib
                                    _pr_sm = difflib.SequenceMatcher(None, _pr_ref_stripped, _pr_current_lines)
                                    _pr_new_visible = set()
                                    for _pr_tag, _pr_i1, _pr_i2, _pr_j1, _pr_j2 in _pr_sm.get_opcodes():
                                        if _pr_tag == 'equal':
                                            for _pr_offset in range(_pr_i2 - _pr_i1):
                                                _pr_old_line = _pr_i1 + _pr_offset + 1  # 1-based
                                                _pr_new_line = _pr_j1 + _pr_offset + 1
                                                if _pr_old_line in _pr_visible:
                                                    _pr_new_visible.add(_pr_new_line)
                                        elif _pr_tag == 'replace':
                                            # 按比例映射：只有对应位置的旧可见行映射到新行
                                            _pr_old_len = _pr_i2 - _pr_i1
                                            _pr_new_len = _pr_j2 - _pr_j1
                                            for _pr_k in range(_pr_old_len):
                                                if (_pr_i1 + _pr_k + 1) in _pr_visible:
                                                    _pr_mapped = int(_pr_k * _pr_new_len / _pr_old_len) if _pr_old_len > 0 else 0
                                                    if _pr_mapped < _pr_new_len:
                                                        _pr_new_visible.add(_pr_j1 + _pr_mapped + 1)
                                        elif _pr_tag == 'insert':
                                            for _pr_k in range(_pr_j2 - _pr_j1):
                                                _pr_new_visible.add(_pr_j1 + _pr_k + 1)
                                        # delete: 旧行从集合移除（不加入新集合即可）
                                    # 强制首尾 10 行可见
                                    _pr_total = len(_pr_current_lines)
                                    _pr_new_visible.update(range(1, min(11, _pr_total + 1)))
                                    _pr_new_visible.update(range(max(1, _pr_total - 9), _pr_total + 1))
                                    _pr_visible = _pr_new_visible
                                    # 更新状态
                                    _pr_state['visible_lines'] = _pr_visible
                                    _pr_state['reference_lines'] = [l + '\n' for l in _pr_current_lines]
                                # 按可见行集合过滤：只保留可见行，连续非可见区间替换为省略标记
                                _pr_filtered = []
                                _pr_in_gap = False
                                for _pr_lnum, _pr_lcontent in (_pr_parsed_lines if _pr_current_lines else []):
                                    if _pr_lnum > 0 and _pr_lnum in _pr_visible:
                                        if _pr_in_gap:
                                            _pr_filtered.append('')
                                            _pr_filtered.append('...')
                                            _pr_filtered.append('')
                                            _pr_in_gap = False
                                        _pr_filtered.append(f'{_pr_lnum}\t{_pr_lcontent}')
                                    else:
                                        if not _pr_in_gap:
                                            _pr_in_gap = True
                                # 重建 content_to_render（保留 Tool Result 头部格式）
                                _pr_header_end = content_to_render.find('```\n')
                                if _pr_header_end >= 0:
                                    _pr_header = content_to_render[:_pr_header_end + 4]
                                    _pr_footer_start = content_to_render.rfind('\n```')
                                    _pr_footer = content_to_render[_pr_footer_start:] if _pr_footer_start > _pr_header_end else '\n```'
                                    content_to_render = _pr_header + '\n'.join(_pr_filtered) + _pr_footer
                                    total_len = len(content_to_render)

            token_k = total_len / 3000
            id_prefix = f"[ID: {msg['id']}] "
            _msg_blocks.append(f"\n\n{id_prefix}[预估: {token_k:.2f}k Tokens] {msg['role'].capitalize()}: {content_to_render}")

            # 未闭合工具调用警告注入：assistant 消息带有 _unclosed_warning 字段时追加到上下文
            if msg.get('_unclosed_warning'):
                _msg_blocks.append(f"\n\n{msg['_unclosed_warning']}")

            # 图文交错：在图片出现的自然位置插入标记，而非统一追加到末尾
            if msg.get('image'):
                _img = msg['image']
                _img_mime = _img.get('mime_type', 'image/jpeg')
                import base64 as _ib64
                if _img.get('path'):
                    _img_fpath = os.path.join(getattr(self, 'data_dir', 'data'), 'images', _img['path'])
                    with open(_img_fpath, 'rb') as _imf:
                        _img_raw = _imf.read()
                    _img_b64 = _ib64.b64encode(_img_raw).decode('ascii')
                else:
                    _img_b64 = _img.get('base64', '')
                    _img_raw = _ib64.b64decode(_img_b64)
                if len(_img_raw) > 4 * 1024 * 1024:
                    try:
                        from PIL import Image as _PILImg2
                        from io import BytesIO as _BIO3
                        _pil2 = _PILImg2.open(_BIO3(_img_raw))
                        _jbio2 = _BIO3()
                        _pil2.save(_jbio2, 'JPEG', quality=80)
                        _img_raw = _jbio2.getvalue()
                        _img_b64 = _ib64.b64encode(_img_raw).decode('ascii')
                        _img_mime = 'image/jpeg'
                        print(f'[IMG COMPRESS] User image compressed to {len(_img_raw)} bytes', flush=True)
                    except Exception as _ce2:
                        print(f'[IMG COMPRESS] Failed: {_ce2}', flush=True)
                _img_marker_counter += 1
                _img_mk = f"\x00IMG{_img_marker_counter}\x00"
                _img_marker_map[_img_mk] = {"type": "image_url", "image_url": {"url": f"data:{_img_mime};base64,{_img_b64}"}}
                _msg_blocks.append(_img_mk)
            if msg.get('multimodal_blocks'):
                for _vb in msg['multimodal_blocks']:
                    if _vb.get('type') in ('image', 'document'):
                        _src = _vb.get('source', {})
                        _vb_raw = None
                        _vb_media = _src.get('media_type', 'image/jpeg')
                        if _src.get('type') == 'file' and _src.get('path'):
                            _img_fpath = os.path.join(getattr(self, 'data_dir', 'data'), 'images', _src['path'])
                            if os.path.exists(_img_fpath):
                                with open(_img_fpath, 'rb') as _vbf:
                                    _vb_raw = _vbf.read()
                        elif _src.get('data'):
                            import base64 as _vb_b64d
                            try: _vb_raw = _vb_b64d.b64decode(_src['data'])
                            except: _vb_raw = _src['data'].encode('ascii') if isinstance(_src['data'], str) else _src['data']
                        if _vb_raw:
                            print(f"[IMG CONTEXT] Loading multimodal block: media={_vb_media}, raw_bytes={len(_vb_raw)}, src_type={_src.get('type','?')}, path={_src.get('path','?')}", flush=True)
                            # 超过 4MB 时压缩图片（Anthropic 限制 5MB）
                            if len(_vb_raw) > 4 * 1024 * 1024 and _vb_media.startswith('image/'):
                                try:
                                    from PIL import Image as _PILImg
                                    from io import BytesIO as _BIO2
                                    _pil = _PILImg.open(_BIO2(_vb_raw))
                                    _jbio = _BIO2()
                                    _pil.save(_jbio, 'JPEG', quality=80)
                                    _vb_raw = _jbio.getvalue()
                                    _vb_media = 'image/jpeg'
                                    print(f'[IMG COMPRESS] Compressed to {len(_vb_raw)} bytes', flush=True)
                                except Exception as _ce:
                                    print(f'[IMG COMPRESS] Failed: {_ce}', flush=True)
                            import base64 as _vb_b64
                            _vb_data = _vb_b64.b64encode(_vb_raw).decode('ascii')
                            _img_marker_counter += 1
                            _img_mk = f"\x00IMG{_img_marker_counter}\x00"
                            if _vb_media == 'application/pdf':
                                _img_marker_map[_img_mk] = {"type": "document", "source": {"type": "base64", "media_type": _vb_media, "data": _vb_data}}
                            else:
                                _img_marker_map[_img_mk] = {"type": "image_url", "image_url": {"url": f"data:{_vb_media};base64,{_vb_data}"}}
                            _msg_blocks.append(_img_mk)

        # Deep Think 注入：作为独立的用户消息插入上下文末尾，统一由 send_message() 传入 is_deep_think 参数控制
        _dt_level = is_deep_think
        if isinstance(_dt_level, bool):
            _dt_level = 2 if _dt_level else 0
        if _dt_level > 0:
            _dt_prompt = self._load_deep_think_prompt(_dt_level)
            if _dt_prompt:
                _dt_temp_id = self._next_id()
                _dt_token_k = len(_dt_prompt) / 3000
                _msg_blocks.append(f"\n\n[ID: {_dt_temp_id}] [预估: {_dt_token_k:.2f}k Tokens] User: {_dt_prompt}")

        if is_reverse:
            _msg_blocks.reverse()
        full_context += ''.join(_msg_blocks)

        real_model_name = model_name or self.config.get("MODEL_NAME", "")
        current_api_url = self.config["API_URL"]
        current_api_key = self.config["API_KEY"]
        
        is_pure_enabled = settings.get('enable_pure_mode', False)
        is_local_model = strip_composite(real_model_name).startswith("[本地]")
        is_stream_enabled = settings.get('enable_stream', True)
        should_stream = not settings.get('force_no_stream', False)
        # Chat-completion 生图模型强制非流式（流式接口不返回图片数据）
        if '-image' in _img_model_check and not _is_image_gen:
            should_stream = False
        
        max_output_tokens = 32768

        # 图文交错辅助函数：按标记分割文本，在标记位置插入图片块
        def _split_text_with_images(text, marker_map):
            if not marker_map:
                return None
            import re as _sre
            _pat = _sre.compile('(' + '|'.join(_sre.escape(m) for m in marker_map) + ')')
            _segments = _pat.split(text)
            result = []
            for seg in _segments:
                if seg in marker_map:
                    result.append(marker_map[seg])
                elif seg:
                    result.append({"type": "text", "text": seg})
            return result if any(p.get('type') != 'text' for p in result) else None

        # Shared cache prefix segmentation logic (protocol-agnostic, called by all paths)
        def _do_cache_segmentation(conv_text, enable_split=True):
            """Split conversation text into multiple messages based on send checkpoints.
            Returns list of message dicts. Updates session state and emits prediction.
            If enable_split=False, returns single message but still tracks state."""
            import hashlib as _hl
            if not enable_split:
                _seg_count = session.get('_cache_segment_count', 0) + 1
                session['_cache_segment_count'] = _seg_count
                _last_id = None
                for _m in session['conversation_history']:
                    if _m['id'] == target_id:
                        break
                    if not _m.get('is_hidden', False) and not _m.get('is_error', False):
                        _last_id = _m['id']
                if _last_id is not None:
                    session['_last_sent_max_id'] = _last_id
                if self.socketio:
                    self.socketio.emit('cache_prediction', {'segments': _seg_count, 'last_sent_max_id': session.get('_last_sent_max_id', 0)})
                _ci = _split_text_with_images(conv_text, _img_marker_map)
                return [{"role": "user", "content": _ci if _ci else conv_text}] if conv_text else []
            _seg_lengths = session.get('_cache_segment_lengths', [])
            _prev_hash = session.get('_cache_prefix_hash', '')
            _prefix_len = sum(_seg_lengths)
            _prefix_text = conv_text[:_prefix_len] if _prefix_len <= len(conv_text) else ''
            _suffix_text = conv_text[_prefix_len:] if _prefix_len <= len(conv_text) else conv_text
            _current_hash = _hl.md5(_prefix_text.encode('utf-8')).hexdigest() if _prefix_text else ''
            if _seg_lengths and _current_hash == _prev_hash:
                _conv_msgs = []
                _offset = 0
                for _sl in _seg_lengths:
                    _sc = conv_text[_offset:_offset + _sl]
                    _offset += _sl
                    if _sc:
                        _si = _split_text_with_images(_sc, _img_marker_map)
                        _conv_msgs.append({"role": "user", "content": _si if _si else _sc})
                if _suffix_text:
                    _si2 = _split_text_with_images(_suffix_text, _img_marker_map)
                    _conv_msgs.append({"role": "user", "content": _si2 if _si2 else _suffix_text})
                    _seg_lengths.append(len(_suffix_text))
                session['_cache_segment_lengths'] = _seg_lengths
                session['_cache_prefix_hash'] = _hl.md5(conv_text.encode('utf-8')).hexdigest() if conv_text else ''
            else:
                session['_cache_segment_lengths'] = [len(conv_text)] if conv_text else []
                session['_cache_prefix_hash'] = _hl.md5(conv_text.encode('utf-8')).hexdigest() if conv_text else ''
                _ci = _split_text_with_images(conv_text, _img_marker_map)
                _conv_msgs = [{"role": "user", "content": _ci if _ci else conv_text}] if conv_text else []
            _seg_count = session.get('_cache_segment_count', 0) + 1
            session['_cache_segment_count'] = _seg_count
            _last_id = None
            for _m in session['conversation_history']:
                if _m['id'] == target_id:
                    break
                if not _m.get('is_hidden', False) and not _m.get('is_error', False):
                    _last_id = _m['id']
            if _last_id is not None:
                session['_last_sent_max_id'] = _last_id
            if self.socketio:
                self.socketio.emit('cache_prediction', {'segments': _seg_count, 'last_sent_max_id': session.get('_last_sent_max_id', 0)})
            return _conv_msgs

        if not is_pure_enabled and settings.get('enable_multiturn_format', False) and _system_portion_end > 0 and not _is_cc:
            # Multi-turn format: use shared segmentation function (same as CC inject and Anthropic paths)
            _sys_part = full_context[:_system_portion_end]
            _conv_part = full_context[_system_portion_end:]
            _sys_interleaved = _split_text_with_images(_sys_part, _img_marker_map)
            _msg1_content = _sys_interleaved if _sys_interleaved else _sys_part
            _should_seg_ncc = is_model_segmented(real_model_name)
            data_to_send = {
                "model": real_model_name,
                "messages": [{"role": "user", "content": _msg1_content}] + _do_cache_segmentation(_conv_part, enable_split=_should_seg_ncc),
                "stream": should_stream,
                "max_tokens": max_output_tokens
            }
        elif is_pure_enabled:
            pure_messages = []
            last_role_pure = 'system'
            for pm in session['conversation_history']:
                if pm['id'] == target_id:
                    break
                if pm.get('is_hidden') or pm.get('is_error'):
                    continue
                pc = ""
                if pm.get('is_omitted'):
                    continue
                elif pm.get('content'):
                    pc = pm['content']
                if not pc or not pc.strip():
                    if not pm.get('image'):
                        continue
                pr = pm['role']
                if pm.get('image'):
                    img = pm['image']
                    parts = []
                    if pc and pc.strip() and pc.strip() not in ('[用户上传了一张图片]',):
                        parts.append({"type": "text", "text": pc})
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['base64']}"}})
                    pure_messages.append({"role": pr, "content": parts})
                    last_role_pure = pr
                elif pr == last_role_pure and pure_messages:
                    if isinstance(pure_messages[-1]['content'], str):
                        pure_messages[-1]['content'] += "\n\n" + pc
                    else:
                        pure_messages.append({"role": pr, "content": pc})
                        last_role_pure = pr
                else:
                    pure_messages.append({"role": pr, "content": pc})
                    last_role_pure = pr
            if not pure_messages or pure_messages[-1]['role'] != 'user':
                pure_messages.append({"role": "user", "content": "请继续。"})
            data_to_send = {
                "model": real_model_name,
                "messages": pure_messages,
                "stream": should_stream,
                "max_tokens": max_output_tokens
            }
        else:
            _interleaved = _split_text_with_images(full_context, _img_marker_map)
            if _interleaved:
                data_to_send = {
                    "model": real_model_name,
                    "messages": [{"role": "user", "content": _interleaved}],
                    "stream": should_stream,
                    "max_tokens": max_output_tokens
                }
            else:
                data_to_send = {
                    "model": real_model_name,
                    "messages": [{"role": "user", "content": full_context}],
                    "stream": should_stream,
                    "max_tokens": max_output_tokens
                }
        
        # 供应商路由：基于 providers.json 配置文件的模型→API端点查找表（详见 provider_routes.py）
        current_api_url, current_api_key, _routed_model = route_provider(real_model_name, self.config)
        data_to_send["model"] = _routed_model
            
        # Claude 专属：注入扩展思考和努力等级配置
        if 'claude' in data_to_send.get('model', '').lower():
            data_to_send['max_tokens'] = 64000
            data_to_send['thinking'] = {"budget_tokens": 63999, "type": "enabled"}
            data_to_send['output_config'] = {"effort": "max"}

        # 自动托管指南文件 AUTOPILOT_GUIDE.md 不再自动注入上下文（已移除该功能，改为系统提示词内置）

        # Anthropic Messages 协议转换
        use_anthropic_protocol = (
            settings.get('enable_anthropic_protocol', False) and
            'claude' in data_to_send.get('model', '').lower()
        )
        if use_anthropic_protocol:
            # 加载外部工具定义和系统提示词（受 enable_tool_inject 开关控制）
            if settings.get('enable_tool_inject', False):
                _tools_filename = "tools.json" if settings.get('enable_tool_simulate', False) else "tools_external.json"
                _tools_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), _tools_filename)
                _tools_text_for_prompt = ""
                if os.path.exists(_tools_path):
                    try:
                        with open(_tools_path, "r", encoding="utf-8") as _tf:
                            _cc_tools_list = json.load(_tf)
                        _tools_text_for_prompt = self._format_tools_as_text(_cc_tools_list)
                    except Exception as _te:
                        print(f"加载 {_tools_filename} 失败: {_te}")
                # chatapp system prompt → Anthropic system 字段；tool_system.json → user 字段开头
                if not is_pure_enabled and _system_portion_end > 0:
                    _sys_text = full_context[:_system_portion_end]
                    _enable_desc_fmt = settings.get('enable_descriptor_tool_calls', True)
                    _sys_text += "\n\n[注：在用户气泡开头附上了Claude Code 的 Prompt 和可用工具列表。请使用描述符格式如 [Edit开始]...[Edit结束] 调用工具，详见工具列表中的格式说明]"
                    _cost_rules = "\n\n[用户计费规范]\n我是按api调用次数计费的，上下文长度1000k，也就是1m token，但是无论当前上下文多长我都被扣固定的费用，无论你单次输出内容多少，思考时间多长也是收固定费用，所以我希望你把单次api调用的效果最大化。你一个简单的查询和你几千token的代码编辑收费是完全相同的。具体来说，对于工具调用，你应该尽可能多地一次并行调用10个左右的工具，只要没有严格时序依赖你就不应该按先后去调用而是并行调用，而且你做工具调用的时候也要说话，不能输出空文本，要尽量多说话，总是有用的，比如说一下详细说你在干啥，你打算未来几次api调用要干啥，你的想象，你对需求的理解，你的疑问，你的计划，你的分析，你对难度的估计，你的思考过程，你可以发散一点。比如你要用bash你就一次性把要用的命令都用了，同时也可以直接提前开始read。你可以做分支预测，在没有获取完全确定的信息之前你也可以提前使用工具调用。\n比如如果你要编辑多个代码，你被禁止使用多次交互来编辑，你应该一次性编辑多个代码，对每个代码使用多个edit调用。绝大多数情况你不要使用write，因为代码通常都很大，你write虽然不会因为大量的输出token额外计费但是可能抄漏之类的写出bug，导致误差累积。任何含edit的气泡都要在同一次回复里同时执行bash对在这个气泡里被修改的多个文件进行语法检查等工具调用。cc要求文件被修改之前要先被读取，但是你只要你稍微了解一点点情况你仍然可以在一次回复中连续使用读取和多个修改调用，反正如果写错了查找字段就会失败，完全没有任何坏处。你不得返回只有工具调用而不讲话的气泡，你每次调用工具都必须说话，并且要有几百字的长度，包含详细描述，规划，分析，你的思考，疑问等等你觉得可以谈论的内容.\n你对环境信息的了解是有限的，如果你高频率的探索环境，每次都只探索一点点，会造成大量的次数浪费，这是灾难性的。比如你可能连续grep三次都找不到你要的代码段。你不被允许对同一个文件使用多次grep，如果你grep了一次，就要决定是否要读入，不读入就彻底放弃这个文件，再也不要想它，否则你就完整读入。绝大多数情况你不被允许部分读入一个文件，除非这个文件超过1m，否则你即使分多次也要完整读入。绝大多数情况你被推荐直接read你觉得你可能需要的整一个文件。如果提示文件太大不能一次read进来，你也可以考虑使用10次左右并行的read来分批次按顺序一次性读入整个文件。并且如果你ls出来之后有多个文件未来可能需要的，你应该在一次回复中批量读入全部你可能用的上的文件，哪怕只是轻微怀疑可能有用也要尝试去read，如果有多个文件都很大那就一次性对多个分批读入，比如5个文件每个用5次read读入一共25个tooluse。每次你尝试通过分批来完整读入多个文件你都要判断每个文件是否已经都被完整地读入到了最后一行为止，如果完整读入一般来说最后一次读入应该是完整的，如果发现最后一次读入不是结尾而是被截断到某个你设定的limit整数行那说明你没有读全。绝大多数情况你不被允许对一个已经被读入过的文件重复read，即使它已经被你自己修改过了你也应该自己推断它当前的状态而不是重复read它。一千行的python代码可能有100kb大小，大概对应20k token。你有1000k token上下文。所以几个几百kb的python之类的代码完全不算大。工具调用失败和冗余的调用不会有任何后果，只是没有用而已，没有任何损失，你不应该保守调用。\n[用户计费规范结束]"
                    if settings.get('enable_planned_tools', False):
                        _serial_cost_replacements = [
                            ('工具调用是严格串行的，这意味着你可以提前写任意多的工具调用，批处理，它们会按计划执行。', '立即工具并行执行不互相等待，计划工具在声明的依赖全部成功后自动执行。你应该在一次回复中尽量多写工具调用，用计划调用表达依赖关系。'),
                            ('比如如果你计划等安装某个库看到工具调用返回成功之后用这个库来进行下一步的工作，你应该直接在这一个气泡里写下一步的工具调用，不要等到你亲眼看到那个安装成功的返回。', '比如安装库后要使用它，直接写一个计划调用等待安装完成后执行使用步骤。永远不要说要等结果出来才写下一步。'),
                            ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。如果你需要执行耗时较长的多个任务并且希望并行以减少等待时间，在同一个消息中写一个python文件并且运行来科学且现代化地实现并行。', '多个立即工具同时并行执行。如果某个工具需要等待另一个的结果，使用计划调用并声明等待依赖即可。'),
                            ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。', '多个立即工具同时并行执行，计划工具在依赖满足后自动执行。'),
                            ('也就是并行发出，串行执行。', '立即工具并行发出并行执行，计划工具按等待清单在依赖成功后执行。'),
                        ]
                        for _old, _new in _serial_cost_replacements:
                            _cost_rules = _cost_rules.replace(_old, _new)
                    # Build system field with all static content
                    _sys_blocks = []
                    if settings.get('enable_cache_control', False):
                        _sys_blocks.append({"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.92.8a3; cc_entrypoint=cli; cch=00000;"})
                    _sys_blocks.append({"type": "text", "text": _sys_text + _cost_rules})
                    _conv_text = full_context[_system_portion_end:]
                    _tool_sys_prefix = ""
                    _sys_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tool_system.json")
                    if os.path.exists(_sys_path):
                        try:
                            with open(_sys_path, "r", encoding="utf-8") as _sf:
                                _cc_sys_blocks = json.load(_sf)
                            _tool_sys_prefix = "\n\n".join(
                                b.get("text", "") for b in _cc_sys_blocks
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                            if _tool_sys_prefix:
                                import datetime as _dt
                                _cwd = os.getcwd()
                                _tool_sys_prefix = _tool_sys_prefix.replace("{WORKING_DIR}", _cwd)
                                _tool_sys_prefix = _tool_sys_prefix.replace("{HOME_DIR}", os.path.expanduser("~"))
                                _tool_sys_prefix = _tool_sys_prefix.replace("{CURRENT_DATE}", _dt.datetime.now().strftime("%Y/%m/%d"))
                                _tool_sys_prefix = _tool_sys_prefix.replace("{CURRENT_MONTH}", _dt.datetime.now().strftime("%B %Y"))
                                _tool_sys_prefix = _tool_sys_prefix.replace("{MEMORY_DIR}", os.path.expanduser("~/.claude/memory/"))
                                _tool_sys_prefix = _tool_sys_prefix.replace("{GIT_STATUS}", "true" if os.path.exists(os.path.join(_cwd, ".git")) else "false")
                                _tool_sys_prefix = _tool_sys_prefix.replace("{MODEL_IDENTITY}", "")
                                _tool_sys_prefix = "[以下是 Claude Code 的 System Prompt 参考信息]\n" + _tool_sys_prefix + "\n[Claude Code Prompt 参考信息结束]\n\n"
                        except Exception as _se:
                            print(f"加载 tool_system.json 失败: {_se}")
                    # Move cc_system + cc_tools into system field as additional blocks
                    if _tool_sys_prefix:
                        _sys_blocks.append({"type": "text", "text": _tool_sys_prefix})
                    if _tools_text_for_prompt:
                        _sys_blocks.append({"type": "text", "text": _tools_text_for_prompt})
                    # cache_control goes on the LAST system block
                    if settings.get('enable_cache_control', False):
                        _sys_blocks[-1]["cache_control"] = {"type": "ephemeral", "scope": "global"}
                    data_to_send['system'] = _sys_blocks
                    # Messages: use shared segmentation for conversation history
                    _should_seg_anth = is_model_segmented(real_model_name)
                    data_to_send['messages'] = _do_cache_segmentation(_conv_text, enable_split=_should_seg_anth)
            for suffix in ['/v1/chat/completions', '/v1/chat']:
                if current_api_url.endswith(suffix):
                    current_api_url = current_api_url[:-len(suffix)] + '/v1/messages?beta=true'
                    break
            anthropic_messages = []
            for msg in data_to_send['messages']:
                content = msg['content']
                if isinstance(content, str):
                    a_content = [{"type": "text", "text": content}]
                elif isinstance(content, list):
                    a_content = []
                    for item in content:
                        if isinstance(item, str):
                            a_content.append({"type": "text", "text": item})
                        elif isinstance(item, dict) and item.get('type') == 'image_url':
                            url = item['image_url']['url']
                            if url.startswith('data:'):
                                media_type = url[5:url.index(';')]
                                base64_data = url.split(',', 1)[1]
                                a_content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_data}})
                            else:
                                a_content.append({"type": "text", "text": f"[Image: {url}]"})
                        elif isinstance(item, dict) and item.get('type') == 'document':
                            # PDF 文档块直接透传给 Anthropic API
                            a_content.append(item)
                        elif isinstance(item, dict):
                            a_content.append(item)
                    if not a_content:
                        a_content = [{"type": "text", "text": ""}]
                else:
                    a_content = [{"type": "text", "text": str(content)}]
                anthropic_messages.append({"role": msg['role'], "content": a_content})
            data_to_send['messages'] = anthropic_messages
            # Add cache_control to the last user message's last content block
            if settings.get('enable_cache_control', False):
                for _msg_r in reversed(data_to_send['messages']):
                    if _msg_r.get('role') == 'user' and isinstance(_msg_r.get('content'), list) and _msg_r['content']:
                        _msg_r['content'][-1]['cache_control'] = {"type": "ephemeral", "scope": "global"}
                        break

        # 非 Claude 模型的 CC 注入模式（如 Gemini）：将 system 内容放入 message 开头
        if not use_anthropic_protocol and _is_cc and not is_pure_enabled:
            _tools_filename2 = "tools.json" if settings.get('enable_tool_simulate', False) else "tools_external.json"
            _tools_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), _tools_filename2)
            _tools_text_for_prompt = ""
            if os.path.exists(_tools_path):
                try:
                    with open(_tools_path, "r", encoding="utf-8") as _tf:
                        _cc_tools_list = json.load(_tf)
                    _tools_text_for_prompt = self._format_tools_as_text(_cc_tools_list)
                except Exception as _te:
                    print(f"加载 tools_external.json 失败: {_te}")

            if _system_portion_end > 0:
                _sys_text = full_context[:_system_portion_end]
                _enable_desc_fmt2 = settings.get('enable_descriptor_tool_calls', True)
                _sys_text += "\n\n[注：在用户气泡开头附上了Claude Code 的 Prompt 和可用工具列表。请使用描述符格式 如[Edit开始]...[Edit结束] 调用工具，详见工具列表中的格式说明]"
                _cost_rules = "\n\n[用户计费规范]\n我是按api调用次数计费的，上下文长度1000k，也就是1m token，但是无论当前上下文多长我都被扣固定的费用，无论你单次输出内容多少，思考时间多长也是收固定费用，所以我希望你把单次api调用的效果最大化。你一个简单的查询和你几千token的代码编辑收费是完全相同的。具体来说，对于工具调用，你应该尽可能多地一次并行调用10个左右的工具，只要没有严格时序依赖你就不应该按先后去调用而是并行调用，而且你做工具调用的时候也要说话，不能输出空文本，要尽量多说话，总是有用的，比如说一下详细说你在干啥，你打算未来几次api调用要干啥，你的想象，你对需求的理解，你的疑问，你的计划，你的分析，你对难度的估计，你的思考过程，你可以发散一点。比如你要用bash你就一次性把要用的命令都用了，同时也可以直接提前开始read。你可以做分支预测，在没有获取完全确定的信息之前你也可以提前使用工具调用。\n比如如果你要编辑多个代码，你被禁止使用多次交互来编辑，你应该一次性编辑多个代码，对每个代码使用多个edit调用。绝大多数情况你不要使用write，因为代码通常都很大，你write虽然不会因为大量的输出token额外计费但是可能抄漏之类的写出bug，导致误差累积。任何含edit的气泡都要在同一次回复里同时执行bash对在这个气泡里被修改的多个文件进行语法检查等工具调用。cc要求文件被修改之前要先被读取，但是你只要你稍微了解一点点情况你仍然可以在一次回复中连续使用读取和多个修改调用，反正如果写错了查找字段就会失败，完全没有任何坏处。你不得返回只有工具调用而不讲话的气泡，你每次调用工具都必须说话，并且要有几百字的长度，包含详细描述，规划，分析，你的思考，疑问等等你觉得可以谈论的内容.\n你对环境信息的了解是有限的，如果你高频率的探索环境，每次都只探索一点点，会造成大量的次数浪费，这是灾难性的。比如你可能连续grep三次都找不到你要的代码段。你不被允许对同一个文件使用多次grep，如果你grep了一次，就要决定是否要读入，不读入就彻底放弃这个文件，再也不要想它，否则你就完整读入。绝大多数情况你不被允许部分读入一个文件，除非这个文件超过1m，否则你即使分多次也要完整读入。绝大多数情况你被推荐直接read你觉得你可能需要的整一个文件。如果提示文件太大不能一次read进来，你也可以考虑使用10次左右并行的read来分批次按顺序一次性读入整个文件。并且如果你ls出来之后有多个文件未来可能需要的，你应该在一次回复中批量读入全部你可能用的上的文件，哪怕只是轻微怀疑可能有用也要尝试去read，如果有多个文件都很大那就一次性对多个分批读入，比如5个文件每个用5次read读入一共25个tooluse。每次你尝试通过分批来完整读入多个文件你都要判断每个文件是否已经都被完整地读入到了最后一行为止，如果完整读入一般来说最后一次读入应该是完整的，如果发现最后一次读入不是结尾而是被截断到某个你设定的limit整数行那说明你没有读全。绝大多数情况你不被允许对一个已经被读入过的文件重复read，即使它已经被你自己修改过了你也应该自己推断它当前的状态而不是重复read它。一千行的python代码可能有100kb大小，大概对应20k token。你有1000k token上下文。所以几个几百kb的python之类的代码完全不算大。工具调用失败和冗余的调用不会有任何后果，只是没有用而已，没有任何损失，你不应该保守调用。\n[用户计费规范结束]"
                if settings.get('enable_planned_tools', False):
                    _serial_cost_replacements2 = [
                        ('工具调用是严格串行的，这意味着你可以提前写任意多的工具调用，批处理，它们会按计划执行。', '立即工具并行执行不互相等待，计划工具在声明的依赖全部成功后自动执行。你应该在一次回复中尽量多写工具调用，用计划调用表达依赖关系。'),
                        ('比如如果你计划等安装某个库看到工具调用返回成功之后用这个库来进行下一步的工作，你应该直接在这一个气泡里写下一步的工具调用，不要等到你亲眼看到那个安装成功的返回。', '比如安装库后要使用它，直接写一个计划调用等待安装完成后执行使用步骤。永远不要说要等结果出来才写下一步。'),
                        ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。如果你需要执行耗时较长的多个任务并且希望并行以减少等待时间，在同一个消息中写一个python文件并且运行来科学且现代化地实现并行。', '多个立即工具同时并行执行。如果某个工具需要等待另一个的结果，使用计划调用并声明等待依赖即可。'),
                        ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。', '多个立即工具同时并行执行，计划工具在依赖满足后自动执行。'),
                        ('也就是并行发出，串行执行。', '立即工具并行发出并行执行，计划工具按等待清单在依赖成功后执行。'),
                    ]
                    for _old2, _new2 in _serial_cost_replacements2:
                        _cost_rules = _cost_rules.replace(_old2, _new2)

                _conv_text = full_context[_system_portion_end:]
                _tool_sys_prefix = ""
                _sys_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tool_system.json")
                if os.path.exists(_sys_path):
                    try:
                        with open(_sys_path, "r", encoding="utf-8") as _sf:
                            _cc_sys_blocks = json.load(_sf)
                        _tool_sys_prefix = "\n\n".join(
                            b.get("text", "") for b in _cc_sys_blocks
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if _tool_sys_prefix:
                            import datetime as _dt
                            _cwd = os.getcwd()
                            _tool_sys_prefix = _tool_sys_prefix.replace("{WORKING_DIR}", _cwd)
                            _tool_sys_prefix = _tool_sys_prefix.replace("{HOME_DIR}", os.path.expanduser("~"))
                            _tool_sys_prefix = _tool_sys_prefix.replace("{CURRENT_DATE}", _dt.datetime.now().strftime("%Y/%m/%d"))
                            _tool_sys_prefix = _tool_sys_prefix.replace("{CURRENT_MONTH}", _dt.datetime.now().strftime("%B %Y"))
                            _tool_sys_prefix = _tool_sys_prefix.replace("{MEMORY_DIR}", os.path.expanduser("~/.claude/memory/"))
                            _tool_sys_prefix = _tool_sys_prefix.replace("{GIT_STATUS}", "true" if os.path.exists(os.path.join(_cwd, ".git")) else "false")
                            _tool_sys_prefix = _tool_sys_prefix.replace("{MODEL_IDENTITY}", "")
                            _tool_sys_prefix = "[以下是 Claude Code 的 System Prompt 参考信息]\n" + _tool_sys_prefix + "\n[Claude Code Prompt 参考信息结束]\n\n"
                            if settings.get('enable_planned_tools', False):
                                _serial_replacements2 = [
                                    ('把单一工具调用尽量拆分成多个按顺序的工具调用，只有当你必须亲自看到某个工具调用的返回值才能写后续的工具调用时允许你停下写更多的工具调用并结束这一次气泡的工具调用编写。', '尽量在一次回复中写尽可能多的工具调用。不需要等待结果，直接用计划调用声明依赖关系。只有当你完全无法预测下一步需要什么时才允许停下。'),
                                    ('工具调用是严格串行的，这意味着你可以提前写任意多的工具调用，批处理，它们会按计划执行。', '立即工具并行执行，计划工具按等待清单在依赖成功后自动执行。你应该大量使用计划调用来表达多步操作链。'),
                                    ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。如果你需要执行耗时较长的多个任务并且希望并行以减少等待时间，在同一个消息中写一个python文件并且运行来科学且现代化地实现并行。', '多个立即工具同时并行执行。需要等待结果的操作使用计划调用声明依赖即可，不需要写python脚本来实现并行。'),
                                    ('多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。', '多个立即工具同时并行执行，计划工具在依赖满足后自动执行。'),
                                    ('也就是并行发出，串行执行。', '立即工具并行发出并行执行，计划工具按等待清单在依赖成功后执行。'),
                                    ('比如如果你计划等安装某个库看到工具调用返回成功之后用这个库来进行下一步的工作，你应该直接在这一个气泡里写下一步的工具调用，不要等到你亲眼看到那个安装成功的返回。', '比如安装库后要使用它，写一个计划调用等待安装成功后自动执行使用步骤。永远不要说要等结果才写下一步。'),
                                ]
                                for _old2, _new2 in _serial_replacements2:
                                    _tool_sys_prefix = _tool_sys_prefix.replace(_old2, _new2)
                    except Exception as _se:
                        print(f"加载 tool_system.json 失败: {_se}")

                # 非 Claude：system 内容放到 message 开头，CC提示和工具也放开头
                if settings.get('enable_multiturn_format', False):
                    _nc_sys_content = _sys_text + _cost_rules + "\n\n" + _tool_sys_prefix + _tools_text_for_prompt
                    _nc_conv_content = _conv_text
                    _should_seg = is_model_segmented(real_model_name)
                    data_to_send['messages'] = [{"role": "user", "content": _nc_sys_content}] + _do_cache_segmentation(_nc_conv_content, enable_split=_should_seg)
                else:
                    _non_claude_content = _sys_text + _cost_rules + "\n\n" + _tool_sys_prefix + _tools_text_for_prompt + "\n\n" + _conv_text
                    _nc_interleaved = _split_text_with_images(_non_claude_content, _img_marker_map)
                    if _nc_interleaved:
                        data_to_send['messages'] = [{"role": "user", "content": _nc_interleaved}]
                    else:
                        data_to_send['messages'] = [{"role": "user", "content": _non_claude_content}]

        # Swap system/messages: move system content to messages beginning when enabled
        if settings.get('swap_system_messages', False) and data_to_send.get('system'):
            _swap_sys = data_to_send.get('system', [])
            _swap_text = ''
            if isinstance(_swap_sys, list):
                _swap_text = '\n\n'.join(b.get('text', '') for b in _swap_sys if isinstance(b, dict))
            elif isinstance(_swap_sys, str):
                _swap_text = _swap_sys
            if _swap_text and data_to_send.get('messages'):
                _first_msg = data_to_send['messages'][0]
                _first_content = _first_msg.get('content', [])
                if isinstance(_first_content, str):
                    _first_msg['content'] = _swap_text + '\n\n' + _first_content
                elif isinstance(_first_content, list):
                    _first_msg['content'] = [{"type": "text", "text": _swap_text + '\n\n'}] + _first_content
            data_to_send['system'] = []

        # Ensure JSON key order: system before messages for proxy compatibility
        if 'system' in data_to_send:
            _ordered = {}
            _ordered['model'] = data_to_send.get('model', '')
            _ordered['system'] = data_to_send['system']
            _ordered['messages'] = data_to_send.get('messages', [])
            for _k, _v in data_to_send.items():
                if _k not in _ordered:
                    _ordered[_k] = _v
            data_to_send = _ordered
        # Cached payload: cache on first build, reuse on retries
        _target_for_cache = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
        if _target_for_cache:
            if not _target_for_cache.get('_cached_payload'):
                _target_for_cache['_cached_payload'] = copy.deepcopy(data_to_send)
            else:
                data_to_send = copy.deepcopy(_target_for_cache['_cached_payload'])

        payload_str = json.dumps(data_to_send, ensure_ascii=False, indent=2)
        _wire_payload = json.dumps(data_to_send)
        _payload_bytes = len(_wire_payload)
        self.payloads[target_id] = payload_str

        # Cache hit predictor: estimate billing breakdown before sending
        _prediction = None
        if use_anthropic_protocol:
            _sys_field = data_to_send.get('system', [])
            _sys_chars = sum(len(b.get('text', '')) for b in _sys_field) if isinstance(_sys_field, list) else len(str(_sys_field))
            _msgs_field = data_to_send.get('messages', [])
            _msgs_chars = sum(len(json.dumps(m.get('content', ''), ensure_ascii=False)) for m in _msgs_field)
            _prev_chars = session.get('_prev_request_msgs_chars', 0)
            _token_ratio = 3.5  # chars per token estimate
            _pred_non_cached = int(_sys_chars / _token_ratio)
            _pred_cache_read = int(min(_prev_chars, _msgs_chars) / _token_ratio) if _prev_chars > 0 else 0
            _pred_cache_write = int(max(0, _msgs_chars - _prev_chars) / _token_ratio)
            session['_prev_request_msgs_chars'] = _msgs_chars
            _prediction = {
                'pred_non_cached': _pred_non_cached,
                'pred_cache_read': _pred_cache_read,
                'pred_cache_write': _pred_cache_write,
            }
            _target_for_pred = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
            if _target_for_pred:
                _target_for_pred['_billing_prediction'] = _prediction
            # Note: cache prediction emit and _last_sent_max_id tracking already handled by
            # _do_cache_segmentation() called earlier when building conversation messages

        print(f"\n" + "="*20 + f"  会话 {sid} | 发送 Payload (ID: {target_id})  " + "="*20)
        if _prediction:
            print(f"[CACHE PREDICT] non_cached~{_prediction['pred_non_cached']/1000:.1f}k cache_read~{_prediction['pred_cache_read']/1000:.1f}k cache_write~{_prediction['pred_cache_write']/1000:.1f}k")

        if is_offline or model_name == "离线":
            if self.socketio:
                self.socketio.emit('copy_to_clipboard', {"text": payload_str})
            if is_parallel:
                session['active_threads'] -= 1
                if session['active_threads'] <= 0: self.finalize_process(sid)
            else:
                self.finalize_process(sid)
            return

        _got_first_token = False
        _first_token_wall_time = None
        _last_error_body = ""
        try:
            with requests.Session() as req_session:
                req_start = time.time()
                _req_headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                }
                if use_anthropic_protocol:
                    _req_headers["x-api-key"] = current_api_key
                    _req_headers["anthropic-version"] = "2023-06-01"
                    if settings.get('enable_cache_control', False):
                        # Mimic Claude Code headers to enable caching on proxies
                        _req_headers["anthropic-beta"] = "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,redact-thinking-2026-02-12,context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24"
                        _req_headers["User-Agent"] = "claude-cli/2.1.92 (external, cli)"
                        _req_headers["x-app"] = "cli"
                        _req_headers["Accept"] = "application/json"
                        _req_headers["anthropic-dangerous-direct-browser-access"] = "true"
                        import uuid as _uuid_hdr
                        _req_headers["X-Claude-Code-Session-Id"] = str(_uuid_hdr.uuid5(_uuid_hdr.NAMESPACE_DNS, sid))
                    else:
                        _req_headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
                else:
                    _req_headers["Authorization"] = f"Bearer {current_api_key}"
                _upload_tracker = _TimedUpload(_wire_payload)
                response = req_session.post(
                    current_api_url,
                    headers=_req_headers,
                    data=_upload_tracker,
                    timeout=((20, 20) if should_stream else (20, 1800)) if settings.get('enable_ttfb_retry', False) else 1800, proxies={"http": None, "https": None},
                    stream=True
                )
                ttfb = response.elapsed.total_seconds()
                _upload_t = (_upload_tracker.read_complete_time - req_start) if _upload_tracker.read_complete_time else ttfb
                # Capture provider request ID for billing matching (only on success)
                _req_id_header = response.headers.get('X-Oneapi-Request-Id', '')
                if response.status_code >= 400:
                    error_body = ""
                    try:
                        error_body = response.text[:2000]
                    except Exception:
                        error_body = "(无法读取响应体)"
                    print(f"[HTTP {response.status_code}] URL: {current_api_url}")
                    print(f"[响应头] {dict(response.headers)}")
                    print(f"[响应体] {error_body}")
                    _last_error_body = error_body
                    response.raise_for_status()
                
                raw_content = ""
                thought_content = ""
                tool_use_blocks = []
                _current_tool_block = None
                _is_dt3 = (is_deep_think is True) or (isinstance(is_deep_think, int) and is_deep_think >= 2)
                _dt3_early_abort = False
                
                target_msg_for_stream = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                _raw_lines = []
                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                    last_push_time = 0
                    _json_buffer = ""
                    _hallucination_stop = False
                    for line in response.iter_lines(chunk_size=8192):
                        if _hallucination_stop or _dt3_early_abort:
                            break
                        if line:
                            line_str = line.decode('utf-8', errors='ignore')
                            _raw_lines.append(line_str)
                            
                            if _json_buffer:
                                # 续接缓冲区：剥离 data: 前缀但不 strip，保留拆行点空格
                                if line_str.startswith("data: "):
                                    _cont = line_str[6:]
                                elif line_str.startswith("data:"):
                                    _cont = line_str[5:]
                                else:
                                    _cont = line_str
                                _json_buffer += _cont
                                try:
                                    chunk = json.loads(_json_buffer)
                                    _json_buffer = ""
                                except Exception:
                                    continue
                            elif line_str.startswith("data:"):
                                # 兼容 "data: "(标准) 和 "data:"(非标准) 两种 SSE 前缀
                                # 某些第三方代理不发标准格式，不兼容会导致整个事件被静默丢弃
                                _prefix_len = 6 if line_str.startswith("data: ") else 5
                                _raw_payload = line_str[_prefix_len:]
                                _payload_stripped = _raw_payload.strip()
                                if _payload_stripped == "[DONE]":
                                    continue
                                try:
                                    chunk = json.loads(_payload_stripped)
                                except Exception:
                                    # 缓冲区保存未 strip 的原始载荷，防止 strip 吞掉
                                    # JSON 字符串值末尾的空格导致续接时词粘连
                                    _json_buffer = _raw_payload
                                    continue
                            else:
                                continue
                                
                            _got_first_token = True
                            if _first_token_wall_time is None:
                                _first_token_wall_time = time.time()

                            if True:
                                try:
                                    updated = False
                                    if use_anthropic_protocol:
                                        chunk_type = chunk.get('type', '')
                                        if chunk_type == 'content_block_start':
                                            block = chunk.get('content_block', {})
                                            if block.get('type') == 'tool_use':
                                                _current_tool_block = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "input_json": ""}
                                            elif _is_dt3 and not is_pure_enabled and settings.get('enable_thinking_retry', False) and block.get('type') == 'text' and not thought_content:
                                                _dt3_n_check = getattr(self, '_dt3_retry_count', {}).get(target_id, 0)
                                                if _dt3_n_check < 3:
                                                    _dt3_early_abort = True
                                                    print(f'[DEEP THINK 3] Early abort: first content block is text (no thinking), attempt {_dt3_n_check + 1}', flush=True)
                                        elif chunk_type == 'content_block_delta':
                                            delta = chunk.get('delta', {})
                                            delta_type = delta.get('type', '')
                                            if delta_type == 'text_delta':
                                                raw_content += delta.get('text', '')
                                                # 幻觉截断：检测模型预测用户消息的行为并立即停止（纯净模式跳过）
                                                if not is_pure_enabled and '[ID: ' in raw_content and 'Tokens] User:' in raw_content:
                                                    import re as _hre_s
                                                    _hm_s = _hre_s.search(r'\[ID:\s*\d+\]\s*\[预估:\s*[\d.]+k\s*Tokens\]\s*User:', raw_content)
                                                    if _hm_s:
                                                        raw_content = raw_content[:_hm_s.start()].rstrip()
                                                        print(f"[HALLUCINATION TRUNCATION] Stream truncated at offset {_hm_s.start()}", flush=True)
                                                        _hallucination_stop = True
                                                updated = True
                                            elif delta_type == 'thinking_delta':
                                                thought_content += delta.get('thinking', '')
                                                updated = True
                                            elif delta_type == 'input_json_delta' and _current_tool_block is not None:
                                                _current_tool_block['input_json'] += delta.get('partial_json', '')
                                        elif chunk_type == 'content_block_stop':
                                            if _current_tool_block is not None:
                                                try:
                                                    _current_tool_block['input'] = json.loads(_current_tool_block.pop('input_json'))
                                                except Exception:
                                                    _current_tool_block['input'] = {}
                                                    _current_tool_block.pop('input_json', None)
                                                tool_use_blocks.append(_current_tool_block)
                                                _current_tool_block = None
                                    else:
                                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                                        _dc = delta.get('content')
                                        if _dc:
                                            if isinstance(_dc, str):
                                                raw_content += _dc
                                            elif isinstance(_dc, list):
                                                # 多模态响应（如 gemini-image）：提取文本和图片 URL
                                                for _dc_item in _dc:
                                                    if isinstance(_dc_item, dict):
                                                        if _dc_item.get('type') == 'text':
                                                            raw_content += _dc_item.get('text', '')
                                                        elif _dc_item.get('type') == 'image_url':
                                                            _iu = _dc_item.get('image_url', {}).get('url', '')
                                                            if _iu:
                                                                raw_content += f'\n![generated image]({_iu})\n'
                                                    elif isinstance(_dc_item, str):
                                                        raw_content += _dc_item
                                            else:
                                                raw_content += str(_dc)
                                            updated = True
                                            if _is_dt3 and not is_pure_enabled and settings.get('enable_thinking_retry', False) and not thought_content and len(raw_content) > 500 and not _dt3_early_abort:
                                                if not raw_content.lstrip()[:6].lower().startswith('\x3cthink'):
                                                    _dt3_n_check = getattr(self, '_dt3_retry_count', {}).get(target_id, 0)
                                                    if _dt3_n_check < 3:
                                                        _dt3_early_abort = True
                                                        print(f'[DEEP THINK 3] Early abort: 500+ chars without thinking (non-Anthropic), attempt {_dt3_n_check + 1}', flush=True)
                                        if delta.get('reasoning_content') or delta.get('thinking') or delta.get('reasoning'):
                                            thought_content += delta.get('reasoning_content') or delta.get('thinking') or delta.get('reasoning')
                                            updated = True
                                        
                                    if updated and target_msg_for_stream:
                                        if is_stream_enabled:
                                            # 完整渲染模式：实时更新气泡内容（打字机效果）
                                            temp_content = ""
                                            if thought_content:
                                                temp_content += f"> **💭 思考过程：**\n{thought_content}\n\n---\n\n"
                                            temp_content += raw_content
                                            temp_content = temp_content.replace('<turn|>', '').replace('<end_of_turn>', '')
                                            display_content = temp_content
                                            # 过滤隐藏令牌
                                            import re as _re
                                            display_content = _re.sub(r'\[令牌[a-z0-9]+\]\s*', '', display_content)
                                            
                                            target_msg_for_stream['content'] = display_content
                                        
                                            clean_text = display_content.replace('\n', ' ').strip()
                                            if len(clean_text) > 40:
                                                target_msg_for_stream['summary'] = f"生成中: ...{clean_text[-40:]}"
                                            else:
                                                target_msg_for_stream['summary'] = f"生成中: {clean_text}"
                                            
                                            if not target_msg_for_stream.get('_stream_started'):
                                                target_msg_for_stream['is_collapsed'] = True
                                                target_msg_for_stream['_stream_started'] = True
                                            
                                            current_time = time.time()
                                            if current_time - last_push_time > 1.0:
                                                if not target_msg_for_stream.get('_stream_first_pushed'):
                                                    # 首次推送：全量状态（触发前端从"等待中"到"流式"的结构转换）
                                                    target_msg_for_stream['_stream_first_pushed'] = True
                                                    self.save_sessions(push_update=True)
                                                else:
                                                    # 后续推送：轻量事件（只更新目标气泡，不重渲染整页）
                                                    if self.socketio:
                                                        self.socketio.emit('streaming_content', {
                                                            'bubble_id': target_id,
                                                            'content': display_content,
                                                            'summary': target_msg_for_stream['summary']
                                                        })
                                                    self.save_sessions(push_update=False)
                                                last_push_time = current_time
                                        else:
                                            # 轻量计数模式（enable_stream=false）：不实时更新气泡内容，仅间隔性更新 summary 显示已接收的 token 量
                                            current_time = time.time()
                                            if not target_msg_for_stream.get('_stream_started'):
                                                target_msg_for_stream['_stream_started'] = True
                                                target_msg_for_stream['is_collapsed'] = True
                                                target_msg_for_stream['summary'] = "已连接，正在生成..."
                                                self.save_sessions(push_update=True)
                                                last_push_time = current_time
                                            elif current_time - last_push_time > 1.5:
                                                received_chars = len(raw_content) + len(thought_content)
                                                elapsed = current_time - (target_msg_for_stream.get('start_time') or current_time)
                                                token_k = received_chars / 3000
                                                thinking_hint = " 💭" if thought_content else ""
                                                target_msg_for_stream['summary'] = f"生成中{thinking_hint}: 已收到 {token_k:.1f}k tokens ({elapsed:.0f}s)"
                                                if self.socketio:
                                                    self.socketio.emit('streaming_content', {
                                                        'bubble_id': target_id,
                                                        'content': '',
                                                        'summary': target_msg_for_stream['summary']
                                                    })
                                                self.save_sessions(push_update=False)
                                                last_push_time = current_time
                                except Exception:
                                    pass
                else:
                    _got_first_token = True
                    _first_token_wall_time = time.time()
                    _raw_lines = [response.text]
                    if use_anthropic_protocol:
                        result = response.json()
                        for block in result.get('content', []):
                            if block.get('type') == 'text':
                                raw_content += block.get('text', '')
                            elif block.get('type') == 'thinking':
                                thought_content += block.get('thinking', '')
                            elif block.get('type') == 'tool_use':
                                tool_use_blocks.append(block)
                    else:
                        completion = response.json()
                        message = completion.get('choices', [{}])[0].get('message', {})
                        _msg_content = message.get('content') or ""
                        if isinstance(_msg_content, list):
                            # 多模态响应：提取文本和图片
                            _text_parts = []
                            for _mc_item in _msg_content:
                                if isinstance(_mc_item, dict):
                                    if _mc_item.get('type') == 'text':
                                        _text_parts.append(_mc_item.get('text', ''))
                                    elif _mc_item.get('type') == 'image_url':
                                        _iu = _mc_item.get('image_url', {}).get('url', '')
                                        if _iu:
                                            _text_parts.append(f'\n![generated image]({_iu})\n')
                                elif isinstance(_mc_item, str):
                                    _text_parts.append(_mc_item)
                            raw_content = ''.join(_text_parts)
                        else:
                            raw_content = str(_msg_content) if _msg_content else ""
                        thought_content = message.get('reasoning_content') or message.get('thinking') or message.get('reasoning') or ""
                    
                download_t = (time.time() - req_start) - ttfb
                
                # 思考三 early abort: 流式期间检测到无思维链，立即重试
                if _dt3_early_abort:
                    if not hasattr(self, '_dt3_retry_count'):
                        self._dt3_retry_count = {}
                    _dt3_n = self._dt3_retry_count.get(target_id, 0)
                    if _dt3_n < 3:
                        self._dt3_retry_count[target_id] = _dt3_n + 1
                        print(f'[DEEP THINK 3] Early abort during streaming, retrying ({_dt3_n + 1}/3)', flush=True)
                        if self.socketio:
                            self.socketio.emit('show_toast', {'message': f'思考三：模型未返回思维链，重试中 ({_dt3_n + 1}/3)', 'type': 'error'})
                        _target_bubble = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                        if _target_bubble:
                            _target_bubble['content'] = ''
                            _target_bubble['is_collapsed'] = True
                            _target_bubble['summary'] = f'思考三重试中 ({_dt3_n + 1}/3)...'
                            _target_bubble.pop('content_parts', None)
                            _target_bubble.pop('_stream_started', None)
                            _target_bubble.pop('_stream_first_pushed', None)
                            self.save_sessions(push_update=True)
                        import threading as _thr
                        _thr.Thread(target=self.api_worker_thread, args=(sid, stage, total, is_parallel, target_id, is_offline, model_name, is_deep_think)).start()
                        return

                import re as _re
                original_content = raw_content
                
                if not thought_content:
                    # 扩大正则拦截网，兼容 Gemma 4 特有的 <|channel>thought 结构
                    think_match = _re.search(r'(?:<think(?:ing)?>|<\|channel>thought\n?)\s*([\s\S]*?)(?:</think(?:ing)?>|<channel\|>|<\|channel>)', original_content, flags=_re.IGNORECASE | _re.DOTALL)
                    if think_match:
                        thought_content = think_match.group(1).strip()
                        
                raw_content = _re.sub(r'(?:<think(?:ing)?>|<\|channel>thought\n?)[\s\S]*?(?:</think(?:ing)?>|<channel\|>|<\|channel>)', '', original_content, flags=_re.IGNORECASE | _re.DOTALL).strip()
                if not raw_content and not thought_content:
                    raw_content = original_content
                
                raw_content = raw_content.replace('<turn|>', '').replace('<end_of_turn>', '').strip()
                if thought_content:
                    thought_content = thought_content.replace('<turn|>', '').replace('<end_of_turn>', '').strip()
                    
                # 思考三模式：模型未返回思维链直接开始正文时，丢弃本次回复并重试（最多3次）
                _is_dt3 = (is_deep_think is True) or (isinstance(is_deep_think, int) and is_deep_think >= 2)
                if (_is_dt3 and settings.get('enable_thinking_retry', False) and not thought_content and raw_content and not tool_use_blocks
                    and not is_pure_enabled
                    and not _re.match(r'\s*\x3cthink', raw_content, _re.IGNORECASE)):
                    if not hasattr(self, '_dt3_retry_count'):
                        self._dt3_retry_count = {}
                    _dt3_n = self._dt3_retry_count.get(target_id, 0)
                    if _dt3_n < 3:
                        self._dt3_retry_count[target_id] = _dt3_n + 1
                        print(f'[DEEP THINK 3] No thinking chain in response, discarding and retrying ({_dt3_n + 1}/3)', flush=True)
                        if self.socketio:
                            self.socketio.emit('show_toast', {'message': f'思考三：模型未返回思维链，重试中 ({_dt3_n + 1}/3)', 'type': 'error'})
                        _target_bubble = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                        if _target_bubble:
                            _target_bubble['content'] = ''
                            _target_bubble['is_collapsed'] = True
                            _target_bubble['summary'] = f'思考三重试中 ({_dt3_n + 1}/3)...'
                            _target_bubble.pop('content_parts', None)
                            _target_bubble.pop('_stream_started', None)
                            _target_bubble.pop('_stream_first_pushed', None)
                            self.save_sessions(push_update=True)
                        import threading as _thr
                        _thr.Thread(target=self.api_worker_thread, args=(sid, stage, total, is_parallel, target_id, is_offline, model_name, is_deep_think)).start()
                        return
                    else:
                        self._dt3_retry_count.pop(target_id, None)
                        print(f'[DEEP THINK 3] All 3 retries exhausted, returning error', flush=True)
                        _target_bubble = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                        if _target_bubble:
                            _target_bubble['content'] = '**\u26a0\ufe0f \u601d\u8003\u91cd\u8bd5\u5931\u8d25\uff1a**\n\n\u6df1\u5ea6\u601d\u8003\u6a21\u5f0f\u4e0b\u6a21\u578b\u8fde\u7eed 3 \u6b21\u672a\u8fd4\u56de\u601d\u7ef4\u94fe\u3002\n\n\u8bf7\u624b\u52a8\u91cd\u8bd5\u6216\u5173\u95ed\u6df1\u5ea6\u601d\u8003\u6a21\u5f0f\u3002'
                            _target_bubble['is_error'] = True
                            _target_bubble['summary'] = '\u601d\u8003\u91cd\u8bd5\u5931\u8d25'
                            _target_bubble.pop('content_parts', None)
                            _target_bubble.pop('_stream_started', None)
                            _target_bubble.pop('_stream_first_pushed', None)
                        session['autopilot_active'] = False
                        self.save_sessions(push_update=True)
                        self.finalize_process(sid)
                        return

                # 兜底正文分离：raw_content 为空时，以正确令牌为分界点划分思维链和正文
                if thought_content and not raw_content and not tool_use_blocks and not is_pure_enabled:
                    _expected_token = self._to_base36(target_id)
                    _token_marker = f'[令牌{_expected_token}]'
                    _token_pos = thought_content.find(_token_marker)
                    if _token_pos >= 0:
                        _split_pos = thought_content.rfind('\n', 0, _token_pos)
                        if _split_pos >= 0:
                            raw_content = thought_content[_split_pos:].strip()
                            thought_content = thought_content[:_split_pos].strip()
                        else:
                            raw_content = thought_content[_token_pos:].strip()
                            thought_content = thought_content[:_token_pos].strip()
                        print(f'[MISCLASS FIX] Split at token <<<{_expected_token}>>>, thought={len(thought_content)} chars, raw={len(raw_content)} chars', flush=True)
                    else:
                        # 增强 misclass fix：精确令牌未匹配时的分割策略
                        # 优先级：1. 思维链停止符+令牌组合  2. 任意令牌  3. 单独思维链停止符
                        _think_stop_pat = _re.compile(r'(?:</think(?:ing)?>|<channel\|>)', _re.IGNORECASE)
                        _any_token_matches = list(_re.finditer(r'\[令牌([a-z0-9]+)\]', thought_content))
                        _think_stop_match = _think_stop_pat.search(thought_content)
                        
                        _split_done = False
                        
                        # 策略1：思维链停止符后面紧跟令牌（最可靠的分割点）
                        if _think_stop_match and _any_token_matches:
                            _stop_end = _think_stop_match.end()
                            # 找停止符之后的第一个令牌
                            _token_after_stop = None
                            for _m in _any_token_matches:
                                if _m.start() >= _stop_end:
                                    _token_after_stop = _m
                                    break
                            if _token_after_stop:
                                _split_at = _think_stop_match.start()
                                raw_content = thought_content[_split_at:].strip()
                                thought_content = thought_content[:_split_at].strip()
                                _split_done = True
                                print(f'[MISCLASS FIX] Split at stop+token combo, thought={len(thought_content)} chars, raw={len(raw_content)} chars', flush=True)
                        
                        # 策略2：任意 base36 令牌（无停止符或停止符后无令牌）
                        if not _split_done and _any_token_matches:
                            _summary_start_in_thinking = thought_content.find('[概括开头]')
                            if _summary_start_in_thinking >= 0:
                                _best = None
                                for _m in _any_token_matches:
                                    if _m.start() < _summary_start_in_thinking:
                                        _best = _m
                                _any_token_match = _best or _any_token_matches[0]
                            else:
                                _any_token_match = _any_token_matches[0]
                            _any_token_pos = _any_token_match.start()
                            _split_pos = thought_content.rfind('\n', 0, _any_token_pos)
                            if _split_pos >= 0:
                                raw_content = thought_content[_split_pos:].strip()
                                thought_content = thought_content[:_split_pos].strip()
                            else:
                                raw_content = thought_content[_any_token_pos:].strip()
                                thought_content = thought_content[:_any_token_pos].strip()
                            _split_done = True
                            print(f'[MISCLASS FIX] Split at generic token <<<{_any_token_match.group(1)}>>>, thought={len(thought_content)} chars, raw={len(raw_content)} chars', flush=True)
                        
                        # 策略3：单独思维链停止符（无令牌，但有明确的结构边界）
                        if not _split_done and _think_stop_match:
                            _split_at = _think_stop_match.end()
                            _after_stop = thought_content[_split_at:].strip()
                            if _after_stop:  # 停止符后有内容才分割
                                raw_content = _after_stop
                                thought_content = thought_content[:_think_stop_match.start()].strip()
                                _split_done = True
                                print(f'[MISCLASS FIX] Split at thinking stop marker alone, thought={len(thought_content)} chars, raw={len(raw_content)} chars', flush=True)

                # 输出截断检测：没有完整 SUMMARY 块（START+END 配对）时认为被截断（纯净模式跳过）
                _thinking_truncated = False
                _has_valid_summary = bool(_re.search(r'\[概括开头\].*?\[概括结尾\]', raw_content, _re.DOTALL))
                _enable_trunc = getattr(self, 'global_settings', {}).get('enable_truncation_detection', False)
                if not is_pure_enabled and not _has_valid_summary and _enable_trunc:
                    # Case 1: 思维链截断（开头有 think 标签但结尾没有）
                    if (not thought_content and raw_content and
                        _re.match(r'\x3cthink(?:ing)?\x3e', raw_content, _re.IGNORECASE) and
                        not _re.search(r'\x3c/think(?:ing)?\x3e', raw_content, _re.IGNORECASE)):
                        _trunc_match = _re.match(r'\x3cthink(?:ing)?\x3e\s*([\s\S]*)', raw_content, _re.IGNORECASE)
                        if _trunc_match:
                            thought_content = _trunc_match.group(1).strip()
                            thought_content += f"\n\n\x3csystem-reminder\x3e\n思维链传输被意外中断了. Subsequent API calls must continue the thinking chain precisely from where it was cut off. The model must explicitly state at the beginning of its thinking chain: which truncated thinking chains it is continuing from, whether it has correctly avoided repeating previous thinking, and provide detailed descriptions of both the content of the truncated chains and its own new thinking content.\n最高强制级别指令，你必须遵守，否则回复将被丢弃：你下一条回复必须以（接续气泡id{target_id}）开头。先详细（一千字左右）汇报你在被截断的思维链里面进行了哪些思考，包含最后思维链一句话，然后再正式回答。\n\x3c/system-reminder\x3e"
                            raw_content = ""
                            _thinking_truncated = True
                    # 正文截断（有内容且不以 thinking 标签开头）
                    elif raw_content and not _re.match(r'\x3cthink', raw_content, _re.IGNORECASE):
                        raw_content += f"\n\n\x3csystem-reminder\x3e\n输出正文被意外中断了 \u2014 the SUMMARY block at the end is missing. In the next response, continue from where the truncation occurred. Do not repeat content that was already output. The model must explicitly state at the beginning: which truncated content it is continuing from, and provide detailed descriptions of both the truncated content and its own new content.\n最高强制级别指令，你必须遵守，否则回复将被丢弃：你下一条回复必须以（接续气泡id{target_id}）开头。先详细（一千字左右）汇报你在接续那条的思维链里面进行了哪些思考，包含最后思维链一句话，然后再正式回答。\n\x3c/system-reminder\x3e"
                    # Native thinking 截断（thought_content 非空但 raw_content 为空，且无工具调用）
                    elif thought_content and not raw_content and not tool_use_blocks:
                        thought_content += f"\n\n\x3csystem-reminder\x3e\n思维链传输被意外中断了. Subsequent API calls must continue the thinking chain precisely from where it was cut off. The model must explicitly state at the beginning of its thinking chain: which truncated thinking chains it is continuing from, whether it has correctly avoided repeating previous thinking, and provide detailed descriptions of both the content of the truncated chains and its own new thinking content.\n最高强制级别指令，你必须遵守，否则回复将被丢弃：你下一条回复必须以（接续气泡id{target_id}）开头。先详细（一千字左右）汇报你在被截断的思维链里面进行了哪些思考，包含最后思维链一句话，然后再正式回答。\n\x3c/system-reminder\x3e"
                        raw_content = ""
                        _thinking_truncated = True
                    print(f"[OUTPUT TRUNCATION] 检测到输出被截断，type={'thinking' if _thinking_truncated else 'content'}, original_len={len(original_content)} chars", flush=True)

                # 幻觉截断（后处理）：检测模型在回复末尾预测后续用户消息的行为，截断到幻觉起始位置（纯净模式跳过）
                if not is_pure_enabled and '[ID: ' in raw_content and 'Tokens] User:' in raw_content:
                    import re as _hre_post
                    _hall_match = _hre_post.search(r'\[ID:\s*\d+\]\s*\[预估:\s*[\d.]+k\s*Tokens\]\s*User:', raw_content)
                    if _hall_match:
                        raw_content = raw_content[:_hall_match.start()].rstrip()
                        print(f"[HALLUCINATION TRUNCATION] Post-processing truncated to {len(raw_content)} chars", flush=True)
                if thought_content:
                    target_idx = next((i for i, m in enumerate(session['conversation_history']) if m['id'] == target_id), -1)
                    if target_idx != -1:
                        new_id = self._next_id()

                        display_m_name = model_name or self.config.get("MODEL_NAME", "")
                        _think_summary = "模型内置思维链 (到达8k被截断)" if _thinking_truncated else "模型内置思维链"
                        thought_msg = self._make_msg("assistant", thought_content,
                            summary=_think_summary, msg_id=new_id,
                            is_hidden=not session.get('thinking_visible', True),
                            is_collapsed=True, model_name=f"{display_m_name} (思考过程)",
                            cc_type='thinking')
                        session['conversation_history'].insert(target_idx, thought_msg)
            
            # 保存完整的原始响应到文件（含thinking/正文/tool_use）
            if getattr(self, 'global_settings', {}).get('enable_bulk_logging', False):
                try:
                    _dump_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'resp_dumps')
                    os.makedirs(_dump_dir, exist_ok=True)
                    _ts = time.strftime('%Y%m%d_%H%M%S')
                    _safe_model = real_model_name.replace('/', '_').replace(' ', '_')[:30]
                    _resp_dump = {
                        "timestamp": _ts,
                        "model": real_model_name,
                        "target_id": target_id,
                        "thinking": thought_content,
                        "content": raw_content,
                        "tool_use_blocks": [tb for tb in tool_use_blocks] if tool_use_blocks else [],
                        "raw_response_lines": _raw_lines,
                        "ttfb": ttfb,
                        "download_t": download_t,
                        "stream_enabled": is_stream_enabled,
                    }
                    _resp_path = os.path.join(_dump_dir, f'resp_{_ts}_{_safe_model}.json')
                    with open(_resp_path, 'w', encoding='utf-8') as _df:
                        json.dump(_resp_dump, _df, ensure_ascii=False, indent=2)
                    print(f"[RESP DUMP] Saved to {_resp_path}")
                except Exception as _de:
                    print(f"[RESP DUMP ERROR] {_de}")

            # 图片 URL 自动下载：检测响应中的图片链接，下载并转为 multimodal_blocks
            _img_url_pattern = re.compile(r'(https?://[^\s]+(?:/v1/files/image|/images/generations|/img/|/image\?)[^\s]*)', re.IGNORECASE)
            _found_img_urls = _img_url_pattern.findall(raw_content)
            if not _found_img_urls:
                # 检测以图片扩展名结尾的 URL
                _ext_img_pattern = re.compile(r'(https?://[^\s]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^\s]*)?)', re.IGNORECASE)
                _found_img_urls = _ext_img_pattern.findall(raw_content)
            if not _found_img_urls:
                # 也检测 markdown 图片语法中的 URL
                _md_img_pattern = re.compile(r'!\[.*?\]\((https?://[^\s)]+)\)')
                _found_img_urls = _md_img_pattern.findall(raw_content)
            if _found_img_urls:
                import base64 as _dl_b64
                import uuid as _dl_uuid
                _dl_mm_blocks = []
                _img_save_dir = os.path.join(getattr(self, 'data_dir', 'data'), 'images')
                os.makedirs(_img_save_dir, exist_ok=True)
                for _dl_url in _found_img_urls[:4]:  # 最多下载4张
                    try:
                        _dl_resp = requests.get(_dl_url, timeout=60, proxies={'http': None, 'https': None})
                        if _dl_resp.status_code == 200 and len(_dl_resp.content) > 1000:
                            _ct = _dl_resp.headers.get('Content-Type', 'image/png')
                            if 'image' in _ct or len(_dl_resp.content) > 10000:
                                _ext = '.png' if 'png' in _ct else '.jpg' if 'jpeg' in _ct or 'jpg' in _ct else '.png'
                                _dl_fname = f'gen_{_dl_uuid.uuid4().hex[:12]}{_ext}'
                                with open(os.path.join(_img_save_dir, _dl_fname), 'wb') as _dlf:
                                    _dlf.write(_dl_resp.content)
                                _dl_mm_blocks.append({'type': 'image', 'source': {'type': 'file', 'media_type': _ct.split(';')[0], 'path': _dl_fname, 'size': len(_dl_resp.content)}})
                                print(f'[IMG URL DOWNLOAD] Saved {_dl_fname} ({len(_dl_resp.content)/1024:.1f} KB) from {_dl_url[:80]}', flush=True)
                    except Exception as _dl_e:
                        print(f'[IMG URL DOWNLOAD] Failed for {_dl_url[:80]}: {_dl_e}', flush=True)
                if _dl_mm_blocks:
                    _target_for_img = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                    if _target_for_img:
                        _target_for_img.setdefault('multimodal_blocks', []).extend(_dl_mm_blocks)

            # Store request_id only after successful response (not on retries/errors)
            if _req_id_header:
                _target_for_reqid = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                if _target_for_reqid:
                    _target_for_reqid['_request_id'] = _req_id_header

            self.process_ai_response(sid, raw_content, ttfb, download_t, is_parallel, original_target_id=target_id, tool_use_blocks=tool_use_blocks)

            # 请求成功，清理重试计数器
            if hasattr(self, '_request_retry_count'):
                self._request_retry_count.pop(target_id, None)
            if hasattr(self, '_ttfb_retry_count'):
                self._ttfb_retry_count.pop(target_id, None)
            if hasattr(self, '_rate_limit_retry_count'):
                self._rate_limit_retry_count.pop(target_id, None)

            # 将流式模式标记存入 timing 字典，供前端决定显示「首字+接收」还是「非流式总用时」
            _tm = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
            if _tm:
                # Preserve original creation time for kanban timeline before overwriting
                if 'started_at' not in _tm:
                    _tm['started_at'] = _tm.get('created_at', time.time())
                _tm['completed_at'] = time.time()
                # 修复气泡时间与计费日志时间不匹配的问题：将气泡的创建时间更新为生成结束的时间
                _tm['created_at'] = time.time()
                if not _tm.get('timing'):
                    _tm['timing'] = {}
                _tm['timing']['stream'] = should_stream
                _tm['timing']['download_t'] = round(download_t, 3)
                _tm['timing']['ttfb'] = round(ttfb, 3)
                _tm['timing']['context_t'] = round(req_start - _context_start, 3)
                _tm['timing']['payload_bytes'] = _payload_bytes
                _tm['timing']['upload_t'] = round(_upload_t, 3)
                if _first_token_wall_time:
                    _tm['timing']['first_token'] = round(_first_token_wall_time - req_start, 3)
                if _tm.get('_ttfb_retries'):
                    _tm['timing']['ttfb_retries'] = _tm.pop('_ttfb_retries')

            # 追踪每次调用的费用（会话级 + 全局级累计）
            from .provider_routes import get_model_price
            _call_price = get_model_price(real_model_name)
            if _call_price > 0:
                # Global spending (backward compatibility)
                if not hasattr(self, 'spending'):
                    self.spending = {"total": 0, "by_model": {}}
                self.spending["total"] = self.spending.get("total", 0) + _call_price
                _by_model = self.spending.setdefault("by_model", {})
                _by_model[real_model_name] = _by_model.get(real_model_name, 0) + _call_price
                # Per-session spending
                _s_spending = session.setdefault('spending', {"total": 0, "by_model": {}})
                _s_spending["total"] = _s_spending.get("total", 0) + _call_price
                _s_by_model = _s_spending.setdefault("by_model", {})
                _s_by_model[real_model_name] = _s_by_model.get(real_model_name, 0) + _call_price

        except Exception as e:
            # 首字超时重试：流式请求在收到首个token前超时，独立重试最多10次（enable_ttfb_retry 开关控制）
            if settings.get('enable_ttfb_retry', False) and not _got_first_token and should_stream:
                import requests as _req_exc_mod
                if isinstance(e, (_req_exc_mod.exceptions.Timeout, _req_exc_mod.exceptions.ConnectionError, ConnectionError, OSError)):
                    if not hasattr(self, '_ttfb_retry_count'):
                        self._ttfb_retry_count = {}
                    _ttfb_n = self._ttfb_retry_count.get(target_id, 0)
                    if _ttfb_n < 10:
                        self._ttfb_retry_count[target_id] = _ttfb_n + 1
                        print(f'[TTFB RETRY] No first token within 20s ({str(e)[:60]}), retrying ({_ttfb_n + 1}/10)', flush=True)
                        if self.socketio:
                            self.socketio.emit('show_toast', {'message': f'首字超时，自动重试中 ({_ttfb_n + 1}/10)', 'type': 'error'})
                        target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                        if target_msg:
                            _retries_list = target_msg.setdefault('_ttfb_retries', [])
                            _retries_list.append({'attempt': _ttfb_n + 1, 'elapsed': round(time.time() - req_start, 2), 'error': str(e)[:80]})
                            target_msg['content'] = ''
                            target_msg['summary'] = f'首字超时重试中 ({_ttfb_n + 1}/10)...'
                            target_msg['is_collapsed'] = True
                            target_msg.pop('content_parts', None)
                            target_msg.pop('_stream_started', None)
                            target_msg.pop('_stream_first_pushed', None)
                            target_msg.pop('is_error', None)
                            self.save_sessions(push_update=True)
                        # Re-check deletion before retry
                        if target_id in session.get('deleted_msg_ids', []) or not any(m['id'] == target_id for m in session.get('conversation_history', [])):
                            print(f'[TTFB ABORT] target_id={target_id} deleted during backoff, aborting', flush=True)
                            self._ttfb_retry_count.pop(target_id, None)
                            return
                        import threading as _thr
                        _thr.Thread(target=self.api_worker_thread, args=(sid, stage, total, is_parallel, target_id, is_offline, model_name, is_deep_think)).start()
                        return
                    self._ttfb_retry_count.pop(target_id, None)

            # 429/Rate limit detection: exponential backoff, unlimited retries
            _is_rate_limit = '429' in _last_error_body or 'rate' in _last_error_body.lower()
            if _is_rate_limit:
                if not hasattr(self, '_rate_limit_retry_count'):
                    self._rate_limit_retry_count = {}
                _rl_n = self._rate_limit_retry_count.get(target_id, 0)
                self._rate_limit_retry_count[target_id] = _rl_n + 1
                _backoff_s = min(5 * (2 ** _rl_n), 300)  # 5, 10, 20, 40, 80, 160, 300 cap
                print(f'[RATE LIMIT] 429 detected, backing off {_backoff_s}s (attempt {_rl_n + 1})', flush=True)
                if self.socketio:
                    self.socketio.emit('show_toast', {'message': f'上游限流，{_backoff_s}秒后重试 (第{_rl_n + 1}次)', 'type': 'error'})
                target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                if target_msg:
                    target_msg['content'] = ''
                    target_msg['summary'] = f'限流等待 {_backoff_s}s (第{_rl_n + 1}次)...'
                    target_msg['is_collapsed'] = True
                    target_msg.pop('content_parts', None)
                    target_msg.pop('_stream_started', None)
                    target_msg.pop('_stream_first_pushed', None)
                    target_msg.pop('is_error', None)
                    self.save_sessions(push_update=True)
                time.sleep(_backoff_s)
                # Re-check deletion after long sleep (up to 300s)
                _post_sleep_deleted = target_id in session.get('deleted_msg_ids', []) or not any(m['id'] == target_id for m in session.get('conversation_history', []))
                if _post_sleep_deleted:
                    print(f'[RATE LIMIT ABORT] target_id={target_id} deleted during backoff sleep, aborting', flush=True)
                    self._rate_limit_retry_count.pop(target_id, None)
                    return
                import threading as _thr
                _thr.Thread(target=self.api_worker_thread, args=(sid, stage, total, is_parallel, target_id, is_offline, model_name, is_deep_think)).start()
                return

            # 自动重试：非限流的其他请求失败，最多3次立刻重试
            if not hasattr(self, '_request_retry_count'):
                self._request_retry_count = {}
            _retry_n = self._request_retry_count.get(target_id, 0)
            if _retry_n < 3:
                self._request_retry_count[target_id] = _retry_n + 1
                print(f'[AUTO RETRY] Request failed ({str(e)[:80]}), retrying ({_retry_n + 1}/3)', flush=True)
                if self.socketio:
                    self.socketio.emit('show_toast', {'message': f'请求失败，自动重试中 ({_retry_n + 1}/3)', 'type': 'error'})
                target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                if target_msg:
                    target_msg['content'] = ''
                    target_msg['summary'] = f'重试中 ({_retry_n + 1}/3)...'
                    target_msg['is_collapsed'] = True
                    target_msg.pop('content_parts', None)
                    target_msg.pop('_stream_started', None)
                    target_msg.pop('_stream_first_pushed', None)
                    target_msg.pop('is_error', None)
                    self.save_sessions(push_update=True)
                import threading as _thr
                _thr.Thread(target=self.api_worker_thread, args=(sid, stage, total, is_parallel, target_id, is_offline, model_name, is_deep_think)).start()
                return
            self._request_retry_count.pop(target_id, None)

            print(f'请求失败: {str(e)}')
            if self.socketio:
                self.socketio.emit('show_toast', {'message': f"请求失败: {str(e)}", 'type': 'error'})
            target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
            if target_msg and not target_msg.get('content'):
                target_msg['content'] = f"**⚠️ 请求发生异常：**\n\n{'`'*3}text\n{str(e)}\n{'`'*3}"
                target_msg['summary'] = "请求失败"
                target_msg['is_error'] = True
                
            if session.get('autopilot_active') and session.get('autopilot_turns_left', 0) > 0:
                # 触发器模式下请求失败：带重试计数器，5分钟后重试，3次用完停止
                if session.get('trigger_autopilot'):
                    _retries = session.get('_trigger_retries_left', 0)
                    session['autopilot_active'] = False
                    session['autopilot_turns_left'] = 0
                    session['trigger_autopilot'] = False
                    if _retries > 0:
                        session['_trigger_retries_left'] = _retries - 1
                        import datetime as _dt_retry
                        _retry_time = _dt_retry.datetime.now() + _dt_retry.timedelta(minutes=5)
                        self._add_trigger(sid, _retry_time)
                        print(f'[TRIGGER] Request failed ({str(e)[:80]}), retry in 5 min (retries left: {_retries-1})', flush=True)
                    else:
                        print(f'[TRIGGER] Request failed, all 3 retries exhausted', flush=True)
                    self.finalize_process(sid)
                else:
                    session['autopilot_turns_left'] -= 1
                    if session['autopilot_turns_left'] <= 0:
                        session['autopilot_active'] = False
                        self.finalize_process(sid)
                    else:
                        _retry_append = "\n\n系统提示：刚刚的生成请求因网络或接口故障发生异常，已扣除1次托管次数并自动重试。请直接继续您未完成的任务。"
                        auto_text = self._make_autopilot_prompt(session, append=_retry_append)
                        self.send_message(auto_text, [session.get('autopilot_model')], is_early=True, is_deep_think=session.get('_deep_think_active', False), sid=sid)
            else:
                session['autopilot_active'] = False
                session['autopilot_turns_left'] = 0
                self.finalize_process(sid)

    def _handle_image_generation(self, sid, session, target_id, model_name):
        """Handle image generation models (DALL-E, gpt-image-2, Flux, etc.).

        Extracts the user's last message as prompt, sends to /v1/images/generations,
        saves the generated image, and creates a bubble with multimodal_blocks.
        """
        import base64 as _ig_b64
        import uuid as _ig_uuid

        # 提取用户最后一条消息作为 prompt
        prompt = ''
        for msg in reversed(session['conversation_history']):
            if msg['id'] == target_id:
                continue
            if msg.get('role') == 'user' and not msg.get('is_hidden') and msg.get('content', '').strip():
                prompt = msg['content'].strip()
                break

        if not prompt:
            target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
            if target_msg:
                target_msg['content'] = '**⚠️ 生图失败：** 未找到有效的用户消息作为提示词。'
                target_msg['summary'] = '生图失败: 无提示词'
                target_msg['is_error'] = True
            self.save_sessions(push_update=True)
            self.finalize_process(sid)
            return

        # 提供商路由
        current_api_url, current_api_key, _routed_model = route_provider(model_name, self.config)

        # 构建图片生成端点 URL
        _img_api_url = current_api_url
        for suffix in ['/v1/chat/completions', '/v1/chat', '/v1/messages']:
            if _img_api_url.endswith(suffix):
                _img_api_url = _img_api_url[:-len(suffix)]
                break
        _img_api_url = _img_api_url.rstrip('/') + '/v1/images/generations'

        # 解析 prompt 中的尺寸指令
        _size = '1024x1024'
        # NAI 模型只支持特定尺寸
        _is_nai = 'nai-diffusion' in _routed_model.lower()
        if _is_nai:
            _ratio_map = {'1:1': '1024x1024', '4:3': '1216x832', '3:4': '832x1216',
                          '16:9': '1216x832', '9:16': '832x1216', '3:2': '1216x832', '2:3': '832x1216'}
        else:
            _ratio_map = {'1:1': '1024x1024', '4:3': '1536x1024', '3:4': '1024x1536',
                          '16:9': '1792x1024', '9:16': '1024x1792', '3:2': '1536x1024', '2:3': '1024x1536'}
        _size_match = re.search(r'(\d{3,4})\s*[xX\u00d7]\s*(\d{3,4})', prompt)
        _ratio_match = re.search(r'(\d+)\s*[\uff1a:]\s*(\d+)', prompt)
        if _size_match:
            _req_size = f'{_size_match.group(1)}x{_size_match.group(2)}'
            if _is_nai and _req_size not in ('1024x1024', '832x1216', '1216x832'):
                # NAI 不支持的尺寸，自动映射到最接近的
                _w, _h = int(_size_match.group(1)), int(_size_match.group(2))
                if _w > _h:
                    _size = '1216x832'
                elif _h > _w:
                    _size = '832x1216'
                else:
                    _size = '1024x1024'
            else:
                _size = _req_size
        elif _ratio_match:
            _ratio_key = f'{_ratio_match.group(1)}:{_ratio_match.group(2)}'
            if _ratio_key in _ratio_map:
                _size = _ratio_map[_ratio_key]

        payload = {
            'model': _routed_model,
            'prompt': prompt,
            'n': 1,
            'size': _size,
        }

        print(f'[IMAGE GEN] model={_routed_model}, size={_size}, prompt={prompt[:80]}...', flush=True)
        print(f'[IMAGE GEN] URL: {_img_api_url}', flush=True)

        # 更新气泡状态为生成中
        target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
        if target_msg:
            target_msg['summary'] = f'\U0001f3a8 生图中: {prompt[:30]}...'
            target_msg['model_name'] = model_name
            self.save_sessions(push_update=True)

        try:
            req_start = time.time()
            resp = requests.post(
                _img_api_url,
                headers={
                    'Authorization': f'Bearer {current_api_key}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=180,
                proxies={'http': None, 'https': None}
            )
            elapsed = time.time() - req_start

            if resp.status_code >= 400:
                error_body = resp.text[:2000]
                print(f'[IMAGE GEN] Error {resp.status_code}: {error_body}', flush=True)
                if target_msg:
                    target_msg['content'] = f'**\u26a0\ufe0f 生图失败 (HTTP {resp.status_code})：**\n\n{chr(96)*3}\n{error_body}\n{chr(96)*3}'
                    target_msg['summary'] = f'生图失败: HTTP {resp.status_code}'
                    target_msg['is_error'] = True
                    target_msg['timing'] = {'ttfb': elapsed, 'download': 0, 'stream': False}
                self.save_sessions(push_update=True)
                self.finalize_process(sid)
                return

            data = resp.json()
            _img_dir = os.path.join(getattr(self, 'data_dir', 'data'), 'images')
            os.makedirs(_img_dir, exist_ok=True)

            if 'data' in data and len(data['data']) > 0:
                item = data['data'][0]
                _img_fname = f'gen_{_ig_uuid.uuid4().hex[:12]}.png'
                _img_path = os.path.join(_img_dir, _img_fname)

                if 'b64_json' in item:
                    _img_bytes = _ig_b64.b64decode(item['b64_json'])
                    with open(_img_path, 'wb') as _f:
                        _f.write(_img_bytes)
                    _img_size = len(_img_bytes)
                elif 'url' in item:
                    try:
                        _dl_resp = requests.get(item['url'], timeout=60, proxies={'http': None, 'https': None})
                        if _dl_resp.status_code == 200:
                            with open(_img_path, 'wb') as _f:
                                _f.write(_dl_resp.content)
                            _img_size = len(_dl_resp.content)
                        else:
                            raise Exception(f'Image download failed: HTTP {_dl_resp.status_code}')
                    except Exception as _dl_e:
                        if target_msg:
                            target_msg['content'] = f'**\u26a0\ufe0f 图片下载失败：** {str(_dl_e)}'
                            target_msg['summary'] = '生图失败: 下载错误'
                            target_msg['is_error'] = True
                            target_msg['timing'] = {'ttfb': elapsed, 'download': 0, 'stream': False}
                        self.save_sessions(push_update=True)
                        self.finalize_process(sid)
                        return
                else:
                    if target_msg:
                        target_msg['content'] = f'**\u26a0\ufe0f 生图失败：** 响应格式未知\n\n{chr(96)*3}json\n{json.dumps(item, ensure_ascii=False, indent=2)[:1000]}\n{chr(96)*3}'
                        target_msg['summary'] = '生图失败: 未知格式'
                        target_msg['is_error'] = True
                    self.save_sessions(push_update=True)
                    self.finalize_process(sid)
                    return

                # 成功：创建带 multimodal_blocks 的气泡
                if target_msg:
                    target_msg['content'] = f'\U0001f3a8 **生成完成** ({_size}, {_img_size/1024:.1f} KB)\n\n提示词: {prompt[:200]}'
                    target_msg['summary'] = f'\U0001f3a8 生图完成: {prompt[:30]}...'
                    target_msg['is_collapsed'] = True
                    target_msg['timing'] = {'ttfb': elapsed, 'download': 0, 'stream': False}
                    target_msg['multimodal_blocks'] = [{
                        'type': 'image',
                        'source': {'type': 'file', 'media_type': 'image/png', 'path': _img_fname, 'size': _img_size}
                    }]
                print(f'[IMAGE GEN] Success: {_img_fname} ({_img_size/1024:.1f} KB, {elapsed:.1f}s)', flush=True)
            else:
                if target_msg:
                    target_msg['content'] = f'**\u26a0\ufe0f 生图失败：** 响应中无图片数据\n\n{chr(96)*3}json\n{json.dumps(data, ensure_ascii=False, indent=2)[:1000]}\n{chr(96)*3}'
                    target_msg['summary'] = '生图失败: 无数据'
                    target_msg['is_error'] = True

            self.save_sessions(push_update=True)
            self.finalize_process(sid)

        except Exception as e:
            print(f'[IMAGE GEN] Exception: {e}', flush=True)
            import traceback
            traceback.print_exc()
            if target_msg:
                target_msg['content'] = f'**\u26a0\ufe0f 生图异常：**\n\n{chr(96)*3}\n{str(e)}\n{chr(96)*3}'
                target_msg['summary'] = f'生图异常: {str(e)[:30]}'
                target_msg['is_error'] = True
            self.save_sessions(push_update=True)
            self.finalize_process(sid)



