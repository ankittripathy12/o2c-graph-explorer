
import time
import logging
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

MAX_TURNS   = 10    
SESSION_TTL = 1800  


class ConversationMemory:
    def __init__(self):

        self._history: Dict[str, List[Dict]] = defaultdict(list)
       
        self._last_access: Dict[str, float] = {}

    def add(self, session_id: str, role: str, content: str) -> None:
        """Add a message to the session history."""
        self._history[session_id].append({"role": role, "content": content})
        self._last_access[session_id] = time.time()
        # Trim to MAX_TURNS pairs (2 messages per turn)
        max_msgs = MAX_TURNS * 2
        if len(self._history[session_id]) > max_msgs:
            self._history[session_id] = self._history[session_id][-max_msgs:]

    def get(self, session_id: str) -> List[Dict]:
        """Get full message history for a session."""
        self._last_access[session_id] = time.time()
        return list(self._history.get(session_id, []))

    def get_context_summary(self, session_id: str) -> str:
        """
        Return a plain-text summary of recent exchanges.
        Used to inject context into the Cypher generation prompt.
        """
        history = self.get(session_id)
        if not history:
            return ""
        lines = []
        for msg in history[-6:]:   # last 3 turns
            role = "User" if msg["role"] == "user" else "Assistant"
            # truncate long assistant answers
            content = msg["content"][:200] + "…" if len(msg["content"]) > 200 else msg["content"]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def clear(self, session_id: str) -> None:
        """Clear history for a session."""
        self._history.pop(session_id, None)
        self._last_access.pop(session_id, None)

    def cleanup_expired(self) -> int:
        """Remove sessions older than SESSION_TTL. Returns count removed."""
        now = time.time()
        expired = [
            sid for sid, ts in self._last_access.items()
            if now - ts > SESSION_TTL
        ]
        for sid in expired:
            self.clear(sid)
        if expired:
            logger.info("Cleaned up %d expired sessions", len(expired))
        return len(expired)

    def session_info(self, session_id: str) -> Dict:
        """Return metadata about a session."""
        history = self.get(session_id)
        return {
            "session_id":   session_id,
            "message_count": len(history),
            "turns":        len(history) // 2,
        }


# singleton used by the query router
memory = ConversationMemory()
