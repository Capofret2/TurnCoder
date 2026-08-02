"""platform_shell 的两平台分支。

开发机是 Linux，因此 Windows 分支若只存在于 Popen 调用内部就是永远无法执行的死区。
platform_shell 里的函数一律返回数据而不起进程，这个文件是那个取向的兑现：monkeypatch
os.name 之后，两条分支能在同一台机器上被逐条断言。

本机没装 PowerShell，所以 shutil.which('pwsh') 恒为 None。需要覆盖回退链前两档的
用例改为伪造 which，并在各自的 docstring 里写明这一点——把「伪造出来的通过」误当成
「真机验证过」是这类测试最容易造成的伤害。
"""
import os

import pytest

from api import platform_shell as ps


@pytest.fixture
def nt(monkeypatch):
    """把平台判定翻成 Windows。

    替换 is_windows 而不是 os.name：模块内每个函数都在调用时从模块全局查这个名字，
    所以换掉它就覆盖了全部分支。而 os.name 是进程级全局，pathlib.Path.__new__ 靠它
    决定造 PosixPath 还是 WindowsPath，pytest 自己的 cache 写入器也会 Path()——改它
    会让整个套件在 sessionfinish 阶段炸成 cannot instantiate 'WindowsPath'。
    """
    monkeypatch.setattr(ps, 'is_windows', lambda: True)


@pytest.fixture
def fake_pwsh(monkeypatch):
    """伪造 pwsh 存在。路径故意用 posix 风格：basename 在 Linux 上走 posixpath，
    反斜杠不是分隔符，用 Windows 风格路径会让 label 变成整条路径。"""
    monkeypatch.setattr(ps.shutil, 'which',
                        lambda n: '/usr/bin/pwsh' if n == 'pwsh' else None)


# ------------------------------------------------ 默认解释器

def test_default_shell_is_bash_on_posix():
    assert ps.default_shell() == 'bash'


def test_default_shell_is_powershell_on_windows(nt):
    assert ps.default_shell() == 'powershell'


def test_is_windows_matches_the_real_platform():
    """不打补丁：这是唯一一条验证判定本身而非被模拟结果的用例。"""
    assert ps.is_windows() == (os.name == 'nt')


# ------------------------------------------------ 一次性执行的 argv

def test_posix_wraps_in_bash_dash_c():
    argv, label = ps.shell_argv('ls')
    assert argv[1:] == ['-c', 'ls']
    assert label == 'bash'


def test_windows_defaults_to_powershell_with_utf8_first(nt):
    """UTF-8 设置必须排在命令之前，否则前面的输出已经按旧代码页写出去了。"""
    argv, label = ps.shell_argv('Get-ChildItem')
    assert argv[1:4] == ['-NoProfile', '-NonInteractive', '-Command']
    assert label.lower().startswith('powershell')
    body = argv[-1]
    assert body.startswith('[Console]::OutputEncoding')
    assert body.endswith('Get-ChildItem')


def test_windows_prefers_pwsh_over_powershell(nt, fake_pwsh):
    """伪造 which 的用例：本机无 PowerShell，回退链前两档否则走不到。"""
    _argv, label = ps.shell_argv('Get-Date')
    assert label == 'pwsh'


def test_cmd_sets_codepage_before_the_command(nt):
    """chcp 用 && 串联而非 &：设置失败时不该继续执行，否则输出编码与读回口径不一致，
    而这种不一致只表现为乱码。"""
    argv, label = ps.shell_argv('dir', 'cmd')
    assert argv[:2] == ['cmd.exe', '/c']
    assert argv[2].startswith('chcp 65001>nul && ')
    assert label == 'cmd.exe'


def test_bash_can_be_forced_on_windows(nt):
    """Windows 上仍可显式要 bash（WSL / Git Bash 的场景）。"""
    argv, label = ps.shell_argv('ls', 'bash')
    assert argv[1] == '-c'
    assert label == 'bash'


def test_unknown_shell_raises_instead_of_falling_back():
    """静默回退到 bash 会让一条 PowerShell 命令得到一堆语法错误，而错误里没有任何
    线索指向「解释器名拼错了」。"""
    with pytest.raises(ValueError):
        ps.shell_argv('echo hi', 'zsh')


# ------------------------------------------------ 进程组与脱离

def test_posix_detach_uses_new_session():
    assert ps.detached_kwargs() == {'start_new_session': True}


def test_windows_detach_uses_creationflags(nt):
    """start_new_session 在 Windows 被静默忽略（CPython 的 Windows 版
    _execute_child 把该形参命名为 unused_start_new_session），所以必须换成
    creationflags，否则「重启不影响」是一句无声的假话。"""
    assert ps.detached_kwargs() == {
        'creationflags': ps.DETACHED_PROCESS | ps.CREATE_NEW_PROCESS_GROUP}


def test_posix_process_group_kwargs_are_empty():
    """刻意不返回 start_new_session：持久化终端应随 ChatApp 一同退出，脱离会话会让
    它活成孤儿进程。与 detached_kwargs 的差别正在这里，两者不可互换。"""
    assert ps.new_process_group_kwargs() == {}


def test_windows_process_group_enables_ctrl_break(nt):
    assert ps.new_process_group_kwargs() == {
        'creationflags': ps.CREATE_NEW_PROCESS_GROUP}


def test_win32_flag_values_match_the_abi():
    """这两个常量是从 _winapi 抄来的 ABI 数值，因为那两个名字在 Linux 的 subprocess
    里取不到，而本模块要在两个平台都能导入。"""
    assert ps.DETACHED_PROCESS == 0x00000008
    assert ps.CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert ps.CTRL_BREAK_EVENT == 1


# ------------------------------------------------ 终止与中断

def test_windows_kill_uses_taskkill_with_the_tree_flag(nt, monkeypatch):
    """/T 才是「连同后代」那一半。原代码调 os.killpg，而该属性在 Windows 根本不存在，
    必然退化成只杀直接子进程、留下整棵子树继续占 CPU 与文件锁。"""
    seen = {}

    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        seen['argv'] = argv
        return _R()

    monkeypatch.setattr(ps.subprocess, 'run', _fake_run)
    assert ps.kill_process_tree(4242) is True
    assert seen['argv'][:3] == ['taskkill', '/T', '/F']
    assert seen['argv'][-1] == '4242'


def test_interrupt_signal_is_sigint_on_posix():
    import signal
    assert ps.interrupt_signal() == signal.SIGINT


def test_interrupt_signal_is_ctrl_break_on_windows(nt):
    """CTRL_C_EVENT 无法定向到单个进程组，所以只能用 CTRL_BREAK_EVENT。

    比的是模块常量而不是 signal.CTRL_BREAK_EVENT：后者在 Linux 上根本不存在，直接
    引用会让这条用例以 AttributeError 失败——原实现也因此在模拟 Windows 下走不到。
    """
    assert ps.interrupt_signal() == ps.CTRL_BREAK_EVENT


# ------------------------------------------------ 交互式终端

def test_interactive_argv_never_passes_noninteractive(nt, fake_pwsh):
    """-NonInteractive 会让 PowerShell 不再把 stdin 当作命令来源，而持久化终端的全部
    工作方式就是往 stdin 喂命令。这是它与 shell_argv 必须分开的原因。"""
    argv, label = ps.interactive_shell_argv()
    assert '-NonInteractive' not in argv
    assert argv[-2:] == ['-Command', '-']
    assert label == 'pwsh'


def test_interactive_falls_back_to_cmd_when_no_powershell(nt):
    """本机确实没有 PowerShell，所以这条是真实回退而非伪造。"""
    assert ps.interactive_shell_argv() == (['cmd.exe'], 'cmd.exe')


def test_powershell_prelude_silences_the_prompt():
    """PowerShell 把提示符写进 stdout，与命令输出同流。把 prompt 函数改成返回空串是
    唯一能从进程内部关掉它的办法。"""
    pre = ps.interactive_prelude('pwsh')
    assert any('prompt' in line for line in pre)
    assert any('OutputEncoding' in line for line in pre)


def test_cmd_prelude_disables_echo():
    pre = ps.interactive_prelude('cmd.exe')
    assert '@echo off' in pre


def test_bash_needs_no_prelude():
    assert ps.interactive_prelude('bash') == []


# ------------------------------------------------ 环境事实占位符

def test_environment_facts_covers_all_three_placeholders():
    facts = ps.environment_facts()
    assert set(facts) == {'{PLATFORM}', '{SHELL}', '{OS_VERSION}'}
    assert all(facts.values()), '占位符取到空值会让那一行变成半句话'


def test_environment_facts_reports_bash_on_posix():
    facts = ps.environment_facts()
    assert facts['{PLATFORM}'] == 'linux'
    assert facts['{SHELL}'] == 'bash'


def test_environment_facts_reports_powershell_on_windows(nt):
    facts = ps.environment_facts()
    assert facts['{PLATFORM}'] == 'windows'
    assert facts['{SHELL}'] == 'powershell'


def test_tool_system_prompt_has_no_hardcoded_platform(read_text):
    """写死的平台三行必须换成占位符。

    模型判断该写什么语法的首要依据就是这段环境信息。留着 `平台：linux` 意味着它在
    Windows 上会被明确告知自己在 Linux，然后照旧写 bash——前面所有解释器分派都白做。
    """
    src = read_text('tool_system.json')
    assert '平台：linux' not in src
    assert 'Shell：bash' not in src
    for ph in ('{PLATFORM}', '{SHELL}', '{OS_VERSION}'):
        assert ph in src, 'tool_system.json 缺少占位符 %s' % ph


def test_worker_engine_substitutes_the_environment_facts(read_text):
    """两个渲染点都要接线。

    一处服务 Claude 的 Anthropic 协议路径，另一处服务非 Claude 的注入路径。只接一处
    的症状是「换某些模型对了、换另一些又不对」，几乎不可能被联想成渲染点漏接。
    """
    src = read_text('api/worker_engine.py')
    assert src.count('environment_facts()') == 2