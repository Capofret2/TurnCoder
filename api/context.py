"""Api mixin: System prompt, code monitoring, and context assembly."""
import json
import os
import re



class ContextMixin:
    """Api mixin: System prompt, code monitoring, and context assembly."""

    def get_system_prompt(self):
        import re
        def _decode_prompt(text):
            return re.sub(r'\{([\'"])(.*?)\1\*(\d+)\}', lambda m: m.group(2) * int(m.group(3)), text)

        # 检查文件是否存在，不存在则将 app.py 中的默认配置写入文件
        if not os.path.exists(self.prompt_file_path):
            default_prompt = self.config.get("SYSTEM_PROMPT", "")
            try:
                with open(self.prompt_file_path, "w", encoding="utf-8") as f:
                    f.write(default_prompt)
                self.cached_prompt = default_prompt
                self.prompt_mtime = os.path.getmtime(self.prompt_file_path)
            except Exception as e:
                print(f"写入初始系统提示词失败: {e}")
                return _decode_prompt(default_prompt)
        
        # 校验最后修改时间，实现低开销热更新
        try:
            current_mtime = os.path.getmtime(self.prompt_file_path)
            if current_mtime > self.prompt_mtime:
                with open(self.prompt_file_path, "r", encoding="utf-8") as f:
                    self.cached_prompt = f.read()
                self.prompt_mtime = current_mtime
        except Exception as e:
            print(f"读取系统提示词失败: {e}")
        
        return _decode_prompt(self.cached_prompt)


    def _load_deep_think_prompt(self, level=2):
        """Load deep think prompt by level from file. Level 2=data/deep_think_prompt.txt, Level 1=data/deep_think_prompt_medium.txt."""
        if not hasattr(self, '_dt_cache'):
            self._dt_cache = {}
        _fname = "deep_think_prompt.txt" if level >= 2 else "deep_think_prompt_medium.txt"
        _dt_path = os.path.join(self.data_dir, _fname)
        if not os.path.exists(_dt_path):
            return ""
        try:
            _dt_mtime = os.path.getmtime(_dt_path)
            _cached = self._dt_cache.get(level)
            if not _cached or _dt_mtime > _cached[0]:
                with open(_dt_path, "r", encoding="utf-8") as f:
                    _content = f.read().strip()
                self._dt_cache[level] = (_dt_mtime, _content)
                return _content
            return _cached[1]
        except Exception:
            return ""


    def scan_files(self, paths, extensions):
        ext_tuple = tuple(extensions) if extensions else ()
        results = []
        for base_path in paths:
            base_path = base_path.strip()
            if not os.path.exists(base_path): continue
            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', 'venv', '.idea']]
                for file in files:
                    if file.endswith(ext_tuple):
                        filepath = os.path.join(root, file)
                        abs_path = os.path.abspath(filepath).replace('\\', '/')
                        rel_path = os.path.relpath(filepath, base_path)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                token_k = len(f.read()) / 3000
                            display_str = f"[{base_path}] {rel_path} ({token_k:.2f}k)"
                        except Exception:
                            display_str = f"[{base_path}] {rel_path} (?k)"
                        full_rel = os.path.normpath(os.path.join(base_path, rel_path)).replace('\\', '/')
                        results.append({"abs_path": abs_path, "display": display_str, "rel_path": rel_path.replace('\\', '/'), "full_rel": full_rel})
        return {"files": results}


    def get_code_context(self, sid=None):
        target_sid = sid or self._active_sid
        session = self.sessions.get(target_sid)
        if not session:
            return ""
        code_cfg = session.get('code_config') or {}
        paths = code_cfg.get('paths', [])
        extensions = code_cfg.get('extensions', [])
        selected_files = code_cfg.get('selected_files', [])
        if not paths:
            return ""

        ext_tuple = tuple(extensions) if extensions else ()
        all_code = ""
        tree_str = ""
        
        limit_k = code_cfg.get('tree_limit_k', 10)
        limit_chars = limit_k * 1000

        full_tree_str = ""
        use_full_tree = True
        for base_path in paths:
            base_path = base_path.strip()
            if not os.path.exists(base_path): continue
            full_tree_str += f"[{base_path}]\n"
            all_paths_dict = {}
            file_count = 0
            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', 'venv', '.idea']]
                for file in files:
                    file_count += 1
                    filepath = os.path.join(root, file)
                    rel_p = os.path.relpath(filepath, base_path).replace('\\', '/')
                    
                    token_str = ""
                    if not ext_tuple or file.endswith(ext_tuple):
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                token_k = len(f.read()) / 3000
                            token_str = f" ({token_k:.2f}k)"
                        except Exception:
                            token_str = " (?k)"
                    all_paths_dict[rel_p] = token_str
                    
                if file_count > 3000:
                    break
                    
            if file_count > 3000:
                use_full_tree = False
                break
            
            paths_set_with_dirs = set()
            for p in all_paths_dict.keys():
                parts = p.split('/')
                for i in range(1, len(parts) + 1):
                    paths_set_with_dirs.add('/'.join(parts[:i]))
            
            for p in sorted(list(paths_set_with_dirs)):
                depth = p.count('/')
                indent = "  " * depth
                name = p.split('/')[-1]
                is_file = p in all_paths_dict
                if is_file:
                    full_tree_str += f"{indent}|-- {name}{all_paths_dict[p]}\n"
                else:
                    full_tree_str += f"{indent}|-- {name}/\n"
            full_tree_str += "\n"

        if use_full_tree and len(full_tree_str) <= limit_chars:
            tree_str = full_tree_str
        else:
            use_full_tree = False
            if not selected_files:
                tree_str = "（由于目录结构过大超过设定阈值，全量目录树已被折叠。请在监听配置中选中部分文件以展示局部树结构，或调大全量树回退阈值。）\n\n"

        for base_path in paths:
            base_path = base_path.strip()
            if not os.path.exists(base_path): continue
            valid_files = []
            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', 'venv', '.idea']]
                for file in files:
                    if file.endswith(ext_tuple):
                        filepath = os.path.join(root, file)
                        abs_path = os.path.abspath(filepath).replace('\\', '/')
                        if abs_path in selected_files:
                            valid_files.append((root, file, filepath, os.path.relpath(abs_path).replace('\\', '/')))

            if not valid_files: continue

            if not use_full_tree:
                tree_str += f"[{base_path}]\n"
                paths_set = set()
                valid_file_dict = {}
                for root_path, filename, filepath, rel_p in valid_files:
                    vp = rel_p.replace('\\', '/')
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            token_k = len(f.read()) / 3000
                        valid_file_dict[vp] = f" ({token_k:.2f}k)"
                    except Exception:
                        valid_file_dict[vp] = " (?k)"

                for vp in valid_file_dict.keys():
                    parts = vp.split('/')
                    for i in range(1, len(parts) + 1):
                        paths_set.add('/'.join(parts[:i]))

                for p in sorted(list(paths_set)):
                    depth = p.count('/')
                    indent = "  " * depth
                    name = p.split('/')[-1]
                    is_file = p in valid_file_dict
                    if is_file:
                        tree_str += f"{indent}|-- {name}{valid_file_dict[p]}\n"
                    else:
                        tree_str += f"{indent}|-- {name}/\n"
                tree_str += "\n"

            for vf in valid_files:
                try:
                    with open(vf[2], 'r', encoding='utf-8') as f:
                        content = f.read()
                    if vf[2].endswith('.ipynb'):
                        try:
                            nb = json.loads(content)
                            clean_text = []
                            for cell in nb.get('cells', []):
                                if cell.get('cell_type') in ['code', 'markdown']:
                                    source = cell.get('source', [])
                                    if isinstance(source, list):
                                        source = "".join(source)
                                    if source.strip():
                                        clean_text.append(source)
                            content = "\n\n".join(clean_text)
                        except Exception:
                            pass
                    all_code += f"--- {vf[3]} ---\n{content}\n\n"
                except Exception as e:
                    all_code += f"--- {vf[3]} (读取失败: {str(e)}) ---\n\n"

        res = ""
        if tree_str: res += "=== 目录树 ===\n" + tree_str
        if all_code: res += "=== 代码内容 ===\n" + all_code
        return res.strip()


    def update_code_config(self, config, sid=None):
        target_sid = sid or self._active_sid
        if not target_sid or target_sid not in self.sessions:
            return
        cfg = config or {}
        self.sessions[target_sid]['code_config'] = cfg
        if target_sid == self._active_sid:
            self.code_config = cfg
        self.save_sessions(push_update=False)


