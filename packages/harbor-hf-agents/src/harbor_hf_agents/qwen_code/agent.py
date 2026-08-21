"""Qwen Code over the Harbor-HF sandbox inference route."""

from __future__ import annotations

from typing import Any, override

from harbor.agents.installed.qwen_code import QwenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.support.isolated_user import IsolatedProviderAgent
from harbor_hf_agents.support.sandbox_inference_route import (
    use_sandbox_inference_route,
)


class QwenCodeAgent(IsolatedProviderAgent, QwenCode):
    """Harbor Qwen Code bound to the locked sandbox loopback inference route.

    Upstream Qwen Code reads ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` from the
    Job process. Execution Jobs do not receive those values. This wrapper loads
    ``/run/harbor-hf-inference.json`` from the Sandbox and injects the
    placeholder route into the agent process.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor API
        super().__init__(*args, **kwargs)
        self._route_env: dict[str, str] | None = None

    def _allowed_model(self) -> str:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        return self.model_name.split("/", 1)[1]

    async def _prepare_inference_env(
        self,
        environment: BaseEnvironment,
    ) -> dict[str, str]:
        """Load the sandbox loopback route. Fail if it is missing."""
        env: dict[str, str] = {}
        bridged = await use_sandbox_inference_route(
            self,
            environment,
            env,
            base_url_key="OPENAI_BASE_URL",
            api_key_key="OPENAI_API_KEY",
            api="chat-completions",
            allowed_model=self._allowed_model(),
        )
        if not bridged:
            raise RuntimeError("Qwen Code requires the Sandbox inference route")
        if "OPENAI_BASE_URL" not in env or "OPENAI_API_KEY" not in env:
            raise RuntimeError(
                "Sandbox inference route did not provide Qwen Code credentials"
            )
        return env

    @override
    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:  # noqa: ANN401 -- Harbor API
        merged = dict(env or {})
        if self._route_env is not None:
            merged.update(self._route_env)
        return await super().exec_as_agent(
            environment,
            command,
            env=merged,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "apt-get update && apt-get install -y --no-install-recommends "
                "ca-certificates curl passwd util-linux"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await super().install(environment)

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._route_env = await self._prepare_inference_env(environment)
        await super().run(instruction, environment, context)
