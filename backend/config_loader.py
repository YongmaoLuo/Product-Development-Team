"""
Configuration Loader
====================

Load agent configuration from YAML files with base config merging support.
"""

import os
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from config import AgentConfig


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge two dictionaries. Override values take precedence.

    Args:
        base: Base dictionary
        override: Override dictionary

    Returns:
        Merged dictionary
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _build_planner_prompt(config_data: Dict[str, Any]) -> str:
    """
    Build the planner system prompt from config data.

    The planner prompt is built in this order:
    1. Domain-specific planner_intro (who am I)
    2. Task ID rules (from base)
    3. Domain-specific planning_guidelines (how to plan)
    4. JSON format (from base)

    Args:
        config_data: Raw config data from YAML (merged with base)

    Returns:
        Complete planner system prompt
    """
    parts = []

    # 1. Domain-specific planner intro
    if 'planner_intro' in config_data:
        parts.append(config_data['planner_intro'])

    # 2. Task ID rules from base
    if '_task_id_rules' in config_data:
        parts.append(config_data['_task_id_rules'])

    # 3. Domain-specific planning guidelines
    if 'planning_guidelines' in config_data:
        parts.append(config_data['planning_guidelines'])

    # 4. JSON format from base
    if '_planner_json_format' in config_data:
        parts.append(config_data['_planner_json_format'])

    return '\n\n'.join(parts)


def _build_executor_prompt(config_data: Dict[str, Any]) -> str:
    """
    Build the executor system prompt from config data.

    The executor prompt is built in this order:
    1. Domain-specific executor_intro (who am I)
    2. Domain-specific implementation_guidelines (how to implement)
    3. File format (from base)

    Args:
        config_data: Raw config data from YAML (merged with base)

    Returns:
        Complete executor system prompt
    """
    parts = []

    # 1. Domain-specific executor intro
    if 'executor_intro' in config_data:
        parts.append(config_data['executor_intro'])

    # 2. Domain-specific implementation guidelines
    if 'implementation_guidelines' in config_data:
        parts.append(config_data['implementation_guidelines'])

    # 3. File format from base
    if '_executor_file_format' in config_data:
        parts.append(config_data['_executor_file_format'])

    return '\n\n'.join(parts)


def _build_refiner_prompt(config_data: Dict[str, Any]) -> str:
    """
    Build the refiner system prompt from config data.

    The refiner prompt is built in this order:
    1. Domain-specific refiner_intro (who am I)
    2. Domain-specific refinement_guidelines (how to refine)
    3. Task ID rules (from base)
    4. Failure rules (from base)
    5. JSON format (from base)

    Args:
        config_data: Raw config data from YAML (merged with base)

    Returns:
        Complete refiner system prompt
    """
    parts = []

    # 1. Domain-specific refiner intro
    if 'refiner_intro' in config_data:
        parts.append(config_data['refiner_intro'])

    # 2. Domain-specific refinement guidelines
    if 'refinement_guidelines' in config_data:
        parts.append(config_data['refinement_guidelines'])

    # 3. Task ID rules from base
    if '_task_id_rules' in config_data:
        parts.append(config_data['_task_id_rules'])

    # 4. Failure rules from base
    if '_refiner_failure_rules' in config_data:
        parts.append(config_data['_refiner_failure_rules'])

    # 5. JSON format from base
    if '_refiner_json_format' in config_data:
        parts.append(config_data['_refiner_json_format'])

    return '\n\n'.join(parts)


def load_config(config_path: str, merge_base: bool = True) -> AgentConfig:
    """
    Load agent configuration from YAML file.

    The config file is merged with _base.yaml to include common components.
    Domain-specific configs only need to define their unique content.

    Args:
        config_path: Path to YAML configuration file
        merge_base: Whether to merge with base config (default: True)

    Returns:
        AgentConfig instance

    Raises:
        ImportError: If PyYAML is not installed
        FileNotFoundError: If config file doesn't exist
        ValueError: If config file is invalid
    """
    if not YAML_AVAILABLE:
        raise ImportError(
            "PyYAML is required for loading config files. "
            "Install it with: pip install PyYAML"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, 'r') as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid config file format: expected dict, got {type(data)}")

    # Merge with base config if requested
    if merge_base:
        configs_dir = path.parent
        base_path = configs_dir / "_base.yaml"
        if base_path.exists():
            with open(base_path, 'r') as f:
                base_data = yaml.safe_load(f)
            if isinstance(base_data, dict):
                data = _deep_merge(base_data, data)

    # Build prompts from components
    planner_prompt = _build_planner_prompt(data)
    executor_prompt = _build_executor_prompt(data)
    refiner_prompt = _build_refiner_prompt(data)

    return AgentConfig(
        planner_system_prompt=planner_prompt or data.get('planner_system_prompt', ''),
        planner_task_template=data.get('planner_task_template'),
        executor_system_prompt=executor_prompt or data.get('executor_system_prompt', ''),
        executor_task_template=data.get('executor_task_template'),
        refiner_system_prompt=refiner_prompt or data.get('refiner_system_prompt', ''),
        domain_knowledge=data.get('domain_knowledge', ''),
        file_patterns=data.get('file_patterns', ['*']),
        max_retries=data.get('max_retries', 5),
        background_task_timeout=data.get('background_task_timeout', 180),
        model_map=data.get('model_map'),
        provider_priority=data.get('provider_priority'),
    )


def load_config_by_name(name: str, configs_dir: Optional[str] = None) -> Optional[AgentConfig]:
    """
    Load a configuration by name from the configs directory.

    Args:
        name: Configuration name (without .yaml extension)
        configs_dir: Optional custom configs directory path

    Returns:
        AgentConfig instance or None if not found
    """
    if configs_dir is None:
        # Default to configs directory relative to this module
        configs_dir = Path(__file__).parent / "configs"
    else:
        configs_dir = Path(configs_dir)

    config_path = configs_dir / f"{name}.yaml"
    if config_path.exists():
        return load_config(str(config_path))

    return None


def list_available_configs(configs_dir: Optional[str] = None) -> list:
    """
    List all available configuration names.

    Args:
        configs_dir: Optional custom configs directory path

    Returns:
        List of configuration names (without .yaml extension)
    """
    if configs_dir is None:
        configs_dir = Path(__file__).parent / "configs"
    else:
        configs_dir = Path(configs_dir)

    if not configs_dir.exists():
        return []

    configs = []
    for config_file in configs_dir.glob("*.yaml"):
        # Skip files starting with underscore (internal/base configs)
        if not config_file.name.startswith('_'):
            configs.append(config_file.stem)

    return sorted(configs)
