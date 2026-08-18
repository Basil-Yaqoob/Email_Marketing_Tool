"""Multi-step sequences — follow-ups with threading.

- Any human reply halts sequence immediately
- Out-of-office does not halt; it reschedules
- Follow-ups thread onto original via In-Reply-To and References
- A failed linter blocks that step, not the campaign
"""

from __future__ import annotations

__all__ = []
