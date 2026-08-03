"""prompts/ 默认值与 data/ 覆写的解析规则。

三份提示词（system_prompt / deep_think_prompt / deep_think_prompt_medium）的默认
值随仓库分发在 prompts/ 下，data/ 下的同名文件覆写它。这里固化的是解析顺序、缓存
失效条件，以及「默认值绝不被复制进 data/」这条约束——一旦复制，那个存在性检查从此
永远成立，prompts/ 里的后续改动对任何启动过一次的安装都不再可见。

用例全部在 tmp_path 上构造 data_dir，不触碰真实的 data/：那里面是用户的对话记录。
"""
import os
import re

import pytest

from api.context import ContextMixin, DEFAULT_PROMPT_DIR

PROMPT_NAMES = (
    'system_prompt.txt',
    'deep_think_prompt.txt',
    'deep_think_prompt_medium.txt',
)


class _Host(ContextMixin):
    """ContextMixin 的最小宿主：只提供解析所需的 data_dir 与三个缓存字段。

    构造真正的 Api 会读 CHATAPP_DATA_DIR、makedirs 并加载该目录下每一个会话文件，
    而这些用例要验证的只是路径解析，不需要那整套。
    """

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.cached_prompt = ''
        self.prompt_mtime = 0
        self._prompt_src = None


@pytest.fixture
def host(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    return _Host(d)


@pytest.fixture
def no_defaults(monkeypatch, tmp_path):
    """把 DEFAULT_PROMPT_DIR 指向一个空目录，模拟默认值也缺失。"""
    empty = tmp_path / 'no-prompts'
    empty.mkdir()
    monkeypatch.setattr('api.context.DEFAULT_PROMPT_DIR', str(empty))
    return empty


def _write_override(host, name, text, mtime=None):
    """在宿主的 data_dir 里写一份覆写文件，可指定 mtime。"""
    path = os.path.join(host.data_dir, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ------------------------------------------------ 默认值随仓库分发

def test_every_default_prompt_is_present_in_the_repo():
    """缺任何一份都会让对应功能静默退化为空提示词。"""
    for name in PROMPT_NAMES:
        path = os.path.join(DEFAULT_PROMPT_DIR, name)
        assert os.path.exists(path), '缺少默认提示词: %s' % path
        assert os.path.getsize(path) > 0, '默认提示词为空: %s' % path


def test_system_prompt_falls_back_to_the_tracked_default(host):
    assert host.get_system_prompt(), '无覆写时应读到 prompts/ 下的默认值'
    assert host._prompt_src == os.path.join(DEFAULT_PROMPT_DIR, 'system_prompt.txt')


def test_deep_think_falls_back_for_both_levels(host):
    """level 1 与 level 2 读的是不同文件，必须各自验证。"""
    assert host._load_deep_think_prompt(2)
    assert host._load_deep_think_prompt(1)


def test_no_unexpanded_brace_escape_remains(host):
    """{'`'*3} 之类的写法必须展开成字面量。

    展开失效是静默的：模型收到一份格式说明被破坏的协议，表现为它不再按代码块格式
    输出，而没有任何报错指向根因。
    """
    text = host.get_system_prompt()
    assert '`' * 3 in text, '反引号三连未被展开'
    leftover = re.search(r"\{(['\"]).*?\1\*\d+\}", text)
    assert leftover is None, '残留未展开的转义: %s' % (leftover.group(0) if leftover else '')


# ------------------------------------------------ data/ 覆写优先

def test_data_override_wins_over_the_default(host):
    _write_override(host, 'system_prompt.txt', 'OVERRIDE')
    assert host.get_system_prompt() == 'OVERRIDE'


def test_deep_think_override_wins_per_level(host):
    _write_override(host, 'deep_think_prompt.txt', 'L2')
    _write_override(host, 'deep_think_prompt_medium.txt', 'L1')
    assert host._load_deep_think_prompt(2) == 'L2'
    assert host._load_deep_think_prompt(1) == 'L1'


def test_an_override_created_later_wins_even_with_an_older_mtime(host):
    """缓存键必须含路径，不能只比 mtime。

    覆写文件常常是从别处复制来的，mtime 可能远早于 prompts/ 下的默认值；只比 mtime
    会让这次切换被整个跳过，症状是「我明明建了覆写文件却没生效」。
    """
    default_text = host.get_system_prompt()
    _write_override(host, 'system_prompt.txt', 'LATE-OVERRIDE', mtime=0)
    assert host.get_system_prompt() == 'LATE-OVERRIDE'
    assert host.get_system_prompt() != default_text


def test_deep_think_override_created_later_wins_with_an_older_mtime(host):
    """同上，深度思考的缓存元组也必须带路径。"""
    default_text = host._load_deep_think_prompt(2)
    _write_override(host, 'deep_think_prompt.txt', 'LATE-L2', mtime=0)
    assert host._load_deep_think_prompt(2) == 'LATE-L2'
    assert host._load_deep_think_prompt(2) != default_text


def test_editing_the_override_is_picked_up_without_a_restart(host):
    """mtime 热更新仍然有效——引入路径判断没有把它替换掉。"""
    _write_override(host, 'system_prompt.txt', 'V1', mtime=1000)
    assert host.get_system_prompt() == 'V1'
    _write_override(host, 'system_prompt.txt', 'V2', mtime=2000)
    assert host.get_system_prompt() == 'V2'


def test_editing_the_deep_think_override_is_picked_up(host):
    _write_override(host, 'deep_think_prompt.txt', 'D1', mtime=1000)
    assert host._load_deep_think_prompt(2) == 'D1'
    _write_override(host, 'deep_think_prompt.txt', 'D2', mtime=2000)
    assert host._load_deep_think_prompt(2) == 'D2'


# ------------------------------------------------ 绝不写入 data/

def test_reading_never_materialises_a_copy_in_data(host):
    """默认值绝不被复制进 data/。

    旧实现在文件缺失时会写出一份，而 CONFIG['SYSTEM_PROMPT'] 是硬编码空串，所以
    写出的是一个空文件；此后存在性检查永远成立，prompts/ 里的改动再也不可见。
    """
    host.get_system_prompt()
    host._load_deep_think_prompt(2)
    host._load_deep_think_prompt(1)
    assert os.listdir(host.data_dir) == [], 'data/ 被写入了内容'


# ------------------------------------------------ 两处皆缺时的降级

def test_both_missing_yields_empty_and_warns_exactly_once(host, no_defaults, capsys):
    """缺失必须留下痕迹，但只留一次。

    本方法在每次组装上下文时都会被调用，每次都打印会把控制台淹掉；而完全不打印就
    是旧行为——模型在毫无协议约束的情况下工作且无人知晓。
    """
    assert host.get_system_prompt() == ''
    assert '[PROMPT]' in capsys.readouterr().out
    assert host.get_system_prompt() == ''
    assert '[PROMPT]' not in capsys.readouterr().out


def test_missing_deep_think_prompt_is_empty_not_an_exception(host, no_defaults):
    """深度思考提示词缺失只影响该功能，不该让整次请求崩掉。"""
    assert host._load_deep_think_prompt(2) == ''
    assert host._load_deep_think_prompt(1) == ''


# ------------------------------------------------ 打包清单

def test_packaging_never_ships_user_overrides(read_text):
    """打包清单里永远不能出现 data/ 下的提示词。

    更新包里的每一项都会被自动生成的 update.py 按相同相对路径 copy2 覆写，因此清单
    中出现 data/ 下的提示词等于一条静默覆盖用户改动的通道。

    **这个用例换过形态，值得说明原因。** 它原先还断言 `'prompts/<name>'` 必须出现在
    app.py 里，而 export_release 与 export_snapshot 两处清单已随自动更新功能整体移除，
    于是那半条断言指向一段不存在的代码。

    直接删掉整条用例是错的：它记录的危险是真实的，只是暂时无处发生。所以禁止 data/
    那一半保持无条件（永远适用，成本为零），而要求 prompts/ 那一半改成条件式，以
    `import zipfile` 作为「这个文件里有打包代码」的探针——任何打包实现都需要它。

    结果是守卫在功能缺席期间静默，在它回来的那一刻自动重新武装，不依赖下一个人
    记得手工恢复一条断言。
    """
    src = read_text('app.py')
    packs = 'import zipfile' in src
    for name in PROMPT_NAMES:
        assert "'data/%s'" % name not in src, 'app.py 仍在打包 data/%s' % name
        if packs:
            assert "'prompts/%s'" % name in src, (
                'app.py 有打包代码但未打包 prompts/%s' % name)