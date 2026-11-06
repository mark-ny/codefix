"""Confidence thresholds."""
from __future__ import annotations

from ..models import Finding, ProjectConfig


def threshold_for(config: ProjectConfig, finding: Finding) -> float:
    """Security findings use the stricter of the two configured minimums."""
    if finding.category == "security":
        return max(config.minimum_confidence, config.security_minimum_confidence)
    return config.minimum_confidence


def meets_threshold(config: ProjectConfig, finding: Finding) -> bool:
    return finding.confidence + 1e-9 >= threshold_for(config, finding)
