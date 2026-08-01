"""Static checks for the CSS and HTML, which have no build step to catch them.

Each failure mode here is silent in the browser:

- An unbalanced brace makes the parser discard every rule after it in that file
  and report nothing. The symptom is "some styles stopped working" with no clue
  where.
- A misspelled custom property does not warn either; the declaration just fails
  to resolve.
- A stale ?v= means the change appears not to have happened at all. HANDOVER
  lists it first precisely because it is the one item that lets every other
  check pass while the user still sees the old interface.

Comments are stripped before counting: writing an old value into a comment as
documentation is common here and a substring search would match the comment.
"""
import re

import pytest

CSS_FILES = ['libs/tokens.css', 'libs/styles.css']
SCAN_FOR_REFS = CSS_FILES + [
    'frontend.html', 'libs/chat.js', 'libs/main.js', 'libs/minimap.js',
    'libs/kanban.js', 'libs/panels.js', 'libs/utils.js', 'libs/editops.js',
]

_CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)
_VAR_REF = re.compile(r'var\(\s*(--md-sys-[A-Za-z0-9_-]+)')
_VAR_DEF = re.compile(r'^\s*(--md-sys-[A-Za-z0-9_-]+)\s*:', re.M)
_VERSIONED = re.compile(r'(?:src|href)="/libs/([A-Za-z0-9_.-]+)\?v=(\d+)"')
_UNVERSIONED = re.compile(r'(?:src|href)="/libs/([A-Za-z0-9_.-]+)"')


@pytest.mark.parametrize('rel', CSS_FILES)
def test_braces_balance(rel, read_text):
    body = _CSS_COMMENT.sub('', read_text(rel))
    opens, closes = body.count('{'), body.count('}')
    assert opens == closes, (
        '%s has %d { against %d } — every rule after the imbalance is dropped '
        'silently' % (rel, opens, closes))


@pytest.mark.parametrize('rel', CSS_FILES)
def test_no_stray_closing_brace_before_its_opener(rel, read_text):
    """Depth must never go negative, which a raw count would not notice."""
    depth = 0
    body = _CSS_COMMENT.sub('', read_text(rel))
    for lineno, line in enumerate(body.splitlines(), 1):
        for ch in line:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                assert depth >= 0, '%s:%d closes a block that never opened' % (
                    rel, lineno)


def test_every_referenced_token_is_defined(read_text):
    defined = set()
    for rel in CSS_FILES:
        defined |= set(_VAR_DEF.findall(_CSS_COMMENT.sub('', read_text(rel))))
    assert defined, 'no token definitions found at all'

    missing = {}
    for rel in SCAN_FOR_REFS:
        body = _CSS_COMMENT.sub('', read_text(rel))
        for lineno, line in enumerate(body.splitlines(), 1):
            for name in _VAR_REF.findall(line):
                if name not in defined:
                    missing.setdefault(name, []).append('%s:%d' % (rel, lineno))
    assert not missing, 'undefined tokens resolve to nothing: %s' % (
        {k: v[:3] for k, v in missing.items()},)


def test_first_party_libs_references_carry_a_cache_version(read_text):
    """Vendored *.min.* files are exempt; they never carried a version."""
    html = read_text('frontend.html')
    bare = [name for name in _UNVERSIONED.findall(html) if '.min.' not in name]
    assert not bare, 'these references would be served from cache forever: %s' % bare


def test_cache_versions_are_positive_integers(read_text):
    hits = _VERSIONED.findall(read_text('frontend.html'))
    assert hits, 'no versioned libs/ references found'
    for name, ver in hits:
        assert int(ver) >= 1, '%s has a non-positive version' % name