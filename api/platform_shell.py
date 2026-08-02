"""平台相关的 shell 选择与进程控制。

只有这个模块知道 os.name 的取值。其余模块调用这些函数，因此新增一个平台、调整
解释器回退链或改动编码对齐方式都是单点改动，而不是去追散落在各处的
`os.name == 'nt'`。

函数一律返回数据（argv 列表、Popen 关键字字典）而不直接起进程。这不是风格偏好：
开发机是 Linux，如果 Windows 分支只存在于 Popen 调用内部，它在这里就是永远无法
被执行的死区，而它恰好是最容易写错的部分。返回数据意味着两个平台的分支都能在任
意平台上被断言。
"""
import os
import platform
import shutil
import signal
import subprocess
import sys

# Win32 进程创建标志。写成字面量而不是 getattr(subprocess, 'DETACHED_PROCESS')：
# 这两个名字只在 Windows 的 subprocess 模块里存在，Linux 上取不到，而本模块需要
# 在两个平台都能被导入。数值属于 Win32 ABI，不会变。
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
# signal.CTRL_BREAK_EVENT 同样只在 Windows 的 signal 模块里存在，Linux 上取不到。
# 用裸值是等价的：Popen.send_signal 拿 == 比较，而 signal.Signals 是 IntEnum。
CTRL_BREAK_EVENT = 1

# Windows PowerShell 5.1 的输出编码跟随 ANSI 代码页（中文系统上是 GBK），而所有
# 输出一律按 UTF-8 读回。pwsh 7 默认已是 UTF-8，重复设置无害。不设的症状是中文
# 乱码而非报错——没有任何东西会提示你编码不对。
_PS_UTF8 = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8'


def is_windows() -> bool:
    """单点平台判定。测试用 monkeypatch.setattr(os, 'name', 'nt') 翻转它。"""
    return os.name == 'nt'


def default_shell() -> str:
    """未显式指定 shell 时用哪个解释器。"""
    return 'powershell' if is_windows() else 'bash'


def shell_argv(command: str, requested: str = ''):
    """把一条命令包装成完整 argv。返回 (argv, label)。

    label 会进结果摘要。默认解释器随平台变化，不显示出来的话，一条因语法不合而
    失败的命令就无从判断它究竟被交给了谁——这在跨平台会话里是最常见的困惑来源。

    Raises:
        ValueError: requested 不是已知的解释器名。宁可报错也不静默回退到 bash：
            在 Windows 上把 PowerShell 命令喂给 bash 会得到一堆语法错误，而错误
            信息里不会有任何线索指向「你要的解释器名拼错了」。
    """
    name = (requested or '').strip().lower() or default_shell()
    if name in ('powershell', 'pwsh'):
        exe = shutil.which('pwsh') or shutil.which('powershell') or 'powershell.exe'
        return ([exe, '-NoProfile', '-NonInteractive', '-Command',
                 _PS_UTF8 + '\n' + command], os.path.basename(exe))
    if name == 'cmd':
        # chcp 与命令本体用 && 串联而非 &：代码页设置失败时不该继续执行，否则输出
        # 编码与读回口径不一致，而这种不一致只会表现为乱码。
        return (['cmd.exe', '/c', 'chcp 65001>nul && ' + command], 'cmd.exe')
    if name != 'bash':
        raise ValueError('unknown shell: %r' % (requested,))
    return ([shutil.which('bash') or '/bin/bash', '-c', command], 'bash')


def detached_kwargs() -> dict:
    """让子进程脱离 ChatApp 进程组，从而在 ChatApp 重启后继续存活。

    start_new_session 在 Windows 上不会报错：CPython 的 Windows 版 _execute_child
    把这个形参命名为 unused_start_new_session 并静默忽略它（见 subprocess.py
    第 1414 行）。所以原先那句「脱离进程组，ChatApp 重启不会杀死它」的注释在
    Windows 上是一句无声的假话，进程会随 ChatApp 一起没。
    """
    if is_windows():
        return {'creationflags': DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def new_process_group_kwargs() -> dict:
    """让子进程进入独立进程组。这是 CTRL_BREAK_EVENT 能定向送达的前提条件。

    POSIX 上刻意返回空字典而不是 start_new_session：持久化终端应该随 ChatApp 一同
    退出，而脱离会话会让它活下来变成孤儿进程。与 detached_kwargs 的差别正在这里，
    两者不可互换。
    """
    if is_windows():
        return {'creationflags': CREATE_NEW_PROCESS_GROUP}
    return {}


def kill_process_tree(pid: int) -> bool:
    """终止一个进程及其全部后代。返回是否成功发出了终止指令。

    Windows 上 os.killpg 这个属性根本不存在，原代码因此会走进 except 退化成
    proc.kill()，只杀掉直接子进程而留下整棵子树继续运行——一条超时的命令看起来
    被清理了，实际还在占着 CPU 和文件锁。
    """
    if is_windows():
        try:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10)
            return True
        except Exception:
            return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


def interactive_shell_argv():
    """持久化终端用的解释器 argv。返回 (argv, label)。

    与 shell_argv 分开是因为要求相反：这里的进程要长期活着并从 stdin 逐行读取命令，
    因此不能带 -NonInteractive——那个开关会让它不再把 stdin 当作命令来源。
    """
    if not is_windows():
        return ([shutil.which('bash') or '/bin/bash'], 'bash')
    exe = shutil.which('pwsh') or shutil.which('powershell')
    if exe:
        return ([exe, '-NoLogo', '-NoProfile', '-Command', '-'], os.path.basename(exe))
    return (['cmd.exe'], 'cmd.exe')


def interactive_prelude(label: str):
    """终端启动后要先喂进去的命令列表，可能为空。

    PowerShell 会把提示符写进 stdout，与命令输出混在同一条流里；把 prompt 函数改成
    返回空串是唯一能从进程内部关掉它的办法。cmd 的 @echo off 同理，它默认回显每一
    条收到的命令。
    """
    low = (label or '').lower()
    if low.startswith(('pwsh', 'powershell')):
        return ["function prompt { '' }", _PS_UTF8]
    if low == 'cmd.exe':
        return ['@echo off', 'chcp 65001>nul']
    return []


def environment_facts() -> dict:
    """tool_system.json 里三个环境占位符的取值。

    那段文本原先写死 `平台：linux` / `Shell：bash` / `操作系统版本：Linux 6.8...`，
    于是模型在 Windows 上会被明确告知自己身处 Linux，然后照旧写 bash 语法。这一处
    不改，其余所有解释器分派都失去意义——它是纯文本，也因此最容易在整理时被跳过。

    返回字典而非三个函数：两个渲染点相隔上百行且缩进层级不同，三行重复替换在那里
    只会变成「改了一处漏了另一处」。
    """
    return {
        '{PLATFORM}': 'windows' if is_windows() else sys.platform,
        '{SHELL}': default_shell(),
        '{OS_VERSION}': platform.platform(),
    }


def interrupt_signal():
    """中断当前解释器该用哪个信号。

    Windows 上没有 SIGINT 可发：send_signal 在那里只接受 CTRL_C_EVENT 与
    CTRL_BREAK_EVENT，而前者无法定向到单个进程组。因此用 CTRL_BREAK_EVENT，它与
    new_process_group_kwargs 是一对——去掉那个创建标志，这个信号就送不到。
    """
    if is_windows():
        return CTRL_BREAK_EVENT
    return signal.SIGINT