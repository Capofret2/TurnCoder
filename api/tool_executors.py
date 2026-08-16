"""Local tool executors for ChatApp-intercepted tool calls.

Provides a registry of tool executors that run locally in ChatApp
instead of being delegated to Claude Code. Each executor is a pure
function that takes tool input and returns a ToolResult.

The unified pipeline in cc_accept.py handles all common concerns
(dedup, ordering, status management, queue advancement) and routes
to executors based on the registry.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

from .platform_shell import detached_kwargs, kill_process_tree, shell_argv
 


def _prepare_autoread_content(file_path, content=None):
    """Read a file and format as numbered lines for autoread bubbles, with 100k token truncation.

    Uses the same estimation formula as execute_read: est_tokens = len(content) / 3.
    When the numbered content exceeds 100k tokens (~300k chars), truncates by line and appends
    a system-reminder annotation.

    Args:
        file_path: Absolute path to the file.
        content: If provided, use this content instead of reading from disk.

    Returns:
        tuple: (numbered_content: str, total_lines: int, was_truncated: bool)
        Returns (None, 0, False) if the file cannot be read.
    """
    if content is None:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            return None, 0, False
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    numbered = ''.join(f'{i+1}\t{line}' for i, line in enumerate(lines))
    est_tokens = len(numbered) / 3
    if est_tokens <= 100000:
        return numbered, total_lines, False
    # Truncate to ~300k characters by line
    truncated_parts = []
    char_count = 0
    max_chars = 300000
    for i, line in enumerate(lines):
        formatted = f'{i+1}\t{line}'
        if char_count + len(formatted) > max_chars:
            break
        truncated_parts.append(formatted)
        char_count += len(formatted)
    truncated_content = ''.join(truncated_parts)
    truncated_content += f'\n\n<system-reminder>\nFile exceeds 100k token limit and was not fully read. Total lines in file: {total_lines}. Use Read tool with offset/limit parameters to access specific sections.\n</system-reminder>'
    return truncated_content, total_lines, True


class ToolResult:
    """Standardized result from a local tool executor.

    Attributes:
        content: The tool result text (will be wrapped in Tool Result/Error format by pipeline).
        summary: Short summary for the bubble.
        is_error: If True, bubble uses "Tool Error" prefix; if False, uses "Tool Result".
        part_status: Override for content_part status. None = auto ('adopted' for success, 'failed' for error).
        _image_data: Base64-encoded image data (set by Read executor for image files).
        _image_mime: MIME type of the image (e.g. 'image/png').
        _image_path: Original file path of the image.
    """
    __slots__ = ('content', 'summary', 'is_error', 'part_status', '_image_data', '_image_mime', '_image_path', '_read_file_path', 'execution_time_s')

    def __init__(self, content, summary, is_error=False, part_status=None):
        self.content = content
        self.summary = summary
        self.is_error = is_error
        self.part_status = part_status
        self._image_data = None
        self._image_mime = None
        self._image_path = None
        self._read_file_path = None
        self.execution_time_s = None


class ToolReject(Exception):
    """Raised when an executor wants to reject adoption (keep tool pending).

    Use for transient failures like proxy connectivity issues where
    the user might want to retry after fixing the problem.
    The pipeline catches this, reverts UI status to 'pending', and shows a toast.
    """
    def __init__(self, message):
        self.message = message
        super().__init__(message)


# Registry: tool_name -> executor_function(tool_input, settings, cache_dir) -> ToolResult | None
LOCAL_EXECUTORS = {}

# Fallback registry: executors that only run when CC returns "No such tool" error
FALLBACK_EXECUTORS = {}


def register_executor(tool_name, fallback_only=False):
    """Decorator to register a local tool executor.

    There used to be a `setting_check` parameter here (a global_settings key or a
    predicate) plus an EXECUTOR_SETTINGS dict holding it. Both are gone, and the
    parameter is gone rather than merely unused on purpose.

    The same accident happened three times: enable_tool_simulate gating
    Read/Write/Edit/Bash, the two enable_custom_webfetch* flags gating WebFetch,
    enable_custom_websearch gating WebSearch. In every case a false or absent key
    made tool_accept.py fall through to 「不支持的工具」, and since the Claude Code
    CLI direct connection was removed there is no second branch for a gate to
    select — the only thing it could do was switch a tool off and report a reason
    unrelated to the real one. On a fresh install those keys do not exist at all.

    Passing setting_check= now raises TypeError at import time. That is louder and
    earlier than a comment, which is the point.

    Args:
        tool_name: The CC tool name (e.g. 'WebSearch', 'WebFetch').
        fallback_only: If True, executor is only invoked when CC returns 'No such tool' error,
                       not in the normal interception pipeline. Used for tools removed in newer CC versions.
    """
    def decorator(func):
        if fallback_only:
            FALLBACK_EXECUTORS[tool_name] = func
        else:
            LOCAL_EXECUTORS[tool_name] = func
        return func
    return decorator


# 确定性的客户端错误。403 重试三次只会拿到三次 403，唯一的效果是把时间预算烧掉：
# 实测 r.jina.ai 对 google.com 回 403，三次尝试花了 7 秒。429 不在此列（配额会随
# 时间恢复），408/425 也不在（超时与过早都值得重试），5xx 同理 —— Jina 的 503 必须
# 继续享受重试。
_NO_RETRY_STATUS = frozenset({400, 401, 402, 403, 404, 405, 406, 410, 451})


def _retry_request(func, description, max_retries=3, initial_delay=2, max_delay=30,
                   max_429_retries=None, max_total_s=90):
    """Retry a network request with exponential backoff until HTTP 200.

    The default used to be max_retries=999. With 1.5x backoff capped at 30s that
    is roughly eight hours of spinning, which is what a caller that forgot to
    pass a limit actually got. max_total_s is the real guard: retry counts cannot
    be converted into wall time (the backoff curve decides that), so the deadline
    is enforced directly and every call site inherits it.

    Args:
        func: Callable that makes the request and returns a response object.
        description: Human-readable label for log messages.
        max_retries: Maximum retry attempts.
        initial_delay: Initial delay in seconds between retries.
        max_delay: Maximum delay in seconds (cap for exponential growth).
        max_429_retries: If set, abort after seeing HTTP 429 this many times (e.g. 1 = abort on 2nd 429).
        max_total_s: Wall-clock budget for the whole retry sequence. None disables it.

    Returns:
        The response object once status_code == 200.

    Raises:
        The last exception if all retries are exhausted or the budget runs out.
    """
    delay = initial_delay
    last_error = None
    _429_count = 0
    _deadline = (time.time() + max_total_s) if max_total_s else None
    for attempt in range(max_retries + 1):
        _fatal = False  # 本次响应确定性失败，重试无意义
        try:
            resp = func()
            if hasattr(resp, 'status_code') and resp.status_code == 200:
                if attempt > 0:
                    print(f'[RETRY] {description}: succeeded on attempt {attempt + 1}', flush=True)
                return resp
            elif hasattr(resp, 'status_code'):
                last_error = Exception(f'HTTP {resp.status_code}')
                if resp.status_code == 429:
                    _429_count += 1
                    if max_429_retries is not None and _429_count > max_429_retries:
                        print(f'[RETRY] {description}: HTTP 429 limit ({max_429_retries}) exceeded after {_429_count} hits, aborting', flush=True)
                        last_error = Exception(f'HTTP 429 after {_429_count} occurrences')
                        _fatal = True
                elif resp.status_code in _NO_RETRY_STATUS:
                    print(f'[RETRY] {description}: HTTP {resp.status_code} is deterministic, not retrying', flush=True)
                    _fatal = True
                if not _fatal and attempt < max_retries:
                    print(f'[RETRY] {description}: HTTP {resp.status_code}, attempt {attempt + 1}, retrying in {delay:.0f}s...', flush=True)
            else:
                return resp  # Non-HTTP response (e.g. mock), return as-is
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                print(f'[RETRY] {description}: {str(e)[:100]}, attempt {attempt + 1}, retrying in {delay:.0f}s...', flush=True)
        # 放弃的唯一出口。原先 429 超限走的是 try 内部 raise，被自己的 except 抓住之后
        # 又睡下去继续重试，日志里的 aborting 从来只是一句空话。
        if _fatal:
            break
        if attempt < max_retries:
            if _deadline is not None and time.time() + delay >= _deadline:
                print(f'[RETRY] {description}: {max_total_s}s budget exhausted after {attempt + 1} attempts, aborting', flush=True)
                break
            time.sleep(delay)
            delay = min(delay * 1.5, max_delay)
    print(f'[RETRY] {description}: giving up (up to {max_retries + 1} attempts)', flush=True)
    raise last_error if last_error else Exception(f'{description}: no successful response')


# ---------------------------------------------------------------------------
#  Proxy resolution
# ---------------------------------------------------------------------------

# 显式直连。requests 默认 trust_env=True，proxies=None 意思是「用环境变量」而不是
# 「不走代理」，想真的直连必须把值置空。
_NO_PROXY = {'http': None, 'https': None}

# 常见本地代理端口，按命中概率排序：7897 是 ClashVerge 当前默认，7890 是旧默认，
# 7891 是 Clash 的另一个混合端口，10809/10808 是 v2rayN，1080 通用 socks，
# 8889 Surge，20171 Netch。
_COMMON_PROXY_PORTS = (7897, 7890, 7891, 10809, 10808, 1080, 8889, 20171)

_PROXY_CACHE = {'value': None, 'ts': 0.0}
_PROXY_CACHE_TTL = 60.0


def _port_alive(host, port, timeout=0.3):
    """True when a TCP connect to host:port completes within timeout."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _detect_proxies(settings, force=False):
    """Resolve the proxy to use for outbound fetches.

    Order: settings['webfetch_proxy'] -> HTTPS/HTTP/ALL_PROXY env vars ->
    TCP scan of _COMMON_PROXY_PORTS on 127.0.0.1.

    Liveness is a listening socket, nothing more. The previous check demanded
    HTTP 200 from a canary URL, which a healthy proxy fails for reasons that
    have nothing to do with reachability (302 to a country page, rate limits) —
    and that false negative was then fed to an unbounded retry loop.

    A hardcoded port is not enough either: the port that is actually listening
    on this machine right now is 7897, while the code asked for 7890.

    Returns:
        A requests-style proxies dict, or None when nothing is listening
        (callers then go direct).
    """
    if not force and _PROXY_CACHE['value'] is not None and time.time() - _PROXY_CACHE['ts'] < _PROXY_CACHE_TTL:
        return _PROXY_CACHE['value'][0]
    resolved = ((settings or {}).get('webfetch_proxy') or '').strip()
    if not resolved:
        for _ev in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
            _v = (os.environ.get(_ev) or '').strip()
            if _v:
                resolved = _v
                break
    if resolved:
        if '://' not in resolved:
            resolved = 'http://' + resolved
        _hp = resolved.split('://', 1)[1].split('/')[0]
        if '@' in _hp:
            _hp = _hp.rsplit('@', 1)[1]
        _h, _, _p = _hp.rpartition(':')
        if _p.isdigit() and not _port_alive(_h or '127.0.0.1', _p, timeout=1.0):
            print(f'[WEBFETCH] Configured proxy {resolved} is not listening, autodetecting instead', flush=True)
            resolved = ''
    if not resolved:
        for _port in _COMMON_PROXY_PORTS:
            if _port_alive('127.0.0.1', _port):
                resolved = f'http://127.0.0.1:{_port}'
                print(f'[WEBFETCH] Autodetected local proxy on port {_port}', flush=True)
                break
    proxies = {'http': resolved, 'https': resolved} if resolved else None
    _PROXY_CACHE['value'] = (proxies,)
    _PROXY_CACHE['ts'] = time.time()
    return proxies


# ---------------------------------------------------------------------------
#  WebSearch executor
# ---------------------------------------------------------------------------

# WebSearch 刻意不带 setting_check。**不要给它加回门控。**
#
# 和 WebFetch 是同一颗地雷：enable_custom_websearch 关掉或在全新安装的机器上根本不存在
# 时，tool_accept.py 里 _use_local 取到假，WebSearch 落到那句「不支持的工具」。Claude
# Code CLI 直连移除后本地 Serper 调用是唯一的搜索实现，门控已无第二条分支可选，于是它
# 唯一的效果就是把工具关掉并且报出一个与原因无关的错误。
@register_executor('WebSearch')
def execute_websearch(tool_input, settings, cache_dir, **kwargs):
    """Execute WebSearch using Google Serper API.

    Returns top-10 search results including answer box, knowledge graph,
    organic results, people also ask, and related searches. The request goes
    direct first and falls back to the detected proxy, so a machine that needs
    a proxy to reach the API is not left without search at all.
    """
    query = tool_input.get('query', '')
    allowed_domains = tool_input.get('allowed_domains', [])
    blocked_domains = tool_input.get('blocked_domains', [])

    if not query:
        return ToolResult('WebSearch query is empty.', 'WebSearch: 空查询', is_error=True)

    import requests
    SERPER_KEY = '0725a2ea9df6eef80e708702e99218b0e502181c'
    q = query
    if allowed_domains:
        q += ' ' + ' OR '.join(f'site:{d}' for d in allowed_domains)

    # 直连优先、代理兜底。写死直连的机器一旦需要代理才能出网就彻底没有搜索，而一律走
    # 代理又会在代理不通时误伤本来能直连的机器；两条都试一遍才两头都不落空。
    _routes = [('direct', _NO_PROXY)]
    _px = _detect_proxies(settings)
    if _px:
        _routes.append(('proxy', _px))
    _errs = []
    resp = None
    for _label, _route in _routes:
        try:
            resp = _retry_request(
                lambda: requests.post(
                    'https://google.serper.dev/search',
                    headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
                    json={'q': q, 'num': 10},
                    timeout=15,
                    proxies=_route
                ),
                f'Serper API via {_label} ({query[:30]})',
                max_retries=2, max_total_s=45
            )
            break
        except Exception as _se:
            _errs.append(f'{_label}: {str(_se)[:150]}')
    if resp is None:
        # 原先这里让 _retry_request 的异常直接穿出执行器，模型看到的是一条框架层报错，
        # 而不是「搜索失败，这两条路线分别死在哪」。
        return ToolResult(
            f'WebSearch failed for query: {query}\nAttempted routes:\n'
            + '\n'.join(f'- {e}' for e in _errs),
            f'WebSearch 失败: {query[:30]}', is_error=True
        )
    data = resp.json()

    lines = [f'Google Search Results for: {query}\n']

    # Answer Box
    ab = data.get('answerBox', {})
    if ab:
        ab_title = ab.get('title', '')
        ab_answer = ab.get('answer', '') or ab.get('snippet', '')
        ab_highlighted = ' '.join(ab.get('snippetHighlighted', []))
        if ab_answer or ab_highlighted:
            lines.append('**Direct Answer:**')
            if ab_title:
                lines.append(f'  {ab_title}')
            if ab_answer:
                lines.append(f'  {ab_answer}')
            if ab_highlighted and ab_highlighted != ab_answer:
                lines.append(f'  Highlighted: {ab_highlighted}')
            if ab.get('link'):
                lines.append(f'  Source: {ab["link"]}')
            lines.append('')

    # Knowledge Graph
    kg = data.get('knowledgeGraph', {})
    if kg.get('title'):
        lines.append(f'**Knowledge Graph: {kg["title"]}**')
        if kg.get('type'):
            lines.append(f'  Type: {kg["type"]}')
        if kg.get('description'):
            lines.append(f'  {kg["description"]}')
        if kg.get('descriptionSource'):
            lines.append(f'  Source: {kg["descriptionSource"]}')
        if kg.get('descriptionLink'):
            lines.append(f'  Link: {kg["descriptionLink"]}')
        for attr in ['Born', 'Died', 'Founded', 'Headquarters', 'CEO', 'Height',
                      'Weight', 'Nationality', 'Awards', 'Education', 'Spouse', 'Children']:
            if kg.get(attr):
                lines.append(f'  {attr}: {kg[attr]}')
        lines.append('')

    # Organic results
    count = 0
    for r in data.get('organic', [])[:10]:
        title = r.get('title', 'No title')
        link = r.get('link', '')
        snippet = r.get('snippet', '')
        date = r.get('date', '')
        if blocked_domains and any(d in link for d in blocked_domains):
            continue
        count += 1
        date_str = f' ({date})' if date else ''
        lines.append(f'{count}. [{title}]({link}){date_str}')
        if snippet:
            lines.append(f'   {snippet}')
        sitelinks = r.get('sitelinks', [])
        if sitelinks:
            for sl in sitelinks[:3]:
                lines.append(f'   → [{sl.get("title", "?")}]({sl.get("link", "")})')
        lines.append('')
    if count == 0:
        lines.append('No results found.')

    # People Also Ask
    paa = data.get('peopleAlsoAsk', [])
    if paa:
        lines.append('**People Also Ask:**')
        for pq in paa[:5]:
            lines.append(f'- **{pq.get("question", "")}**')
            if pq.get('snippet'):
                lines.append(f'  {pq["snippet"]}')
        lines.append('')

    # Related Searches
    rs = data.get('relatedSearches', [])
    if rs:
        lines.append('**Related Searches:** ' + ', '.join(rq.get('query', '') for rq in rs[:8]))
        lines.append('')

    result_text = '\n'.join(lines)
    result_text += '\n\n这些返回的条目的目的仅仅是给你提供url，以便你可能在后续气泡调用的多个webfetch。禁止向用户提及这些条目的内容作为汇报的结果，通常你的工作还远远没有完成，你只能向用户提供webfetch返回的结果。'
    print(f'[WEBSEARCH] Serper: query="{query}", results={count}', flush=True)
    return ToolResult(result_text, f'WebSearch: {query[:50]}')


# ---------------------------------------------------------------------------
#  WebFetch executor
# ---------------------------------------------------------------------------

# WebFetch 刻意不带 setting_check。**不要给它加回门控。**
#
# 两个 enable_custom_webfetch* 开关都关掉时 _use_local 取到假，WebFetch 整个落到
# tool_accept.py 末尾那句「不支持的工具」——和 enable_tool_simulate 门控四个核心
# 执行器时同一种死法（见本文件下方那段注释）。Claude Code CLI 直连移除后本地执行是
# WebFetch 唯一的路径，门控已无第二条分支可选，而全新安装的机器上这两个键根本不存在。
# 开关现在只影响抓取路径的先后顺序。
@register_executor('WebFetch')
def execute_webfetch(tool_input, settings, cache_dir, **kwargs):
    """Execute WebFetch locally, trying several fetch paths in order.

    Pipeline:
    1. Cache check (15min TTL).
    2. Proxy resolution via _detect_proxies (setting -> env -> port scan).
       Local URLs bypass the proxy entirely.
    3. PDF detection -> download (proxy first, then direct) + PyMuPDF.
    4. HTML paths, each with its own retry cap and wall-clock budget: Jina Reader
       and direct HTTP (order follows enable_custom_webfetch_jina), then
       Playwright when installed. The first path yielding >=1KB wins.
    5. Truncate at 100KB, cache when >1KB.

    Never returns None. When every path fails it returns an error ToolResult
    listing what was tried and why, so the real cause is visible instead of
    being reported as an unsupported tool.
    """
    url = tool_input.get('url', '')
    prompt = tool_input.get('prompt', '')

    if not url:
        return ToolResult('WebFetch URL is empty.', 'WebFetch: 空URL', is_error=True)
    if not url.startswith('http'):
        url = 'https://' + url

    # 只决定 Jina 与直连谁先试，不再决定是否执行。原先还有个 pw_mode 变量，赋值之后
    # 没有任何分支读它——Playwright 路径从来只看「前面的路径有没有拿到内容」。
    jina_mode = settings.get('enable_custom_webfetch_jina', False)

    # Cache check (15 min TTL)
    os.makedirs(cache_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w\-.]', '_', url.replace('https://', '').replace('http://', ''))[:120]
    cache_path = os.path.join(cache_dir, f'{safe_name}.md')
    content = ''
    title = ''
    cached = False
    # 在 cached 分支外初始化：命中缓存时下面的块整个跳过，而空缓存文件会一路走到
    # 末尾的错误分支并引用这两个名字。
    errors = []
    proxies = None

    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 900:
            with open(cache_path, 'r', encoding='utf-8') as f:
                content = f.read()
            cached = True
            title = safe_name
            print(f'[WEBFETCH] Cache hit: {url[:80]} (age={age:.0f}s)', flush=True)

    if not cached:
        # 代理解析。原先这里是「requests.get('https://www.google.com') 必须回 200」，
        # 且一个 retry 参数都没传 —— 于是吃到 max_retries=999 的默认值。写死的 7890
        # 与实际监听的端口不一致时，这个循环按 1.5 倍退避、单次上限 30 秒，能空转八个
        # 小时，这就是「代理可达但 WebFetch 卡住数小时」的全部来源。
        _host = url.split('://', 1)[-1].split('/')[0].split('@')[-1].split(':')[0].lower()
        if (_host in ('localhost', '127.0.0.1', '::1', '0.0.0.0')
                or _host.startswith('192.168.') or _host.startswith('10.')
                or _host.endswith('.local')):
            print(f'[WEBFETCH] {_host} is local, bypassing proxy', flush=True)
        else:
            proxies = _detect_proxies(settings)
            if proxies:
                print(f'[WEBFETCH] Proxy: {proxies["https"]}', flush=True)
            else:
                errors.append('proxy: no local proxy port is listening, went direct')
                print('[WEBFETCH] No local proxy detected, going direct', flush=True)

        # PDF detection。arXiv 的现代链接不带扩展名（/pdf/1706.03762），实测它因此走进
        # HTML 分支：requests 把 PDF 字节按文本解码得到 66 万字符，长度判据反而认为内容
        # 充足，于是 97.7KB 的二进制噪声被当作正文返回并写进缓存。
        _ul = url.lower().rstrip('/')
        is_pdf = _ul.endswith('.pdf') or 'application/pdf' in _ul or '/pdf/' in _ul
        if is_pdf:
            pdf_content, pdf_note = _fetch_pdf(url, proxies=proxies)
            if pdf_content:
                content = pdf_content
                title = pdf_note
            else:
                # 猜错不致命：`/pdf/` 这个线索可能命中一个普通页面，所以失败之后复位并
                # 继续走 HTML 路径，而不是像原先那样直接返回错误。
                is_pdf = False
                errors.append(f'PDF: {pdf_note}')
                print(f'[WEBFETCH] PDF path failed ({pdf_note}), falling back to HTML paths', flush=True)

        # 有序路径表。每条路径自带重试上限与时间预算，任何一条卡住都不会拖住整个工具
        # 调用；一条失败只记一行原因，继续下一条。第一条拿到**可用**内容就停 —— 判据
        # 是 _unusable_reason 而不是字符数，否则 example.com 这种本来就只有一百多字符
        # 的完整页面会被当成失败，白跑一次 Jina 而且永远进不了缓存。
        if not is_pdf and not content:
            def _try_jina():
                _jc = _fetch_jina_fallback(url, proxies=proxies, errors_out=errors)
                # Jina 的 markdown 头部自带真实标题，比 safe_name（清洗过的 URL）好读。
                _jt = re.search(r'^Title:\s*(.+)$', _jc[:500], re.MULTILINE)
                return (_jt.group(1).strip() if _jt else safe_name), _jc

            def _try_direct():
                return _fetch_http_fallback(url, proxies=proxies, errors_out=errors)

            def _try_playwright():
                from playwright.sync_api import sync_playwright  # noqa: F401
                return _fetch_with_playwright(url, settings, proxies=proxies)

            paths = ([('Jina Reader', _try_jina), ('Direct HTTP', _try_direct)] if jina_mode
                     else [('Direct HTTP', _try_direct), ('Jina Reader', _try_jina)])
            paths.append(('Playwright', _try_playwright))

            for _pname, _pfn in paths:
                _err_mark = len(errors)
                try:
                    _pt, _pc = _pfn()
                except ToolReject:
                    raise
                except ImportError:
                    errors.append(f'{_pname}: not installed')
                    print(f'[WEBFETCH] {_pname} not installed, skipping', flush=True)
                    continue
                except Exception as _pe:
                    errors.append(f'{_pname}: {str(_pe)[:200]}')
                    print(f'[WEBFETCH] {_pname} failed: {str(_pe)[:200]}', flush=True)
                    continue
                _reason = _unusable_reason(_pc)
                if _reason:
                    # 路径自己已经解释过就不重复记：它掌握原始 HTML 长度，理由更准确。
                    if len(errors) == _err_mark:
                        errors.append(f'{_pname}: {_reason}')
                    print(f'[WEBFETCH] {_pname}: {_reason}, trying next path', flush=True)
                    continue
                content = _pc
                title = _pt or safe_name
                print(f'[WEBFETCH] {_pname} OK: {len(content)} chars', flush=True)
                break

        # 截断与写缓存对所有路径生效。原先这两步嵌在 Playwright 的 try 里，于是 Jina
        # 命中时缓存从不落盘，15 分钟 TTL 等于没有。
        if len(content) > 100000:
            content = content[:100000] + f'\n\n[Content truncated at 100KB ({len(content)} total chars)]'

        # 走到这里的内容已经过 _unusable_reason 筛查，所以「>1KB 才缓存」那道防错误页
        # 的门可以撤掉 —— 它的副作用是短页面永远不进缓存，每次调用都重抓一遍。
        if content:
            with open(cache_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f'[WEBFETCH] OK: url={url[:80]}, title={str(title)[:50]}, size={len(content) / 1024:.1f}KB', flush=True)

    if content:
        kb = len(content) / 1024
        cache_note = ' (cached)' if cached else ''
        result_text = f'Fetched: {url}\nTitle: {title}\nSize: {kb:.1f}KB{cache_note}\nPrompt: {prompt}\n\n{content}'
        return ToolResult(result_text, f'WebFetch: {title[:50]}')

    # 所有路径都失败。这里曾经是 return None，而 None 会落到 tool_accept.py 末尾的
    # 「不支持的工具」——把一次网络失败伪装成工具缺失，模型看到的错误与真实原因无关。
    _diag = '\n'.join(f'- {e}' for e in (errors or ['no fetch path produced content']))
    return ToolResult(
        f'WebFetch failed for {url}\n'
        f'Proxy: {proxies["https"] if proxies else "none (direct)"}\n'
        f'Attempted paths:\n{_diag}',
        f'WebFetch 失败: {url[:40]}', is_error=True
    )


# ---------------------------------------------------------------------------
#  Internal helper functions for WebFetch
# ---------------------------------------------------------------------------

# 明确属于「机器人墙」的措辞。刻意不含 captcha 这类可能出现在正常文章里的词，判定时
# 还要求文档很短，避免把一篇讲验证码的文章误判成验证码页面。
_BLOCK_MARKERS = (
    'just a moment', 'checking your browser', 'enable javascript and cookies',
    'performing security verification', 'verify you are human',
    'attention required', 'access denied', 'unusual traffic from your computer',
)


def _looks_walled(text):
    """True when text reads like a bot wall or challenge page."""
    _low = (text or '').lower()
    return any(_m in _low for _m in _BLOCK_MARKERS)


def _unusable_reason(text, raw_len=None):
    """Explain why `text` cannot serve as page content, or '' when it can.

    Length is the wrong criterion and the old `>= 1024 chars` bar was wrong in
    both directions: example.com's entire body is ~140 characters, so a complete
    fetch counted as a failure — it cost a second network round trip and never
    reached the cache — while a 2KB Cloudflare interstitial sails past 1024 and
    passes as content.

    A wall states what it is. A JS shell gives itself away by ratio instead: a
    couple hundred characters of text carved out of tens of KB of markup.

    Args:
        text: Extracted plain text.
        raw_len: Length of the source HTML when the caller has it. Without it
            the ratio test is skipped.

    Returns:
        A short human-readable reason, or '' when the text is usable.
    """
    if not text or not text.strip():
        return 'empty response'
    if len(text) < 4096 and _looks_walled(text[:2000]):
        return f'bot wall or challenge page ({len(text)} chars)'
    if len(text) >= 1024:
        return ''
    if raw_len and raw_len > 20000 and len(text) / raw_len < 0.02:
        return f'looks JS-rendered ({len(text)} chars of text out of {raw_len // 1024}KB of HTML)'
    return ''


def _html_to_text(html):
    """Reduce an HTML document to readable plain text.

    Prefers lxml: it drops script/style subtrees and keeps block boundaries as
    newlines. The regex reduction below is the fallback when lxml is missing —
    it collapses the whole page into a single line, which is readable but much
    worse. bs4 and html2text are not assumed to be installed.
    """
    if not html:
        return ''
    try:
        from lxml import html as _lxml_html
        _doc = _lxml_html.fromstring(html)
        for _bad in _doc.xpath('//script | //style | //noscript | //template | //svg | //iframe'):
            _parent = _bad.getparent()
            if _parent is not None:
                _parent.remove(_bad)
        _lines = [_ln.strip() for _ln in _doc.text_content().splitlines()]
        _text = '\n'.join(_ln for _ln in _lines if _ln)
        if _text:
            return _text
    except Exception as _lx_err:
        print(f'[WEBFETCH] lxml extraction failed ({_lx_err}), using regex reduction', flush=True)
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'&#\d+;', '', html)
    return re.sub(r'\s+', ' ', html).strip()


def _ensure_fitz():
    """Import PyMuPDF, installing it into this interpreter on first need.

    The local Read executor and the PDF fetch paths both need it, and both used
    to carry their own handling. The fetch side's version gave up and returned
    resp.text — PDF bytes decoded as text, 660k characters of noise for the
    arXiv paper this was measured on, long enough to clear every length-based
    check and land in the model's context as if it were the document.

    sys.executable -m pip, never a bare `pip`: the pip on PATH need not belong
    to the interpreter running the app, and a package installed into the wrong
    environment fails the next import with a message that says nothing about
    where it went.

    Returns:
        The fitz module.

    Raises:
        ImportError when it is neither importable nor installable.
    """
    try:
        import fitz
        return fitz
    except ImportError:
        pass
    from .platform_shell import CREATE_NO_WINDOW, is_windows
    print(f'[PDF] PyMuPDF not found, auto-installing into {sys.executable}...', flush=True)
    _kw = {'creationflags': CREATE_NO_WINDOW} if is_windows() else {}
    try:
        _r = subprocess.run([sys.executable, '-m', 'pip', 'install', 'pymupdf'],
                            capture_output=True, text=True, timeout=300, **_kw)
    except Exception as e:
        raise ImportError(f'PyMuPDF install into {sys.executable} could not start: {e}')
    if _r.returncode != 0:
        raise ImportError(f'PyMuPDF install into {sys.executable} failed: '
                          f'{(_r.stderr or _r.stdout or "")[-300:]}')
    import importlib
    importlib.invalidate_caches()
    import fitz
    return fitz


def _pdf_bytes_to_text(data):
    """Extract page text from PDF bytes already in memory.

    Args:
        data: Raw PDF bytes.

    Returns:
        (text, '') on success, ('', reason) on failure. A PDF with no text
        layer counts as a failure and says so, because the alternative is
        handing back an empty document that reads like a fetch that worked.
    """
    try:
        fitz = _ensure_fitz()
    except Exception as e:
        return '', str(e)
    try:
        doc = fitz.open(stream=data, filetype='pdf')
        pages = [f'--- Page {i + 1} ---\n{doc[i].get_text()}' for i in range(len(doc))]
        doc.close()
    except Exception as e:
        return '', f'PDF parse failed: {e}'
    text = '\n\n'.join(pages)
    if not text.strip():
        return '', f'PDF has no text layer ({len(pages)} pages, probably scanned images)'
    print(f'[WEBFETCH] PDF extracted: {len(text)} chars, {len(pages)} pages', flush=True)
    return text, ''


def _fetch_pdf(url, proxies=None):
    """Fetch and extract text from a PDF URL.

    Tries the proxy first when one was detected, then a direct connection: a
    proxy that cannot reach this particular host should not also take the
    direct route away. _retry_request raises on any non-200, so there is no
    status check here.

    Args:
        url: Target PDF URL.
        proxies: requests-style proxies dict, or None to go direct only.

    Returns:
        (content, title) on success.
        (None, error_message) on failure.
    """
    import requests
    _routes = [proxies, _NO_PROXY] if proxies else [_NO_PROXY]
    _last = 'no route attempted'
    for _px in _routes:
        try:
            resp = _retry_request(
                lambda: requests.get(url, timeout=30, proxies=_px,
                                   headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}),
                f'PDF download ({url.split("/")[-1][:30]})',
                max_retries=3, max_total_s=120
            )
        except Exception as e:
            _last = str(e)
            print(f'[WEBFETCH] PDF route {"proxy" if _px else "direct"} failed: {_last[:120]}', flush=True)
            continue
        # 原先这里在 ImportError 分支返回 resp.text —— PDF 字节按文本解码后的乱码。
        # 那份「内容」长达几十万字符，因此能通过任何以长度为准的检查，然后被当作正文
        # 送进模型上下文并写入缓存。
        _text, _err = _pdf_bytes_to_text(resp.content)
        if _text:
            return _text, f'PDF: {url.rstrip("/").split("/")[-1]}'
        return None, _err
    return None, _last


def _fetch_with_playwright(url, settings, proxies=None):
    """Fetch page content using Playwright Chromium.

    Args:
        url: Target URL.
        settings: Global settings dict (reads enable_webfetch_headless).
        proxies: requests-style proxies dict. Chromium needs its own
            --proxy-server: the browser has a separate network stack and reads
            neither the requests-level proxies nor the process environment
            (settings.json leaves HTTP_PROXY empty). Without this the one path
            that can render JS is also the one path that cannot reach a blocked
            host — and pages that need Playwright are usually exactly those.

    Returns:
        (title, content) tuple.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _playwright_fetch(_url):
        """Run Playwright in a real OS thread to avoid greenlet/eventlet conflicts."""
        from playwright.sync_api import sync_playwright
        is_headless = settings.get('enable_webfetch_headless', True)
        pws = sync_playwright().start()
        _launch_kw = {
            'headless': is_headless,
            'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        }
        if proxies and proxies.get('https'):
            _launch_kw['proxy'] = {'server': proxies['https']}
        br = pws.chromium.launch(**_launch_kw)
        ctx = br.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(pg)
        except ImportError:
            pass
        try:
            pg.goto(_url, wait_until='networkidle', timeout=30000)
        except Exception:
            try:
                pg.goto(_url, wait_until='domcontentloaded', timeout=20000)
            except Exception as ne:
                for cleanup in [pg.close, ctx.close, br.close, pws.stop]:
                    try:
                        cleanup()
                    except Exception:
                        pass
                raise ne

        # Cookie/consent dismissal
        for selector in [
            'button:has-text("Accept")', 'button:has-text("Accept all")',
            'button:has-text("I agree")', 'button:has-text("Got it")',
            'button:has-text("OK")', 'button:has-text("接受")',
            'button:has-text("同意")', '#onetrust-accept-btn-handler',
            '.cookie-accept', '[data-testid="cookie-policy-manage-dialog-btn-accept"]'
        ]:
            try:
                pg.click(selector, timeout=800)
                pg.wait_for_timeout(300)
                break
            except Exception:
                pass

        # Scroll to trigger lazy loading
        try:
            pg.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            pg.wait_for_timeout(1500)
            pg.evaluate('window.scrollTo(0, 0)')
            pg.wait_for_timeout(500)
        except Exception as e:
            print(f'[WEBFETCH] Scroll failed (page navigated): {e}', flush=True)
            try:
                pg.wait_for_timeout(2000)
            except Exception:
                pass

        # Extract content
        t = ''
        c = ''
        try:
            t = pg.title() or ''
            c = pg.inner_text('body')
        except Exception as e:
            print(f'[WEBFETCH] Content extraction failed: {e}', flush=True)

        # Cloudflare detection. 措辞表复用 _BLOCK_MARKERS：原先这里另有一份四条目的
        # 拷贝，而且大小写敏感，页面文案换个大小写就检测不到。
        if c and len(c) < 500 and _looks_walled(c):
            print(f'[WEBFETCH] Cloudflare challenge detected, waiting up to 15s...', flush=True)
            for wait in range(15):
                try:
                    pg.wait_for_timeout(1000)
                    c = pg.inner_text('body')
                    if len(c) > 500 or not _looks_walled(c):
                        t = pg.title() or t
                        print(f'[WEBFETCH] Cloudflare passed after {wait+1}s, content={len(c)} chars', flush=True)
                        break
                except Exception:
                    break

        for cleanup in [pg.close, ctx.close, br.close, pws.stop]:
            try:
                cleanup()
            except Exception:
                pass
        return t, c

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_playwright_fetch, url)
        return future.result(timeout=60)


def _fetch_http_fallback(url, proxies=None, max_retries=2, errors_out=None):
    """Fetch a page over plain HTTP(S) and reduce it to text.

    This is a first-class path, not only a fallback: with Playwright absent it
    is the one route that actually reaches a site the proxy can see. It used to
    force a direct connection, which cannot work for a blocked host.

    Args:
        url: Target URL.
        proxies: requests-style proxies dict, or None for a direct connection.
        max_retries: Retry budget for the request itself.
        errors_out: Optional list; the failure reason is appended to it so the
            caller can surface it instead of leaving it in the console only.

    Returns:
        (title, content) tuple. ('', '') on failure.
    """
    try:
        import requests
        resp = _retry_request(
            lambda: requests.get(url, timeout=20,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml',
                    'Accept-Language': 'en-US,en;q=0.5'
                },
                proxies=proxies or _NO_PROXY, allow_redirects=True),
            f'Direct HTTP ({url[:50]})',
            max_retries=max_retries, max_total_s=45
        )
        # 内容类型是权威判据，URL 后缀只是猜测：arXiv 的 /pdf/1706.03762 没有扩展名，
        # 于是 PDF 字节走到这里被 resp.text 解码成 66 万字符乱码，而任何以长度为准的
        # 检查都会放它过去。字节已经在手上，就地解析不需要第二次请求。
        _ctype = (resp.headers.get('Content-Type') or '').lower()
        if 'application/pdf' in _ctype or resp.content[:5] == b'%PDF-':
            _ptext, _perr = _pdf_bytes_to_text(resp.content)
            if not _ptext:
                if errors_out is not None:
                    errors_out.append(f'Direct HTTP: PDF at this URL, {_perr}')
                print(f'[WEBFETCH] Direct HTTP: PDF at this URL, {_perr}', flush=True)
                return '', ''
            return f'PDF: {url.rstrip("/").split("/")[-1]}', _ptext
        if _ctype and not any(_t in _ctype for _t in ('text/', 'html', 'xml', 'json', 'javascript')):
            if errors_out is not None:
                errors_out.append(f'Direct HTTP: non-text content type {_ctype.split(";")[0]}')
            print(f'[WEBFETCH] Direct HTTP: non-text content type {_ctype}', flush=True)
            return '', ''
        _raw = resp.text or ''
        _text = _html_to_text(_raw)
        # 这里是全流程唯一同时掌握纯文本长度与原始 HTML 长度的位置，比值能区分「页面
        # 本来就短」和「内容由 JS 渲染」，所以判定放在这里而不是调用方。
        _reason = _unusable_reason(_text, raw_len=len(_raw))
        if _reason:
            if errors_out is not None:
                errors_out.append(f'Direct HTTP: {_reason}')
            print(f'[WEBFETCH] Direct HTTP: {_reason}', flush=True)
            return '', ''
        _title_m = re.search(r'<title[^>]*>(.*?)</title>', _raw, re.IGNORECASE | re.DOTALL)
        return (_title_m.group(1).strip() if _title_m else ''), _text
    except Exception as e:
        if errors_out is not None:
            errors_out.append(f'Direct HTTP: {str(e)[:200]}')
        print(f'[WEBFETCH] Direct HTTP failed: {e}', flush=True)
    return '', ''


def _fetch_jina_fallback(url, proxies=None, errors_out=None):
    """Fetch a page as markdown through Jina Reader.

    The old version first required HTTP 200 from google.com through a hardcoded
    proxy before it would even try. That gate is the same false negative as the
    old proxy check: the socket probe already established that the proxy is
    listening, and a healthy proxy answers that canary with a 302.

    Args:
        url: Target URL.
        proxies: requests-style proxies dict, or None for a direct connection.
        errors_out: Optional list to append the failure reason to.

    Returns:
        Content string, or empty string on failure.
    """
    try:
        import requests
        jina_resp = _retry_request(
            lambda: requests.get(
                f'https://r.jina.ai/{url}',
                headers={'Accept': 'text/markdown', 'User-Agent': 'Mozilla/5.0'},
                timeout=20,
                proxies=proxies or _NO_PROXY
            ),
            f'Jina Reader ({url[:50]})',
            max_retries=2, max_total_s=45
        )
        # 成功日志由调用方的路径循环统一打印。这里再打一遍会让日志里出现两行一模一样的
        # 「Jina Reader OK: N chars」，读起来像同一个 URL 被抓了两次。
        return jina_resp.text or ''
    except Exception as e:
        if errors_out is not None:
            errors_out.append(f'Jina Reader: {str(e)[:200]}')
        print(f'[WEBFETCH] Jina Reader request failed: {e}', flush=True)
    return ''


# ---------------------------------------------------------------------------
#自动审稿 executor
# ---------------------------------------------------------------------------

@register_executor('自动审稿')
def execute_auto_review(tool_input, settings, cache_dir, **kwargs):
    """Execute the automatic paper review pipeline.

    Reads papers from 自动审稿工作区/, pairs user's paper with each reference paper,
    makes API calls for review (with thinking) and planning (without thinking),
    saves all results and payloads to sequentially numbered directories.
    No parameters required. When api/target_sid/tool_use_id are provided,
    individual results are emitted as separate bubbles for real-time visibility.
    """
    from .auto_review import run_auto_review
    try:
        result_text = run_auto_review(
            api=kwargs.get('api'),
            target_sid=kwargs.get('target_sid'),
            tool_use_id=kwargs.get('tool_use_id'),
            force_full=tool_input.get('force_full', False),
            enable_baseline=tool_input.get('enable_baseline', False),
            workspace_dir=tool_input.get('workspace_dir')
        )
        return ToolResult(result_text, '自动审稿完成')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ToolResult(
            f'自动审稿执行失败: {str(e)}\n\n{traceback.format_exc()}',
            '自动审稿失败', is_error=True
        )


# ---------------------------------------------------------------------------
#  单关卡审稿 executor
# ---------------------------------------------------------------------------

@register_executor('单关卡审稿')
def execute_single_checkpoint(tool_input, settings, cache_dir, **kwargs):
    """Run one or more review checkpoints by 1-based index.

    Supports both single (checkpoint_index) and batch (checkpoint_indices) modes.
    In batch mode, checkpoints run in parallel via ThreadPoolExecutor.
    """
    from .auto_review import run_single_checkpoint
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Resolve indices: prefer checkpoint_indices (array), fallback to checkpoint_index (single)
    indices = tool_input.get('checkpoint_indices')
    if indices is None:
        idx = tool_input.get('checkpoint_index')
        if idx is None:
            return ToolResult('缺少 checkpoint_index 或 checkpoint_indices 参数', '缺参数', is_error=True)
        try:
            indices = [int(idx)]
        except (ValueError, TypeError):
            return ToolResult(f'checkpoint_index 必须是整数，收到: {idx!r}', '参数类型错误', is_error=True)
    else:
        if not isinstance(indices, list) or not indices:
            return ToolResult('checkpoint_indices 必须是非空整数数组', '参数格式错误', is_error=True)
        try:
            indices = [int(i) for i in indices]
        except (ValueError, TypeError) as e:
            return ToolResult(f'checkpoint_indices 中包含非整数值: {e}', '参数类型错误', is_error=True)

    _api = kwargs.get('api')
    _sid = kwargs.get('target_sid')
    _tuid = kwargs.get('tool_use_id')
    _ws_dir = tool_input.get('workspace_dir')

    if len(indices) == 1:
        # Single checkpoint: run directly
        try:
            result = run_single_checkpoint(indices[0], api=_api, target_sid=_sid, tool_use_id=_tuid, workspace_dir=_ws_dir)
            return ToolResult(result, f'单关卡 #{indices[0]} 完成')
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ToolResult(f'单关卡审稿执行失败: {str(e)}\n\n{traceback.format_exc()}', '单关卡失败', is_error=True)

    # Batch mode: parallel execution
    results = {}
    def _run_one(idx):
        try:
            return idx, run_single_checkpoint(idx, api=_api, target_sid=_sid, tool_use_id=_tuid, workspace_dir=_ws_dir)
        except Exception as e:
            return idx, f'关卡 #{idx} 失败: {e}'

    with ThreadPoolExecutor(max_workers=min(len(indices), 20)) as pool:
        futures = {pool.submit(_run_one, i): i for i in indices}
        for f in as_completed(futures):
            idx, res = f.result()
            results[idx] = res

    # Assemble summary
    parts = [f'批量单关卡审稿完成 ({len(indices)} 个关卡)']
    for idx in sorted(results.keys()):
        parts.append(f'\n--- 关卡 #{idx} ---\n{results[idx]}')
    return ToolResult('\n'.join(parts), f'批量 {len(indices)} 关卡完成')


# ---------------------------------------------------------------------------
#  压缩 executor
# ---------------------------------------------------------------------------

@register_executor('压缩')
def execute_compress(tool_input, settings, cache_dir, **kwargs):
    """Compress a message by setting it to omitted mode with custom summary text."""
    message_id = tool_input.get('message_id')
    compressed_text = tool_input.get('compressed_text', '')
    if message_id is None:
        return ToolResult('缺少 message_id 参数', '压缩失败', is_error=True)
    try:
        message_id = int(message_id)
    except (ValueError, TypeError):
        return ToolResult(f'message_id 必须是整数，收到: {message_id!r}', '压缩失败', is_error=True)
    if not compressed_text or not compressed_text.strip():
        return ToolResult('compressed_text 不能为空', '压缩失败', is_error=True)
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '压缩失败', is_error=True)
    session = api.sessions.get(target_sid)
    if not session:
        return ToolResult(f'会话 {target_sid} 不存在', '压缩失败', is_error=True)
    target_msg = None
    for m in session.get('conversation_history', []):
        if m.get('id') == message_id:
            target_msg = m
            break
    if target_msg is None:
        return ToolResult(
            f'在当前会话中未找到 ID 为 {message_id} 的气泡。权限限制：只能压缩当前会话的气泡。',
            '压缩失败: ID不存在', is_error=True
        )
    original_len = len(target_msg.get('content', '') or '')
    target_msg['is_omitted'] = True
    target_msg['summary'] = compressed_text.strip()
    api.save_sessions(push_update=True)
    compressed_len = len(compressed_text)
    ratio = (1 - compressed_len / max(original_len, 1)) * 100
    return ToolResult(
        f'已压缩气泡 ID {message_id}:\n- 原始: {original_len} 字符 ({original_len/3000:.1f}k)\n- 压缩后: {compressed_len} 字符 ({compressed_len/3000:.1f}k)\n- 压缩率: {ratio:.0f}%',
        f'压缩 #{message_id} ({ratio:.0f}%)'
    )


# ---------------------------------------------------------------------------
#  展开气泡 executor
# ---------------------------------------------------------------------------

@register_executor('展开气泡')
def execute_expand_bubbles(tool_input, settings, cache_dir, **kwargs):
    """Expand specified message bubbles by restoring them from omitted/collapsed state."""
    message_ids = tool_input.get('message_ids', [])
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '展开失败', is_error=True)
    session = api.sessions.get(target_sid)
    if not session:
        return ToolResult(f'会话 {target_sid} 不存在', '展开失败', is_error=True)
    count = 0
    for m in session.get('conversation_history', []):
        if m.get('id') in message_ids:
            if m.get('is_omitted', False) or m.get('is_collapsed', False):
                m['is_omitted'] = False
                m['is_collapsed'] = False
                count += 1
    api.save_sessions(push_update=True)
    return ToolResult(f'已展开 {count} 条气泡', f'展开了 {count} 条')


# ---------------------------------------------------------------------------
#  CC模拟 executors: Read, Write, Edit, Bash (本地执行)
# ---------------------------------------------------------------------------

# 四个核心执行器刻意不带 setting_check。**不要给它们加回门控。**
#
# 原先是 setting_check='enable_tool_simulate'，而那个开关关掉时 _use_local 取到假，
# Read / Write / Edit / Bash 四个工具全部落到 tool_accept.py 末尾那句「不支持的工具」
# 错误——一点就废掉整个工具系统。Claude Code CLI 直连移除后本地执行是唯一路径，这个
# 门控已无第二条分支可选。
#
# 现在没有任何执行器再带 setting_check。WebSearch 与 WebFetch 是最后两个，它们也因为
# 同一种死法被拆掉了，所以这里不必再解释「为什么相邻的带而这四个不带」——谁都不带。
@register_executor('Read')
def execute_read(tool_input, settings, cache_dir, **kwargs):
    """Local Read executor. No line limit; rejects files > 100k tokens."""
    file_path = tool_input.get('file_path', '')
    offset = tool_input.get('offset')
    limit = tool_input.get('limit')
    if not file_path:
        return ToolResult('file_path is required', 'Read: 缺少路径', is_error=True)
    # Read 防重拦截：如果该文件已有未隐藏/未概括/未过时的有效读取结果，直接拦截
    _r_api = kwargs.get('api')
    _r_sid = kwargs.get('target_sid')
    _r_tuid = kwargs.get('tool_use_id', '')
    _is_system_read = _r_tuid.startswith('toolu_autoread_') or _r_tuid.startswith('toolu_wfread_') or _r_tuid.startswith('toolu_readfb_')
    # enable_partial_read 模式下的去重逻辑替代
    _pr_settings = getattr(_r_api, 'global_settings', {}) if _r_api else {}
    if _r_api and _r_sid and not _is_system_read and _pr_settings.get('enable_partial_read', False) and (offset is not None or limit is not None):
        _pr_session = _r_api.sessions.get(_r_sid, {})
        _pr_state = _pr_session.get('_partial_read_state', {})
        _pr_entry = _pr_state.get(file_path)
        if _pr_entry:
            _pr_visible = _pr_entry.get('visible_lines', set())
            if isinstance(_pr_visible, list):
                _pr_visible = set(_pr_visible)
            # 计算本次 Read 指定范围扩展后的行号集合
            _pr_start = int(offset) if offset else 0
            _pr_end = _pr_start + int(limit) if limit else _pr_start
            _pr_expand_start = max(1, _pr_start + 1 - 10)
            _pr_expand_end = _pr_end + 10  # 上界在实际读取时会被文件行数限制
            _pr_new_lines = set(range(_pr_expand_start, _pr_expand_end + 1))
            # 如果新行集合是当前可见行集合的子集，则拦截（不会带来新可见行）
            if _pr_new_lines.issubset(_pr_visible):
                return ToolResult(
                    f'<system-reminder>\nThis Read was intercepted: the requested range (lines {_pr_start+1}-{_pr_end}) is already within the visible line set for {file_path}. No new lines would be added. Use the existing content from the previous read.\n</system-reminder>',
                    f'Read 部分读入防重: {os.path.basename(file_path)}'
                )
    if _r_api and _r_sid and not _is_system_read and not _pr_settings.get('enable_partial_read', False):
        _r_session = _r_api.sessions.get(_r_sid, {})
        for _dm in _r_session.get('conversation_history', []):
            if not _dm.get('is_tool_result') or _dm.get('is_hidden') or _dm.get('is_omitted') or _dm.get('is_outdated_read'):
                continue
            # 跳过错误/拒绝/拦截结果（不是真正的文件内容）
            _dm_c = _dm.get('content', '')
            if '**Tool Error**' in _dm_c[:50]:
                continue
            _bt_pos = _dm_c.find('```\n')
            if _bt_pos >= 0 and _dm_c[_bt_pos + 4:].lstrip().startswith('<system-reminder>'):
                continue
            if _dm.get('is_auto_read') and _dm.get('auto_read_file') == file_path:
                print(f'[READ DEDUP] Local intercepted: {file_path}', flush=True)
                return ToolResult(
                    f'<system-reminder>\nThis Read was intercepted: the file {file_path} already has a valid, non-hidden read result in this conversation. Re-reading is unnecessary — use the existing content from the previous read.\n</system-reminder>',
                    f'Read 防重拦截: {os.path.basename(file_path)}'
                )
            elif not _dm.get('is_auto_read'):
                # 检查是否是同文件的 Read 工具返回
                _dm_tid = _dm.get('tool_use_id', '')
                for _cm in _r_session.get('conversation_history', []):
                    if _cm.get('content_parts'):
                        for _cp in _cm['content_parts']:
                            if _cp.get('type') == 'tool_use_part':
                                try:
                                    import json as _rj
                                    _td = _rj.loads(_cp['content'])
                                    if _td.get('id') == _dm_tid and _td.get('name') == 'Read' and _td.get('input', {}).get('file_path') == file_path:
                                        print(f'[READ DEDUP] Local intercepted (non-autoread): {file_path}', flush=True)
                                        return ToolResult(
                                            f'<system-reminder>\nThis Read was intercepted: the file {file_path} already has a valid, non-hidden read result in this conversation. Re-reading is unnecessary — use the existing content from the previous read.\n</system-reminder>',
                                            f'Read 防重拦截: {os.path.basename(file_path)}'
                                        )
                                except: pass
    try:
        # 图片文件：以 base64 编码返回，多模态模型可以直接看到
        _img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}
        _ext = os.path.splitext(file_path)[1].lower()
        if _ext in _img_exts:
            import base64 as _rb64
            _size = os.path.getsize(file_path)
            if _size > 5 * 1024 * 1024:
                # Auto-compress large images using PIL
                try:
                    from PIL import Image as _PILImage
                    from io import BytesIO as _BytesIO
                    _pil_img = _PILImage.open(file_path)
                    _compress_buf = _BytesIO()
                    _pil_img.save(_compress_buf, 'JPEG', quality=80)
                    _compressed_bytes = _compress_buf.getvalue()
                    _img_data = _rb64.b64encode(_compressed_bytes).decode('ascii')
                    _mime = 'image/jpeg'
                    print(f'[READ] Image auto-compressed: {_size} -> {len(_compressed_bytes)} bytes', flush=True)
                except ImportError:
                    return ToolResult(
                        '<tool_use_error>Image exceeds 5MB and Pillow is not installed for auto-compression. Install with: pip install Pillow</tool_use_error>',
                        'Read: 需要Pillow', is_error=True
                    )
            else:
                with open(file_path, 'rb') as _imgf:
                    _img_data = _rb64.b64encode(_imgf.read()).decode('ascii')
                _mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp', '.ico': 'image/x-icon'}
                _mime = _mime_map.get(_ext, 'image/png')
            # 返回特殊格式的 ToolResult，pipeline 会识别并注入 multimodal_blocks
            _result = ToolResult(
                f'[Image: {os.path.basename(file_path)}] ({_size/1024:.1f} KB, {_mime})',
                f'Read: {os.path.basename(file_path)} (image)'
            )
            _result._image_data = _img_data
            _result._image_mime = _mime
            _result._image_path = file_path
            return _result
        # Jupyter notebook：提取所有单元格
        if _ext == '.ipynb':
            try:
                import json as _nb_json
                with open(file_path, 'r', encoding='utf-8') as _nbf:
                    nb = _nb_json.load(_nbf)
                cells = nb.get('cells', [])
                parts = []
                for i, cell in enumerate(cells):
                    ctype = cell.get('cell_type', 'unknown')
                    source = ''.join(cell.get('source', []))
                    outputs = ''
                    for out in cell.get('outputs', []):
                        if out.get('text'):
                            outputs += ''.join(out['text'])
                        elif out.get('data', {}).get('text/plain'):
                            outputs += ''.join(out['data']['text/plain'])
                    parts.append(f'--- Cell {i+1} [{ctype}] ---\n{source}')
                    if outputs:
                        parts.append(f'[Output]:\n{outputs}')
                content = '\n\n'.join(parts)
                return ToolResult(content, f'Read: {os.path.basename(file_path)} (notebook, {len(cells)} cells)')
            except Exception as _nbe:
                return ToolResult(f'<tool_use_error>Failed to parse notebook: {str(_nbe)}</tool_use_error>', 'Read: notebook解析失败', is_error=True)
        # PDF 文件：使用 PyMuPDF 提取文本
        if _ext == '.pdf':
            # 装包逻辑连同「裸 pip 会装进别的解释器」那段教训一并移到 _ensure_fitz，因为
            # WebFetch 的 PDF 路径需要同一件事，而它当时的做法是缺依赖就返回原始字节。
            # 同一个需求两份实现，其中一份是错的。
            try:
                fitz = _ensure_fitz()
            except ImportError as _fe:
                return ToolResult(
                    f'<tool_use_error>{_fe}</tool_use_error>',
                    'Read: PyMuPDF安装失败', is_error=True
                )
            try:
                doc = fitz.open(file_path)
                pages_param = tool_input.get('pages', '')
                if pages_param:
                    # 解析页面范围如 "1-5" 或 "3"
                    parts = pages_param.replace(' ', '').split('-')
                    start_page = int(parts[0]) - 1
                    end_page = int(parts[-1]) if len(parts) > 1 else start_page + 1
                else:
                    start_page = 0
                    end_page = min(len(doc), 20)
                texts = [f'--- Page {i+1} ---\n{doc[i].get_text()}' for i in range(start_page, min(end_page, len(doc)))]
                doc.close()
                content = '\n\n'.join(texts)
                return ToolResult(content, f'Read: {os.path.basename(file_path)} (PDF, {end_page-start_page} pages)')
            except Exception as _pdf_err:
                return ToolResult(f'<tool_use_error>PDF read error: {str(_pdf_err)}</tool_use_error>', 'Read: PDF读取失败', is_error=True)
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if not content:
            return ToolResult(
                f'<system-reminder>\nThe file {file_path} exists but is empty (0 bytes).\n</system-reminder>',
                f'Read: {os.path.basename(file_path)} (empty)'
            )
        est_tokens = len(content) / 3
        lines = content.splitlines(keepends=True)
        # Read 优化逻辑：
        # 1. 文件 <= 100k tokens → 无视 offset/limit，全量返回
        # 2. 文件 > 100k tokens 且有 offset/limit → 按参数分段返回
        # 3. 文件 > 100k tokens 且无参数 → 报错
        if est_tokens <= 100000:
            # 如果模型传了 offset/limit 但文件足够小，追加提醒告知参数已被忽略
            _read_override_note = ''
            if offset is not None or limit is not None:
                _read_override_note = f'\n\n<system-reminder>\nThe file was small enough to be read in full. The original offset={offset} and limit={limit} parameters from your request have been discarded. The content above is the complete file.\n</system-reminder>'
            start = 0
            selected = lines
        elif offset is not None or limit is not None:
            start = int(offset) if offset else 0
            end = start + int(limit) if limit else len(lines)
            selected = lines[start:end]
        else:
            return ToolResult(
                f'<tool_use_error>File too large: ~{est_tokens/1000:.0f}k tokens (limit: 100k tokens). Use offset/limit parameters to read a portion.</tool_use_error>',
                'Read: 文件过大', is_error=True
            )
        numbered = ''.join(f'{start + i + 1}\t{line}' for i, line in enumerate(selected))
        if '_read_override_note' in dir() and _read_override_note:
            numbered += _read_override_note
        # 注册文件读取引用（跨会话追踪）
        _r_api = kwargs.get('api')
        _r_sid = kwargs.get('target_sid')
        if _r_api and _r_sid and hasattr(_r_api, '_register_file_read'):
            _r_api._register_file_read(file_path, _r_sid)
        # 标记旧的同文件读取为过时（确保同一文件只有一个有效读取结果）
        if _r_api and _r_sid:
            _rd_session = _r_api.sessions.get(_r_sid, {})
            _rd_tui = {}
            for _rdm in _rd_session.get('conversation_history', []):
                for _rdp in (_rdm.get('content_parts') or []):
                    if _rdp.get('type') == 'tool_use_part':
                        try:
                            import json as _rdj
                            _rdt = _rdj.loads(_rdp['content'])
                            if _rdt.get('id'):
                                _rd_tui[_rdt['id']] = (_rdt.get('name', ''), _rdt.get('input', {}).get('file_path', ''))
                        except Exception:
                            pass
            for _rdm in _rd_session.get('conversation_history', []):
                if _rdm.get('is_tool_result') and not _rdm.get('is_outdated_read'):
                    if _rdm.get('is_auto_read') and _rdm.get('auto_read_file') == file_path:
                        _rdm['is_outdated_read'] = True
                        _rdm['content'] = f"（已省略，概括为：{os.path.basename(file_path)} 的旧版本读取结果，已被更新的读取替代）"
                    elif not _rdm.get('is_auto_read'):
                        _rdinfo = _rd_tui.get(_rdm.get('tool_use_id', ''))
                        if _rdinfo and _rdinfo[0] == 'Read' and _rdinfo[1] == file_path:
                            _rdm['is_outdated_read'] = True
                            _rdm['content'] = f"（已省略，概括为：{os.path.basename(file_path)} 的旧版本读取结果，已被更新的读取替代）"
        # 维护 _partial_read_state（无论 enable_partial_read 开关状态都执行）
        if _r_api and _r_sid:
            _pr_session = _r_api.sessions.get(_r_sid, {})
            _pr_state = _pr_session.setdefault('_partial_read_state', {})
            _pr_entry = _pr_state.get(file_path)
            if _pr_entry and isinstance(_pr_entry.get('visible_lines'), list):
                _pr_entry['visible_lines'] = set(_pr_entry['visible_lines'])
            if not _pr_entry:
                _pr_entry = {'visible_lines': [], 'reference_lines': []}
                _pr_state[file_path] = _pr_entry
            total_lines = len(lines)
            # 转为 set 做集合运算
            _vl_set = set(_pr_entry['visible_lines'])
            if offset is not None or limit is not None:
                # 带 offset/limit 的读取：将指定范围向两侧各扩展 10 行后加入集合
                _off = int(offset) if offset is not None else 0
                _lim = int(limit) if limit is not None else total_lines
                _start_line = _off + 1  # offset 是 0-based 索引，转为 1-based 行号
                _end_line = min(_off + _lim, total_lines)  # 1-based 结束行号
                _expand_start = max(1, _start_line - 10)
                _expand_end = min(total_lines, _end_line + 10)
                _vl_set.update(range(_expand_start, _expand_end + 1))
            else:
                # 不带 offset/limit：全部行号加入集合，等价于全量可见
                _vl_set = set(range(1, total_lines + 1))
            # 强制可见：首 10 行和末 10 行
            _vl_set.update(range(1, min(11, total_lines + 1)))
            _vl_set.update(range(max(1, total_lines - 9), total_lines + 1))
            # 存回为 sorted list，避免 set 类型导致 JSON 序列化崩溃
            _pr_entry['visible_lines'] = sorted(_vl_set)
            # 更新 reference_lines 为当前文件内容
            _pr_entry['reference_lines'] = list(lines)
            # 回溯设置 _read_file_path 到所有已存在的同文件 tool_result bubble
            # (旧 bubble 在本功能添加前创建，缺少该字段，需要补充)
            for _bkm in _pr_session.get('conversation_history', []):
                if _bkm.get('is_tool_result') and not _bkm.get('_read_file_path'):
                    if _bkm.get('auto_read_file') == file_path:
                        _bkm['_read_file_path'] = file_path
                    elif not _bkm.get('is_auto_read'):
                        _bk_tid = _bkm.get('tool_use_id', '')
                        for _bkcm in _pr_session.get('conversation_history', []):
                            if _bkcm.get('content_parts'):
                                for _bkcp in _bkcm['content_parts']:
                                    if _bkcp.get('type') == 'tool_use_part' and _bkcp.get('tool_id') == _bk_tid:
                                        _bk_input = _bkcp.get('tool_input') or {}
                                        if _bkcp.get('tool_name') == 'Read' and _bk_input.get('file_path') == file_path:
                                            _bkm['_read_file_path'] = file_path
        _result = ToolResult(numbered, f'Read: {os.path.basename(file_path)}')
        _result._read_file_path = file_path
        return _result
    except FileNotFoundError:
        return ToolResult(f'<tool_use_error>File not found: {file_path}</tool_use_error>', f'Read: 文件不存在', is_error=True)
    except UnicodeDecodeError:
        _size = os.path.getsize(file_path)
        return ToolResult(f'<tool_use_error>Binary file cannot be read as text: {file_path} ({_size} bytes)</tool_use_error>', 'Read: 二进制文件', is_error=True)
    except Exception as e:
        return ToolResult(f'<tool_use_error>Read error: {str(e)}</tool_use_error>', f'Read: 错误', is_error=True)


@register_executor('Write')
def execute_write(tool_input, settings, cache_dir, **kwargs):
    """Local Write executor."""
    file_path = tool_input.get('file_path', '')
    content = tool_input.get('content', '')
    if not file_path:
        return ToolResult('<tool_use_error>file_path is required</tool_use_error>', 'Write: 缺少路径', is_error=True)
    try:
        _dir = os.path.dirname(file_path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return ToolResult(f'File created successfully at: {file_path}', f'Write: {os.path.basename(file_path)}')
    except Exception as e:
        return ToolResult(f'<tool_use_error>Write error: {str(e)}</tool_use_error>', f'Write: 错误', is_error=True)


@register_executor('Edit')
def execute_edit(tool_input, settings, cache_dir, **kwargs):
    """Local Edit executor."""
    file_path = tool_input.get('file_path', '')
    old_string = tool_input.get('old_string', '')
    new_string = tool_input.get('new_string', '')
    replace_all = tool_input.get('replace_all', False)
    if not file_path:
        return ToolResult('file_path is required', 'Edit: 缺少路径', is_error=True)
    if not old_string:
        return ToolResult('old_string is required', 'Edit: 缺少old_string', is_error=True)
    if old_string == new_string:
        return ToolResult('old_string and new_string must be different', 'Edit: 相同内容', is_error=True)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return ToolResult(f'<tool_use_error>File not found: {file_path}. Create it with Write tool first.</tool_use_error>', 'Edit: 文件不存在', is_error=True)
    if old_string not in content:
        # Edit 失败时触发自动读取，让模型看到当前文件内容以便重试
        api = kwargs.get('api')
        target_sid = kwargs.get('target_sid')
        if api and target_sid:
            _fail_session = api.sessions.get(target_sid, {})
            _fail_numbered, _fail_total_lines, _fail_truncated = _prepare_autoread_content(file_path, content=content)
            if _fail_numbered is None:
                _fail_numbered = ''
            bt = chr(96) * 3
            _fail_ar_id = f'toolu_autoread_{int(time.time())}'
            _fail_ar_bubble = {
                'id': api._next_id(),
                'role': 'user',
                'content': f'**Tool Result** (tool: {_fail_ar_id})\n\n{bt}\n{_fail_numbered}\n{bt}',
                'summary': f'自动读取 (Edit失败): {os.path.basename(file_path)}',
                'is_omitted': False,
                'is_collapsed': False,
                'is_tool_result': True,
                'is_auto_read': True,
                'auto_read_file': file_path,
                'tool_use_id': _fail_ar_id,
                'auto_read_trigger_id': kwargs.get('tool_use_id', ''),
            }
            # 标记旧的同文件读取为过时（autoread 和手动 Read 都标记）
            _fail_tui = {}
            for _ftm in _fail_session.get('conversation_history', []):
                for _ftp in (_ftm.get('content_parts') or []):
                    if _ftp.get('type') == 'tool_use_part':
                        try:
                            import json as _ftj
                            _ftd = _ftj.loads(_ftp['content'])
                            if _ftd.get('id'):
                                _fail_tui[_ftd['id']] = (_ftd.get('name', ''), _ftd.get('input', {}).get('file_path', ''))
                        except Exception:
                            pass
            for _m in _fail_session.get('conversation_history', []):
                if _m.get('is_tool_result') and not _m.get('is_outdated_read'):
                    if _m.get('is_auto_read') and _m.get('auto_read_file') == file_path:
                        _m['is_outdated_read'] = True
                        _m['content'] = f"（已省略，概括为：{os.path.basename(file_path)} 的旧版本读取结果，已被更新的读取替代）"
                    elif not _m.get('is_auto_read'):
                        _finfo = _fail_tui.get(_m.get('tool_use_id', ''))
                        if _finfo and _finfo[0] == 'Read' and _finfo[1] == file_path:
                            _m['is_outdated_read'] = True
                            _m['content'] = f"（已省略，概括为：{os.path.basename(file_path)} 的旧版本读取结果，已被更新的读取替代）"
            _fail_session.setdefault('conversation_history', []).append(_fail_ar_bubble)
            print(f'[EDIT FAIL AUTOREAD] Created autoread bubble for {os.path.basename(file_path)}, session has {len(_fail_session.get("conversation_history", []))} messages now', flush=True)
        return ToolResult(
            f'<tool_use_error>String to replace not found in file.\nString: {old_string[:200]}</tool_use_error>',
            'Edit: 未找到', is_error=True
        )
    if replace_all:
        new_content = content.replace(old_string, new_string)
        count = content.count(old_string)
    else:
        if content.count(old_string) > 1:
            return ToolResult(
                f'<tool_use_error>old_string appears {content.count(old_string)} times in file. Use replace_all or provide more context.</tool_use_error>',
                'Edit: 不唯一', is_error=True
            )
        new_content = content.replace(old_string, new_string, 1)
        count = 1
    # 备份
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if api and target_sid:
        session = api.sessions.get(target_sid, {})
        backups = session.setdefault('code_backups', {})
        tool_use_id = kwargs.get('tool_use_id', '')
        if tool_use_id:
            backups[tool_use_id] = {'path': file_path, 'content': content}
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    # 仅注册当前会话对该文件的引用，但不更新 mtime/hash 缓存。
    # 这确保 accept_tool 中的 _check_registered_file_changes 能找到这个文件
    # 并检测到 mtime 变化，从而创建 autoread 气泡。
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if api and target_sid and hasattr(api, 'file_read_registry'):
        _entry = api.file_read_registry.setdefault(file_path, {'sessions': set(), 'last_access': 0})
        if isinstance(_entry, set):
            _entry = {'sessions': _entry, 'last_access': time.time()}
            api.file_read_registry[file_path] = _entry
        _entry['sessions'].add(target_sid)
        _entry['last_access'] = time.time()
        # 不更新 mtime/hash，让 _check 能检测到差异
    if replace_all:
        return ToolResult(f'The file {file_path} has been updated. All occurrences were successfully replaced.', f'Edit: {os.path.basename(file_path)}')
    return ToolResult(f'The file {file_path} has been updated successfully.', f'Edit: {os.path.basename(file_path)}')


@register_executor('Bash')
def execute_bash(tool_input, settings, cache_dir, **kwargs):
    """Local Bash executor.

    All commands run as detached nohup processes (survive ChatApp restart).
    'Foreground' mode polls until completion; 'background' mode returns immediately.
    """
    command = tool_input.get('command', '')
    timeout = tool_input.get('timeout', 120000)
    run_bg = tool_input.get('run_in_background', False)
    if not command:
        return ToolResult('command is required', 'Bash: 缺少命令', is_error=True)
    timeout_sec = int(timeout) / 1000
    try:
        _argv, _shell = shell_argv(command, tool_input.get('shell', ''))
    except ValueError as e:
        return ToolResult(str(e), 'Bash: 未知 shell', is_error=True)
    # 输出文件在 Popen 调用时就被打开，因此不存在「子进程已开始写而文件尚未建立」
    # 的竞态。目录取 tempfile.gettempdir() 而不是写死 /tmp：后者在 Windows 上不
    # 存在，症状是每一条命令都在 open() 阶段抛 FileNotFoundError，而那个异常里没
    # 有任何线索指向平台。
    _run_id = uuid.uuid4().hex[:8]
    _out_file = os.path.join(tempfile.gettempdir(), f'chatapp_bash_{_run_id}.out')
    _t_start = time.time()
    try:
        _out_fd = open(_out_file, 'w')
        proc = subprocess.Popen(
            _argv,
            stdout=_out_fd, stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            **detached_kwargs()  # 脱离进程组，ChatApp 重启不会杀死它
        )
    except Exception as e:
        return ToolResult(f'Failed to launch command: {str(e)}', 'Bash: 启动失败', is_error=True)
    _pid = proc.pid
    if run_bg:
        return ToolResult(
            f'Command started in background (pid={_pid}, shell={_shell}):\n{command[:200]}\nOutput file: {_out_file}',
            f'{_shell} (bg): {command[:30]}'
        )
    # 前台模式：等待进程结束
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        if not kill_process_tree(_pid):
            try:
                proc.kill()
            except Exception:
                pass
        _out_fd.close()
        _partial = ''
        try:
            with open(_out_file, 'r', encoding='utf-8', errors='replace') as _of:
                _partial = _of.read()
        except Exception:
            pass
        try:
            os.remove(_out_file)
        except Exception:
            pass
        return ToolResult(
            f'Command timed out after {timeout_sec}s (pid={_pid})\n\nPartial output:\n{_partial[-2000:]}',
            'Bash: 超时', is_error=True
        )
    _out_fd.close()
    # 读取输出
    output = ''
    try:
        with open(_out_file, 'r', encoding='utf-8', errors='replace') as _of:
            output = _of.read()
    except Exception as e:
        output = f'(failed to read output: {e})'
    # 截断过长输出（模拟 CC 的输出限制，防止上下文爆炸）
    MAX_BASH_OUTPUT = 10000
    if len(output) > MAX_BASH_OUTPUT:
        _half = MAX_BASH_OUTPUT // 2
        _total = len(output)
        output = f"{output[:_half]}\n\n... [Output truncated: {_total} chars total, showing first and last {_half} chars] ...\n\n{output[-_half:]}"
    if proc.returncode != 0:
        output += f'\n\nExit code: {proc.returncode}'
    if not output.strip():
        # 真实墙钟耗时。原先是 timeout_sec 减去「now - out_file.mtime」，而 mtime 就是
        # 刚才，于是那个差值恒等于 timeout_sec，让「瞬间结束且无输出」显示成「跑满了
        # 超时」。这个数字本身有诊断价值：退出码 0、无输出、耗时不到 0.1 秒，基本就是
        # 进程根本没执行命令。
        _real_elapsed = time.time() - _t_start
        output = (f'(no output)\n\nCommand completed with exit code {proc.returncode}.\n'
                  f'PID: {_pid}\nShell: {_shell}\nElapsed: {_real_elapsed:.2f}s\n'
                  f'Command: {command[:200]}')
    # 清理临时文件
    try:
        os.remove(_out_file)
    except Exception:
        pass
    return ToolResult(output, f'{_shell}: {command[:40]}')


# ---------------------------------------------------------------------------
#  触发器 executor
# ---------------------------------------------------------------------------

@register_executor('触发器')
def execute_trigger(tool_input, settings, cache_dir, **kwargs):
    """Set up timed triggers. All triggers are in-memory only (lost on restart)."""
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '触发器失败', is_error=True)
    session = api.sessions.get(target_sid)
    if not session:
        return ToolResult('会话不存在', '触发器失败', is_error=True)
    if not hasattr(api, '_triggers'):
        api._triggers = []

    if tool_input.get('cancel'):
        remaining = []
        cancelled = 0
        for t in api._triggers:
            if t['sid'] == target_sid:
                try:
                    t['timer'].cancel()
                except Exception:
                    pass
                cancelled += 1
            else:
                remaining.append(t)
        api._triggers = remaining
        if session.get('trigger_autopilot'):
            session['trigger_autopilot'] = False
            session['autopilot_active'] = False
            session['autopilot_turns_left'] = 0
        return ToolResult(f'已取消 {cancelled} 个待触发的触发器', '触发器已取消')

    mode = tool_input.get('mode', 'timer')
    import datetime
    now = datetime.datetime.now()

    if mode == 'timer':
        interval = tool_input.get('interval_minutes')
        count = tool_input.get('count', 1)
        if not interval or interval <= 0:
            return ToolResult('interval_minutes 必须为正数', '触发器参数错误', is_error=True)
        fire_times = [now + datetime.timedelta(minutes=float(interval) * i) for i in range(1, int(count) + 1)]
        for ft in fire_times:
            api._add_trigger(target_sid, ft)
        times_str = ', '.join(ft.strftime('%H:%M:%S') for ft in fire_times)
        return ToolResult(f'定时器已设置：{count} 次触发\n触发时间点: {times_str}', f'触发器: {interval}min × {count}')

    elif mode == 'alarm':
        alarm_time = tool_input.get('alarm_time', '')
        if not alarm_time:
            return ToolResult('alarm_time 不能为空（格式 HH:MM）', '触发器参数错误', is_error=True)
        try:
            hour, minute = map(int, alarm_time.split(':'))
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time <= now:
                target_time += datetime.timedelta(days=1)
        except (ValueError, TypeError):
            return ToolResult(f'alarm_time 格式错误: {alarm_time}，应为 HH:MM', '触发器参数错误', is_error=True)
        api._add_trigger(target_sid, target_time)
        delay_min = (target_time - now).total_seconds() / 60
        return ToolResult(f'闹钟已设置：将在 {alarm_time} 触发（约 {delay_min:.0f} 分钟后）', f'触发器: {alarm_time}')

    return ToolResult(f'未知模式: {mode}，支持 timer 或 alarm', '触发器参数错误', is_error=True)


# ---------------------------------------------------------------------------
#  Grep fallback executor (for CC versions that removed the Grep tool)
# ---------------------------------------------------------------------------

_GREP_NA = object()  # Sentinel: binary not installed, try next level
_TYPE_EXTS = {'py':['*.py'],'js':['*.js','*.mjs'],'ts':['*.ts','*.tsx'],'java':['*.java'],'go':['*.go'],'rust':['*.rs'],'c':['*.c','*.h'],'cpp':['*.cpp','*.cc','*.hpp'],'html':['*.html','*.htm'],'css':['*.css'],'json':['*.json'],'yaml':['*.yaml','*.yml'],'md':['*.md'],'txt':['*.txt'],'sh':['*.sh'],'ruby':['*.rb'],'php':['*.php']}

def _grep_rg(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml):
    import subprocess
    cmd = ['rg','--no-heading','--color=never']
    if mode == 'files_with_matches': cmd.append('-l')
    elif mode == 'count': cmd.append('-c')
    if ci: cmd.append('-i')
    if ln and mode == 'content': cmd.append('-n')
    if ml: cmd.extend(['-U','--multiline-dotall'])
    if cb: cmd.extend(['-B',str(int(cb))])
    if ca: cmd.extend(['-A',str(int(ca))])
    if ctx: cmd.extend(['-C',str(int(ctx))])
    if glb: cmd.extend(['--glob',glb])
    if ft: cmd.extend(['--type',ft])
    cmd.extend(['--',pattern,path])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return '' if (r.returncode == 1 and not r.stdout) else r.stdout
    except FileNotFoundError: return _GREP_NA
    except: return _GREP_NA

def _grep_system(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml):
    import subprocess
    cmd = ['grep','-r','--color=never']
    if mode == 'files_with_matches': cmd.append('-l')
    elif mode == 'count': cmd.append('-c')
    if ci: cmd.append('-i')
    if ln and mode == 'content': cmd.append('-n')
    if cb: cmd.extend(['-B',str(int(cb))])
    if ca: cmd.extend(['-A',str(int(ca))])
    if ctx: cmd.extend(['-C',str(int(ctx))])
    if glb: cmd.extend(['--include',glb])
    if ft and ft in _TYPE_EXTS:
        for ext in _TYPE_EXTS[ft]: cmd.extend(['--include',ext])
    for d in ['.git','__pycache__','node_modules','venv','.venv']: cmd.extend(['--exclude-dir',d])
    if ml: cmd.extend(['-P','-z'])
    cmd.extend(['--',pattern,path])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return '' if (r.returncode == 1 and not r.stdout) else r.stdout
    except FileNotFoundError: return _GREP_NA
    except: return _GREP_NA

def _grep_python(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml):
    import fnmatch
    flags = re.IGNORECASE if ci else 0
    if ml: flags |= re.DOTALL
    try: regex = re.compile(pattern, flags)
    except re.error as e: return f'(invalid regex: {e})'
    exts = _TYPE_EXTS.get(ft, []) if ft else ([glb] if glb else [])
    files = []
    if os.path.isfile(path): files = [path]
    else:
        for root, dirs, fnames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ['.git','__pycache__','node_modules','venv','.venv']]
            for f in fnames:
                fp = os.path.join(root, f)
                if exts and not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(fp, g) for g in exts): continue
                files.append(fp)
    c_ctx = int(ctx or 0); c_b = int(cb or c_ctx); c_a = int(ca or c_ctx)
    results = []
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f: content = f.read()
        except: continue
        lines = content.split('\n')
        idxs = [i for i, line in enumerate(lines) if regex.search(line)]
        if not idxs: continue
        if mode == 'files_with_matches': results.append(fp)
        elif mode == 'count': results.append(f'{fp}:{len(idxs)}')
        else:
            printed = set()
            for mi in idxs:
                s, e = max(0, mi - c_b), min(len(lines), mi + c_a + 1)
                if printed and s > 0 and (s-1) not in printed: results.append('--')
                for li in range(s, e):
                    if li not in printed:
                        printed.add(li)
                        sep = ':' if li in idxs else '-'
                        results.append(f'{fp}:{li+1}{sep}{lines[li]}' if ln else f'{fp}:{lines[li]}')
    return '\n'.join(results)


@register_executor('Grep', fallback_only=True)
def execute_grep(tool_input, settings, cache_dir, **kwargs):
    """Local Grep fallback with multi-level chain: rg -> grep -> Python re."""
    pattern = tool_input.get('pattern', '')
    path = tool_input.get('path') or os.getcwd()
    mode = tool_input.get('output_mode', 'files_with_matches')
    glb = tool_input.get('glob', '')
    ci = tool_input.get('-i', False)
    cb = tool_input.get('-B', 0)
    ca = tool_input.get('-A', 0)
    ctx = tool_input.get('-C') or tool_input.get('context', 0)
    ln = tool_input.get('-n', True)
    ft = tool_input.get('type', '')
    head_limit = tool_input.get('head_limit', 250)
    offset = tool_input.get('offset', 0)
    ml = tool_input.get('multiline', False)
    if not pattern:
        return ToolResult('(empty pattern)', 'Grep: empty pattern', is_error=True)
    # Fallback chain: rg -> grep -> python
    output = _grep_rg(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml)
    level = 'rg'
    if output is _GREP_NA:
        output = _grep_system(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml)
        level = 'grep'
    if output is _GREP_NA:
        output = _grep_python(pattern, path, mode, ci, cb, ca, ctx, ln, glb, ft, ml)
        level = 'python'
    if not output:
        return ToolResult('(no matches found)', f'Grep: no matches for {pattern[:30]}')
    # Apply head_limit and offset
    if (head_limit and int(head_limit) > 0) or (offset and int(offset) > 0):
        lines = output.split('\n')
        if offset and int(offset) > 0: lines = lines[int(offset):]
        if head_limit and int(head_limit) > 0 and len(lines) > int(head_limit): lines = lines[:int(head_limit)]
        output = '\n'.join(lines)
    output = output.rstrip('\n')
    print(f'[FALLBACK] Grep OK ({level}): pattern={pattern[:30]}, path={path}, mode={mode}', flush=True)
    return ToolResult(output if output else '(no matches found)', f'Grep: {pattern[:40]}')


# ---------------------------------------------------------------------------
#  Glob fallback executor (for CC versions that removed the Glob tool)
# ---------------------------------------------------------------------------

@register_executor('命名会话')
def execute_rename_session(tool_input, settings, cache_dir, **kwargs):
    """Rename the current session to a meaningful name."""
    name = tool_input.get('name', '')
    if not name or not name.strip():
        return ToolResult('name 参数不能为空', '命名失败', is_error=True)
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '命名失败', is_error=True)
    if target_sid not in api.sessions:
        return ToolResult(f'会话 {target_sid} 不存在', '命名失败', is_error=True)
    old_name = api.sessions[target_sid].get('name', '')
    api.sessions[target_sid]['name'] = name.strip()
    api.save_sessions(push_update=True)
    return ToolResult(
        f'会话已命名: "{old_name}" → "{name.strip()}"',
        f'命名: {name.strip()[:20]}'
    )


@register_executor('Glob', fallback_only=True)
def execute_glob(tool_input, settings, cache_dir, **kwargs):
    """Local glob fallback for Glob tool.

    Mirrors the original Glob tool behavior: matches files by glob pattern,
    returns paths sorted by modification time (newest first).
    """
    import glob as glob_module
    pattern = tool_input.get('pattern', '')
    path = tool_input.get('path') or os.getcwd()

    if not pattern:
        return ToolResult('(empty pattern)', 'Glob: empty pattern', is_error=True)
    if path in ('undefined', 'null', 'None', ''):
        path = os.getcwd()

    search_pattern = os.path.join(path, pattern)
    try:
        matches = glob_module.glob(search_pattern, recursive=True)
        matches.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
        if not matches:
            return ToolResult('(no matches found)', f'Glob: no matches for {pattern[:30]}')
        result = '\n'.join(matches)
        print(f'[FALLBACK] Glob OK: pattern={pattern[:30]}, path={path}, matches={len(matches)}', flush=True)
        return ToolResult(result, f'Glob: {len(matches)} files')
    except Exception as e:
        return ToolResult(f'(Glob fallback error: {str(e)})', 'Glob: error', is_error=True)


# ---------------------------------------------------------------------------
#  创建子会话 executor
# ---------------------------------------------------------------------------

@register_executor('创建子会话')
def execute_create_child_session(tool_input, settings, cache_dir, **kwargs):
    """Create a child session that blocks the parent until it ends.

    Creates a new session with the given first_message, inherits parent's model selection,
    starts autopilot with max_steps, and blocks the parent's executor thread until
    the child calls 结束子会话 or autopilot naturally ends.
    """
    import threading
    import uuid as _uuid
    import random as _random

    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')

    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '创建子会话失败', is_error=True)

    first_message = tool_input.get('first_message', '')
    max_steps = tool_input.get('max_steps', 10)

    # Validate first_message
    if not first_message or not first_message.strip():
        return ToolResult('first_message 不能为空', '创建子会话失败', is_error=True)

    # Check if current session is already a child session (prevent nesting)
    parent_session = api.sessions.get(target_sid, {})
    if parent_session.get('_is_child_session'):
        return ToolResult('不允许在子会话中嵌套创建子会话', '嵌套子会话禁止', is_error=True)

    # Create child session manually (avoid switching current_session_id)
    child_sid = str(_uuid.uuid4())
    child_session = {
        "name": f"子会话: {first_message[:20]}",
        "order": time.time(),
        "conversation_history": [],
        "message_queue": [],
        "is_paused": False,
        "is_processing": False,
        "active_threads": 0,
        "current_chain_steps": 0,
        "max_steps": 1,
        "autopilot_active": True,
        "autopilot_turns_left": int(max_steps),
        "autopilot_turns": int(max_steps),
        "autopilot_total_steps": 0,
        "autopilot_max_k": 8.0,
        "autopilot_model": None,
        "autopilot_env_model": None,
        "code_config": {},
        "code_backups": {},
        "deleted_msg_ids": [],
        "bound_cc_id": None,
        "spending": {"total": 0, "by_model": {}},
        "_theme_hue": _random.randint(0, 359),
        "_deep_think_level": 0,
        "_selected_models": list(parent_session.get('_selected_models', [])),
        "_draft_text": "",
        "_is_child_session": True,
        "_parent_sid": target_sid,
        "_deep_think_active": False,
    }

    # Determine model for autopilot
    models = child_session['_selected_models']
    try:
        from .provider_routes import strip_composite
        llm_model = next((m for m in models if not strip_composite(m).startswith('[ARC3]')), None)
    except Exception:
        llm_model = None
    if not llm_model:
        llm_model = models[0] if models else api.config.get("MODEL_NAME", "")
    child_session['autopilot_model'] = llm_model

    # Add first message to conversation history
    first_msg_obj = api._make_msg("user", first_message)
    child_session['conversation_history'].append(first_msg_obj)

    # Register child session
    api.sessions[child_sid] = child_session

    # Set up synchronization
    event = threading.Event()
    if not hasattr(api, '_child_session_events'):
        api._child_session_events = {}
    api._child_session_events[child_sid] = {
        'event': event,
        'parent_sid': target_sid,
        'result': None
    }

    api.save_sessions()

    # Start API thread for the child session
    try:
        api.start_api_thread(child_sid, model_name=llm_model)
    except Exception as e:
        print(f'[CHILD SESSION] start_api_thread failed: {e}', flush=True)
    print(f'[CHILD SESSION] Created {child_sid[:8]} for parent {target_sid[:8]}, model={llm_model}, max_steps={max_steps}', flush=True)

    # Spawn background daemon thread to wait for child end and inject tool_result.
    # We CANNOT block the HTTP thread (would cause ReadTimeout), so we return None
    # and let the pipeline fall through. The background thread injects result later.
    _tool_use_id = kwargs.get('tool_use_id', '')

    def _child_waiter():
        _start = time.time()
        while True:
            if event.wait(timeout=1):
                break  # Event set by 结束子会话 executor or _notify_child_ended
            # Grace period: don't check natural end in first 3 seconds
            if time.time() - _start < 3:
                continue
            child_sess = api.sessions.get(child_sid, {})
            if not child_sess:
                break  # Session was removed
            if not child_sess.get('autopilot_active') and not child_sess.get('is_processing'):
                # Natural autopilot end detected via polling
                if child_sid in getattr(api, '_child_session_events', {}):
                    _entry = api._child_session_events[child_sid]
                    if not _entry['event'].is_set():
                        _entry['result'] = {
                            'child_sid': child_sid,
                            'steps': child_sess.get('autopilot_total_steps', 0)
                        }
                        _entry['event'].set()
                break

        # Retrieve result and inject tool_result bubble into parent session
        result_info = api._child_session_events.pop(child_sid, {})
        result_data = result_info.get('result') or {}
        child_sess = api.sessions.get(child_sid, {})
        steps = result_data.get('steps', child_sess.get('autopilot_total_steps', 0))
        _child_summary = result_data.get('summary', '')

        parent_sess = api.sessions.get(target_sid)
        if parent_sess and _tool_use_id:
            bt = chr(96) * 3
            _sum_line = f'\n返回摘要: {_child_summary}' if _child_summary else ''
            bubble = {
                'id': api._next_id(),
                'role': 'user',
                'content': f'**Tool Result** (tool: {_tool_use_id})\n\n{bt}\n子会话已结束\nID: {child_sid}\n执行步数: {steps}{_sum_line}\n{bt}',
                'summary': f'子会话完成 ({steps}步)',
                'is_omitted': False,
                'is_collapsed': True,
                'is_tool_result': True,
                'tool_use_id': _tool_use_id,
            }
            parent_sess['conversation_history'].append(bubble)
            # Update the content_part status from 'executing' to 'adopted'
            for _pm in parent_sess.get('conversation_history', []):
                for _pp in (_pm.get('content_parts') or []):
                    if _pp.get('type') == 'tool_use_part':
                        try:
                            import json as _wj
                            _ptd = _wj.loads(_pp['content'])
                            if _ptd.get('id') == _tool_use_id:
                                _pp['status'] = 'adopted'
                        except Exception:
                            pass
            api.save_sessions(push_update=True)
            # Advance parent's autopilot queue if applicable
            try:
                api._continue_autopilot_tool_queue(parent_sess, target_sid)
            except Exception as _e:
                print(f'[CHILD SESSION] _continue_autopilot_tool_queue error: {_e}', flush=True)

        print(f'[CHILD SESSION] Parent {target_sid[:8]} unblocked, child {child_sid[:8]} completed with {steps} steps', flush=True)

    import threading as _cw_threading
    _waiter = _cw_threading.Thread(target=_child_waiter, daemon=True)
    _waiter.start()

    # Return None on purpose: this executor is listed in tool_accept.py's
    # _ASYNC_LOCAL_EXECUTORS, so the pipeline marks the part 'executing' and stops
    # there. No immediate tool_result is created; the waiter thread above injects
    # one when the child session ends.
    #
    # The old comment here said None fell through to a CC queue path — that path
    # went away with the Claude Code CLI direct connection. Following it now leads
    # nowhere, while the real handoff is in the other file.
    return None


# ---------------------------------------------------------------------------
#  结束子会话 executor
# ---------------------------------------------------------------------------

@register_executor('结束子会话')
def execute_end_child_session(tool_input, settings, cache_dir, **kwargs):
    """End the current child session and unblock the parent.

    Stops autopilot in the current session, signals the parent session
    to resume, and returns a summary of execution.
    """
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')

    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '结束子会话失败', is_error=True)

    session = api.sessions.get(target_sid, {})

    # Check if this is actually a child session
    if not session.get('_is_child_session'):
        return ToolResult('当前会话不是子会话，无法调用结束子会话', '非子会话错误', is_error=True)

    # Stop autopilot
    session['autopilot_active'] = False
    session['autopilot_turns_left'] = 0
    session['_deep_think_active'] = False

    steps = session.get('autopilot_total_steps', 0)
    summary = tool_input.get('summary', '')

    # Signal parent to unblock
    if hasattr(api, '_child_session_events') and target_sid in api._child_session_events:
        entry = api._child_session_events[target_sid]
        entry['result'] = {
            'child_sid': target_sid,
            'steps': steps,
            'summary': summary
        }
        entry['event'].set()
        print(f'[CHILD SESSION] End signal sent from {target_sid[:8]}, steps={steps}, summary={summary[:50]}', flush=True)

    api.save_sessions()

    return ToolResult(
        f'子会话已结束，已通知父会话恢复执行\n执行步数: {steps}\n返回摘要: {summary[:200] if summary else "无"}',
        f'结束子会话 ({steps}步)'
    )


# ---------------------------------------------------------------------------
#  申请审批 executor
# ---------------------------------------------------------------------------

@register_executor('申请审批')
def execute_approval(tool_input, settings, cache_dir, **kwargs):
    """Execute approval tool: returns approval confirmation."""
    description = tool_input.get('description', '')
    return ToolResult(f"用户已批准审批：{description}", "审批通过")


# ---------------------------------------------------------------------------
#  缩减读取 executor
# ---------------------------------------------------------------------------

@register_executor('缩减读取')
def execute_shrink_read(tool_input, settings, cache_dir, **kwargs):
    """Remove lines from the visible line set of a file's partial read state.

    Actual removal range is smaller than specified: upper and lower 10 lines are kept as redundancy.
    First 10 and last 10 lines of the file are never removed.
    """
    file_path = tool_input.get('file_path', '')
    offset = tool_input.get('offset')
    limit = tool_input.get('limit')
    if not file_path:
        return ToolResult('file_path is required', '缩减读取: 缺少路径', is_error=True)
    if offset is None or limit is None:
        return ToolResult('offset and limit are required', '缩减读取: 缺少参数', is_error=True)
    try:
        offset = int(offset)
        limit = int(limit)
    except (ValueError, TypeError):
        return ToolResult('offset and limit must be integers', '缩减读取: 参数类型错误', is_error=True)
    api = kwargs.get('api')
    target_sid = kwargs.get('target_sid')
    if not api or not target_sid:
        return ToolResult('内部错误：缺少 API 或会话上下文', '缩减读取失败', is_error=True)
    session = api.sessions.get(target_sid, {})
    _pr_state = session.get('_partial_read_state', {})
    _pr_entry = _pr_state.get(file_path)
    if not _pr_entry:
        return ToolResult(f'No partial read state for {file_path}', '缩减读取: 无状态', is_error=True)
    visible = _pr_entry.get('visible_lines', set())
    if isinstance(visible, list):
        visible = set(visible)
        _pr_entry['visible_lines'] = visible
    # 实际移除范围比指定的小 20 行（上下各保留 10 行冗余）
    actual_start = offset + 10
    actual_end = offset + limit - 1 - 10
    if actual_start > actual_end:
        return ToolResult(f'Range too small to shrink (need > 20 lines)', '缩减读取: 范围过小', is_error=True)
    lines_to_remove = set(range(actual_start, actual_end + 1))
    # 获取文件总行数用于首尾保护
    ref_lines = _pr_entry.get('reference_lines', [])
    total_lines = len(ref_lines) if ref_lines else 0
    if total_lines > 0:
        # 首 10 行和末 10 行不可被移除
        protected = set(range(1, min(11, total_lines + 1)))
        protected.update(range(max(1, total_lines - 9), total_lines + 1))
        lines_to_remove -= protected
    removed_count = len(lines_to_remove & visible)
    visible -= lines_to_remove
    _pr_entry['visible_lines'] = sorted(visible)
    return ToolResult(
        f'已从 {file_path} 的可见行集合中移除 {removed_count} 行\n当前可见行总数: {len(visible)}',
        f'缩减读取: -{removed_count}行, 剩余{len(visible)}行'
    )
