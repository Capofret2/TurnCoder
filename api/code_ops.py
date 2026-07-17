"""Api mixin: Code block apply - file creation, find-replace, terminal ops."""
import json
import os
import re

from .terminal import TerminalProcess


def _make_block_error(code, short, details=None, context=None, target=None):
    """Construct a standardized error response for code block operations."""
    payload = {"success": False, "error_code": code, "error": short}
    if details: payload["details"] = details
    if context: payload["context"] = context
    if target: payload["target"] = target
    return payload



class CodeOpsMixin:
    """Api mixin: Code block apply - file creation, find-replace, terminal ops."""

    def apply_block(self, block_text, index=None, part_id=None, directory=".", dry_run=False):
        make_error = _make_block_error  # Module-level function, promoted from nested def

        lines = block_text.strip().split('\n')
        if not lines:
            return make_error("empty_block", "代码块为空")

        # CC注入模式安全保护：禁止chatapp自带的代码块和终端操作
        _cc_guard = getattr(self, 'global_settings', {})
        if _cc_guard.get('enable_tool_inject', False) and not dry_run:
            return make_error("cc_mode_blocked", "CC注入模式已启用，chatapp自带的代码块和终端操作已被禁用，请使用CC工具进行操作")

        #幂等性保护：阻止对已处理的代码块重复应用
        if index is not None and part_id is not None and not dry_run:
            _session = self._session
            if _session and 0 <= index < len(_session.get('conversation_history', [])):
                _msg = _session['conversation_history'][index]
                for _p in _msg.get('content_parts', []):
                    if _p.get('id') == part_id and _p.get('status') not in (None, 'pending'):
                        return make_error("already_processed", f"该代码块已被处理（当前状态: {_p.get('status')}），拒绝重复操作")

        first_line = lines[0].strip()
        
        if first_line.startswith("Action:"):
            return self._handle_terminal_action(lines, first_line, dry_run)

        if not first_line.startswith("File:"):
            return make_error("missing_file_header", "缺失 'File:' 或 'Action:' 前缀")

        rel_path = first_line[5:].strip().replace('`', '')
        if not rel_path:
            return make_error("empty_file_path", "未指定目标文件路径")

        full_path = os.path.join(directory, rel_path)
        content_lines = lines[1:]

        if rel_path == "@code_config":
            return self._handle_config_update(content_lines, rel_path, dry_run)

        if "+" * 4 in content_lines:
            return self._handle_file_create(content_lines, rel_path, full_path, dry_run)
        else:
            return self._handle_find_replace(content_lines, rel_path, full_path, index, part_id, dry_run)

    def _handle_terminal_action(self, lines, first_line, dry_run):
        """Extracted: Handle terminal create/delete/interrupt/execute actions."""
        action = first_line[7:].strip().lower()
        term_line = next((l for l in lines if l.startswith("Terminal:")), None)
        if not term_line:
            return _make_block_error("missing_terminal", "缺失 Terminal 名称指定")
        term_name = term_line[9:].strip()
        if dry_run:
            return {"success": True, "applied": "terminal_action", "target": term_name}
        sid = self._active_sid
        session = self.sessions.get(sid)
        self.terminals.setdefault(sid, {})
        if action == 'create':
            if term_name not in self.terminals[sid]:
                tp = TerminalProcess(term_name, sid, self)
                msg_id = self._next_id()
                tp.msg_id = msg_id
                new_msg = self._make_msg("assistant", f"[终端 {term_name} 已创建]\n",
                    summary=f"终端: {term_name}", msg_id=msg_id,
                    term_content=f"[终端 {term_name} 已创建]\n",
                    is_terminal=True, term_state="idle")
                session['conversation_history'].append(new_msg)
                self.terminals[sid][term_name] = tp
                self.save_sessions(push_update=True)
        elif action == 'delete':
            tp = self.terminals[sid].get(term_name)
            if tp:
                tp.terminate()
                del self.terminals[sid][term_name]
        elif action == 'interrupt':
            tp = self.terminals[sid].get(term_name)
            if tp:
                tp.interrupt()
        elif action == 'execute':
            tp = self.terminals[sid].get(term_name)
            if not tp:
                return _make_block_error("terminal_not_found", f"终端 {term_name} 不存在，请先创建")
            try:
                cmd_start = lines.index("Command:") + 1
                cmd_str = "\n".join(lines[cmd_start:]).strip()
                tp.execute(cmd_str)
            except ValueError:
                return _make_block_error("missing_command", "缺失 Command: 关键字")
        return {"success": True, "applied": "terminal_action", "target": term_name}

    def _handle_config_update(self, content_lines, rel_path, dry_run):
        """Extracted: Handle @code_config update blocks."""
        if "+" * 4 not in content_lines:
            return _make_block_error("invalid_config_format", f"更新监听配置必须使用 {'+' * 4} 初始完整内容格式包裹 JSON")
        try:
            start_idx = content_lines.index("+" * 4)
            end_idx = len(content_lines) - 1 - content_lines[::-1].index("+" * 4)
            if start_idx >= end_idx:
                return _make_block_error("invalid_create_block", f"配置块格式错误，缺少闭合 {'+' * 4}")
            config_json_str = "\n".join(content_lines[start_idx + 1:end_idx]).strip()
            if dry_run:
                json.loads(config_json_str)
                return {"success": True, "applied": "config_update", "target": rel_path, "bytes": len(config_json_str)}
            new_config = json.loads(config_json_str)
            paths = new_config.get('paths', getattr(self, 'code_config', {}).get('paths', ['.']))
            extensions = new_config.get('extensions', ['.py', '.html', '.json', '.md', '.ipynb', '.yaml', '.yml', '.tex', '.bib', '.txt'])
            target_files = new_config.get('selected_files', [])
            scan_res = self.scan_files(paths, extensions)
            new_config['paths'] = paths
            new_config['extensions'] = extensions
            filtered_abs, unmatched = [], []
            if target_files:
                norm_targets = set()
                for tf in target_files:
                    tf = tf.replace('\\', '/')
                    if tf.startswith('./'): tf = tf[2:]
                    norm_targets.add(tf)
                matched_inputs = set()
                for f in scan_res.get('files', []):
                    full_rel = f.get('full_rel', '')
                    if full_rel in norm_targets:
                        filtered_abs.append(f['abs_path'])
                        matched_inputs.add(full_rel)
                    elif f['abs_path'] in target_files:
                        filtered_abs.append(f['abs_path'])
                        matched_inputs.add(f['abs_path'])
                for tf in target_files:
                    tf_norm = tf.replace('\\', '/').lstrip('./')
                    if tf_norm not in matched_inputs:
                        unmatched.append(tf)
            if unmatched:
                return _make_block_error("config_match_failed", "监听配置包含未匹配文件，已拒绝应用",
                    details="未找到以下路径:\n" + "\n".join(unmatched), target=rel_path)
            new_config['selected_files'] = filtered_abs
            self.update_code_config(new_config, self._active_sid)
            return {"success": True, "applied": "config_update", "target": rel_path, "bytes": len(config_json_str)}
        except json.JSONDecodeError as e:
            return _make_block_error("invalid_config_json", "解析配置 JSON 失败", details=str(e), target=rel_path)
        except Exception as e:
            return _make_block_error("config_update_exception", "更新监听配置时发生异常", details=str(e), target=rel_path)

    def _handle_file_create(self, content_lines, rel_path, full_path, dry_run):
        """Extracted: Handle file creation blocks with ++++ delimiters."""
        try:
            start_idx = content_lines.index("+" * 4)
            end_idx = len(content_lines) - 1 - content_lines[::-1].index("+" * 4)
            if start_idx >= end_idx:
                return _make_block_error("invalid_create_block", f"创建文件格式错误，缺少闭合 {'+' * 4}", target=rel_path)
            file_content = "\n".join(content_lines[start_idx + 1:end_idx]).strip('\n')
            if not file_content.strip():
                return _make_block_error("empty_new_file", f"不得新建空文件，{'+' * 4} 内部必须有内容", target=rel_path)
            if dry_run:
                res = {"success": True, "applied": "create_file", "target": rel_path, "bytes": len(file_content)}
                if rel_path.endswith('.py'):
                    try:
                        compile(file_content, '<string>', 'exec')
                    except Exception as e:
                        res["warning"] = f"预检发现异常 ({type(e).__name__}): {str(e)}"
                return res
            dir_name = os.path.dirname(full_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(file_content)
            session = self.sessions.get(self._active_sid)
            if session:
                cfg = session.get('code_config', {})
                selected = cfg.setdefault('selected_files', [])
                abs_p = os.path.abspath(full_path).replace('\\', '/')
                if abs_p not in selected:
                    selected.append(abs_p)
                    self.update_code_config(cfg, self._active_sid)
            res = {"success": True, "applied": "create_file", "target": rel_path, "bytes": len(file_content)}
            if full_path.endswith('.py'):
                try:
                    compile(file_content, '<string>', 'exec')
                except Exception as e:
                    res["warning"] = f"语法检查异常 ({type(e).__name__}): {str(e)}"
            return res
        except Exception as e:
            return _make_block_error("create_exception", "创建文件时发生异常", details=str(e), target=rel_path)

    def _handle_find_replace(self, content_lines, rel_path, full_path, index, part_id, dry_run):
        """Extracted: Handle find-replace blocks with <<<<, ====, >>>> delimiters."""
        try:
            stripped_lines = [line.strip() for line in content_lines]
            start_marker_idx = stripped_lines.index("<" * 4)
            sep_marker_idx = stripped_lines.index("=" * 4)
            end_marker_idx = stripped_lines.index(">" * 4)
            if not (start_marker_idx < sep_marker_idx < end_marker_idx):
                return _make_block_error("invalid_marker_order",
                    f"查找替换格式的 {'<' * 4}, {'=' * 4}, {'>' * 4} 标记顺序错误", target=rel_path)
            search_lines = content_lines[start_marker_idx + 1: sep_marker_idx]
            replace_lines = content_lines[sep_marker_idx + 1: end_marker_idx]
        except ValueError:
            return _make_block_error("invalid_replace_block",
                f"查找替换格式不完整，缺失 {'<' * 4}, {'=' * 4} 或 {'>' * 4} 分割符", target=rel_path)
        search_text = "\n".join(search_lines)
        replace_text = "\n".join(replace_lines)
        if not os.path.exists(full_path):
            return _make_block_error("missing_file", f"目标文件不存在: {rel_path}", target=rel_path)
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('\r\n', '\n')
            if part_id:
                session = self.sessions.get(self._active_sid)
                if session:
                    session.setdefault('code_backups', {})
                    session['code_backups'][part_id] = {"path": full_path, "content": content}
                    self.save_sessions()
            if full_path.endswith('.ipynb'):
                try:
                    nb = json.loads(content)
                    for cell in nb.get('cells', []):
                        if 'outputs' in cell: cell['outputs'] = []
                        if 'execution_count' in cell: cell['execution_count'] = None
                    content = json.dumps(nb, ensure_ascii=False, indent=1)
                except Exception:
                    pass
            search_clean = search_text.strip('\n')
            replace_clean = replace_text.strip('\n').replace('\xa0', ' ').replace('\u200b', '')
            if full_path.endswith('.ipynb'):
                def to_ipynb_json_lines(text):
                    if not text: return ""
                    lines = text.split('\n')
                    has_trailing = len(lines) > 1 and lines[-1] == ""
                    if has_trailing: lines = lines[:-1]
                    res = []
                    for i, line in enumerate(lines):
                        if i < len(lines) - 1 or has_trailing:
                            res.append(json.dumps(line + '\n', ensure_ascii=False))
                        else:
                            res.append(json.dumps(line, ensure_ascii=False)[:-1])
                    return ',\n    '.join(res)
                search_clean = to_ipynb_json_lines(search_clean)
                replace_clean = to_ipynb_json_lines(replace_clean)
            if not search_clean:
                return _make_block_error("empty_search", "要查找的内容为空", target=rel_path)
            lines_to_search = search_clean.split('\n')
            escaped_lines = []
            for line in lines_to_search:
                line_stripped = line.strip()
                if not line_stripped:
                    escaped_lines.append(r'[^\S\n]*')
                else:
                    parts = re.split(r'[^\S\n]+', line_stripped)
                    escaped_parts = [re.escape(p) for p in parts]
                    line_pattern = r'[^\S\n]+'.join(escaped_parts)
                    escaped_lines.append(r'[^\S\n]*' + line_pattern + r'[^\S\n]*')
            pattern_str = r'\n'.join(escaped_lines)
            pattern = re.compile(pattern_str)
            matches = list(pattern.finditer(content))
            if len(matches) == 0:
                snippet = search_clean if len(search_clean) <= 200 else search_clean[:200] + "...(截断)"
                return _make_block_error("no_match", "要查找的内容未在原文中找到", details=snippet, target=rel_path)
            elif len(matches) > 1:
                return _make_block_error("multiple_matches",
                    "要查找的内容在原文中存在多个匹配，为安全已终止替换。",
                    details=f"匹配数量: {len(matches)}", target=rel_path)
            match = matches[0]
            start, end = match.span()
            new_content = content[:start] + replace_clean + content[end:]
            if dry_run:
                replaced_lines = len(search_clean.split('\n'))
                res = {"success": True, "applied": "replace", "target": rel_path, "replaced_lines": replaced_lines}
                if rel_path.endswith('.py'):
                    try:
                        compile(new_content, '<string>', 'exec')
                    except Exception as e:
                        res["warning"] = f"预检发现异常 ({type(e).__name__}): {str(e)}"
                return res
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            replaced_lines = len(search_clean.split('\n'))
            res = {"success": True, "applied": "replace", "target": rel_path, "replaced_lines": replaced_lines}
            if full_path.endswith('.py'):
                try:
                    compile(new_content, '<string>', 'exec')
                except Exception as e:
                    res["warning"] = f"语法检查异常 ({type(e).__name__}): {str(e)}"
            return res
        except Exception as e:
            import traceback
            return _make_block_error("replace_exception", "处理查找替换时出现异常",
                details=f"[{type(e).__name__}] {str(e)}\n{traceback.format_exc()}", target=rel_path)



