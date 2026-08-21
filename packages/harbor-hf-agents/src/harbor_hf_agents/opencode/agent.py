"""OpenCode over the Harbor-HF sandbox inference route."""

from __future__ import annotations

import copy
from typing import Any, override

from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.support.isolated_user import IsolatedProviderAgent
from harbor_hf_agents.support.sandbox_inference_route import (
    use_sandbox_inference_route,
)


class OpenCodeAgent(IsolatedProviderAgent, OpenCode):
    """Harbor OpenCode bound to the locked sandbox loopback inference route.

    Upstream OpenCode reads ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` from the
    Job process. Execution Jobs do not receive those values. This wrapper loads
    ``/run/harbor-hf-inference.json`` from the Sandbox and injects the
    placeholder route into the agent process and ``opencode.json``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor API
        super().__init__(*args, **kwargs)
        self._route_env: dict[str, str] | None = None

    def _provider_model(self) -> tuple[str, str]:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        provider, model_id = self.model_name.split("/", 1)
        return provider, model_id

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
            allowed_model=self._provider_model()[1],
        )
        if not bridged:
            raise RuntimeError("OpenCode requires the Sandbox inference route")
        if "OPENAI_BASE_URL" not in env or "OPENAI_API_KEY" not in env:
            raise RuntimeError(
                "Sandbox inference route did not provide OpenCode credentials"
            )
        return env

    def _apply_route_config(self) -> None:
        """Write the loopback base URL into OpenCode's provider options."""
        if self._route_env is None:
            raise RuntimeError("OpenCode inference route is not prepared")
        provider, model_id = self._provider_model()
        self._opencode_config = self._deep_merge(
            copy.deepcopy(self._opencode_config),
            {
                "provider": {
                    provider: {
                        "models": {model_id: {}},
                        "options": {"baseURL": self._route_env["OPENAI_BASE_URL"]},
                    }
                }
            },
        )

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
        self._apply_route_config()
        await super().run(instruction, environment, context)
