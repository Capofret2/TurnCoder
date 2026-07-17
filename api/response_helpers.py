"""Api mixin: Response finalization and error recovery."""


class ResponseHelpersMixin:
    """Api mixin: Response finalization and error recovery."""

    def maybe_finalize_after_error(self, sid, is_parallel):
        session = self.sessions.get(sid)
        if not session:
            return
        if is_parallel:
            session['active_threads'] = max(0, session.get('active_threads', 0) - 1)
            if session['active_threads'] <= 0:
                self.finalize_process(sid)
        else:
            self.finalize_process(sid)


    def finalize_process(self, sid):
        session = self.sessions[sid]
        session['is_processing'] = False
        self.save_sessions(push_update=True)
        if session['message_queue'] and not session['is_paused']:
            next_msg = session['message_queue'].pop(0)
            models = next_msg.pop('target_models', [self.config.get("MODEL_NAME", "")])
            _deep_think = next_msg.pop('_deep_think', False)
            session['conversation_history'].append(next_msg)
            self.save_sessions()
            self.start_api_thread(sid, model_name=models[0] if models else None, is_deep_think=_deep_think)



