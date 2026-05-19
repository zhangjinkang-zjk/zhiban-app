"""Prompt 加载工具 — 从 YAML 文件读取 Agent 的 system prompt"""

import yaml
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent / "ai_core" / "prompts"


def load_prompt(name: str) -> str:
    """
    读取 prompts/{name}.yaml 并返回 system 字段。
    支持分类路径，如 "resource/leader" → prompts/resource/leader.yaml
    """
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["system"]
