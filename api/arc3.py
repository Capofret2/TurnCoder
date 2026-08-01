"""Api mixin: ARC3 environment integration and step handling."""
import re
import time

from .provider_routes import strip_composite



class Arc3Mixin:
    """Api mixin: ARC3 environment integration and step handling."""

    def _handle_arc3_step(self, sid, session, target_id, model_name):
        """处理 ARC3 环境交互，通过 HTTP 调用本地环境桥接服务"""
        import requests as req
        env_id = strip_composite(model_name).replace("[ARC3]", "", 1)

        action_text = ""
        for m in reversed(session['conversation_history']):
            if m['role'] == 'user' and m.get('id') != target_id and m.get('content', '').strip():
                action_text = m.get('content', '').strip()
                break
            elif m['role'] == 'assistant' and not m.get('model_name', '').startswith('[ARC3]') and m.get('content', '').strip():
                # LLM 回复：仅取最后一行作为动作指令
                content = m.get('content', '').strip()
                action_text = content.split('\n')[-1].strip()
                break

        target_msg = next((m for m in session['conversation_history'] if m['id'] == target_id), None)
        if not target_msg:
            self.finalize_process(sid)
            return

        是否重置 = not action_text or 'RESET' in action_text.upper() or '重置' in action_text
        arc3_url = "http://127.0.0.1:5002"

        try:
            if 是否重置:
                resp = req.post(f"{arc3_url}/reset", json={"env_id": env_id, "session_id": sid}, timeout=15)
            else:
                resp = req.post(f"{arc3_url}/step", json={"session_id": sid, "action": action_text}, timeout=15)

            data = resp.json()

            if resp.status_code != 200 or data.get('error'):
                target_msg['content'] = f"环境错误: {data.get('error', '未知')}\n{data.get('trace', '')}"
                target_msg['summary'] = "环境错误"
                target_msg['is_error'] = True
            else:
                frames = data.get('frames', [])
                if frames:
                    is_first_text_bubble = True
                    for idx, frame in enumerate(frames):
                        text_map = frame.get('text_map', '')
                        step_val = frame.get('step', 0)
                        if is_first_text_bubble:
                            target_msg['content'] = text_map if text_map else "(无网格文本)"
                            target_msg['summary'] = f"{env_id} 步{step_val} 文本"
                            target_msg['model_name'] = f"[ARC3]{env_id}"
                            target_msg['is_collapsed'] = True
                            is_first_text_bubble = False
                        else:
                            txt_msg = self._make_msg("assistant",
                                text_map if text_map else "(无网格文本)",
                                summary=f"{env_id} 步{step_val} 文本",
                                is_collapsed=True, model_name=f"[ARC3]{env_id}")
                            session['conversation_history'].append(txt_msg)
                            
                        img_content_parts = []
                        if idx == len(frames) - 1:
                            actions_str = '、'.join(frame.get('available_actions', []))
                            if actions_str:
                                img_content_parts.append(f"可用动作：{actions_str} | 当前状态：{frame.get('state')} | 步数：{step_val}")
                        
                        if frame.get('label'):
                            img_content_parts.append(f"{frame.get('label')}")
                            
                        img_msg = self._make_msg("assistant",
                            "\n\n".join(img_content_parts),
                            summary=frame.get('label', f"{env_id} 画面"),
                            is_collapsed=idx < len(frames) - 1,
                            model_name=f"[ARC3]{env_id}")
                        if frame.get('image_base64'):
                            img_msg["image"] = {"base64": frame['image_base64'], "mime_type": "image/png"}
                            
                        session['history_buffer_tmp_var'] = True 
                        session['conversation_history'].append(img_msg)

                        if frame.get('system_hint'):
                            sys_msg = self._make_msg("assistant", frame['system_hint'],
                                summary="系统指令", model_name=f"[ARC3]{env_id}")
                            session['conversation_history'].append(sys_msg)
                            
                else:
                    # 兼容旧格式 fallback
                    res_item = data
                    if res_item.get('system_hint'):
                        sys_msg = self._make_msg("assistant", res_item['system_hint'],
                            summary="系统指令", model_name=f"[ARC3]{env_id}")
                        session['conversation_history'].append(sys_msg)

                    parts = []
                    if res_item.get('action_taken'):
                        parts.append(f"**{res_item['action_taken']}**")
                    actions_str = '、'.join(res_item.get('available_actions', []))
                    if actions_str:
                        parts.append(f"可用动作：{actions_str} | 当前状态：{res_item.get('state')} | 步数：{res_item.get('step')}")
                    if res_item.get('text_map'):
                        parts.append(res_item['text_map'])
                    target_msg['content'] = "\n\n".join(parts)
                    target_msg['summary'] = f"{env_id} 步{res_item.get('step', 0)}"
                    target_msg['model_name'] = f"[ARC3]{env_id}"
                    if res_item.get('image_base64'):
                        target_msg['image'] = {"base64": res_item['image_base64'], "mime_type": "image/png"}

        except Exception as e:
            target_msg['content'] = f"环境服务连接失败: {str(e)}\n\n请确认 arc_env_server.py 已在 conda arc3 环境中的 5002 端口启动。"
            target_msg['summary'] = "连接失败"
            target_msg['is_error'] = True

        session['last_updated'] = time.time()
        self.save_sessions(push_update=True)

        if session.get('autopilot_active') and session.get('autopilot_turns_left', 0) > 0:
            session['autopilot_turns_left'] -= 1
            if session['autopilot_turns_left'] <= 0:
                session['autopilot_active'] = False
            
            self._apply_sliding_window(session)
            
            # 向大模型抛去控制权，发一条透明触发包或空触发包
            new_msg = self._make_msg("user", "", summary="环境已回调大模型", is_hidden=True)
            session['conversation_history'].append(new_msg)
            self.save_sessions()
            self.start_api_thread(sid, model_name=session.get('autopilot_model'))
            return

        self.finalize_process(sid)


