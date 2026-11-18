"""
Claude 客户端工厂。
统一从环境变量读取配置，支持官方 API 和公司代理两种模式。
"""

import os
import anthropic


def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "placeholder")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    timeout = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "90"))

    kwargs = {
        "api_key": api_key,
        "timeout": timeout,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if auth_token:
        kwargs["default_headers"] = {"Authorization": f"Bearer {auth_token}"}

    return anthropic.Anthropic(**kwargs)


def get_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
