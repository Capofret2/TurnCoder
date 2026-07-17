"""Api mixin: Worker utilities - base36 encoding and offline response handling."""
import re



class WorkerMixin:
    """Api mixin: Worker utilities - base36 encoding and offline response handling."""

    def _to_base36(self, num):
        if num == 0: return '0'
        chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        base36 = ''
        while num > 0:
            num, i = divmod(num, 36)
            base36 = chars[i] + base36
        return base36.lower()



    def process_offline_response(self, text):
        import re
        match = re.search(r'\[令牌([a-z0-9]+)\]', text)
        target_id = int(match.group(1)) if match else None
        valid_sid = None
        
        if match:
            try:
                target_id = int(match.group(1), 36)
            except ValueError:
                target_id = None
        else:
            target_id = None
            
        if target_id is not None:
            for sid, session in self.sessions.items():
                if any(m['id'] == target_id and not m['content'] for m in session['conversation_history']):
                    valid_sid = sid
                    break
                    
        if not valid_sid:
            valid_sid = self._active_sid
            
        if valid_sid:
            self.process_ai_response(valid_sid, text, 0.0, 0.0, is_parallel=False, original_target_id=target_id, is_offline_return=True)



