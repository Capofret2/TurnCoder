"""platform_shell 的两平台分支。

开发机是 Linux，因此 Windows 分支若只存在于 Popen 调用内部就是永远无法执行的死区。
platform_shell 里的函数一律返回数据而不起进程，这个文件是那个取向的兑现：monkeypatch
os.name 之后，两条分支能在同一台机器上被逐条断言。

两个方向都显式打补丁（nt / posix 两个 fixture），因此这个文件的结果与运行它的机器是
什么平台无关。原先只给 Windows 方向打补丁、posix 方向靠「开发机恰好是 Linux」，那等于
把开发机的平台悄悄写进了测试的前提——搬到 Windows 上之后那批用例集体反向失败。

which 的结果同样要打补丁。开发机没装 PowerShell 而 Windows 机器装了 pwsh 7，所以两台
机器上「回退链走到第几档」正好相反。凡是伪造出来的通过都在各自 docstring 里写明——把
「伪造出来的通过」误当成「真机验证过」是这类测试最容易造成的伤害。
"""
import os
import sys

import pytest

from api import platform_shell as ps


@pytest.fixture
def posix(monkeypatch):
    """把平台判定翻成 POSIX。与 nt 对称，理由见模块 docstring。"""
    monkeypatch.setattr(ps, 'is_windows', lambda: False)


@pytest.fixture
def no_powershell(monkeypatch):
    """伪造「一个 PowerShell 都没装」。

    与 nt 是两个独立条件：平台是 Windows 不代表装了 pwsh。开发机上这个状态是真实的，
    在装了 pwsh 7 的机器上必须伪造才能走到回退链最后一档。
    """
    monkeypatch.setattr(ps.shutil, 'which', lambda n: None)


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

def test_default_shell_is_bash_on_posix(posix):
    assert ps.default_shell() == 'bash'


def test_default_shell_is_powershell_on_windows(nt):
    assert ps.default_shell() == 'powershell'


def test_is_windows_matches_the_real_platform():
    """不打补丁：这是唯一一条验证判定本身而非被模拟结果的用例。"""
    assert ps.is_windows() == (os.name == 'nt')


# ------------------------------------------------ 一次性执行的 argv

def test_posix_wraps_in_bash_dash_c(posix):
    """只比 argv[1:]：argv[0] 取决于机器上装没装 bash（取不到时回退 /bin/bash 字面量），
    而这条用例要验的是包装形式，不是环境。"""
    argv, label = ps.shell_argv('ls')
    assert argv[1:] == ['-c', 'ls']
    assert label == 'bash'


def test_windows_defaults_to_powershell_with_utf8_first(nt):
    """UTF-8 设置必须排在命令之前，否则前面的输出已经按旧代码页写出去了。

    label 的判据与 interactive_prelude 保持一致（pwsh 或 powershell 开头）而不是写死
    'powershell'：label 是 which 实际找到的可执行文件名，装了 pwsh 7 的机器上是
    `pwsh.EXE`，两个都没装的机器上才回退到 `powershell.exe` 字面量。原断言把「开发机
    没装 pwsh」写进了前提——这是本文件里最后一条同类错误。

    两处用同一个谓词还有个附带好处：对「什么算 PowerShell」的判断不会在实现与测试
    之间分叉。
    """
    argv, label = ps.shell_argv('Get-ChildItem')
    assert argv[1:4] == ['-NoProfile', '-NonInteractive', '-Command']
    assert label.lower().startswith(('pwsh', 'powershell'))
    body = argv[-1]
    assert body.startswith('[Console]::OutputEncoding')
    assert body.endswith('Get-ChildItem')


def test_windows_prefers_pwsh_over_powershell(nt, fake_pwsh):
    """回退链第一档：pwsh 优先于 powershell。

    which 与路径都是伪造的（`/usr/bin/pwsh`，posix 风格），所以这条验的是「优先顺序 +
    label 取 basename」这个逻辑，不是真机上的回退行为。真机行为已由手工探测确认：装了
    pwsh 7 的机器上 shell_argv 拿到的是 `...\\7-preview\\pwsh.EXE`。
    """
    _argv, label = ps.shell_argv('Get-Date')
    assert label == 'pwsh'


def test_cmd_sets_codepage_before_the_command(nt):
    """chcp 用 && 串联而非 &：设置失败时不该继续执行，否则输出编码与读回口径不一致，
    而这种不一致只表现为乱码。

    argv 与 label 刻意不同：argv[0] 是真实要执行的 `cmd.exe`，label 是 `cmd`（展示用，
    也是 shell 参数接受的词汇）。两条断言并存是为了防止有人为「统一」把 argv 也改掉——
    那会让命令根本启动不起来。
    """
    argv, label = ps.shell_argv('dir', 'cmd')
    assert argv[:2] == ['cmd.exe', '/c']
    assert argv[2].startswith('chcp 65001>nul && ')
    assert label == 'cmd'


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

def test_posix_detach_uses_new_session(posix):
    assert ps.detached_kwargs() == {'start_new_session': True}


def test_windows_detach_uses_creationflags(nt):
    """start_new_session 在 Windows 被静默忽略（CPython 的 Windows 版
    _execute_child 把该形参命名为 unused_start_new_session），所以必须换成
    creationflags，否则「重启不影响」是一句无声的假话。"""
    assert ps.detached_kwargs() == {
        'creationflags': ps.CREATE_NO_WINDOW | ps.CREATE_NEW_PROCESS_GROUP}


def test_windows_detach_suppresses_the_console_window(nt):
    """必须带 CREATE_NO_WINDOW，否则每条命令都闪一个黑窗口。

    这是用户实际报告过的现象。它纯属观感、不影响功能、不会让任何别的测试变红，所以
    极容易在某次「简化 creationflags」时被摘掉而无人察觉——因此单独立一条。
    """
    assert ps.detached_kwargs()['creationflags'] & ps.CREATE_NO_WINDOW


def test_windows_detach_never_uses_detached_process(nt):
    """DETACHED_PROCESS 必须不出现在这里。

    实测（Windows 10 / pwsh 7）：带上它之后 PowerShell 启动、host 初始化失败、不执行
    任何命令、以退出码 0 退出。调用方看到的是「成功」，而实际什么都没发生。cmd 与
    python 在同一标志下正常，所以这是 PowerShell 特有的。

    这条用例的存在理由：那个标志的名字听起来正是「让后台进程活下去」该用的东西，加
    回它的动机很强，而症状是所有命令静默返回空并报告成功——几乎不可能被联想到是一个
    创建标志。所以让那个动作变成一次失败。
    """
    _flags = ps.detached_kwargs().get('creationflags', 0)
    assert not (_flags & ps.DETACHED_PROCESS)


def test_posix_process_group_kwargs_are_empty(posix):
    """刻意不返回 start_new_session：持久化终端应随 ChatApp 一同退出，脱离会话会让
    它活成孤儿进程。与 detached_kwargs 的差别正在这里，两者不可互换。"""
    assert ps.new_process_group_kwargs() == {}


def test_windows_process_group_enables_ctrl_break(nt):
    """终端进程长期存活，缺了 NO_WINDOW 会留下一个常驻控制台窗口而非一次闪现。"""
    assert ps.new_process_group_kwargs() == {
        'creationflags': ps.CREATE_NO_WINDOW | ps.CREATE_NEW_PROCESS_GROUP}


def test_win32_flag_values_match_the_abi():
    """四个常量都是从 _winapi 与 Windows signal 抄来的 ABI 数值。

    那些名字在 Linux 的 subprocess / signal 里根本取不到，而本模块要在两个平台都能被
    导入，所以只能写字面量。而 CreateProcess 对未知标志位是**静默忽略**的——抄错一位
    不会有任何报错，只会让那个标志失效。把数值本身写成断言是唯一的保护。
    """
    assert ps.DETACHED_PROCESS == 0x00000008
    assert ps.CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert ps.CREATE_NO_WINDOW == 0x08000000
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


def test_interrupt_signal_is_sigint_on_posix(posix):
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


def test_interactive_falls_back_to_cmd_when_no_powershell(nt, no_powershell):
    """回退链最后一档：Windows 但一个 PowerShell 都没装。

    which 必须打补丁。这条原先写着「本机确实没有 PowerShell，所以是真实回退」——那句话
    只在开发机上成立，搬到装了 pwsh 7 的 Windows 机器上就变成一句会误导人的记录，比断言
    失败更糟。
    """
    assert ps.interactive_shell_argv() == (['cmd.exe'], 'cmd')


def test_powershell_prelude_silences_the_prompt():
    """PowerShell 把提示符写进 stdout，与命令输出同流。把 prompt 函数改成返回空串是
    唯一能从进程内部关掉它的办法。"""
    pre = ps.interactive_prelude('pwsh')
    assert any('prompt' in line for line in pre)
    assert any('OutputEncoding' in line for line in pre)


def test_cmd_prelude_disables_echo():
    pre = ps.interactive_prelude('cmd')
    assert '@echo off' in pre


def test_prelude_matches_the_label_the_producer_emits(nt, fake_pwsh):
    """生产者与消费者必须对上。

    其余 prelude 用例都传字面量 label，因此「interactive_shell_argv 改了 label 格式而
    interactive_prelude 的匹配没跟上」这种情况对它们完全不可见。症状是 PowerShell 终端
    失去提示符抑制、cmd 终端失去 @echo off，两者都表现为「输出里多出几行看不懂的东西」
    而不是报错——这类失败正是最难反推的一种。这条把两端接起来。
    """
    _argv, label = ps.interactive_shell_argv()
    assert ps.interactive_prelude(label), '产出的 label 喂回 prelude 得到了空列表'


def test_prelude_matches_the_cmd_label_too(nt, no_powershell):
    """cmd 那一档同理。它是 Windows 上没装任何 PowerShell 时的唯一选择，所以这条链路
    断了就等于那类机器上的终端全都带着回显与乱码。"""
    _argv, label = ps.interactive_shell_argv()
    assert '@echo off' in ps.interactive_prelude(label)


def test_bash_needs_no_prelude():
    assert ps.interactive_prelude('bash') == []


# ------------------------------------------------ 环境事实占位符

def test_environment_facts_covers_all_three_placeholders():
    facts = ps.environment_facts()
    assert set(facts) == {'{PLATFORM}', '{SHELL}', '{OS_VERSION}'}
    assert all(facts.values()), '占位符取到空值会让那一行变成半句话'


def test_environment_facts_reports_bash_on_posix(posix):
    """比 sys.platform 而不是写死 'linux'：posix 分支的行为是透传 sys.platform，写死
    平台名等于把开发机的身份写进断言。"""
    facts = ps.environment_facts()
    assert facts['{PLATFORM}'] == sys.platform
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


# ------------------------------------------------ Windows 路径处理

def test_no_file_path_is_split_on_forward_slash_only(read_text):
    """取文件名要用 os.path.basename，不能用 split("/")[-1]。

    Windows 路径用反斜杠，那个表达式在 `D:\\Repos\\app.py` 上找不到任何 `/`，于是 `[-1]`
    返回**整条路径**。后果不只是摘要变长：过时读取的「已省略，概括为：…」替换文本会进入
    模型上下文，一条说明里塞进整条绝对路径等于每次为一个文件名付出四五十个字符。

    这个写法极易被重新引入，因为它在 Linux 上完全正确（那里路径就是用 `/`），所以在那台
    机器上写代码的人不会收到任何提示，而它还比 basename 短、看起来更顺手。

    os.path.basename 在两个平台上都是严格改进而非取舍：Windows 的 ntpath 把 `/` 与 `\\`
    都当分隔符，Linux 的 posixpath 只认 `/` 而那里的路径本来就用 `/`。

    含 `url` 的行是刻意的例外——URL 永远用正斜杠，对它调路径函数是把工具用错了地方。
    这个例外写在这里，免得下一个人以为守卫漏了几处而去「补全」它。

    **这条守卫刻意是钝的，不要给它加逐点例外。** 它第一次运行时抓到了 context.py 的两处
    树片段拼接，而那两处其实是对的：那里的 `p` 由本函数自己用 `replace('\\\\', '/')` 归一化
    后再 `'/'.join` 拼出，所以只含正斜杠。当时的处理是改掉那两处而不是给守卫开例外，依据
    是两侧代价不对称——假阳性的代价是一次无害的编辑（basename 在归一化路径上等价），漏判
    的代价是跨平台的静默错误输出。一个带例外清单的检查很快就没人敢相信它。
    """
    import glob as _g
    import os as _o

    offenders = []
    for rel in ['app.py'] + sorted(_g.glob('api/*.py')):
        for i, line in enumerate(read_text(rel).splitlines(), 1):
            if 'url' in line:
                continue
            if '.split("/")[-1]' in line or ".split('/')[-1]" in line:
                offenders.append('%s:%d: %s' % (rel, i, line.strip()[:90]))
    assert not offenders, '改用 os.path.basename：\n' + '\n'.join(offenders)


def test_worker_engine_substitutes_the_environment_facts(read_text):
    """两个渲染点都要接线。

    一处服务 Claude 的 Anthropic 协议路径，另一处服务非 Claude 的注入路径。只接一处
    的症状是「换某些模型对了、换另一些又不对」，几乎不可能被联想成渲染点漏接。
    """
    src = read_text('api/worker_engine.py')
    assert src.count('environment_facts()') == 2