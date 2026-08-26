"""Kimi Code over the Harbor-HF Job inference route."""

from harbor.agents.installed.kimi_code import KimiCode

from harbor_hf_agents.support.job_chat_completions import (
    JobChatCompletionsAgent,
    inference_max_output_tokens,
)
from harbor_hf_agents.support.job_inference_route import (
    JOB_INFERENCE_MAX_OUTPUT_TOKENS_ENV,
)


class KimiCodeAgent(JobChatCompletionsAgent, KimiCode):
    """Harbor Kimi Code bound to the locked Job loopback inference route.

    Upstream Kimi Code reads ``KIMI_MODEL_API_KEY`` and ``KIMI_MODEL_BASE_URL``
    from the process environment. Execution Jobs do not receive those values.
    This wrapper loads ``/run/harbor-hf-inference.json`` and injects the
    placeholder Chat Completions route.
    """

    route_base_url_key = "KIMI_MODEL_BASE_URL"
    route_api_key_key = "KIMI_MODEL_API_KEY"
    route_label = "Kimi Code"

    def extend_route_env(self, env: dict[str, str]) -> None:
        env["KIMI_MODEL_NAME"] = self.allowed_model_id()
        env["KIMI_MODEL_MAX_COMPLETION_TOKENS"] = str(
            inference_max_output_tokens(env[JOB_INFERENCE_MAX_OUTPUT_TOKENS_ENV])
        )
