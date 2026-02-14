"""LLM-based relevance scoring and summary generation agent.

Uses bmlib.agents.BaseAgent for LLM interaction and bmlib.templates
for prompt rendering. Returns structured JSON with relevance score
and a concise summary.
"""

from __future__ import annotations

import logging
from typing import Any

from bmlib.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class RelevanceAgent(BaseAgent):
    """Scores paper relevance against user research interests and generates summaries."""

    def score(
        self,
        title: str,
        abstract: str,
        interests: str,
        categories: str = "",
    ) -> dict[str, Any]:
        """Score a paper for relevance and generate a summary.

        Returns a dict with keys:
            - relevance_score (float 0.0–1.0)
            - summary (str, 2–3 sentence summary)
            - relevance_rationale (str, brief explanation)
            - key_findings (list[str])
            - matched_tags (list[str])
        """
        prompt = self.render_template(
            "relevance_scoring.txt",
            title=title,
            abstract=abstract,
            interests=interests,
            categories=categories,
        )

        system_prompt = self.render_template("relevance_system.txt")

        response = self.chat(
            [self.system_msg(system_prompt), self.user_msg(prompt)],
            json_mode=True,
        )

        try:
            result = self.parse_json(response.content)
        except ValueError:
            logger.warning("Failed to parse scoring response for: %s", title[:80])
            result = {
                "relevance_score": 0.0,
                "summary": "",
                "relevance_rationale": "Parse error",
                "key_findings": [],
                "matched_tags": [],
            }

        # Clamp score
        score = float(result.get("relevance_score", 0.0))
        result["relevance_score"] = max(0.0, min(1.0, score))

        return result
