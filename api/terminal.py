"""Api mixin: Terminal process management - persistent shell sessions."""
import queue
import subprocess
import threading
import time

from .platform_shell import (interactive_prelude, interactive_shell_argv,
                             interrupt_signal, new_process_group_kwargs)


class TerminalProcess:
    def __init__(self, name, sid, api_instance):
        self.name = name
        self.sid = sid
        self.api = api_instance
        self.cmd_queue = queue.Queue()
        self.is_running = True
        self.state = "idle"
        self.msg_id = None
        self.process = None
        
        # Windows 上优先 pwsh、回退 powershell.exe、最后才是 cmd.exe。原先直接写死
        # cmd.exe，因此 Windows 用户从持久化终端里永远拿不到 PowerShell。
        argv, self.shell_label = interactive_shell_argv()
        
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # 不给 encoding 就走 locale.getpreferredencoding()，中文 Windows 上是
            # GBK，而 PowerShell 的输出编码由 $OutputEncoding 决定。两边不对齐的
            # 症状是中文乱码而非报错，不会有任何东西提示你编码错了。
            encoding='utf-8',
            errors='replace',
            **new_process_group_kwargs()
        )
        # PowerShell 把提示符写进 stdout，与命令输出混在同一条流里；cmd 默认回显每
        # 条收到的命令。两者都必须在第一条真实命令之前关掉，否则 _read_output 会把
        # 提示符当成输出行推给前端。
        for _pre in interactive_prelude(self.shell_label):
            self.process.stdin.write(_pre + '\n')
        self.process.stdin.flush()
        
        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def _read_output(self):
        for line in iter(self.process.stdout.readline, ''):
            if not line: break
            if f"__TERM_DONE_{self.name}__" in line:
                if self.cmd_queue.empty():
                    self.state = "idle"
                    self.api.handle_terminal_status(self.sid, self.name, "idle")
                continue
            self.api.handle_terminal_output(self.sid, self.name, line)

    def _worker(self):
        while self.is_running:
            try:
                cmd = self.cmd_queue.get(timeout=1)
                self.state = "running"
                self.api.handle_terminal_status(self.sid, self.name, "running")
                self.process.stdin.write(cmd + "\n")
                # 哨兵用 echo 而不是各 shell 的原生写法：bash、PowerShell（echo 是
                # Write-Output 的别名）与 cmd 三者都认它，所以这里本来就不需要平台
                # 分支。原代码的 if is_win / else 两条分支写的是完全相同的一行。
                self.process.stdin.write(f"echo __TERM_DONE_{self.name}__\n")
                self.process.stdin.flush()
                self.cmd_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                pass

    def execute(self, cmd):
        self.state = "running"
        self.cmd_queue.put(cmd)

    def interrupt(self):
        """中断当前正在执行的命令。

        Windows 上原先整条分支是空的：点中断毫无反应且不报错，看起来像命令还没跑完。
        现在走 CTRL_BREAK_EVENT，它与创建时的 CREATE_NEW_PROCESS_GROUP 是一对——缺
        了那个创建标志，这个信号会打到 ChatApp 自己身上。
        """
        if not self.process or self.process.poll() is not None:
            return
        try:
            self.process.send_signal(interrupt_signal())
        except Exception as e:
            print(f'[TERMINAL] 中断失败 ({getattr(self, "shell_label", "?")}): {e}', flush=True)

    def terminate(self):
        self.is_running = False
        if self.process:
            self.process.terminate()




class TerminalMixin:
    """Api mixin: Terminal process management - persistent shell sessions."""

    def handle_terminal_output(self, sid, term_name, line):
        session = self.sessions.get(sid)
        if not session: return
        term = self.terminals.get(sid, {}).get(term_name)
        if not term or not term.msg_id: return
        
        msg = next((m for m in session['conversation_history'] if m['id'] == term.msg_id), None)
        if msg:
            if 'term_content' not in msg:
                msg['term_content'] = ""
            msg['term_content'] += line
            
            # 定期置底和触发保存更新（简易节流）
            current_time = time.time()
            if current_time - term.api.term_output_buffer.get(f"{sid}_{term_name}", 0) > 0.5:
                msg['content'] = msg['term_content']
                try:
                    session['conversation_history'].remove(msg)
                except ValueError:
                    pass
                session['conversation_history'].append(msg)
                self.save_sessions(push_update=True)
                term.api.term_output_buffer[f"{sid}_{term_name}"] = current_time


    def handle_terminal_status(self, sid, term_name, status):
        session = self.sessions.get(sid)
        if not session: return
        
        # 更新终端气泡的运行状态标记，供前端区分颜色
        term_ref = self.terminals.get(sid, {}).get(term_name)
        if term_ref and term_ref.msg_id:
            state_msg = next((m for m in session['conversation_history'] if m['id'] == term_ref.msg_id), None)
            if state_msg:
                state_msg['term_state'] = status
                if status == "running":
                    self.save_sessions(push_update=True)
        
        # 终端进入空闲时，强制将缓冲中的全部输出同步到气泡内容，防止节流吞尾
        if status == "idle":
            term = self.terminals.get(sid, {}).get(term_name)
            if term and term.msg_id:
                msg = next((m for m in session['conversation_history'] if m['id'] == term.msg_id), None)
                if msg and msg.get('term_content'):
                    msg['content'] = msg['term_content']
                    # 置底
                    try:
                        session['conversation_history'].remove(msg)
                    except ValueError:
                        pass
                    session['conversation_history'].append(msg)
                    self.save_sessions(push_update=True)
        
        # 当状态变更为 idle 时，检查是否处于托管模式并等待终端，视情况推进
        if status == "idle" and session.get('waiting_for_terminals'):
            any_running = False
            for term in self.terminals.get(sid, {}).values():
                if term.state != "idle":
                    any_running = True
                    break
                    
            if not any_running:
                session['waiting_for_terminals'] = False
                if session.get('autopilot_turns_left', 0) > 0:
                    session['autopilot_turns_left'] -= 1
                    if session['autopilot_turns_left'] <= 0:
                        session['autopilot_active'] = False
                        self.save_sessions(push_update=True)
                        return
                    
                    _term_append = "\n\n【系统拦截】当前所有活跃终端均已执行完毕并处于静止状态。请查看终端输出结果，判断接下来的操作并继续。"
                    auto_text = self._make_autopilot_prompt(session, append=_term_append)
                    new_msg = self._make_msg("user", auto_text, summary="终端执行完毕")
                    session['conversation_history'].append(new_msg)
                    self.save_sessions(push_update=True)
                    self.start_api_thread(sid, model_name=session.get('autopilot_model'))


