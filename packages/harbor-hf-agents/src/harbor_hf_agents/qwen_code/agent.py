"""Qwen Code over the Harbor-HF Job inference route."""

from harbor.agents.installed.qwen_code import QwenCode

from harbor_hf_agents.support.job_chat_completions import (
    JobChatCompletionsAgent,
)


class QwenCodeAgent(JobChatCompletionsAgent, QwenCode):
    """Harbor Qwen Code bound to the locked Job loopback inference route.

    Upstream Qwen Code reads ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` from the
    Job process. Execution Jobs do not receive those values. This wrapper loads
    ``/run/harbor-hf-inference.json`` from the Job and injects the
    placeholder route into the agent process.
    """

    route_base_url_key = "OPENAI_BASE_URL"
    route_api_key_key = "OPENAI_API_KEY"
    route_label = "Qwen Code"
