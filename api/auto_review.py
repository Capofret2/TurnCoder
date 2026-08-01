"""Automatic paper review pipeline.

Reads papers from 自动审稿工作区/, pairs user's paper with reference papers,
makes API calls for review and planning, saves results to numbered directories.
All prompts are loaded from files (not hardcoded).
All request payloads are saved for debugging.
"""
import json
import os
import random
import re
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .provider_routes import route_provider

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.path.join(BASE_DIR, '自动审稿工作区')
PROMPTS_DIR = os.path.join(WORKSPACE_DIR, 'prompts')

# Per-provider concurrency limiting
_provider_semaphores = {}
_provider_sem_lock = threading.Lock()

def _get_provider_sem(provider_name, max_concurrent=5):
    """Get or create a semaphore for the given provider (max max_concurrent concurrent requests)."""
    with _provider_sem_lock:
        if provider_name not in _provider_semaphores:
            _provider_semaphores[provider_name] = threading.Semaphore(max_concurrent)
        return _provider_semaphores[provider_name]


def _resolve_provider(model_name, provider_name=None):
    """Resolve a model to (api_url, api_key, clean_model_name).

    If provider_name is given, look up the specific provider in providers.json
    by its 'name' field to avoid model name conflicts across providers.
    Otherwise fall back to route_provider (which may match the wrong provider
    if the model name exists in multiple providers).
    """
    if provider_name:
        providers_path = os.path.join(BASE_DIR, 'providers.json')
        if os.path.exists(providers_path):
            with open(providers_path, 'r', encoding='utf-8') as f:
                providers = json.load(f)
            for prov in providers:
                if prov.get('name') == provider_name:
                    api_url = prov.get('api_url', '')
                    api_key = prov.get('api_key', '')
                    strip = prov.get('strip_prefix', True)
                    prefixes = prov.get('prefixes', [])
                    clean = model_name
                    if strip:
                        for prefix in prefixes:
                            if model_name.startswith(prefix) and prefix:
                                clean = model_name[len(prefix):]
                                break
                    return api_url, api_key, clean
        # Provider not found, fall through to route_provider
        print(f'[AUTO-REVIEW] WARNING: provider "{provider_name}" not found in providers.json, using route_provider fallback', flush=True)
    return route_provider(model_name, {})


def _load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _save_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _get_next_run_number(ws_dir=None):
    """Find the next sequential run number from existing第N次审稿 directories."""
    _ws = ws_dir or WORKSPACE_DIR
    if not os.path.exists(_ws):
        return 1
    pattern = re.compile(r'^第(\d+)次审稿$')
    max_num = 0
    for name in os.listdir(_ws):
        if os.path.isdir(os.path.join(_ws, name)):
            m = pattern.match(name)
            if m:
                max_num = max(max_num, int(m.group(1)))
    return max_num + 1


def _collect_level_history(ref_file, review_model, current_run_num, ws_dir=None):
    """Collect historical workspace snapshots and review results for a specific level.

    A level = (reference_file, review_model) combination.
    Scans all previous 第N次审稿 directories for matching review results.
    Returns chronologically ordered list of dicts.
    """
    _ws = ws_dir or WORKSPACE_DIR
    history = []
    for n in range(1, current_run_num):
        review_dir = os.path.join(_ws, f'第{n}次审稿')
        if not os.path.isdir(review_dir):
            continue
        ws_path = os.path.join(review_dir, 'workspace_snapshot.txt')
        if os.path.exists(ws_path):
            ws_snap = _load_text(ws_path)
        else:
            ws_snap = f'（第{n}轮工作区快照不可用——该轮运行时尚未实现快照保存功能）'
        # Find matching review result for this level
        for fname in sorted(os.listdir(review_dir)):
            if not fname.endswith('_result.json') or fname.startswith('workspace'):
                continue
            try:
                result = _load_json(os.path.join(review_dir, fname))
                if result.get('reference_file') == ref_file and result.get('model') == review_model:
                    history.append({
                        'run_num': n,
                        'workspace': ws_snap,
                        'review_content': result.get('content', ''),
                        'review_thinking': result.get('thinking', ''),
                        'my_position': result.get('my_position', '?'),
                    })
                    break
            except Exception:
                pass
    return history


def _parse_judgment(content):
    """Parse structured judgment tag from review content.
    Looks for [最终判断开始]X[最终判断结束] where X is A or B.
    """
    m = re.search(r'\[最终判断开始\]\s*([AB])\s*\[最终判断结束\]', content)
    return m.group(1) if m else None


def _level_key(ref_file, review_model):
    return f"{ref_file}|{review_model}"


def _compute_win_rate(ref_file, review_model, current_run_num, window_size, ws_dir=None):
    """Compute win rate by scanning previous review directories' result files.

    No external state file needed — all data is in 第N次审稿/*_result.json.
    Scans backwards and stops after collecting enough entries.
    Returns (win_rate, count).
    """
    _ws = ws_dir or WORKSPACE_DIR
    entries = []  # (run_num, won)
    for n in range(current_run_num - 1, 0, -1):
        if len(entries) >= window_size:
            break
        review_dir = os.path.join(_ws, f'第{n}次审稿')
        if not os.path.isdir(review_dir):
            continue
        for fname in sorted(os.listdir(review_dir)):
            if not fname.endswith('_result.json') or fname.startswith(('workspace', 'agg', 'run_meta')):
                continue
            try:
                result = _load_json(os.path.join(review_dir, fname))
                if (result.get('reference_file') == ref_file
                        and result.get('model') == review_model
                        and result.get('we_won') is not None
                        and not result.get('skipped')):
                    entries.append(result['we_won'])
                    break
            except Exception:
                pass
    if not entries:
        return 0.0, 0
    wins = sum(1 for w in entries if w)
    return wins / len(entries), len(entries)


def _get_latest_review(ref_file, review_model, current_run_num, ws_dir=None):
    """Find the most recent review result for a level from previous runs."""
    _ws = ws_dir or WORKSPACE_DIR
    for n in range(current_run_num - 1, 0, -1):
        review_dir = os.path.join(_ws, f'第{n}次审稿')
        if not os.path.isdir(review_dir):
            continue
        for fname in sorted(os.listdir(review_dir)):
            if not fname.endswith('_result.json') or fname.startswith(('workspace', 'agg')):
                continue
            try:
                result = _load_json(os.path.join(review_dir, fname))
                if result.get('reference_file') == ref_file and result.get('model') == review_model:
                    return result
            except Exception:
                pass
    return None


def _make_api_call(model_name, messages, include_thinking=True,
                   api_url_override=None, api_key_override=None, clean_model_override=None,
                   provider_name=None):
    """Make a single API call.

    Returns (content, thinking, model_used, full_payload_dict).
    Supports both Anthropic Messages protocol (for Claude) and OpenAI protocol.
    """
    if api_url_override and api_key_override:
        api_url = api_url_override
        api_key = api_key_override
        clean_model = clean_model_override or model_name
    else:
        api_url, api_key, clean_model = _resolve_provider(model_name, provider_name)

    is_claude = 'claude' in clean_model.lower()

    data = {
        "model": clean_model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "stream": True,
        "max_tokens": 65536,
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    if is_claude:
        # Switch to Anthropic Messages endpoint
        for suffix in ['/v1/chat/completions', '/v1/chat']:
            if api_url.endswith(suffix):
                api_url = api_url[:-len(suffix)] + '/v1/messages'
                break
        if not api_url.endswith('/v1/messages'):
            api_url = api_url.rstrip('/') + '/v1/messages'

        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        if include_thinking:
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"

        # Convert messages to Anthropic format
        anthropic_msgs = []
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                anthropic_msgs.append({"role": m["role"], "content": [{"type": "text", "text": c}]})
            else:
                anthropic_msgs.append({"role": m["role"], "content": c})
        data["messages"] = anthropic_msgs
        data["max_tokens"] = 64000
        if include_thinking:
            data["thinking"] = {"budget_tokens": 63999, "type": "enabled"}
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    # Gemini bonus: encourage deeper thinking and longer responses (loaded from file)
    if 'gemini' in clean_model.lower():
        _gemini_bonus_file = os.environ.get('_AUTO_REVIEW_GEMINI_BONUS', os.path.join(PROMPTS_DIR, 'gemini_bonus.txt'))
        if os.path.exists(_gemini_bonus_file):
            _gemini_bonus = '\n\n' + _load_text(_gemini_bonus_file)
            if data['messages']:
                last_msg = data['messages'][-1]
                if isinstance(last_msg.get('content'), str):
                    last_msg['content'] += _gemini_bonus
                elif isinstance(last_msg.get('content'), list):
                    last_msg['content'].append({'type': 'text', 'text': _gemini_bonus})

    # Snapshot payload for debugging (before sending)
    payload_snapshot = json.loads(json.dumps(data, ensure_ascii=False, default=str))

    # HTTP 重试循环：覆盖 SSL 握手、连接重置、流式传输中途断连等瞬时网络错误
    # （go-ai.cc 等供应商在高负载时会 drop 连接，需要带指数退避的重试）
    _http_retries = 999
    _http_base_delay = 3
    content = ""
    thinking = ""
    stop_reason = ""
    _stream_completed = False

    for _http_attempt in range(_http_retries):
        # 每次重试重置累积状态
        content = ""
        thinking = ""
        stop_reason = ""
        _stream_completed = False
        _json_buffer = ""

        # Each call gets its own Session to ensure thread safety
        # (requests.Session is NOT thread-safe for concurrent streaming)
        _session = requests.Session()
        try:
            resp = _session.post(
                api_url, headers=headers, json=data,
                timeout=1800, proxies={"http": None, "https": None},
                stream=True
            )
            # 429 重试：API 限流时带指数退避重试
            if resp.status_code == 429:
                try:
                    _session.close()
                except Exception:
                    pass
                if _http_attempt < _http_retries - 1:
                    _wait = min(_http_base_delay * (2 ** min(_http_attempt, 6)), 120)
                    print(f'[AUTO-REVIEW API] 429 限流重试 {_http_attempt+1}/{_http_retries} (model={clean_model}), 等待 {_wait}s', flush=True)
                    time.sleep(_wait)
                    continue
                else:
                    resp.raise_for_status()
            resp.raise_for_status()

            _content_type = resp.headers.get('Content-Type', '')
            if 'text/event-stream' in _content_type:
                # SSE stream parsing (fallback if API ignores stream=false)
                for line in resp.iter_lines(chunk_size=8192):
                    if not line:
                        continue
                    line_str = line.decode('utf-8', errors='ignore')
                    if _json_buffer:
                        _cont = line_str[6:] if line_str.startswith('data: ') else (line_str[5:] if line_str.startswith('data:') else line_str)
                        _json_buffer += _cont
                        try:
                            chunk = json.loads(_json_buffer)
                            _json_buffer = ""
                        except Exception:
                            continue
                    elif line_str.startswith('data:'):
                        _prefix_len = 6 if line_str.startswith('data: ') else 5
                        _raw = line_str[_prefix_len:]
                        if _raw.strip() == '[DONE]':
                            _stream_completed = True
                            continue
                        try:
                            chunk = json.loads(_raw.strip())
                        except Exception:
                            _json_buffer = _raw
                            continue
                    else:
                        continue
                    if is_claude:
                        chunk_type = chunk.get('type', '')
                        if chunk_type == 'content_block_delta':
                            delta = chunk.get('delta', {})
                            dt = delta.get('type', '')
                            if dt == 'text_delta':
                                content += delta.get('text', '')
                            elif dt == 'thinking_delta':
                                thinking += delta.get('thinking', '')
                        elif chunk_type == 'message_delta':
                            _md = chunk.get('delta', {})
                            if _md.get('stop_reason'):
                                stop_reason = _md['stop_reason']
                        elif chunk_type == 'message_stop':
                            _stream_completed = True
                    else:
                        _choices = chunk.get('choices', [])
                        if not _choices:
                            continue  # Skip heartbeat/empty chunks
                        _first_choice = _choices[0]
                        delta = _first_choice.get('delta', {})
                        if delta.get('content'):
                            content += delta['content']
                        if delta.get('reasoning_content') or delta.get('thinking') or delta.get('reasoning'):
                            thinking += delta.get('reasoning_content') or delta.get('thinking') or delta.get('reasoning') or ''
                        # OpenAI 流式协议：finish_reason 在 choices[0] 顶层而非 delta 内
                        _fr = _first_choice.get('finish_reason')
                        if _fr:
                            stop_reason = _fr
                            if _fr in ('stop', 'end_turn'):
                                _stream_completed = True
            else:
                # Standard JSON response (stream=false)
                _stream_completed = True  # JSON 解析成功即完整
                result = resp.json()
                if is_claude:
                    stop_reason = result.get('stop_reason', '')
                    for block in result.get('content', []):
                        if block.get('type') == 'text':
                            content += block.get('text', '')
                        elif block.get('type') == 'thinking':
                            thinking += block.get('thinking', '')
                else:
                    _nsc = result.get('choices', [])
                    _choice = _nsc[0] if _nsc else {}
                    message = _choice.get('message', {})
                    stop_reason = _choice.get('finish_reason', '')
                    content = message.get('content', '') or ''
                    thinking = message.get('reasoning_content', '') or message.get('thinking', '') or message.get('reasoning', '') or ''

            # Success: close session and exit retry loop
            _session.close()
            break
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as _he:
            try:
                _session.close()
            except Exception:
                pass
            if _http_attempt < _http_retries - 1:
                _wait = _http_base_delay * (2 ** _http_attempt)
                print(f'[AUTO-REVIEW API] 网络错误重试 {_http_attempt+1}/{_http_retries} (model={clean_model}): {type(_he).__name__}: {str(_he)[:120]}, 等待 {_wait}s', flush=True)
                time.sleep(_wait)
            else:
                print(f'[AUTO-REVIEW API] 网络错误重试 {_http_retries} 次全部失败 (model={clean_model}): {type(_he).__name__}: {str(_he)[:120]}', flush=True)
                raise
        except Exception:
            try:
                _session.close()
            except Exception:
                pass
            raise

    # Extract thinking from <think> tags if present in content (non-Claude models)
    if not is_claude:
        think_match = re.search(
            r'(?:<think(?:ing)?>|<\|channel>thought\n?)([\s\S]*?)(?:</think(?:ing)?>|<channel\|>|<\|channel>)',
            content, re.IGNORECASE | re.DOTALL
        )
        if think_match and not thinking:
            thinking = think_match.group(1).strip()
        content = re.sub(
            r'(?:<think(?:ing)?>|<\|channel>thought\n?)[\s\S]*?(?:</think(?:ing)?>|<channel\|>|<\|channel>)',
            '', content, flags=re.IGNORECASE | re.DOTALL
        ).strip()

    # 流完整性检测
    _think_k = len(thinking) / 3000
    _content_k = len(content) / 3000
    _total_k = _think_k + _content_k
    if not _stream_completed:
        print(f'[AUTO-REVIEW API] 流未正常结束（连接中断）！thinking={_think_k:.1f}k, content={_content_k:.1f}k, total={_total_k:.1f}k, stop_reason={stop_reason or "无"}, model={clean_model}', flush=True)
    if stop_reason == 'max_tokens':
        print(f'[AUTO-REVIEW API] 输出被 max_tokens 截断！thinking={_think_k:.1f}k, content={_content_k:.1f}k, model={clean_model}', flush=True)
    # 独立的空内容检测：不依赖 stop_reason 和 _stream_completed
    if not content.strip():
        print(f'[AUTO-REVIEW API] 正文为空！stream_completed={_stream_completed}, stop_reason={stop_reason or "无"}, thinking={_think_k:.1f}k, model={clean_model}', flush=True)
    # 内容级截断检测：判断标签开始但未闭合
    elif '最终判断开始' in content and '最终判断结束' not in content:
        print(f'[AUTO-REVIEW API] 正文被截断（判断标签未闭合）！stream_completed={_stream_completed}, stop_reason={stop_reason or "无"}, content_tail={content[-100:]!r}, model={clean_model}', flush=True)

    return content, thinking, model_name, payload_snapshot


def run_single_checkpoint(checkpoint_index, api=None, target_sid=None, tool_use_id=None, workspace_dir=None):
    """Run a single review checkpoint without planning or skip logic.

    Args:
        checkpoint_index: 1-based index into reference_papers array in index.json
        workspace_dir: Custom workspace directory path. Defaults to 自动审稿工作区/.
    Returns:
        Result string for display.
    """
    # Shadow module-level constants for workspace isolation (thread-safe for parallel workspaces)
    WORKSPACE_DIR = workspace_dir or os.path.join(BASE_DIR, '自动审稿工作区')  # noqa: F841
    PROMPTS_DIR = os.path.join(WORKSPACE_DIR, 'prompts')  # noqa: F841
    config = _load_json(os.path.join(WORKSPACE_DIR, 'index.json'))
    _english_mode = config.get('english_mode', False)
    _my_position = config.get('my_position', 'random')

    my_paper_file = config['my_paper']
    _my_paper_path = os.path.join(WORKSPACE_DIR, my_paper_file)
    if _english_mode and not os.path.exists(_my_paper_path):
        _en_path = os.path.join(WORKSPACE_DIR, 'en_' + my_paper_file)
        if os.path.exists(_en_path):
            _my_paper_path = _en_path
    my_paper_text = _load_text(_my_paper_path)

    ws_file = config.get('my_paper_workspace')
    ws_text = _load_text(os.path.join(WORKSPACE_DIR, ws_file)) if ws_file else my_paper_text

    ref_papers = config['reference_papers']
    aliases = config.get('model_aliases', {})
    if aliases:
        for ref in ref_papers:
            alias = ref.pop('review', None)
            if alias and alias in aliases:
                alias_val = aliases[alias]
                if isinstance(alias_val, list):
                    ref['review_model'] = alias_val[0]['model']
                    ref['review_provider'] = alias_val[0]['provider']
                else:
                    ref['review_model'] = alias_val['model']
                    ref['review_provider'] = alias_val['provider']

    if _english_mode:
        _en_ws_file = 'en_' + (ws_file or my_paper_file)
        if os.path.exists(os.path.join(WORKSPACE_DIR, _en_ws_file)):
            ws_text = _load_text(os.path.join(WORKSPACE_DIR, _en_ws_file))
            my_paper_text = ws_text
        for ref in ref_papers:
            _en_ref = 'en_' + ref['file']
            if os.path.exists(os.path.join(WORKSPACE_DIR, _en_ref)):
                ref['_original_file'] = ref['file']
                ref['file'] = _en_ref
        _en_bonus = os.path.join(PROMPTS_DIR, 'en_gemini_bonus.txt')
        if os.path.exists(_en_bonus):
            os.environ['_AUTO_REVIEW_GEMINI_BONUS'] = _en_bonus

    n = len(ref_papers)
    idx = checkpoint_index - 1  # Convert to 0-based
    if idx < 0 or idx >= n:
        return f"错误：关卡索引 {checkpoint_index} 超出范围 (1-{n})"

    ref = ref_papers[idx]
    ref_file = ref['file']
    ref_scores = ref['scores']
    ref_text = _load_text(os.path.join(WORKSPACE_DIR, ref_file))
    model = ref.get('review_model', 'unknown')
    model_provider = ref.get('review_provider')

    # A/B position (per-entry override from index.json, fallback to global my_position)
    _pos = ref.get('position', _my_position)
    if _pos == 'A':
        pa, pb, my_pos = ws_text, ref_text, 'A'
    elif _pos == 'B':
        pa, pb, my_pos = ref_text, ws_text, 'B'
    else:
        if random.random() < 0.5:
            pa, pb, my_pos = ws_text, ref_text, 'A'
        else:
            pa, pb, my_pos = ref_text, ws_text, 'B'

    # Load prompt template (style from config: "simple" or "detailed")
    _prompt_prefix = 'en_' if _english_mode else ''
    _review_style = config.get('review_prompt_style', 'simple')
    _review_suffix = '_old' if _review_style == 'detailed' else ''
    review_prompt_tmpl = _load_text(os.path.join(PROMPTS_DIR, f'{_prompt_prefix}review_prompt{_review_suffix}.txt'))
    scores_str = ', '.join(str(s) for s in ref_scores)
    prompt = (review_prompt_tmpl
              .replace('{scores}', scores_str)
              .replace('{paper_a}', pa)
              .replace('{paper_b}', pb))
    msgs = [{"role": "user", "content": prompt}]

    try:
        content, thinking, model_used, payload = _make_api_call(
            model, msgs, include_thinking=True, provider_name=model_provider
        )
    except Exception as e:
        return f"API调用失败: {e}"

    judgment = _parse_judgment(content)
    if judgment:
        we_won = (judgment != my_pos)
        win_label = '我方胜' if we_won else '我方负'
    else:
        win_label = '解析失败'

    # Emit result bubble if in stream mode
    if api and target_sid and tool_use_id and target_sid in api.sessions:
        session = api.sessions[target_sid]
        bt = chr(96) * 3
        _th_preview = ''
        if thinking:
            _th_trunc = thinking[:8000] if len(thinking) > 8000 else thinking
            _th_preview = f"\n思维链:\n{_th_trunc}\n\n---\n"
        bubble = api._make_msg("user",
            f"**单关卡审稿 #{checkpoint_index} ({model_used}) {win_label}**\n\n{bt}\n"
            f"参考分数: [{scores_str}] | 我方位置: 论文{my_pos} | 判断: {judgment or '?'}\n"
            f"{_th_preview}\n{content}\n{bt}",
            summary=f"单关卡 #{checkpoint_index} {win_label}",
            is_collapsed=True, is_tool_result=True, tool_use_id=tool_use_id)
        session['conversation_history'].append(bubble)
        api.save_sessions(push_update=True)

    return (
        f"单关卡审稿 #{checkpoint_index} 完成\n"
        f"参考论文: {ref_file}\n"
        f"审稿模型: {model_used}\n"
        f"我方位置: 论文{my_pos}\n"
        f"判断: {judgment or '解析失败'}\n"
        f"结果: {win_label}"
    )


def run_auto_review(api=None, target_sid=None, tool_use_id=None, force_full=False, enable_baseline=False, workspace_dir=None):
    """Execute the full auto-review pipeline.

    When api/target_sid/tool_use_id are provided, each review/planning result
    is emitted as an individual bubble for real-time visibility.
    Otherwise falls back to returning one combined result string.
    workspace_dir: Custom workspace directory path. Defaults to 自动审稿工作区/.
    """
    # Shadow module-level constants for workspace isolation (thread-safe for parallel workspaces)
    WORKSPACE_DIR = workspace_dir or os.path.join(BASE_DIR, '自动审稿工作区')  # noqa: F841
    PROMPTS_DIR = os.path.join(WORKSPACE_DIR, 'prompts')  # noqa: F841
    config = _load_json(os.path.join(WORKSPACE_DIR, 'index.json'))

    # Detect English mode early to adjust file paths before loading
    _english_mode = config.get('english_mode', False)
    # A/B position for our paper: "A" (first), "B" (second), or "random"
    _my_position = config.get('my_position', 'random')

    my_paper_file = config['my_paper']
    # In English mode, load en_ prefixed version if original doesn't exist
    _my_paper_path = os.path.join(WORKSPACE_DIR, my_paper_file)
    if _english_mode and not os.path.exists(_my_paper_path):
        _en_path = os.path.join(WORKSPACE_DIR, 'en_' + my_paper_file)
        if os.path.exists(_en_path):
            _my_paper_path = _en_path
    my_paper_text = _load_text(_my_paper_path)

    # Editable workspace version (if configured); otherwise use original
    ws_file = config.get('my_paper_workspace')
    ws_text = _load_text(os.path.join(WORKSPACE_DIR, ws_file)) if ws_file else my_paper_text

    # Full original paper (for planning model as factual reference)
    full_paper_file = config.get('my_paper_full')
    if full_paper_file:
        full_paper_text = _load_text(os.path.join(WORKSPACE_DIR, full_paper_file))
    else:
        full_paper_text = '(全文未提供。请在 index.json 中设置 my_paper_full 字段指向完整论文文件。)'
        print('[AUTO-REVIEW] WARNING: my_paper_full not configured in index.json, planning model will lack full paper context', flush=True)

    ref_papers = config['reference_papers']
    review_models = config.get('review_models', [])  # legacy fallback list
    planning_models = config.get('planning_models', [])

    # Resolve model aliases: expand short codes (a/o/g) into full model+provider
    aliases = config.get('model_aliases', {})
    if aliases:
        for ref in ref_papers:
            alias = ref.pop('review', None)
            if alias and alias in aliases:
                alias_val = aliases[alias]
                if isinstance(alias_val, list):
                    ref['_review_pool'] = alias_val
                    ref['review_model'] = alias_val[0]['model']
                    ref['review_provider'] = alias_val[0]['provider']
                else:
                    ref['review_model'] = alias_val['model']
                    ref['review_provider'] = alias_val['provider']
        for pm in planning_models:
            alias = pm.pop('alias', None)
            if alias and alias in aliases:
                alias_val = aliases[alias]
                if isinstance(alias_val, list):
                    pm['_pool'] = alias_val
                    pm['model'] = alias_val[0]['model']
                    pm['provider'] = alias_val[0]['provider']
                else:
                    pm['model'] = alias_val['model']
                    pm['provider'] = alias_val['provider']

    # English mode: override remaining file paths and prompt templates
    if _english_mode:
        # Override workspace and paper files with en_ prefixed versions
        _en_ws_file = 'en_' + (ws_file or my_paper_file)
        if os.path.exists(os.path.join(WORKSPACE_DIR, _en_ws_file)):
            ws_text = _load_text(os.path.join(WORKSPACE_DIR, _en_ws_file))
            my_paper_text = ws_text  # For consistency
        _en_full = 'en_' + full_paper_file if full_paper_file else None
        if _en_full and os.path.exists(os.path.join(WORKSPACE_DIR, _en_full)):
            full_paper_text = _load_text(os.path.join(WORKSPACE_DIR, _en_full))
        # Override reference paper file paths
        for ref in ref_papers:
            _en_ref = 'en_' + ref['file']
            if os.path.exists(os.path.join(WORKSPACE_DIR, _en_ref)):
                ref['_original_file'] = ref['file']  # Keep original for display
                ref['file'] = _en_ref
        # Override Gemini bonus to English version
        _en_bonus = os.path.join(PROMPTS_DIR, 'en_gemini_bonus.txt')
        if os.path.exists(_en_bonus):
            # Monkey-patch the bonus file path for _make_api_call
            os.environ['_AUTO_REVIEW_GEMINI_BONUS'] = _en_bonus
        print(f'[AUTO-REVIEW] English mode enabled', flush=True)

    # ────────────────── Baseline mode: load o_ prefix workspace and double ref_papers ──────────────────
    baseline_text = None
    if enable_baseline:
        # Try multiple candidate paths: o_en_ws, o_ws, o_en_my_paper, o_my_paper
        _candidates = []
        _ws_or_paper = ws_file or my_paper_file
        if _english_mode:
            _candidates.append('o_en_' + _ws_or_paper)
            _candidates.append('o_' + _ws_or_paper)
            _candidates.append('o_en_' + my_paper_file)
        _candidates.append('o_' + my_paper_file)
        # Also try directly using the file we know exists: o_en_0-DART...txt.txt
        for _bf in os.listdir(WORKSPACE_DIR):
            if _bf.startswith('o_en_') and _bf.endswith('.txt') and 'review' not in _bf and 'planning' not in _bf:
                _candidates.append(_bf)
            elif _bf.startswith('o_') and not _bf.startswith('o_en_') and _bf.endswith('.txt') and 'review' not in _bf and 'planning' not in _bf:
                _candidates.append(_bf)
        _baseline_path = None
        for _c in _candidates:
            _p = os.path.join(WORKSPACE_DIR, _c)
            if os.path.exists(_p):
                _baseline_path = _p
                break
        if _baseline_path:
            baseline_text = _load_text(_baseline_path)
            print(f'[AUTO-REVIEW] Baseline mode: loaded {os.path.basename(_baseline_path)} ({len(baseline_text)} chars)', flush=True)
            # Expand ref_papers: each entry cloned twice (modified + baseline)
            _expanded = []
            for ref in ref_papers:
                _ref_modified = dict(ref)
                _ref_modified['_is_baseline'] = False
                _expanded.append(_ref_modified)
                _ref_baseline = dict(ref)
                _ref_baseline['_is_baseline'] = True
                _expanded.append(_ref_baseline)
            ref_papers = _expanded
            print(f'[AUTO-REVIEW] Baseline mode: expanded checkpoints to {len(ref_papers)} (45 modified + 45 baseline)', flush=True)
        else:
            print(f'[AUTO-REVIEW] WARNING: enable_baseline=True 但找不到 o_ 前缀工作区文件，自动禁用 baseline 模式', flush=True)
            enable_baseline = False

    # Validate planning model probabilities
    if planning_models:
        total_p = sum(m.get('probability', 0) for m in planning_models)
        if abs(total_p - 1.0) > 0.01:
            return (f"错误：规划模型概率总和为{total_p:.4f}，必须为 1.0。"
                    f"请修改 index.json 中的 planning_models 配置。")

    # Load prompt templates from files (English mode uses en_ prefixed versions)
    _prompt_prefix = 'en_' if _english_mode else ''
    _review_style = config.get('review_prompt_style', 'simple')
    _review_suffix = '_old' if _review_style == 'detailed' else ''
    review_prompt_tmpl = _load_text(os.path.join(PROMPTS_DIR, f'{_prompt_prefix}review_prompt{_review_suffix}.txt'))
    print(f'[AUTO-REVIEW] Prompt style: {_review_style} ({_prompt_prefix}review_prompt{_review_suffix}.txt)', flush=True)
    planning_prompt_tmpl = _load_text(os.path.join(PROMPTS_DIR, f'{_prompt_prefix}planning_prompt.txt'))
    # In English mode, optionally load Chinese planning prompt for mixed-language output
    _planning_zh_prob = config.get('planning_chinese_probability', 0) if _english_mode else 0
    _planning_zh_tmpl = _load_text(os.path.join(PROMPTS_DIR, 'planning_prompt.txt')) if _planning_zh_prob > 0 else None
    _planning_agg_zh_tmpl = None
    if _planning_zh_prob > 0:
        _zh_agg_path = os.path.join(PROMPTS_DIR, 'planning_aggregate_prompt.txt')
        _planning_agg_zh_tmpl = _load_text(_zh_agg_path) if os.path.exists(_zh_agg_path) else None

    n = len(ref_papers)

    # Create output directories with sequential numbering
    run_num = _get_next_run_number(ws_dir=WORKSPACE_DIR)
    review_dir = os.path.join(WORKSPACE_DIR, f'第{run_num}次审稿')
    planning_dir = os.path.join(WORKSPACE_DIR, f'第{run_num}次规划')

    # Resume detection: check if previous run has failed/missing results
    _resume_mode = False
    _prev_run = run_num - 1
    if _prev_run > 0:
        _prev_dir = os.path.join(WORKSPACE_DIR, f'第{_prev_run}次审稿')
        if os.path.isdir(_prev_dir):
            _prev_ok = 0
            _prev_fail = 0
            for _ci in range(n):
                _rp = os.path.join(_prev_dir, f'{_ci+1}_result.json')
                _ep = os.path.join(_prev_dir, f'{_ci+1}_error.json')
                if os.path.exists(_rp):
                    try:
                        _cr = _load_json(_rp)
                        if _cr.get('skipped') or 'content' in _cr:
                            _prev_ok += 1
                        else:
                            _prev_fail += 1
                    except Exception:
                        _prev_fail += 1
                elif os.path.exists(_ep):
                    _prev_fail += 1
                # Missing entirely also counts as incomplete
            if _prev_fail > 0 or (_prev_ok + _prev_fail) < n:
                run_num = _prev_run
                _resume_mode = True
                review_dir = _prev_dir
                planning_dir = os.path.join(WORKSPACE_DIR, f'第{_prev_run}次规划')
                print(f"[AUTO-REVIEW] 续接第{_prev_run}次审稿（{_prev_ok}成功, {_prev_fail}失败）", flush=True)

    os.makedirs(review_dir, exist_ok=True)

    # Save workspace snapshot (skip if resuming to preserve original)
    if not _resume_mode:
        _save_text(os.path.join(review_dir, 'workspace_snapshot.txt'), ws_text)
        print(f"[AUTO-REVIEW] 工作区快照已保存 ({len(ws_text)} chars)", flush=True)
    else:
        print(f"[AUTO-REVIEW] 续接模式：保留原有工作区快照", flush=True)

    _used_models = list(set(r.get('review_model', '?') for r in ref_papers))
    output_parts = [f"=== 第{run_num}次自动审稿 ({n}篇参考论文, {len(_used_models)}个审稿模型: {_used_models}) ===\n"]
    review_results = {}

    # Resume: load existing successful results from the directory
    if _resume_mode:
        for _ri in range(n):
            _rp = os.path.join(review_dir, f'{_ri+1}_result.json')
            if os.path.exists(_rp):
                try:
                    _cr = _load_json(_rp)
                    if _cr.get('skipped') or ('content' in _cr and 'error' not in _cr):
                        review_results[_ri] = _cr
                except Exception:
                    pass
        print(f"[AUTO-REVIEW] 续接：已加载 {len(review_results)} 个已有结果", flush=True)

    # Real-time result emitter: creates individual bubbles in the conversation
    _stream_mode = bool(api and target_sid and tool_use_id)
    def _emit(title, content):
        """Emit a single result as an independent bubble (thread-safe).

        Sets is_tool_result=True and tool_use_id=parent tool ID so the frontend
        attaches (吸附) all 20 results inline beneath the 自动审稿 tool block.
        """
        if _stream_mode and target_sid in api.sessions:
            session = api.sessions[target_sid]
            bt = chr(96) * 3
            bubble = api._make_msg("user",
                f"**{title}**\n\n{bt}\n{content}\n{bt}",
                summary=title, is_collapsed=True,
                is_tool_result=True, tool_use_id=tool_use_id)
            session['conversation_history'].append(bubble)
            api.save_sessions(push_update=True)

    # ────────────────── Skip logic: per-level win-rate + per-level cooldown ──────────────────
    _window_size = config.get('skip_window_size', 5)
    _skip_mask = {}  # i -> win_rate
    if not force_full:
        for _si in range(n):
            if _si in review_results:  # Already loaded from resume
                continue
            _sref = ref_papers[_si]
            _srf = _sref.get('_original_file', _sref['file'])
            _srm = _sref.get('review_model', '')
            _swr, _swc = _compute_win_rate(_srf, _srm, run_num, _window_size, ws_dir=WORKSPACE_DIR)
            if _swr > 0.75 and _swc >= _window_size:
                # Per-level cooldown: count how many consecutive times THIS level was skipped
                _level_consec = 0
                for _cn in range(run_num - 1, 0, -1):
                    _meta_path = os.path.join(WORKSPACE_DIR, f'第{_cn}次审稿', 'run_meta.json')
                    if not os.path.exists(_meta_path):
                        break
                    try:
                        _meta = _load_json(_meta_path)
                        _found_level = False
                        for _le in _meta.get('levels', []):
                            if _le.get('ref_file') == _srf and _le.get('review_model') == _srm:
                                _found_level = True
                                if _le.get('skipped'):
                                    _level_consec += 1
                                else:
                                    _level_consec = -1  # Not skipped → stop counting
                                break
                        if not _found_level or _level_consec < 0:
                            break
                    except Exception:
                        break
                if _level_consec >= 2:
                    print(f"[AUTO-REVIEW] 关卡 {_si+1} 冷却触发（连续{_level_consec}次跳过），强制审稿", flush=True)
                    continue  # Don't skip — force review for this level
                # Skip this level
                review_results[_si] = {
                    'skipped': True, 'skip_win_rate': _swr,
                    'reference_file': _srf, 'model': _srm,
                    'review_model': _srm, 'my_position': '?',
                }
                _skip_mask[_si] = _swr
                _emit(f"审稿 [{_si+1}/{n}] 跳过 ({_srm})",
                      f"该关卡胜率稳定高（{_swr:.0%}），跳过审稿")
                print(f"[AUTO-REVIEW] 跳过关卡 {_si+1} (胜率 {_swr:.0%})", flush=True)
    _levels_to_review = [i for i in range(n) if i not in review_results]
    _skipped_count = sum(1 for r in review_results.values() if r.get('skipped'))
    _resumed_count = sum(1 for r in review_results.values() if not r.get('skipped'))
    if _skipped_count > 0 or _resumed_count > 0:
        print(f"[AUTO-REVIEW] 跳过{_skipped_count}个 + 续接{_resumed_count}个 = 需审稿{len(_levels_to_review)}/{n}", flush=True)

    # ────────────────── Phase 1: Review ──────────────────
    _mode_label = '续接' if _resume_mode else '新建'
    print(f"\n{'='*60}\n[AUTO-REVIEW] 第{run_num}次审稿开始 ({_mode_label}, {n}篇, 跳过{_skipped_count}, 续接{_resumed_count}, 审稿{len(_levels_to_review)})\n{'='*60}", flush=True)

    def _do_review(i, _stagger_delay=0):
        if _stagger_delay > 0:
            time.sleep(_stagger_delay)
        ref = ref_papers[i]
        _is_baseline = ref.get('_is_baseline', False)
        # Select workspace text based on baseline flag (baseline_text from o_ prefix file)
        actual_ws_text = baseline_text if (_is_baseline and baseline_text) else ws_text
        ref_file = ref['file']
        ref_scores = ref['scores']
        ref_text = _load_text(os.path.join(WORKSPACE_DIR, ref_file))
        # Per-paper model assignment with provider pool (no concurrency limit, rely on 429 retry)
        _pool = ref.get('_review_pool')
        if _pool:
            _selected = random.choice(_pool)
            model = _selected['model']
            model_provider = _selected['provider']
        else:
            model = ref.get('review_model') or (review_models[i % len(review_models)] if review_models else 'unknown')
            model_provider = ref.get('review_provider')

        # A/B position (per-entry override, fallback to global my_position)
        _pos = ref.get('position', _my_position)
        if _pos == 'A':
            pa, pb, my_pos = actual_ws_text, ref_text, 'A'
        elif _pos == 'B':
            pa, pb, my_pos = ref_text, actual_ws_text, 'B'
        else:
            if random.random() < 0.5:
                pa, pb, my_pos = actual_ws_text, ref_text, 'A'
            else:
                pa, pb, my_pos = ref_text, actual_ws_text, 'B'

        scores_str = ', '.join(str(s) for s in ref_scores)
        prompt = (review_prompt_tmpl
                  .replace('{scores}', scores_str)
                  .replace('{paper_a}', pa)
                  .replace('{paper_b}', pb))

        msgs = [{"role": "user", "content": prompt}]

        try:
            content, thinking, model_used, payload = _make_api_call(
                model, msgs, include_thinking=True, provider_name=model_provider
            )

            # Save payload for debugging (always)
            _save_json(os.path.join(review_dir, f'{i+1}_payload.json'), payload)

            # Parse judgment FIRST — only save result when successfully parsed
            judgment = _parse_judgment(content)

            result_data = {
                'index': i + 1, 'reference_file': ref_file,
                'reference_scores': ref_scores, 'my_position': my_pos,
                'is_baseline': _is_baseline,
                'model': model_used, 'thinking': thinking, 'content': content,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }

            if judgment:
                we_won = (judgment != my_pos)
                result_data['judgment'] = judgment
                result_data['we_won'] = we_won
                _win_label = '我方胜' if we_won else '我方负'
                _save_json(os.path.join(review_dir, f'{i+1}_result.json'), result_data)
                readable = (
                    f"审稿模型: {model_used}\n"
                    f"参考论文: {ref_file}\n"
                    f"参考分数: [{scores_str}]\n"
                    f"我的论文位置: 论文{my_pos}\n"
                    f"时间: {result_data['timestamp']}\n"
                    f"\n{'='*40} 思维链 {'='*40}\n\n{thinking or '(无)'}\n"
                    f"\n{'='*40} 正文 {'='*40}\n\n{content}\n"
                )
                _save_text(os.path.join(review_dir, f'{i+1}_result.txt'), readable)
            else:
                _win_label = '解析失败（不保存，将重试）'
                _last_lines = content.strip().split('\n')[-5:]
                print(f"[AUTO-REVIEW] 关卡 {i+1} 判断解析失败！最后5行:\n" + '\n'.join(_last_lines), flush=True)

            # Emit individual result bubble for real-time visibility
            _th_preview = ''
            if thinking:
                _th_trunc = thinking[:8000] if len(thinking) > 8000 else thinking
                _th_preview = f"\n思维链:\n{_th_trunc}\n\n---\n"
            _baseline_tag = ' [Baseline]' if _is_baseline else ''
            _emit(f"审稿 [{i+1}/{n}]{_baseline_tag} {ref_file[:30]}... ({model_used}) {_win_label}",
                  f"参考分数: [{scores_str}] | 我方位置: 论文{my_pos} | 判断: {judgment or '?'}\n{_th_preview}\n{content}")

            print(f"[AUTO-REVIEW] 审稿 {i+1}/{n} 完成 (model={model_used})", flush=True)
            return i, result_data

        except Exception as e:
            traceback.print_exc()
            err = {'index': i + 1, 'error': str(e), 'model': model, 'reference_file': ref_file}
            _emit(f"审稿 [{i+1}/{n}] 失败 ({model})", str(e))
            print(f"[AUTO-REVIEW] 审稿 {i+1}/{n} 失败: {e}", flush=True)
            return i, err

    with ThreadPoolExecutor(max_workers=len(_levels_to_review) or 1) as pool:
        futures = {pool.submit(_do_review, i, _stagger_delay=0): i for idx, i in enumerate(_levels_to_review)}
        for f in as_completed(futures):
            idx, res = f.result()
            review_results[idx] = res

    # ────────────────── Serial retry for unparsed judgments ──────────────────
    _max_retries = config.get('parse_retry_count', 999)
    if _max_retries > 0:
        for _retry_round in range(1, _max_retries + 1):
            _unparsed_indices = [
                i for i in range(n)
                if not review_results.get(i, {}).get('skipped')
                and (
                    i not in review_results  # 完全缺失
                    or 'error' in review_results[i]  # API 出错
                    or review_results[i].get('judgment') is None  # 解析失败
                )
            ]
            if not _unparsed_indices:
                break
            print(f"\n[AUTO-REVIEW] 解析失败串行重试 第{_retry_round}轮: {len(_unparsed_indices)} 个关卡待重试: {[i+1 for i in _unparsed_indices]}", flush=True)
            _emit(f"解析重试 第{_retry_round}轮 ({len(_unparsed_indices)}个)",
                  f"以下关卡判断解析失败，将逐个串行重试: {[i+1 for i in _unparsed_indices]}")
            _retry_success = 0
            for _ri in _unparsed_indices:
                print(f"[AUTO-REVIEW] 串行重试关卡 {_ri+1}...", flush=True)
                try:
                    _ri_idx, _ri_res = _do_review(_ri, _stagger_delay=0)
                    if 'error' not in _ri_res and _ri_res.get('judgment') is not None:
                        review_results[_ri_idx] = _ri_res
                        _retry_success += 1
                        print(f"[AUTO-REVIEW] 重试成功: 关卡 {_ri+1} 判断={_ri_res['judgment']}", flush=True)
                    else:
                        # 重试仍然解析失败或出错，保留新结果（可能内容更好）
                        review_results[_ri_idx] = _ri_res
                        print(f"[AUTO-REVIEW] 重试仍失败: 关卡 {_ri+1}", flush=True)
                except Exception as _re:
                    print(f"[AUTO-REVIEW] 重试异常: 关卡 {_ri+1}: {_re}", flush=True)
            print(f"[AUTO-REVIEW] 第{_retry_round}轮重试完成: {_retry_success}/{len(_unparsed_indices)} 成功", flush=True)
            # 不再因本轮无成功而停止——永远继续重试直到全部解析成功
            print(f"[AUTO-REVIEW] 第{_retry_round}轮: {_retry_success} 成功，继续下一轮", flush=True)

    # Save run metadata for cooldown tracking + per-level details
    _level_details = []
    for _mi in range(n):
        _mr = review_results.get(_mi, {})
        _mref = ref_papers[_mi]
        _level_details.append({
            'index': _mi + 1,
            'ref_file': _mref.get('_original_file', _mref['file']),
            'review_model': _mref.get('review_model', '?'),
            'skipped': bool(_mr.get('skipped')),
            'judgment': _mr.get('judgment'),
            'we_won': _mr.get('we_won'),
            'error': _mr.get('error') if 'error' in _mr else None,
        })
    _save_json(os.path.join(review_dir, 'run_meta.json'), {
        'run_num': run_num,
        'levels': _level_details,
    })

    # Build review section of tool result
    for i in range(n):
        r = review_results.get(i, {})
        if 'error' in r:
            output_parts.append(
                f"\n--- [{i+1}/{n}] {r.get('reference_file', '?')} ---\n"
                f"审稿失败: {r['error']}\n"
)
        else:
            _wl_display = ''
            if r.get('skipped'):
                _wl_display = f" | 跳过 (胜率{r.get('skip_win_rate', 0):.0%})"
            elif r.get('we_won') is not None:
                _wl_display = ' | 我方胜' if r['we_won'] else ' | 我方负'
            output_parts.append(
                f"\n--- [{i+1}/{n}] {r['reference_file']} (模型: {r['model']}) ---\n"
                f"我的论文位置: 论文{r['my_position']} | 参考分数: {r['reference_scores']}{_wl_display}\n"
            )
            if r.get('thinking'):
                _th = r['thinking']
                if len(_th) > 15000:  # ~5k tokens max
                    _th = _th[:15000] + f"\n\n... [思维链已截断至5k tokens，完整版本见 第{run_num}次审稿/{i+1}_result.txt]"
                output_parts.append(f"\n思维链:\n{_th}\n")
            output_parts.append(f"\n审稿正文:\n{r['content']}\n")

    # ────────────────── Phase 2: Planning ──────────────────
    if not planning_models:
        output_parts.append(
            "\n未配置规划模型 (planning_models 为空)，跳过规划阶段。"
            "请在 index.json 中配置 planning_models 列表后重新运行。\n"
        )
    else:
        os.makedirs(planning_dir, exist_ok=True)
        print(f"\n{'='*60}\n[AUTO-REVIEW] 第{run_num}次规划开始\n{'='*60}", flush=True)

        pm_names = [m['model'] for m in planning_models]
        pm_probs = [m['probability'] for m in planning_models]
        planning_results = {}

        # Pre-load all level histories into memory ONCE before entering thread pool
        # to avoid concurrent file I/O contention (was causing 10+ second delays)
        _hist_prob = config.get('planning_history_probability', 0)
        _preloaded_histories = {}
        if _hist_prob > 0:
            for _pi in range(n):
                _pref = ref_papers[_pi]
                _pk = (_pref['file'], _pref.get('review_model', ''))
                if _pk not in _preloaded_histories:
                    _preloaded_histories[_pk] = _collect_level_history(_pref['file'], _pref.get('review_model', ''), run_num, ws_dir=WORKSPACE_DIR)
            _total_hist = sum(len(v) for v in _preloaded_histories.values())
            print(f"[AUTO-REVIEW] 预加载完成: {len(_preloaded_histories)} 个关卡, 共 {_total_hist} 条历史记录", flush=True)

        _planning_agg_mode = config.get('planning_aggregate', False) and len(planning_models) >= 3

        if _planning_agg_mode:
            # ===== AGGREGATE PLANNING MODE: 3 API calls, one per model =====
            # Filter out baseline entries: only modified-version checkpoints participate in planning
            _agg_indices = [_ai for _ai in range(n) if not ref_papers[_ai].get('_is_baseline', False)]
            random.shuffle(_agg_indices)
            _agg_n = len(_agg_indices)
            _agg_sizes = [_agg_n // 3] * 3
            for _ri in range(_agg_n % 3):
                _agg_sizes[_ri] += 1
            random.shuffle(_agg_sizes)
            _agg_assignments = []
            _agg_offset = 0
            for _sz in _agg_sizes:
                _agg_assignments.append(_agg_indices[_agg_offset:_agg_offset + _sz])
                _agg_offset += _sz
            print(f"[AUTO-REVIEW] 聚合规划: 分配 {[len(a) for a in _agg_assignments]} 给 {[pm['model'] for pm in planning_models]}", flush=True)

            _agg_tmpl_path = os.path.join(PROMPTS_DIR, f'{_prompt_prefix}planning_aggregate_prompt.txt')
            _agg_tmpl = _load_text(_agg_tmpl_path) if os.path.exists(_agg_tmpl_path) else planning_prompt_tmpl

            def _do_aggregate_planning(model_idx):
                # Randomly choose Chinese or English aggregate prompt
                _use_zh_agg = _planning_zh_prob > 0 and random.random() < _planning_zh_prob
                _active_agg_tmpl = _planning_agg_zh_tmpl if (_use_zh_agg and _planning_agg_zh_tmpl) else _agg_tmpl
                pm = planning_models[model_idx]
                assigned = _agg_assignments[model_idx]
                # Skip only if no assigned review returned a successful result
                _has_result = any(
                    not review_results.get(_ai, {}).get('skipped') and 'error' not in review_results.get(_ai, {})
                    for _ai in assigned
                )
                if not _has_result:
                    _skip_labels = ', '.join(str(a+1) for a in assigned)
                    _emit(f"聚合规划 [{model_idx+1}/3] 跳过 ({pm['model']})",
                          f"分配关卡 [{_skip_labels}] 无成功返回的审稿结果")
                    print(f"[AUTO-REVIEW] 聚合规划 {model_idx+1}/3 跳过（无成功结果）", flush=True)
                    return model_idx, {'skipped': True, 'model': pm['model']}
                level_parts = []
                for _li in assigned:
                    ref = ref_papers[_li]
                    ref_file = ref['file']
                    ref_text = _load_text(os.path.join(WORKSPACE_DIR, ref_file))
                    rv = review_results.get(_li, {})
                    _lp = [f"\n== 关卡 {_li+1}: {ref_file} (审稿模型: {ref.get('review_model', '?')}) =="]
                    _lp.append(f"\n=== 参考论文 ===\n{ref_text}")
                    if _hist_prob > 0 and random.random() < _hist_prob:
                        _level_model = ref.get('review_model', '')
                        _lhist = _preloaded_histories.get((ref_file, _level_model), [])
                        if _lhist:
                            _lhp = [f"\n=== 该关卡的历史迭代记录（共{len(_lhist)}轮）==="]
                            for _lh in _lhist:
                                _lhp.append(f"--- 第{_lh['run_num']}轮工作区 ---\n{_lh['workspace']}")
                                _lhp.append(f"--- 第{_lh['run_num']}轮审稿反馈 (我方: 论文{_lh['my_position']}) ---")
                                if _lh.get('review_thinking'):
                                    _lhp.append(f"思维链:\n{_lh['review_thinking'][:8000]}\n")
                                _lhp.append(_lh['review_content'])
                            _lhp.append(f"--- 当前(第{run_num}轮) ---\n审稿反馈见下方。")
                            _lp.append('\n'.join(_lhp))
                            print(f"[AUTO-REVIEW] 聚合 {model_idx+1}: 关卡{_li+1} 注入{len(_lhist)}轮历史", flush=True)
                    if 'error' in rv:
                        _lp.append("\n=== 审稿意见 ===\n（审稿失败）")
                    else:
                        _rt = ''
                        if rv.get('thinking'):
                            _rt += f"=== 审稿思维链 ===\n{rv['thinking']}\n\n"
                        _rt += f"=== 审稿正式输出 ===\n{rv.get('content', '')}"
                        _lp.append(f"\n=== 审稿意见 ===\n{_rt}")
                    level_parts.append('\n'.join(_lp))
                levels_text = '\n\n'.join(level_parts)
                prompt = (_active_agg_tmpl
                          .replace('{workspace}', ws_text)
                          .replace('{levels_section}', levels_text)
                          .replace('{my_paper_original}', full_paper_text))
                msgs = [{"role": "user", "content": prompt}]
                try:
                    content, _thinking, model_used, payload = _make_api_call(
                        pm['model'], msgs, include_thinking=False,
                        provider_name=pm.get('provider'))
                    _save_json(os.path.join(planning_dir, f'agg_{model_idx+1}_payload.json'), payload)
                    result_data = {
                        'model_index': model_idx + 1,
                        'assigned_papers': [ref_papers[a]['file'] for a in assigned],
                        'model': model_used, 'content': content,
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
                    _save_json(os.path.join(planning_dir, f'agg_{model_idx+1}_result.json'), result_data)
                    _save_text(os.path.join(planning_dir, f'agg_{model_idx+1}_result.txt'),
                               f"聚合规划: {model_used}\n关卡: {', '.join(str(a+1) for a in assigned)}\n\n{content}\n")
                    _labels = ', '.join(ref_papers[a]['file'][:20] for a in assigned)
                    _emit(f"聚合规划 [{model_idx+1}/3] ({model_used})", f"关卡: {_labels}\n\n{content}")
                    print(f"[AUTO-REVIEW] 聚合规划 {model_idx+1}/3 完成 ({model_used}, {len(assigned)}关卡)", flush=True)
                    return model_idx, result_data
                except Exception as e:
                    traceback.print_exc()
                    err = {'error': str(e), 'model': pm['model']}
                    _save_json(os.path.join(planning_dir, f'agg_{model_idx+1}_error.json'), err)
                    _emit(f"聚合规划 [{model_idx+1}/3] 失败 ({pm['model']})", str(e))
                    print(f"[AUTO-REVIEW] 聚合规划 {model_idx+1}/3 失败: {e}", flush=True)
                    return model_idx, err

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_do_aggregate_planning, mi): mi for mi in range(len(planning_models))}
                for f in as_completed(futures):
                    idx, res = f.result()
                    planning_results[idx] = res
            output_parts.append(f"\n\n=== 聚合规划 (3模型, 分配: {[len(a) for a in _agg_assignments]}) ===\n")
            for mi in range(len(planning_models)):
                r = planning_results.get(mi, {})
                if 'error' in r:
                    output_parts.append(f"\n--- [Model {mi+1}] 失败: {r['error']}\n")
                else:
                    output_parts.append(f"\n--- [Model {mi+1}] {r.get('model', '?')} ---\n{r.get('content', '')}\n")

        def _do_planning(i, _stagger_delay=0):
            if _stagger_delay > 0:
                time.sleep(_stagger_delay)
            ref = ref_papers[i]
            # Skip baseline entries: they are frozen control samples, no planning needed
            if ref.get('_is_baseline', False):
                return i, {'skipped_baseline': True, 'model': 'N/A', 'reference_file': ref['file']}
            ref_file = ref['file']
            ref_text = _load_text(os.path.join(WORKSPACE_DIR, ref_file))
            rv = review_results.get(i, {})

            if 'error' in rv:
                return i, {'error': f"跳过 (对应审稿失败)"}

            # Select planning model by probability weight
            sel = random.choices(range(len(planning_models)), weights=pm_probs, k=1)[0]
            pm = planning_models[sel]

            # Provider pool for planning (no concurrency limit)
            _plan_pool = pm.get('_pool')
            if _plan_pool:
                _sel_p = random.choice(_plan_pool)
                pm = dict(pm)
                pm['model'] = _sel_p['model']
                pm['provider'] = _sel_p['provider']

            # Paper order (per-entry override, fallback to global my_position)
            _pos = ref.get('position', _my_position)
            if _pos == 'A':
                pa, pb, my_label = ws_text, ref_text, 'A'
            elif _pos == 'B':
                pa, pb, my_label = ref_text, ws_text, 'B'
            else:
                if random.random() < 0.5:
                    pa, pb, my_label = ws_text, ref_text, 'A'
                else:
                    pa, pb, my_label = ref_text, ws_text, 'B'

            # Conditionally include level history (workspace evolution + review feedback)
            history_section = ''
            if _hist_prob > 0 and random.random() < _hist_prob:
                _level_model = ref.get('review_model', '')
                _hist = _preloaded_histories.get((ref_file, _level_model), [])
                if _hist:
                    _hp = []
                    _hp.append(f"== 该关卡的历史迭代记录（共{len(_hist)}轮历史）==")
                    _hp.append(f"以下是我方工作区在之前{len(_hist)}轮迭代中，针对同一参考论文（{ref_file[:40]}...）和同一审稿模型（{_level_model}）获得的审稿反馈历史。")
                    _hp.append("参考工作区在各轮中保持不变，因此不重复展示。历史中不包含之前规划模型的输出，仅包含工作区版本和审稿反馈。\n")
                    for _h in _hist:
                        _hp.append(f"--- 第{_h['run_num']}轮工作区版本 ---")
                        _hp.append(_h['workspace'])
                        _hp.append(f"\n--- 第{_h['run_num']}轮审稿反馈 (我方位置: 论文{_h['my_position']}) ---")
                        if _h.get('review_thinking'):
                            _th_h = _h['review_thinking'][:8000]
                            _hp.append(f"审稿思维链:\n{_th_h}\n")
                        _hp.append(_h['review_content'])
                        _hp.append("")
                    _hp.append(f"--- 当前版本（第{run_num}轮，即上方论文{my_label}）---")
                    _hp.append("这是最新的正在被优化的版本。")
                    _hp.append(f"\n--- 当前审稿反馈 ---")
                    _hp.append("见下方审稿对比意见部分。\n")
                    history_section = '\n'.join(_hp)
                    print(f"[AUTO-REVIEW] 规划 {i+1}: 注入{len(_hist)}轮历史 (关卡: {ref_file[:25]}+{_level_model})", flush=True)

            # Randomly choose Chinese or English planning prompt (English mode only)
            _use_zh_planning = _planning_zh_prob > 0 and random.random() < _planning_zh_prob
            _active_planning_tmpl = _planning_zh_tmpl if (_use_zh_planning and _planning_zh_tmpl) else planning_prompt_tmpl

            # Include both thinking chain and content for the planning model ("mind reading")
            _review_text = ''
            if rv.get('thinking'):
                _review_text += f"=== 审稿模型的内部思维链（读心材料）===\n{rv['thinking']}\n\n"
            _review_text += f"=== 审稿模型的正式输出 ===\n{rv.get('content', '')}"
            prompt = (_active_planning_tmpl
                      .replace('{paper_a}', pa)
                      .replace('{paper_b}', pb)
                      .replace('{review_result}', _review_text)
                      .replace('{my_paper_original}', full_paper_text)
                      .replace('{my_workspace_label}', my_label)
                      .replace('{history_section}', history_section))

            msgs = [{"role": "user", "content": prompt}]

            try:
                content, _thinking, model_used, payload = _make_api_call(
                    pm['model'], msgs, include_thinking=False,
                    api_url_override=pm.get('api_url'),
                    api_key_override=pm.get('api_key'),
                    clean_model_override=pm.get('clean_model'),
                    provider_name=pm.get('provider')
                )

                _save_json(os.path.join(planning_dir, f'{i+1}_payload.json'), payload)

                result_data = {
                    'index': i + 1, 'reference_file': ref_file,
                    'model': model_used, 'content': content,
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                _save_json(os.path.join(planning_dir, f'{i+1}_result.json'), result_data)
                _save_text(
                    os.path.join(planning_dir, f'{i+1}_result.txt'),
                    f"规划模型: {model_used}\n参考论文: {ref_file}\n"
                    f"时间: {result_data['timestamp']}\n\n{content}\n"
                )

                _emit(f"规划 [{i+1}/{n}] {ref_file[:30]}... ({model_used})", content)

                print(f"[AUTO-REVIEW] 规划 {i+1}/{n} 完成 (model={model_used})", flush=True)
                return i, result_data

            except Exception as e:
                traceback.print_exc()
                err = {'index': i + 1, 'error': str(e), 'model': pm['model']}
                _save_json(os.path.join(planning_dir, f'{i+1}_error.json'), err)
                _emit(f"规划 [{i+1}/{n}] 失败 ({pm['model']})", str(e))
                print(f"[AUTO-REVIEW] 规划 {i+1}/{n} 失败: {e}", flush=True)
                return i, err

        if not _planning_agg_mode:
            with ThreadPoolExecutor(max_workers=n or 1) as pool:
                futures = {pool.submit(_do_planning, i, _stagger_delay=0): i for idx, i in enumerate(range(n))}
                for f in as_completed(futures):
                    idx, res = f.result()
                    planning_results[idx] = res

            output_parts.append(f"\n\n=== 规划阶段 ({len(planning_models)}个候选模型) ===\n")
            for i in range(n):
                r = planning_results.get(i, {})
                if 'error' in r:
                    output_parts.append(f"\n--- [{i+1}/{n}] ---\n失败: {r['error']}\n")
                else:
                    output_parts.append(
                        f"\n--- [{i+1}/{n}] {r.get('reference_file', '?')} (模型: {r['model']}) ---\n"
                        f"\n规划正文:\n{r['content']}\n"
                    )

    # Compute summary statistics
    _review_ok = sum(1 for r in review_results.values() if 'error' not in r)
    _review_fail = n - _review_ok
    _plan_ok = sum(1 for r in planning_results.values() if 'error' not in r) if planning_models else 0
    _plan_fail = (len(planning_results) - _plan_ok) if planning_models else 0
    _total_emitted = _review_ok + _review_fail + _plan_ok + _plan_fail

    # Build win rate statistics for this round
    _wins = sum(1 for r in review_results.values() if r.get('we_won') is True)
    _losses = sum(1 for r in review_results.values() if r.get('we_won') is False)
    _unparsed = sum(1 for r in review_results.values() if 'error' not in r and not r.get('skipped') and r.get('judgment') is None)
    _round_wr = f"{_wins}/{_wins + _losses}" if (_wins + _losses) > 0 else '0/0'
    _round_pct = f"{_wins / (_wins + _losses) * 100:.0f}%" if (_wins + _losses) > 0 else 'N/A'
    # Per-model breakdown
    _model_stats_str = ''
    _model_wl = {}
    for r in review_results.values():
        if r.get('we_won') is not None and not r.get('skipped'):
            _rm = r.get('model', '?')
            _model_wl.setdefault(_rm, [0, 0])
            _model_wl[_rm][0 if r['we_won'] else 1] += 1
    if _model_wl:
        _model_parts = [f"{m}: {w}胜{l}负" for m, (w, l) in _model_wl.items()]
        _model_stats_str = ' | '.join(_model_parts)
    _stats_line = (
        f"本轮胜率: {_round_pct} ({_round_wr})"
        f"{f', 跳过{_skipped_count}关卡' if _skipped_count > 0 else ''}"
        f"{f', {_unparsed}个未解析' if _unparsed > 0 else ''}"
        f"{f' | 按模型: {_model_stats_str}' if _model_stats_str else ''}"
    )

    if _stream_mode:
        # Stream mode: individual bubbles already emitted, return short summary
        return (
            f"第{run_num}次自动审稿完成。\n"
            f"{_stats_line}\n"
            f"审稿: {_review_ok}/{n} 成功{f', {_review_fail}个失败' if _review_fail else ''}。"
            f"{'规划: ' + str(_plan_ok) + '/' + str(n) + ' 成功' + (f', {_plan_fail}个失败' if _plan_fail else '') + '。' if planning_models else '规划: 跳过（未配置）。'}\n"
            f"共{_total_emitted}个详细结果已逐个返回到对话中。"
            f"文件: 自动审稿工作区/第{run_num}次审稿/ 和 第{run_num}次规划/"
        )
    else:
        # Legacy mode: return full content as one string
        output_parts.append(
            f"\n{'='*60}\n"
            f"详细结果已保存到:\n"
            f"  审稿:自动审稿工作区/{os.path.basename(review_dir)}/\n"
        )
        if planning_models:
            output_parts.append(f"  规划: 自动审稿工作区/{os.path.basename(planning_dir)}/\n")
        output_parts.append(f"{_stats_line}\n")
        output_parts.append(f"{'='*60}\n")
        return '\n'.join(output_parts)
