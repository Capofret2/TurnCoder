"""Api mixin: System prompt, code monitoring, and context assembly."""
import json
import os
import re


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 随仓库分发的提示词默认值。data/ 下的同名文件覆写它。
DEFAULT_PROMPT_DIR = os.path.join(_REPO_ROOT, "prompts")


class ContextMixin:
    """Api mixin: System prompt, code monitoring, and context assembly."""

    def _resolve_prompt_path(self, filename: str) -> str:
        """定位一份提示词：data/ 下的用户覆写优先于 prompts/ 下的追踪默认值。

        每次调用都做一次存在性检查，而不是启动时解析一次并缓存路径——这样新建
        覆写文件无需重启即可生效，与下方基于 mtime 的热更新是同一套取向。

        默认值永远不会被复制进 data/。一旦复制，上面那个存在性检查从此永远成立，
        prompts/ 里的任何后续改动对「启动过一次」的安装就彻底不可见了，等于这份
        默认值只在全新安装的第一秒有意义。
        """
        override = os.path.join(self.data_dir, filename)
        if os.path.exists(override):
            return override
        return os.path.join(DEFAULT_PROMPT_DIR, filename)

    def get_system_prompt(self) -> str:
        def _decode_prompt(text):
            return re.sub(r'\{([\'"])(.*?)\1\*(\d+)\}', lambda m: m.group(2) * int(m.group(3)), text)

        path = self._resolve_prompt_path("system_prompt.txt")
        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            # 两处都不存在。以前这里会静默写出一个空文件并返回空提示词——
            # 模型在毫无协议约束的情况下工作，控制台一句话都没有。警告只发一次，
            # 因为本方法在每次组装上下文时都会被调用。
            if not getattr(self, "_prompt_missing_warned", False):
                self._prompt_missing_warned = True
                print(f"[PROMPT] 系统提示词缺失：{os.path.join(self.data_dir, 'system_prompt.txt')} "
                      f"与 {path} 均不存在，将以空提示词运行", flush=True)
            return ""

        # 缓存键是「路径 + mtime」而不只是 mtime：用户新建覆写文件时路径变了，
        # 而新文件的 mtime 完全可能早于当前缓存值（例如从别处复制过来的），
        # 只比 mtime 会漏掉这次切换。
        if path != getattr(self, "_prompt_src", None) or current_mtime > self.prompt_mtime:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.cached_prompt = f.read()
                self._prompt_src = path
                self.prompt_mtime = current_mtime
            except OSError as e:
                print(f"读取系统提示词失败 ({path}): {e}", flush=True)

        return _decode_prompt(self.cached_prompt)


    def _load_deep_think_prompt(self, level: int = 2) -> str:
        """按档位加载深度思考提示词。level >= 2 用 deep_think_prompt.txt，
        level 1 用 deep_think_prompt_medium.txt。解析规则与系统提示词一致：
        data/ 覆写优先于 prompts/ 默认值。

        缓存元组是 (path, mtime, content)。带上 path 的理由同 get_system_prompt：
        新建覆写文件会改变路径，而其 mtime 未必大于缓存值。
        """
        if not hasattr(self, '_dt_cache'):
            self._dt_cache = {}
        _fname = "deep_think_prompt.txt" if level >= 2 else "deep_think_prompt_medium.txt"
        _dt_path = self._resolve_prompt_path(_fname)
        try:
            _dt_mtime = os.path.getmtime(_dt_path)
        except OSError:
            return ""
        _cached = self._dt_cache.get(level)
        if _cached and _cached[0] == _dt_path and _dt_mtime <= _cached[1]:
            return _cached[2]
        try:
            with open(_dt_path, "r", encoding="utf-8") as f:
                _content = f.read().strip()
        except OSError:
            return ""
        self._dt_cache[level] = (_dt_path, _dt_mtime, _content)
        return _content


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
                # p 是本函数自己构造的树片段，上游已 replace('\\', '/') 归一化，所以
                # 紧邻的 count('/') 是对的。这里用 basename 而不是 split('/')[-1]：两者
                # 在归一化路径上等价，而选前者是为了让 tests 里那条仓库级守卫保持只有
                # 一个例外（URL）——带逐点例外清单的检查很快就没人敢信。
                depth = p.count('/')
                indent = "  " * depth
                name = os.path.basename(p)
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
                    # 同上：p 已归一化为正斜杠，basename 与 split('/')[-1] 等价。
                    depth = p.count('/')
                    indent = "  " * depth
                    name = os.path.basename(p)
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


