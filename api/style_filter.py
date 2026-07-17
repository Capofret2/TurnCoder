"""LLM response language style post-processing filter.

Applies a set of deterministic and probabilistic text replacement rules
to assistant bubble text content and summaries. Does NOT affect tool_use
content or thinking chain bubbles.
"""
import hashlib
import re


def _prob(msg_id: int, pos: int) -> int:
    """Deterministic 0-99 pseudorandom value, independent of PYTHONHASHSEED.

    Uses MD5 hash of (msg_id, pos) pair to produce a stable value across
    process restarts and different Python invocations.
    """
    data = f'{msg_id}:{pos}'.encode()
    return int(hashlib.md5(data).hexdigest()[:8], 16) % 100


# Rule A: markers sorted longest-first for regex alternation priority
_LEFT_MARKERS = ['\u5e76\u4e0d\u662f', '\u4e0d\u5728\u4e8e', '\u4e0d\u662f', '\u5e76\u975e']
_RIGHT_MARKERS = ['\u800c\u5e94\u8be5\u662f', '\u800c\u5728\u4e8e', '\u53cd\u800c\u662f', '\u800c\u662f']
_RULE_A_RE = re.compile(
    '(' + '|'.join(re.escape(m) for m in _LEFT_MARKERS) + ')'
    r'(.{0,30}?)'
    '(' + '|'.join(re.escape(m) for m in _RIGHT_MARKERS) + ')'
)

# Rule E: innermost angle quotes with short content
_RULE_E_RE = re.compile(r'\u300c([^\u300c\u300d]{0,12})\u300d')


def apply_style_filter(text: str, msg_id: int) -> tuple:
    """Apply language style post-processing rules to text.

    Args:
        text: Input text to process.
        msg_id: Message ID used as seed for deterministic probability decisions.

    Returns:
        Tuple of (processed_text, changes_list).
        changes_list: [{"pos": int, "old": str, "new": str}, ...] ascending by pos.
        Empty list means no modifications were made.
    """
    if not text:
        return text, []

    changes = []  # (start_pos, end_pos_exclusive, replacement_str)
    occupied = set()  # character indices consumed by a higher-priority rule

    # === Rule A: \u4e0d\u662f...\u800c\u662f... pattern (100%, highest priority) ===
    for m in _RULE_A_RE.finditer(text):
        s, e = m.start(), m.end()
        if any(i in occupied for i in range(s, e)):
            continue
        changes.append((s, e, '\u662f'))
        occupied.update(range(s, e))

    # === Rule E: \u300c...\u300d with content <= 12 chars (50% probability) ===
    for m in _RULE_E_RE.finditer(text):
        s, e = m.start(), m.end()
        if any(i in occupied for i in range(s, e)):
            continue
        if _prob(msg_id, s) < 50:
            changes.append((s, e, m.group(1)))  # keep content, remove quotes
            occupied.update(range(s, e))

    # === Rule D: \u2014\u2014 -> \uff0c (100%) ===
    pos = 0
    while pos < len(text) - 1:
        if text[pos:pos + 2] == '\u2014\u2014' and pos not in occupied and (pos + 1) not in occupied:
            changes.append((pos, pos + 2, '\uff0c'))
            occupied.add(pos)
            occupied.add(pos + 1)
            pos += 2
        else:
            pos += 1

    # === Rule C: \uff08 -> \uff0c and \uff09 -> '' (100%) ===
    for pos, ch in enumerate(text):
        if pos in occupied:
            continue
        if ch == '\uff08':
            changes.append((pos, pos + 1, '\uff0c'))
            occupied.add(pos)
        elif ch == '\uff09':
            changes.append((pos, pos + 1, ''))
            occupied.add(pos)

    # === Rule B: \uff01 -> \u3002 (50% probability) ===
    for pos, ch in enumerate(text):
        if pos in occupied:
            continue
        if ch == '\uff01':
            if _prob(msg_id, pos) < 50:
                changes.append((pos, pos + 1, '\u3002'))
                occupied.add(pos)

    # === Rule F: \u6781\u5176 -> delete(50%) / \u975e\u5e38(25%) / keep(25%) ===
    pos = 0
    while pos < len(text) - 1:
        if text[pos:pos + 2] == '\u6781\u5176' and pos not in occupied and (pos + 1) not in occupied:
            v = _prob(msg_id, pos)
            if v < 50:
                changes.append((pos, pos + 2, ''))
                occupied.add(pos)
                occupied.add(pos + 1)
            elif v < 75:
                changes.append((pos, pos + 2, '\u975e\u5e38'))
                occupied.add(pos)
                occupied.add(pos + 1)
            # v >= 75: keep unchanged
            pos += 2
        else:
            pos += 1

    if not changes:
        return text, []

    # Sort descending by start position for right-to-left application
    changes.sort(key=lambda x: x[0], reverse=True)

    # Apply changes right-to-left so positions of earlier changes stay valid
    result = text
    change_records = []
    for s, e, repl in changes:
        old_text = result[s:e]
        result = result[:s] + repl + result[e:]
        change_records.append({"pos": s, "old": old_text, "new": repl})

    # Return in ascending position order
    change_records.reverse()
    return result, change_records
