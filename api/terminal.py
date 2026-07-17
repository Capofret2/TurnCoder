"""Api mixin: Terminal process management - persistent shell sessions."""
import os
import queue
import subprocess
import threading
import time


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
        
        is_win = os.name == 'nt'
        shell = ["cmd.exe"] if is_win else ["/bin/bash"]
        
        self.process = subprocess.Popen(
            shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
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
        is_win = os.name == 'nt'
        while self.is_running:
            try:
                cmd = self.cmd_queue.get(timeout=1)
                self.state = "running"
                self.api.handle_terminal_status(self.sid, self.name, "running")
                self.process.stdin.write(cmd + "\n")
                if is_win:
                    self.process.stdin.write(f"echo __TERM_DONE_{self.name}__\n")
                else:
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
        if self.process and self.process.poll() is None:
            if os.name != 'nt':
                import signal
                try:
                    self.process.send_signal(signal.SIGINT)
                except:
                    pass

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


