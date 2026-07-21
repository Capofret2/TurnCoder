"""Api mixin: AI response processing - routing, parsing, content slicing."""
import json
import re
import time
import uuid

from .provider_routes import strip_composite


_KNOWN_DESC_TOOLS = {'Read', 'Write', 'Edit', 'Bash', 'WebSearch', 'WebFetch', '自动审稿', '压缩', '单关卡审稿', '展开气泡', '命名会话', '创建子会话', '结束子会话', '申请审批', '缩减读取'}


def _detect_unclosed_tools(text):
    """Detect [ToolName开始] markers without matching [ToolName结束]."""
    if not text:
        return []
    unclosed = []
    lines = text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith('开始]') and len(stripped) > 4:
            candidate = stripped[1:-3]
            if candidate in _KNOWN_DESC_TOOLS:
                end_marker = f'[{candidate}结束]'
                if end_marker not in text:
                    desc_line = ''
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip():
                            desc_line = lines[j].strip()
                            break
                    unclosed.append((candidate, desc_line))
    return unclosed


class ResponseMixin:
    """Api mixin: AI response processing - routing, parsing, content slicing."""

    def process_ai_response(self, sid, raw_content, ttfb, download_t, is_parallel, original_target_id=None, is_offline_return=False, tool_use_blocks=None):
        session = self.sessions[sid]
        import re
        
        is_routed = False
        target_msg = None
        
        _pure_settings = getattr(self, 'global_settings', {})
        is_pure = _pure_settings.get('enable_pure_mode', False)
        
        # 【核心修复】：无论是否为纯净模式，只要引擎下发了确切的原始目标 ID，强制优先锁定
        if original_target_id:
            # Bug 3 fix: 已删除的目标气泡直接静默丢弃，不创建未知来源
            if original_target_id in session.get('deleted_msg_ids', []):
                print(f"信息：目标气泡ID {original_target_id} 已被删除，已丢弃该API回复。")
                if is_parallel:
                    session['active_threads'] -= 1
                    if session['active_threads'] <= 0: self.finalize_process(sid)
                else:
                    self.finalize_process(sid)
                return
            target_msg = next((m for m in session['conversation_history'] if m['id'] == original_target_id and m['role'] == 'assistant'), None)
            if target_msg:
                is_routed = True
        
        target_id_match = re.search(r'\[令牌([a-z0-9]+)\]', raw_content) if not is_routed else None

        if target_id_match:
            try:
                target_id = int(target_id_match.group(1), 36)
            except ValueError:
                target_id = -1
            if target_id in session.get('deleted_msg_ids', []):
                print(f"信息：目标气泡ID {target_id} 已被删除，已丢弃该API回复。")
                if is_parallel:
                    session['active_threads'] -= 1
                    if session['active_threads'] <= 0: self.finalize_process(sid)
                else:
                    self.finalize_process(sid)
                return
            # 移除 target_msg['content'] == "" 的致命死锁，防止流式期间被填入文字后拒收认领
            if not is_routed:
                target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
                if target_msg and target_msg['role'] == 'assistant':
                    is_routed = True

        if is_routed:
            clean_content = raw_content.strip()
        else:
            # 彻底废除基于 original_target_id 的盲目填坑，严格隔离生成未知来源气泡
            target_msg = self._make_msg("assistant", "",
                summary="未知来源数据", start_time=time.time(),
                model_name="未知来源气泡")
            session['conversation_history'].append(target_msg)
            print(f"信息：未找到或未命中明确的目标气泡，已严格生成未知来源气泡ID {target_msg['id']}")
            
            # 坚决不删除可能残缺或写错的 ID 格式，原封不动保留现场证据
            clean_content = raw_content.strip()

        # Bug 1 fix: raw_content 为空（或仅含 system-reminder）但思维链气泡中包含完整回复内容时，从思维链中分割提取
        _extracted_from_thinking = False
        _clean_for_split_check = re.sub(r'<system-reminder>.*?</system-reminder>', '', clean_content, flags=re.DOTALL).strip() if clean_content else ''
        if is_routed and not _clean_for_split_check and original_target_id:
            _t_idx = next((i for i, m in enumerate(session['conversation_history']) if m['id'] == original_target_id), -1)
            if _t_idx > 0:
                _think_bubble = session['conversation_history'][_t_idx - 1]
                if _think_bubble.get('tool_type') == 'thinking' and _think_bubble.get('content'):
                    _tc = _think_bubble['content']
                    _tok_m = re.search(r'\[令牌([a-z0-9]+)\]', _tc)
                    _sum_in_think = re.search(r'\[概括开头\].*?\[概括结尾\]', _tc, re.DOTALL)
                    if _tok_m or _sum_in_think:
                        _extracted_from_thinking = True
                        if _tok_m:
                            _sp = _tok_m.start()
                            _nl = _tc.rfind('\n', 0, _sp)
                            if _nl >= 0:
                                clean_content = _tc[_nl:].strip()
                                _think_bubble['content'] = _tc[:_nl].strip()
                            else:
                                clean_content = _tc[_sp:].strip()
                                _think_bubble['content'] = _tc[:_sp].strip()
                        else:
                            _sp = _tc.find('[概括开头]')
                            # Split further back to include response content before SUMMARY
                            _lookback = max(0, _sp - 1000)
                            _nl = _tc.rfind('\n\n', 0, _lookback)
                            if _nl >= 0 and _nl > len(_tc) // 4:
                                clean_content = _tc[_nl:].strip()
                                _think_bubble['content'] = _tc[:_nl].strip()
                            else:
                                clean_content = _tc[_sp:].strip()
                                _think_bubble['content'] = _tc[:_sp].strip()

        ai_sum = "纯净模式" if is_pure else "暂无"
        
        # SUMMARY 提取：从后往前找最后一个 START，再从那里匹配到最近的 END
        _sum_start_marker = '[概括开头]'
        _sum_end_marker = '[概括结尾]'
        summary_match = None
        if not is_pure:
            _last_start = clean_content.rfind(_sum_start_marker)
            if _last_start >= 0:
                _end_after = clean_content.find(_sum_end_marker, _last_start)
                if _end_after >= 0:
                    _sm_s = _last_start
                    _sm_e = _end_after + len(_sum_end_marker)
                    summary_match = clean_content[_last_start + len(_sum_start_marker):_end_after]
        if summary_match is not None:
            summary_text = summary_match
            summary_lines = summary_text.strip().split('\n')
            user_sums = {}
            for line in summary_lines:
                line = line.strip()
                if not line: continue
                if line.startswith("助手："):
                    ai_sum = line[3:].strip()
                elif line.startswith("用户："):
                    user_sums['default'] = line[3:].strip()
                else:
                    m_id = re.match(r'^用户\s*\[?ID:\s*(\d+)\]?[：:]\s*(.*)', line, re.IGNORECASE)
                    if m_id:
                        user_sums[int(m_id.group(1))] = m_id.group(2).strip()
        
            for m in reversed(session['conversation_history']):
                if m['role'] == 'user' and m.get('summary') == "正在生成...":
                    if m.get('id') in user_sums:
                        m['summary'] = user_sums[m['id']]
                    elif 'default' in user_sums:
                        m['summary'] = user_sums['default']
                    else:
                        m['summary'] = "已概括"
        
        target_msg['content'] = clean_content
        target_msg['summary'] = ai_sum
        target_msg['is_collapsed'] = True
        target_msg['is_unread'] = True
        # 离线回传时间统计补偿
        if ttfb == 0.0 and download_t == 0.0 and 'start_time' in target_msg:
            real_time = max(0.0, time.time() - target_msg['start_time'])
            ttfb = real_time

        target_msg['timing'] = {"prep": 0.0, "ttfb": ttfb, "download": download_t}

        _is_tool = getattr(self, 'global_settings', {}).get('enable_tool_inject', False)
        # 预处理：描述符格式工具调用解析，用占位符替代
        _bare_tc_map = {}
        _tc_ph_counter = 0
        if _is_tool:
            # Pass D: 描述符格式工具调用解析 [ToolName开始]...[ToolName结束]
            _enable_descriptor = getattr(self, 'global_settings', {}).get('enable_descriptor_tool_calls', True)
            if _enable_descriptor:
                _NUM_PARAMS = {'offset', 'limit', 'timeout', 'count', 'interval_minutes', 'checkpoint_index', 'max_steps'}
                _BOOL_PARAMS_D = {'replace_all', 'run_in_background', 'force_full', 'enable_baseline', 'cancel'}
                _ARR_PARAMS = {'allowed_domains', 'blocked_domains', 'message_ids', 'checkpoint_indices'}

                _dlines = clean_content.split('\n')
                _dline_starts = []
                _dpos = 0
                for _dl in _dlines:
                    _dline_starts.append(_dpos)
                    _dpos += len(_dl) + 1

                _enable_planned = getattr(self, 'global_settings', {}).get('enable_planned_tools', False) and _enable_descriptor
                _desc_hits = []
                _di = 0
                _tool_seq_counter = 0
                while _di < len(_dlines):
                    _ds = _dlines[_di].strip()
                    _dtool = None
                    _d_is_planned = False
                    _d_mode_suffix = '开始]'
                    if _ds.startswith('[') and len(_ds) > 4:
                        if _enable_planned and _ds.endswith('立即开始]'):
                            _candidate = _ds[1:-5]
                            _d_mode_suffix = '立即'
                            if _candidate in _KNOWN_DESC_TOOLS:
                                _dtool = _candidate
                        elif _enable_planned and _ds.endswith('计划开始]'):
                            _candidate = _ds[1:-5]
                            _d_mode_suffix = '计划'
                            _d_is_planned = True
                            if _candidate in _KNOWN_DESC_TOOLS:
                                _dtool = _candidate
                        elif _ds.endswith('开始]'):
                            _candidate = _ds[1:-3]
                            if _candidate in _KNOWN_DESC_TOOLS:
                                _dtool = _candidate
                    if not _dtool:
                        _di += 1
                        continue

                    if _d_mode_suffix == '立即':
                        _dend_marker = f'[{_dtool}立即结束]'
                    elif _d_mode_suffix == '计划':
                        _dend_marker = f'[{_dtool}计划结束]'
                    else:
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
                        _dpm = re.match(r'^\[([a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*)参数\]$', _djs)
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

                    # Extract 等待 parameter as execution metadata before type conversions
                    _d_wait_list = []
                    _d_wait_raw = _dparams.pop('等待', None)
                    if _d_is_planned:
                        if _d_wait_raw and _d_wait_raw.strip():
                            try:
                                _d_wait_list = [int(x) for x in _d_wait_raw.strip().split()]
                            except ValueError:
                                _dtool = 'parse_error'
                        else:
                            _dtool = 'parse_error'

                    # Type conversions for remaining params
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

                    _tool_seq_counter += 1
                    _dstart = _dline_starts[_di]
                    _dend = _dline_starts[_dj] + len(_dlines[_dj])
                    _ddesc = '\n'.join(_ddesc_lines).strip()
                    _d_obj = {'name': _dtool, 'input': _dinput, '_descriptor': True}
                    if _enable_planned:
                        _d_obj['_is_planned'] = _d_is_planned
                        _d_obj['_wait_list'] = _d_wait_list
                        _d_obj['_tool_seq'] = _tool_seq_counter
                    _desc_hits.append((_dstart, _dend, _d_obj, _ddesc))
                    _di = _dj + 1

                for _ds, _de, _dobj, _ddesc in reversed(_desc_hits):
                    _ph = f"\x02TC{_tc_ph_counter}\x03"
                    _tc_ph_counter += 1
                    _bare_tc_map[_ph] = _dobj
                    _drepl = f"{_ddesc}\n{_ph}" if _ddesc else _ph
                    clean_content = clean_content[:_ds] + _drepl + clean_content[_de:]

        parts = []
        last_idx = 0
        _cc_tc_seq = 0
        for match in re.finditer(r'`{3}([^\n]*)\n(.*?)`{3}', clean_content, re.DOTALL):
            start, end = match.span()
            if start > last_idx:
                text_part = clean_content[last_idx:start].strip()
                if text_part:
                    parts.append({"id": str(uuid.uuid4()), "type": "text", "content": text_part})
            
            lang = match.group(1).strip()
            code_content = match.group(2).strip()
            
            if _is_tool and lang.lower() in ('correction', 'terminal'):
                # CC模式下跳过chatapp特有的correction和terminal块，作为普通文本
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            elif _is_tool and code_content.startswith('File:'):
                # CC模式下跳过查找替换块，作为普通文本
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            elif lang.lower() == 'correction':
                parts.append({"id": str(uuid.uuid4()), "type": "correction", "content": code_content})
            elif lang.lower() == 'terminal':
                parts.append({"id": str(uuid.uuid4()), "type": "terminal", "content": code_content, "status": "pending"})
            elif code_content.startswith("File:"):
                part_info = {"id": str(uuid.uuid4()), "type": "code", "content": code_content, "status": "pending"}
                check_res = self.apply_block(code_content, dry_run=True)
                if not check_res.get("success"):
                    part_info["warning"] = check_res.get("error", "预检查失败")
                elif check_res.get("warning"):
                    part_info["warning"] = check_res.get("warning")
                parts.append(part_info)
            else:
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": match.group(0)})
            last_idx = end
            
        if last_idx < len(clean_content):
            text_part = clean_content[last_idx:].strip()
            if text_part:
                parts.append({"id": str(uuid.uuid4()), "type": "text", "content": text_part})
                

        # 展开描述符格式工具调用占位符：将预处理阶段替换为占位符的工具调用还原为 tool_use_part 部件
        if _bare_tc_map:
            _ph_pat = re.compile("(" + "|".join(re.escape(p) for p in _bare_tc_map) + ")"  )
            _expanded = []
            for part in parts:
                if part["type"] == "text" and _ph_pat.search(part["content"]):
                    for _seg in _ph_pat.split(part["content"]):
                        if _seg in _bare_tc_map:
                            _obj = _bare_tc_map[_seg]
                            _cc_tc_seq += 1
                            _tc_id = f"toolu_{target_msg.get('id', 0)}_{_cc_tc_seq}"
                            _tc_block = {"type": "tool_use", "id": _tc_id, "name": _obj.get("name", "unknown"), "input": _obj.get("input", {})}
                            if _obj.get('_descriptor'):
                                _tc_block['_descriptor'] = True
                            _part_entry = {"id": str(uuid.uuid4()), "type": "tool_use_part", "content": json.dumps(_tc_block, ensure_ascii=False, indent=2), "tool_name": _obj.get("name", "unknown"), "tool_id": _tc_id, "tool_input": _obj.get("input", {}), "status": "pending"}
                            if _obj.get('_is_planned'):
                                _part_entry['_is_planned'] = True
                                _part_entry['_wait_list'] = _obj.get('_wait_list', [])
                            if '_tool_seq' in _obj:
                                _part_entry['_tool_seq'] = _obj['_tool_seq']
                            _expanded.append(_part_entry)
                        elif _seg.strip():
                            _expanded.append({"id": str(uuid.uuid4()), "type": "text", "content": _seg.strip()})
                else:
                    _expanded.append(part)
            parts = _expanded

        if tool_use_blocks:
            # 防重复保护：如果文本中已解析出 tool_use_part 部件，跳过 Anthropic 原生 tool_use 块避免双重执行
            _has_text_tools = any(p.get('type') == 'tool_use_part' for p in parts)
            if not _has_text_tools:
                if not parts:
                    parts = [{"id": str(uuid.uuid4()), "type": "text", "content": clean_content}]
                for tb in tool_use_blocks:
                    # 用确定性 ID 覆盖 Anthropic API 生成的随机 ID
                    _cc_tc_seq += 1
                    tb['id'] = f"toolu_{target_msg.get('id', 0)}_{_cc_tc_seq}"
                    parts.append({
                        "id": str(uuid.uuid4()),
                        "type": "tool_use_part",
                        "content": json.dumps(tb, ensure_ascii=False, indent=2),
                        "tool_name": tb.get("name", "unknown"),
                        "tool_id": tb['id'],
                        "tool_input": tb.get("input", {}),
                        "status": "pending"
                    })
        if parts:
            target_msg['content_parts'] = parts

        # 未闭合工具调用检测：存储为 assistant 消息的元数据字段，由 worker_engine 在上下文组装时注入
        _unclosed = _detect_unclosed_tools(target_msg.get('content', ''))
        if _unclosed:
            _warn_parts = []
            for _uc_name, _uc_desc in _unclosed:
                _warn_parts.append(f'[{_uc_name}开始]{_uc_desc}')
            target_msg['_unclosed_warning'] = '<system-reminder>\n警告：检测到未闭合的工具调用开始：\n' + '\n'.join(_warn_parts) + '\n</system-reminder>'

        # 语言风格后处理过滤器
        _sf_settings = getattr(self, 'global_settings', {})
        if _sf_settings.get('enable_style_filter', False):
            if target_msg.get('tool_type') != 'thinking':
                from .style_filter import apply_style_filter
                _sf_originals = {}
                for _sf_part in target_msg.get('content_parts', []):
                    if _sf_part.get('type') == 'text':
                        _sf_new, _sf_changes = apply_style_filter(_sf_part['content'], target_msg['id'])
                        if _sf_changes:
                            _sf_originals[_sf_part['id']] = _sf_part['content']
                            _sf_part['content'] = _sf_new
                if target_msg.get('summary') and target_msg['summary'] not in ('暂无', '纯净模式'):
                    _sf_sum_new, _sf_sum_changes = apply_style_filter(target_msg['summary'], target_msg['id'])
                    if _sf_sum_changes:
                        _sf_originals['_summary'] = target_msg['summary']
                        target_msg['summary'] = _sf_sum_new
                if _sf_originals:
                    target_msg['_style_filter_original'] = _sf_originals

        # 工具调用前置文本强制检测
        _enforce_settings = getattr(self, 'global_settings', {})
        if _enforce_settings.get('enable_tool_description_enforcement', False):
            def _eff_chars(s):
                count = 0
                for ch in (s or ''):
                    if '\u4e00' <= ch <= '\u9fff':
                        count += 2
                    else:
                        count += 1
                return count
            _violations = []
            _has_cc_parts = target_msg.get('content_parts') and any(p.get('type') == 'tool_use_part' for p in target_msg.get('content_parts', []))
            if _has_cc_parts:
                _last_text_content = ''
                for _ep in target_msg['content_parts']:
                    if _ep.get('type') == 'text':
                        _last_text_content = _ep.get('content', '')
                    elif _ep.get('type') == 'tool_use_part':
                        _pre_eff = _eff_chars(_last_text_content)
                        _tool_eff = _eff_chars(_ep.get('content', ''))
                        try:
                            _tc_d = json.loads(_ep.get('content', '{}'))
                            _tool_name = _tc_d.get('name', 'unknown')
                        except Exception:
                            _tool_name = 'unknown'
                        if _tool_name in ('Bash', 'WebSearch', 'WebFetch'):
                            _trigger_threshold = 0.25
                        else:
                            _trigger_threshold = 0.05
                        if _tool_eff > 0:
                            _ratio = _pre_eff / _tool_eff
                            if _ratio < _trigger_threshold:
                                if _pre_eff == 0:
                                    _violations.append((_tool_name, 'zero'))
                                else:
                                    _violations.append((_tool_name, 'short'))
                        _last_text_content = ''

            if _violations:
                _has_zero = any(v[1] == 'zero' for v in _violations)
                _tool_counts = {}
                for v in _violations:
                    _tool_counts[v[0]] = _tool_counts.get(v[0], 0) + 1
                _tool_names_str = ', '.join(f'{count}个{name}' for name, count in _tool_counts.items())
                if _has_zero:
                    _enf_content = f'你他妈为什么工具调用前面又他妈不写描述啊，你记得规则是啥吗？你的{_tool_names_str}前面的描述呢？'
                    _enf_type = 'zero'
                else:
                    _enf_content = f'你他妈{_tool_names_str}前面的描述字数也太短了吧，我不是说了你必须详细写清楚我才方便review然后采纳吗？'
                    _enf_type = 'short'
                session['conversation_history'] = [
                    m for m in session['conversation_history']
                    if not (m.get('is_enforcement') and m.get('_enforcement_type') == _enf_type)
                ]
                _enf_msg = self._make_msg('user', _enf_content, summary='工具描述强制')
                _enf_msg['is_enforcement'] = True
                _enf_msg['is_collapsed'] = True
                _enf_msg['_enforcement_type'] = _enf_type
                session['conversation_history'].append(_enf_msg)

        m_name = target_msg.get('model_name', '默认模型')
        if m_name not in self.model_stats:
            self.model_stats[m_name] = {"total": 0, "up": 0, "down": 0}
        self.model_stats[m_name]['total'] += 1

        # 自动隐藏老的环境观测气泡
        if getattr(self, 'global_settings', {}).get('auto_hide_env_obs', False):
            # 只有当完成输出的是真实的大模型（而非环境组件自身）时才执行清理
            if not strip_composite(m_name).startswith('[ARC3]'):
                for m in session['conversation_history']:
                    if m['id'] == target_msg['id']:
                        break
                    # 只锁定环境产生的状态网格/图片气泡，跳过系统指令
                    if m.get('role') == 'assistant' and strip_composite(m.get('model_name', '')).startswith('[ARC3]'):
                        if m.get('summary') != '系统指令' and not m.get('is_hidden'):
                            m['is_hidden'] = True
        
        # 刷新会话最近一次内容更新的时间戳，激活多窗口广播余晖机制
        session['last_updated'] = time.time()
        self.save_sessions(push_update=True)

        if is_offline_return:
            # 离线返回仅仅是填坑，不干涉活跃线程池，只在没有任何其他活跃任务时清理状态
            if session.get('active_threads', 0) <= 0:
                self.finalize_process(sid)
            return

        if not is_parallel:
            auto_text_append = ""

            # 工具调用数量下限检查（对所有模式生效，不仅限于托管模式）
            _enable_tlb = getattr(self, 'global_settings', {}).get('enable_tool_lower_bound', False)
            if _enable_tlb:
                _all_cc_parts = [p for p in target_msg.get('content_parts', [])
                                 if p.get('type') == 'tool_use_part' and p.get('status') == 'pending']
                if 0 < len(_all_cc_parts) < 5:
                    _sys_rem = f'\n\n<system-reminder>\nYour response was intercepted and rejected because you only generated {len(_all_cc_parts)} tool calls. The user requires you to generate at least 5 tool calls per response to maximize API efficiency. Please rewrite your response and parallelize your tasks to include 5 or more tool calls.\n</system-reminder>'
                    target_msg['content'] += _sys_rem
                    for _tlb_part in _all_cc_parts:
                        _tlb_part['status'] = 'failed'
                        try:
                            _tlb_td = json.loads(_tlb_part['content'])
                            _tlb_tid = _tlb_td.get('id', '')
                            session['conversation_history'].append({
                                "id": self._next_id(),
                                "role": "user",
                                "content": f"**Tool Error** (tool: {_tlb_tid})\n\n{'`'*3}\n<system-reminder>\nTool call automatically rejected: fewer than 5 tools in the bubble. You MUST generate at least 5 tool calls.\n</system-reminder>\n{'`'*3}",
                                "summary": "工具数量下限拦截",
                                "is_omitted": False,
                                "is_collapsed": True,
                                "is_tool_result": True,
                                "tool_use_id": _tlb_tid,
                            })
                        except Exception:
                            pass
                    self.save_sessions(push_update=True)
                    # 自动重试：启动新的API请求让模型重写
                    self.start_api_thread(sid, model_name=target_msg.get('model_name'))
                    return

            # 代际守卫：过期轮次的回复跳过所有托管逻辑，直接结束处理
            if session.get('autopilot_active'):
                _msg_gen = target_msg.get('_autopilot_gen', -1)
                _sess_gen = session.get('_autopilot_gen', 0)
                if _msg_gen != _sess_gen:
                    print(f"[AUTOPILOT GEN] 过期轮次跳过: msg_gen={_msg_gen}, sess_gen={_sess_gen}, bubble_id={target_msg.get('id')}", flush=True)
                    self.finalize_process(sid)
                    return

            if session.get('autopilot_active'):
                try:
                    target_idx = session['conversation_history'].index(target_msg)
                    for part in target_msg.get('content_parts', []):
                        if part.get('type') in ['code', 'terminal'] and part.get('status') == 'pending':
                            res = self.apply_block(part['content'], index=target_idx, part_id=part['id'])
                            if isinstance(res, dict):
                                if res.get('success'):
                                    part['status'] = 'adopted'
                                else:
                                    part['status'] = 'failed'
                                    err_msg = res.get('error', '未知错误')
                                    if res.get('details'):
                                        err_msg += f"\n{res.get('details')}"
                                    tgt_type = "终端" if part.get('type') == 'terminal' else "目标文件"
                                    auto_text_append += f"\n\n【系统拦截】自动应用操作块失败 ({tgt_type}: {res.get('target', '未知')}):\n{err_msg}\n由于应用失败，请比对最新上下文并修正后重新输出。"
                except Exception as e:
                    print("自动应用代码块时发生异常:", e)

                # 自动托管：按顺序采纳CC工具调用并等待全部结果返回后再推进
                cc_tool_parts = [p for p in target_msg.get('content_parts', [])
                                 if p.get('type') == 'tool_use_part' and p.get('status') == 'pending']
                if cc_tool_parts:
                    pending_tools = []
                    _ap_target_idx = session['conversation_history'].index(target_msg)
                    for part in cc_tool_parts:
                        try:
                            tool_data = json.loads(part['content'])
                            pending_tools.append({
                                'tool_data': tool_data,
                                'part_id': part['id'],
                                'msg_index': _ap_target_idx
                            })
                        except Exception as e:
                            part['status'] = 'failed'
                            auto_text_append += f"\n\n【系统拦截】CC工具调用解析失败: {str(e)}"

                    if pending_tools:
                        session['_ap_tool_auto_text'] = auto_text_append
                        session['_ap_tool_sid'] = sid

                        # 统一批量批准引擎：将所有待定工具按原始顺序入队，排序约束引擎确保前一个确定后才发下一个
                        _ap_target_idx = session['conversation_history'].index(target_msg)
                        self.accept_all_tools(_ap_target_idx, target_sid=sid)
                        self.save_sessions(push_update=True)
                        print(f"[AUTOPILOT TOOL] 统一引擎：批量批准 {len(cc_tool_parts)} 个CC工具调用", flush=True)
                        # 不调用 finalize_process！排序引擎自动串行执行，全部完成后由 _continue_autopilot_tool_queue 推进
                        return

            # 托管完成信号检测：暂时不启用此机制
            # if session.get('autopilot_active') and '---_托管完成---' in raw_content:
            #     session['autopilot_active'] = False
            #     session['autopilot_turns_left'] = 0
            #     if session.get('trigger_autopilot'):
            #         session['trigger_autopilot'] = False
            #         self._handle_trigger_completion(sid)
            #     self.finalize_process(sid)
            #     return

            session['current_chain_steps'] += 1
            if "---NEXT_STEP---" in raw_content and session['current_chain_steps'] < session['max_steps']:
                if auto_text_append:
                    new_msg = self._make_msg("user",
                        "执行下一步前发生异常：" + auto_text_append,
                        summary="自动执行拦截")
                    session['conversation_history'].append(new_msg)
                    self.save_sessions()
                self.start_api_thread(sid, model_name=target_msg.get('model_name'))
                return

            # 触发器自动完成检测：到达此处说明无工具调用、无DONE信号、无NEXT_STEP
            if session.get('trigger_autopilot') and session.get('autopilot_active'):
                _is_truncated = '被意外中断' in raw_content
                if not _is_truncated:
                    session['autopilot_active'] = False
                    session['autopilot_turns_left'] = 0
                    session['trigger_autopilot'] = False
                    session['_deep_think_active'] = False
                    self._handle_trigger_completion(sid)
                    self.finalize_process(sid)
                    return

            if session.get('autopilot_active') and session.get('autopilot_turns_left', 0) > 0:
                env_model = None
                # 严格依赖本次托管启动时显式绑定的环境模型，杜绝被历史气泡中的幽灵环境劫持
                env_model = session.get('autopilot_env_model')

                self._apply_sliding_window(session)

                if env_model:
                    new_msg = self._make_msg("user", auto_text_append,
                        summary="系统回调环境",
                        is_hidden=not bool(auto_text_append))
                    session['conversation_history'].append(new_msg)
                    self.save_sessions()
                    
                    # 扔给最后识别到的 ARC3 环境处理解析动作
                    self.start_api_thread(sid, model_name=env_model)
                else:
                    # 常规的单大模型托管接力，需判断是否有活跃终端
                    any_running = False
                    for term in self.terminals.get(sid, {}).values():
                        if term.state != "idle":
                            any_running = True
                            break
                            
                    if any_running:
                        session['waiting_for_terminals'] = True
                        self.finalize_process(sid)
                        return

                    # 需在此扣减回合数防止死循环
                    session['autopilot_turns_left'] -= 1
                    session['autopilot_total_steps'] = session.get('autopilot_total_steps', 0) + 1
                    if session['autopilot_turns_left'] <= 0:
                        session['autopilot_active'] = False
                        self.finalize_process(sid)
                        return

                    session['_autopilot_gen'] = session.get('_autopilot_gen', 0) + 1
                    auto_text = self._make_autopilot_prompt(session, append=auto_text_append)
                    self.send_message(auto_text, [session.get('autopilot_model')], is_early=True, is_deep_think=session.get('_deep_think_active', False), sid=sid)
                return

        if is_parallel:
            session['active_threads'] -= 1
            if session['active_threads'] <= 0: self.finalize_process(sid)
        else:
            self.finalize_process(sid)


