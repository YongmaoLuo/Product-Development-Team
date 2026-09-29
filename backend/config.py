"""
Agent Configuration
===================

Configuration for domain-specific agent behavior.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentConfig:
    """Configuration for domain-specific agent behavior."""

    # Planner prompts
    planner_system_prompt: str  # System prompt for planning
    planner_task_template: Optional[str] = None  # Template for task generation (optional)

    # Executor prompts
    executor_system_prompt: str = ""  # System prompt for execution
    executor_task_template: Optional[str] = None  # Template for task execution (optional)

    # Refiner prompts
    refiner_system_prompt: str = ""  # System prompt for refinement

    # Domain context
    domain_knowledge: str = ""  # Background knowledge for the domain
    file_patterns: List[str] = field(default_factory=lambda: ["*"])  # Relevant file patterns

    # Behavior settings
    max_retries: int = 5
    background_task_timeout: int = 1800  # 30 minutes max per task

    # Model map (复杂/中等模型分类). 详见 _base.yaml 中 model_map 段。
    # 结构：
    #   {
    #     "complex": {"vendor-b": {"default": "...", "haiku": "..."},
    #                 "vendor-a": {...}},
    #     "medium":  {"vendor-b": {...}, "vendor-a": {...}},
    #   }
    # 当前默认 complex 和 medium 同值，预留未来对 medium 做降级处理。
    model_map: Optional[Dict[str, Dict]] = None

    # Provider fallback order — list of provider keys to try in order.
    # Reads from configs/_base.yaml  field, e.g.:
    #   provider_priority: [vendor-b, vendor-a]   # cheap first, then fallback
    #   provider_priority: [vendor-a, vendor-b]   # fallback first (e.g. for rate limit avoidance)
    # Each item must match a key in model_map (e.g. "vendor-b", "vendor-a").
    # "parent" is always appended implicitly as the last fallback.
    provider_priority: Optional[list] = None