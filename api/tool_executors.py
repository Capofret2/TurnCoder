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

# Settings check: tool_name -> setting_key_string | callable(settings_dict) -> bool
EXECUTOR_SETTINGS = {}

# Fallback registry: executors that only run when CC returns "No such tool" error
FALLBACK_EXECUTORS = {}


def register_executor(tool_name, setting_check=None, fallback_only=False):
    """Decorator to register a local tool executor.

    Args:
        tool_name: The CC tool name (e.g. 'WebSearch', 'WebFetch').
        setting_check: Either a string (global_settings key) or a callable(settings) -> bool
                       that determines whether to use the local executor.
        fallback_only: If True, executor is only invoked when CC returns 'No such tool' error,
                       not in the normal interception pipeline. Used for tools removed in newer CC versions.
    """
    def decorator(func):
        if fallback_only:
            FALLBACK_EXECUTORS[tool_name] = func
        else:
            LOCAL_EXECUTORS[tool_name] = func
            if setting_check is not None:
                EXECUTOR_SETTINGS[tool_name] = setting_check
        return func
    return decorator


def _retry_request(func, description, max_retries=999, initial_delay=2, max_delay=30, max_429_retries=None):
    """Retry a network request with exponential backoff until HTTP 200.

    Args:
        func: Callable that makes the request and returns a response object.
        description: Human-readable label for log messages.
        max_retries: Maximum retry attempts (default 999 ≈ persistent).
        initial_delay: Initial delay in seconds between retries.
        max_delay: Maximum delay in seconds (cap for exponential growth).
        max_429_retries: If set, abort after seeing HTTP 429 this many times (e.g. 1 = abort on 2nd 429).

    Returns:
        The response object once status_code == 200.

    Raises:
        The last exception if all retries are exhausted.
    """
    delay = initial_delay
    last_error = None
    _429_count = 0
    for attempt in range(max_retries + 1):
        try:
            resp = func()
            if hasattr(resp, 'status_code') and resp.status_code == 200:
                if attempt > 0:
                    print(f'[RETRY] {description}: succeeded on attempt {attempt + 1}', flush=True)
                return resp
            elif hasattr(resp, 'status_code'):
                if resp.status_code == 429:
                    _429_count += 1
                    if max_429_retries is not None and _429_count > max_429_retries:
                        print(f'[RETRY] {description}: HTTP 429 limit ({max_429_retries}) exceeded after {_429_count} hits, aborting', flush=True)
                        raise Exception(f'HTTP 429 after {_429_count} occurrences')
                last_error = Exception(f'HTTP {resp.status_code}')
                if attempt < max_retries:
                    print(f'[RETRY] {description}: HTTP {resp.status_code}, attempt {attempt + 1}, retrying in {delay:.0f}s...', flush=True)
            else:
                return resp  # Non-HTTP response (e.g. mock), return as-is
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                print(f'[RETRY] {description}: {str(e)[:100]}, attempt {attempt + 1}, retrying in {delay:.0f}s...', flush=True)
        if attempt < max_retries:
            time.sleep(delay)
            delay = min(delay * 1.5, max_delay)
    print(f'[RETRY] {description}: all {max_retries + 1} attempts exhausted', flush=True)
    raise last_error


# ---------------------------------------------------------------------------
#  WebSearch executor
# ---------------------------------------------------------------------------

@register_executor('WebSearch', setting_check='enable_custom_websearch')
def execute_websearch(tool_input, settings, cache_dir, **kwargs):
    """Execute WebSearch using Google Serper API.

    Returns top-10 search results including answer box, knowledge graph,
    organic results, people also ask, and related searches.
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

    resp = _retry_request(
        lambda: requests.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
            json={'q': q, 'num': 10},
            timeout=15,
            proxies={'http': None, 'https': None}
        ),
        f'Serper API ({query[:30]})'
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

@register_executor('WebFetch', setting_check=lambda s: s.get('enable_custom_webfetch', False) or s.get('enable_custom_webfetch_jina', False))
def execute_webfetch(tool_input, settings, cache_dir, **kwargs):
    """Execute WebFetch using Jina Reader and/or Playwright Chromium.

    Multi-layer fetch strategy:
    1. Cache check (15min TTL)
    2. Proxy connectivity check
    3. PDF detection -> direct download + PyMuPDF
    4. Jina Reader (if enabled, primary mode)
    5. Playwright Chromium (if enabled and installed)
    6. HTTP fallback
    7. Jina fallback

    Returns None if no local method can handle the fetch (falls through to CC).
    Raises ToolReject if proxy is unreachable or Jina fails in primary mode.
    """
    url = tool_input.get('url', '')
    prompt = tool_input.get('prompt', '')

    if not url:
        return ToolResult('WebFetch URL is empty.', 'WebFetch: 空URL', is_error=True)
    if not url.startswith('http'):
        url = 'https://' + url

    jina_mode = settings.get('enable_custom_webfetch_jina', False)
    pw_mode = settings.get('enable_custom_webfetch', False)

    # Cache check (15 min TTL)
    os.makedirs(cache_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w\-.]', '_', url.replace('https://', '').replace('http://', ''))[:120]
    cache_path = os.path.join(cache_dir, f'{safe_name}.md')
    content = ''
    title = ''
    cached = False

    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 900:
            with open(cache_path, 'r', encoding='utf-8') as f:
                content = f.read()
            cached = True
            title = safe_name
            print(f'[WEBFETCH] Cache hit: {url[:80]} (age={age:.0f}s)', flush=True)

    if not cached:
        # Proxy connectivity check (retries until reachable, 429 breaks early to Jina)
        import requests
        _proxy_ok = True
        try:
            _retry_request(
                lambda: requests.get('https://www.google.com', timeout=10,
                    proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}),
                'Proxy connectivity check',
                max_429_retries=1
            )
            print(f'[WEBFETCH] Proxy connectivity OK', flush=True)
        except Exception as _proxy_err:
            print(f'[WEBFETCH] Proxy check 429 limit hit ({_proxy_err}), skipping to Jina', flush=True)
            _proxy_ok = False

        # PDF detection
        is_pdf = url.lower().rstrip('/').endswith('.pdf') or 'application/pdf' in url.lower()
        if is_pdf:
            pdf_content, pdf_title = _fetch_pdf(url)
            if pdf_content is None:
                return ToolResult(
                    f'WebFetch PDF download failed for {url}:\n{pdf_title}',
                    f'WebFetch PDF 失败: {str(pdf_title)[:30]}',
                    is_error=True
                )
            content = pdf_content
            title = pdf_title

        # Jina-first mode (retries until HTTP 200)
        if jina_mode and not is_pdf and not content:
            import requests
            print(f'[WEBFETCH] Jina Reader (primary mode)...', flush=True)
            jina_resp = _retry_request(
                lambda: requests.get(
                    f'https://r.jina.ai/{url}',
                    headers={'Accept': 'text/markdown', 'User-Agent': 'Mozilla/5.0'},
                    timeout=20,
                    proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
                ),
                f'Jina Reader ({url[:50]})'
            )
            if len(jina_resp.text) > 1024:
                content = jina_resp.text
                title = safe_name
                print(f'[WEBFETCH] Jina (primary) OK: {len(content)} chars', flush=True)
            else:
                print(f'[WEBFETCH] Jina returned HTTP 200 but only {len(jina_resp.text)} chars - too short, falling back to Playwright', flush=True)

        # Playwright
        pw_available = bool(not is_pdf and not content)
        if pw_available:
            try:
                from playwright.sync_api import sync_playwright  # noqa: F401
            except ImportError:
                pw_available = False
                print('[WEBFETCH] Playwright not installed, passing through to CC', flush=True)

        if pw_available:
            try:
                title, content = _fetch_with_playwright(url, settings)

                # Playwright fallback: HTTP if content too short
                if len(content) < 1024:
                    print(f'[WEBFETCH] Playwright content too short ({len(content)} chars), trying HTTP fallback...', flush=True)
                    http_title, http_content = _fetch_http_fallback(url)
                    if http_content and len(http_content) > len(content):
                        content = http_content
                        title = http_title or title or safe_name
                        print(f'[WEBFETCH] HTTP fallback OK: {len(content)} chars', flush=True)

                # Third-layer fallback: Jina Reader
                if len(content) < 1024:
                    print(f'[WEBFETCH] HTTP也太短 ({len(content)} chars), checking Jina connectivity...', flush=True)
                    jina_content = _fetch_jina_fallback(url)
                    if jina_content and len(jina_content) > len(content):
                        content = jina_content
                        title = title or safe_name

                # Truncate large content
                if len(content) > 100000:
                    content = content[:100000] + f'\n\n[Content truncated at 100KB ({len(content)} total chars)]'

                # Cache valid content (>1KB) to avoid caching error pages
                if len(content) > 1024:
                    with open(cache_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                else:
                    print(f'[WEBFETCH] Content too short ({len(content)} chars), skipping cache to avoid caching error pages', flush=True)

                kb = len(content) / 1024
                print(f'[WEBFETCH] Playwright OK: url={url[:80]}, title={title[:50]}, size={kb:.1f}KB', flush=True)
            except ToolReject:
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                return ToolResult(
                    f'WebFetch (Playwright) failed for {url}:\n{str(e)}',
                    f'WebFetch 失败: {str(e)[:30]}',
                    is_error=True
                )

    if content:
        kb = len(content) / 1024
        cache_note = ' (cached)' if cached else ''
        result_text = f'Fetched: {url}\nTitle: {title}\nSize: {kb:.1f}KB{cache_note}\nPrompt: {prompt}\n\n{content}'
        return ToolResult(result_text, f'WebFetch: {title[:50]}')

    # No content obtained by any method - fall through to CC
    return None


# ---------------------------------------------------------------------------
#  Internal helper functions for WebFetch
# ---------------------------------------------------------------------------

def _fetch_pdf(url):
    """Fetch and extract text from a PDF URL.

    Returns:
        (content, title) on success.
        (None, error_message) on failure.
    """
    try:
        import requests
        resp = _retry_request(
            lambda: requests.get(url, timeout=30, proxies={'http': None, 'https': None},
                               headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}),
            f'PDF download ({url.split("/")[-1][:30]})',
            max_retries=50
        )
        if resp.status_code == 200:
            try:
                import fitz
                doc = fitz.open(stream=resp.content, filetype='pdf')
                texts = [f'--- Page {i + 1} ---\n{doc[i].get_text()}' for i in range(len(doc))]
                doc.close()
                content = '\n\n'.join(texts)
                title = f'PDF: {url.split("/")[-1]}'
                print(f'[WEBFETCH] PDF extracted: {len(content)} chars, {len(texts)} pages', flush=True)
                return content, title
            except ImportError:
                content = resp.text or resp.content.decode('utf-8', errors='ignore')
                title = f'PDF (raw): {url.split("/")[-1]}'
                print('[WEBFETCH] PyMuPDF not installed, returning raw PDF text', flush=True)
                return content, title
        else:
            return None, f'HTTP {resp.status_code}'
    except Exception as e:
        return None, str(e)


def _fetch_with_playwright(url, settings):
    """Fetch page content using Playwright Chromium.

    Returns:
        (title, content) tuple.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _playwright_fetch(_url):
        """Run Playwright in a real OS thread to avoid greenlet/eventlet conflicts."""
        from playwright.sync_api import sync_playwright
        is_headless = settings.get('enable_webfetch_headless', True)
        pws = sync_playwright().start()
        br = pws.chromium.launch(
            headless=is_headless,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
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

        # Cloudflare detection
        if c:
            cf_keywords = ['Just a moment', 'Performing security verification',
                          'Checking your browser', 'Enable JavaScript and cookies']
            if len(c) < 500 and any(kw in c for kw in cf_keywords):
                print(f'[WEBFETCH] Cloudflare challenge detected, waiting up to 15s...', flush=True)
                for wait in range(15):
                    try:
                        pg.wait_for_timeout(1000)
                        c = pg.inner_text('body')
                        if len(c) > 500 or not any(kw in c for kw in cf_keywords):
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


def _fetch_http_fallback(url):
    """Simple HTTP fallback when Playwright content is too short.

    Returns:
        (title, content) tuple. Empty strings on failure.
    """
    try:
        import requests
        resp = _retry_request(
            lambda: requests.get(url, timeout=15,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml',
                    'Accept-Language': 'en-US,en;q=0.5'
                },
                proxies={'http': None, 'https': None}, allow_redirects=True),
            f'HTTP fallback ({url[:50]})',
            max_retries=10
        )
        if len(resp.text) > 2000:
            html = resp.text
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<[^>]+>', ' ', html)
            html = re.sub(r'&nbsp;', ' ', html)
            html = re.sub(r'&amp;', '&', html)
            html = re.sub(r'&lt;', '<', html)
            html = re.sub(r'&gt;', '>', html)
            html = re.sub(r'&#\d+;', '', html)
            html = re.sub(r'\s+', ' ', html).strip()
            title_m = re.search(r'<title>(.*?)</title>', resp.text, re.IGNORECASE | re.DOTALL)
            title = title_m.group(1).strip() if title_m else ''
            return title, html
    except Exception as e:
        print(f'[WEBFETCH] HTTP fallback failed: {e}', flush=True)
    return '', ''


def _fetch_jina_fallback(url):
    """Jina Reader fallback when other methods produce too-short content.

    Returns:
        Content string, or empty string on failure.
    """
    try:
        import requests
        _retry_request(
            lambda: requests.get('https://www.google.com', timeout=3,
                proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}),
            'Jina fallback connectivity',
            max_retries=5, initial_delay=2, max_delay=10
        )
    except Exception:
        print(f'[WEBFETCH] Jina unreachable after retries, skipping fallback', flush=True)
        return ''

    try:
        import requests
        jina_resp = _retry_request(
            lambda: requests.get(
                f'https://r.jina.ai/{url}',
                headers={'Accept': 'text/markdown', 'User-Agent': 'Mozilla/5.0'},
                timeout=20,
                proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
            ),
            f'Jina fallback ({url[:50]})',
            max_retries=10
        )
        if len(jina_resp.text) > 0:
            print(f'[WEBFETCH] Jina Reader OK (via proxy): {len(jina_resp.text)} chars', flush=True)
            return jina_resp.text
    except Exception as e:
        print(f'[WEBFETCH] Jina Reader request failed after retries: {e}', flush=True)
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

@register_executor('Read', setting_check='enable_tool_simulate')
def execute_read(tool_input, settings, cache_dir, **kwargs):
    """Local Read executor for CC simulate mode. No line limit; rejects files > 100k tokens."""
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
            try:
                import fitz
            except ImportError:
                # Auto-install PyMuPDF
                import subprocess as _pdf_sp
                print('[READ] PyMuPDF not found, auto-installing...', flush=True)
                _install_result = _pdf_sp.run(
                    ['pip', 'install', 'pymupdf'],
                    capture_output=True, text=True, timeout=120
                )
                if _install_result.returncode != 0:
                    return ToolResult(
                        f'<tool_use_error>Failed to auto-install PyMuPDF: {_install_result.stderr[:200]}</tool_use_error>',
                        'Read: PyMuPDF安装失败', is_error=True
                    )
                import fitz
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


@register_executor('Write', setting_check='enable_tool_simulate')
def execute_write(tool_input, settings, cache_dir, **kwargs):
    """Local Write executor for CC simulate mode."""
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


@register_executor('Edit', setting_check='enable_tool_simulate')
def execute_edit(tool_input, settings, cache_dir, **kwargs):
    """Local Edit executor for CC simulate mode."""
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


@register_executor('Bash', setting_check='enable_tool_simulate')
def execute_bash(tool_input, settings, cache_dir, **kwargs):
    """Local Bash executor for CC simulate mode.

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

    # Return None: pipeline falls through to CC queue path (no bound_cc_id → toast + return).
    # No immediate tool_result is created. The background thread injects it when child ends.
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
