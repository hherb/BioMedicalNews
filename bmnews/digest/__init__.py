"""Email digest rendering and delivery."""

from bmnews.digest.renderer import render_digest
from bmnews.digest.sender import send_email

__all__ = ["render_digest", "send_email"]
