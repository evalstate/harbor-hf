"""Qwen Code over the Harbor-HF sandbox inference route."""

from harbor.agents.installed.qwen_code import QwenCode

from harbor_hf_agents.support.sandbox_chat_completions import (
    SandboxChatCompletionsAgent,
)


class QwenCodeAgent(SandboxChatCompletionsAgent, QwenCode):
    """Harbor Qwen Code bound to the locked sandbox loopback inference route.

    Upstream Qwen Code reads ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` from the
    Job process. Execution Jobs do not receive those values. This wrapper loads
    ``/run/harbor-hf-inference.json`` from the Sandbox and injects the
    placeholder route into the agent process.
    """

    route_base_url_key = "OPENAI_BASE_URL"
    route_api_key_key = "OPENAI_API_KEY"
    route_label = "Qwen Code"
