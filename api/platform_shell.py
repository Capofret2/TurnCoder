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
CREATE_NO_WINDOW = 0x08000000
# signal.CTRL_BREAK_EVENT 同样只在 Windows 的 signal 模块里存在，Linux 上取不到。
# 用裸值是等价的：Popen.send_signal 拿 == 比较，而 signal.Signals 是 IntEnum。
CTRL_BREAK_EVENT = 1

# Windows PowerShell 5.1 的输出编码跟随 ANSI 代码页（中文系统上是 GBK），而所有
# 输出一律按 UTF-8 读回。pwsh 7 默认已是 UTF-8，重复设置无害。不设的症状是中文
# 乱码而非报错——没有任何东西会提示你编码不对。
_PS_UTF8 = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8'

# pwsh 7 即使 stdout 被重定向到管道也照旧输出 ANSI 颜色转义。实测持久化终端里跑
# Get-Location 收到的是 `\x1b[32;1mPath\x1b[0m`，而 handle_terminal_output 把行原样
# 拼进 term_content、前端不剥 ANSI，于是气泡里显示成 `←[32;1mPath←[0m` 这种乱码。
#
# 用 if 包住不是防御性冗余：$PSStyle 只存在于 PowerShell 7+，5.1 上它是未定义变量
# 即 $null，直接赋值会抛「无法对 Null 表达式设置属性」，而那条错误会作为终端的第一
# 行输出出现在用户面前。包在 if 里之后 5.1 上条件为假、无副作用。
_PS_ANSI_OFF = "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"


# 不是命令、不该被杀的子进程名。conhost 是控制台主机：CREATE_NO_WINDOW 下 pwsh 仍会
# 分配一个，而它作为**直接子进程**出现在枚举结果里。实测枚举到的是
# [(57468, 'conhost.exe'), (34412, 'python.exe')]，只有后者是命令。
#
# 杀掉 conhost 的症状特别容易误判：proc.poll() 返回 None（进程确实还活着），但
# proc.stdin.write(...) 抛 OSError [Errno 22] Invalid argument——控制台句柄被破坏，
# 管道跟着废了。那个表象看起来像 subprocess 用法有问题，而不像「杀错了进程」。
#
# frozenset 而非可变集合：它是常量，被意外改动的后果是终端在某次中断后损坏。
INFRA_PROCESS_NAMES = frozenset({'conhost.exe', 'werfault.exe'})


def child_query_argv(pid: int):
    """返回「枚举 pid 的直接子进程」所需的 argv。

    用 Get-CimInstance 而不是 wmic：后者在 Windows 11 24H2（build 26100）上**已被移除**，
    调用得到 FileNotFoundError [WinError 2]，而那条信息里没有任何线索指向「这个工具在新
    系统上没了」。
    """
    exe = shutil.which('pwsh') or shutil.which('powershell') or 'powershell.exe'
    query = ("Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d' "
             '| ForEach-Object { "$($_.ProcessId) $($_.Name)" }' % pid)
    return [exe, '-NoProfile', '-NonInteractive', '-Command', query]


def parse_child_lines(text: str):
    """把枚举输出解析成 [(pid, name)]，滤掉基础设施进程。

    纯函数，因此这条过滤逻辑能在任意平台上被断言——而它正是「中断会不会把终端弄坏」的
    唯一分界。
    """
    out = []
    for line in (text or '').splitlines():
        head = line.strip().split(' ', 1)
        if not head or not head[0].isdigit():
            continue
        name = (head[1].strip() if len(head) > 1 else '')
        if name.lower() in INFRA_PROCESS_NAMES:
            continue
        out.append((int(head[0]), name))
    return out


def is_windows() -> bool:
    """单点平台判定。测试用 monkeypatch.setattr(os, 'name', 'nt') 翻转它。"""
    return os.name == 'nt'


def default_shell() -> str:
    """未显式指定 shell 时用哪个解释器。"""
    return 'powershell' if is_windows() else 'bash'


def _exe_label(path: str) -> str:
    """可执行文件路径 → 摘要里显示的解释器名。

    剥掉扩展名而不是只取 basename：Windows 上 which 返回的后缀大小写由 PATHEXT 决定，
    实测拿到的是 `pwsh.EXE`，直接用会让每条工具结果的摘要都显示成 `pwsh.EXE: git log`。

    统一之后 label 的取值恰好等于 shell 参数接受的那四个名字（pwsh / powershell / cmd /
    bash），于是摘要里看到 `cmd: dir` 就知道可以写 shell=cmd。这是它值得统一的实质理由，
    不只是观感。
    """
    return os.path.splitext(os.path.basename(path))[0].lower()


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
                 _PS_UTF8 + '\n' + command], _exe_label(exe))
    if name == 'cmd':
        # chcp 与命令本体用 && 串联而非 &：代码页设置失败时不该继续执行，否则输出
        # 编码与读回口径不一致，而这种不一致只会表现为乱码。
        # argv 里保留 cmd.exe（真实要执行的文件名），label 用 cmd（展示与 shell 参数的
        # 词汇）。两者是不同的东西，混淆它们会让命令根本启动不起来。
        return (['cmd.exe', '/c', 'chcp 65001>nul && ' + command], 'cmd')
    if name != 'bash':
        raise ValueError('unknown shell: %r' % (requested,))
    return ([shutil.which('bash') or '/bin/bash', '-c', command], 'bash')


def detached_kwargs() -> dict:
    """让子进程脱离 ChatApp 进程组，从而在 ChatApp 重启后继续存活。

    start_new_session 在 Windows 上不会报错：CPython 的 Windows 版 _execute_child
    把这个形参命名为 unused_start_new_session 并静默忽略它（见 subprocess.py
    第 1414 行）。所以原先那句「脱离进程组，ChatApp 重启不会杀死它」的注释在
    Windows 上是一句无声的假话，进程会随 ChatApp 一起没。

    **不要把 DETACHED_PROCESS 加回来。** 实测（Windows 10 / pwsh 7）：带上它之后
    PowerShell 会启动、host 初始化失败、不执行任何命令、以退出码 0 退出——既没有
    输出，也没有副作用，而调用方看到的是「成功」。cmd.exe 与 python.exe 在同一
    标志下完全正常，所以这不是通用的重定向问题，是 PowerShell 需要控制台才能初始化
    它的输出管道。这个失败形式是最坏的一种：不报错，假装成功。

    **DETACHED_PROCESS 同时造成两个看起来无关的症状，别只修一个。** 它的语义不是
    「没有控制台」而是「不继承父进程的控制台」，于是控制台程序会自己新分配一个：
      1. cmd.exe 与 python.exe 因此每次执行都闪一个黑窗口（用户可见的那个）；
      2. pwsh 7 的 host 在这种状态下初始化失败，进而不执行任何命令、以 0 退出。
    两者同源。只修掉闪窗而保留这个标志，PowerShell 那条静默空操作还在；反过来也一样。

    CREATE_NO_WINDOW 的语义是「明确不创建控制台窗口」，两个症状一并消除且重定向完好。
    实测（Windows 10）：pwsh 7 与 Windows PowerShell 5.1 在 NO_WINDOW|NEW_PROCESS_GROUP
    下 stdout 与副作用都正常。

    CREATE_NEW_PROCESS_GROUP 仍然保留：它让子进程不接收发往 ChatApp 那个进程组的
    Ctrl+C，也就是「按 Ctrl+C 重启 ChatApp」这个主要场景依然安全。挡不住的是整个终端
    窗口被关闭时广播的 CTRL_CLOSE_EVENT——这是接受的取舍，因为后台命令存在的全部意义
    就是稍后读它的输出，一个没有输出的后台命令是纯粹的浪费。
    """
    if is_windows():
        return {'creationflags': CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def new_process_group_kwargs() -> dict:
    """让子进程进入独立进程组。这是 CTRL_BREAK_EVENT 能定向送达的前提条件。

    POSIX 上刻意返回空字典而不是 start_new_session：持久化终端应该随 ChatApp 一同
    退出，而脱离会话会让它活下来变成孤儿进程。与 detached_kwargs 的差别正在这里，
    两者不可互换。

    CREATE_NO_WINDOW 在这里比在一次性执行那边更要紧：一次性命令的控制台窗口是闪一下，
    而终端进程长期存活，它的窗口会一直挂在桌面上。

    未经真机验证：诊断矩阵测的是一次性执行（-Command 加立即退出）。交互式 PowerShell
    从 stdin 逐行读命令时 CREATE_NO_WINDOW 的行为没有被覆盖，需要在界面上真开一个终端
    才能确认。若终端启动后无响应，先试着摘掉 CREATE_NO_WINDOW 单独验证这一项。
    """
    if is_windows():
        return {'creationflags': CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {}


def kill_process_tree(pid: int) -> bool:
    """终止一个进程及其全部后代。返回是否成功发出了终止指令。

    Windows 上 os.killpg 这个属性根本不存在，原代码因此会走进 except 退化成
    proc.kill()，只杀掉直接子进程而留下整棵子树继续运行——一条超时的命令看起来
    被清理了，实际还在占着 CPU 和文件锁。
    """
    if is_windows():
        try:
            # CREATE_NO_WINDOW 是防御性的：taskkill 是控制台程序，当前它继承 ChatApp 的
            # 控制台所以不闪窗，但 ChatApp 被 pythonw.exe 启动或作为服务运行时父进程没有
            # 控制台，taskkill 就会自己分配一个——正是别处刚修掉的那个闪窗机制。
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
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
        return ([exe, '-NoLogo', '-NoProfile', '-Command', '-'], _exe_label(exe))
    return (['cmd.exe'], 'cmd')


def interactive_prelude(label: str):
    """终端启动后要先喂进去的命令列表，可能为空。

    PowerShell 会把提示符写进 stdout，与命令输出混在同一条流里；把 prompt 函数改成
    返回空串是唯一能从进程内部关掉它的办法。cmd 的 @echo off 同理，它默认回显每一
    条收到的命令。

    label 必须是 interactive_shell_argv 产出的那些值（见 _exe_label）。这两个函数是一对：
    改了 label 格式而没同步这里的匹配，cmd 终端会失去 @echo off 与 chcp，症状是每条命令
    被回显一遍加中文乱码，而不是任何形式的报错。
    """
    low = (label or '').lower()
    if low.startswith(('pwsh', 'powershell')):
        return ["function prompt { '' }", _PS_ANSI_OFF, _PS_UTF8]
    if low == 'cmd':
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


def interrupt_process(proc) -> str:
    """中断 proc 正在执行的命令。返回一句描述做了什么，供调用方打印。

    POSIX 上发 SIGINT：bash 中止当前命令并继续从 stdin 读下一条，这是它一直以来的行为。

    **Windows 上不发信号。** 实测 CTRL_BREAK_EVENT 是静默空操作：`Start-Sleep 20` 收到它
    之后仍然跑满 20 秒，`send_signal` 正常返回、shell 存活、终端此后仍可用——但那条命令
    完全没被中断。三个候选解释已被逐一实测排除（`CREATE_NO_WINDOW` 互斥、只有 cmdlet
    受影响、wmic 可用来枚举），见 docs/HANDOVER.md 第三节第 7 项，别重走。

    改为枚举 shell 的直接子进程并 taskkill 掉。**这只覆盖外部命令。** cmdlet（Start-Sleep、
    Get-Content 之类）跑在 pwsh 进程内部、没有子进程可杀，那一类无解——实测枚举结果过滤后
    是空列表。实际需要中断的几乎都是外部命令（跑测试、构建、训练），所以覆盖面够用。

    返回描述而不是 None：cmdlet 那种情形下没有任何可杀之物，若不把这件事说出来，调用方无从
    区分「中断成功」与「什么都没做」，而后者正是用户看到「点了中断没反应」的情形。

    **杀掉子进程之后不需要手动改 state。** stdin 里那条哨兵 echo 已经排在命令后面，命令一死
    它立刻被执行，state 顺着既有路径回到 idle——实测 0.24 秒。手动置 idle 反而会与哨兵的
    到达竞争。
    """
    if not is_windows():
        proc.send_signal(signal.SIGINT)
        return 'SIGINT 已发送'

    try:
        r = subprocess.run(child_query_argv(proc.pid), capture_output=True, text=True,
                           timeout=30, encoding='utf-8', errors='replace',
                           creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        return '子进程枚举失败: %r' % (e,)

    kids = parse_child_lines(r.stdout)
    if not kids:
        return ('没有可中断的子进程。cmdlet 跑在解释器内部、没有子进程可杀，'
                '这一类无法中断，只能等它自己结束')
    killed = [str(k) for k, _n in kids if kill_process_tree(k)]
    return '已终止子进程 %s（共 %d 个候选）' % (','.join(killed) or '无', len(kids))


# interrupt_signal() 曾在这里。它返回「该发哪个信号」，Windows 上给 CTRL_BREAK_EVENT。
# 删掉的理由不只是零引用：那个信号已被实测证明是静默空操作（详见 interrupt_process 的
# docstring），所以一个名字与返回值都在宣称它有效的函数，等于在代码里放一个看起来权威
# 的错误答案。CTRL_BREAK_EVENT 常量本身留着——它是 ABI 数值，别处的注释会引用它。

# exec_or_spawn() 也曾在这里，随自动更新功能一并移除。它与上面那条删除的性质完全不同，
# 值得区分清楚：interrupt_signal 是**实现被证伪**（它返回的信号是静默空操作），而
# exec_or_spawn 的实现是正确的，只是它唯一的调用场景——updater.py 那个启动器——整体
# 不存在了，于是它成了零调用方的平台分支。
#
# 若将来重新引入某种启动脚本，这里记下它当时解决的问题，免得重新踩一遍：POSIX 上
# os.execv **替换**当前进程映像、PID 不变、不留额外进程；Windows 上没有这个语义，
# CPython 的实现是新起一个进程再让原进程立即退出，于是父进程句柄失效、控制台归属混乱，
# 在被 shell 启动的场景下表现为「命令看起来结束了但服务在后台继续跑」，用户拿回提示符
# 却发现端口被占着。当时的做法是 Windows 走 subprocess.call 加 sys.exit(返回码)：等子
# 进程结束再退出，shell 保持阻塞、控制台归属清晰，且退出码必须透传——那是外层脚本判断
# 应用成败的唯一依据。