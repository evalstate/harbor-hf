"""OpenHands over the Harbor-HF sandbox inference route."""

from __future__ import annotations

import os
from typing import Any, override

from harbor.agents.installed.openhands import OpenHands
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.support.isolated_user import AGENT_USER, IsolatedProviderAgent
from harbor_hf_agents.support.sandbox_inference_route import (
    use_sandbox_inference_route,
)


class OpenHandsAgent(IsolatedProviderAgent, OpenHands):
    """Harbor OpenHands bound to the locked sandbox loopback inference route.

    Upstream OpenHands reads ``LLM_API_KEY`` and ``LLM_BASE_URL`` from the Job
    process before it starts. Execution Jobs do not receive those values. This
    wrapper loads ``/run/harbor-hf-inference.json`` and injects the placeholder
    Chat Completions route.
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
            base_url_key="LLM_BASE_URL",
            api_key_key="LLM_API_KEY",
            api="chat-completions",
            allowed_model=self._allowed_model(),
        )
        if not bridged:
            raise RuntimeError("OpenHands requires the Sandbox inference route")
        if "LLM_BASE_URL" not in env or "LLM_API_KEY" not in env:
            raise RuntimeError(
                "Sandbox inference route did not provide OpenHands credentials"
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
        """Create the isolated user before the OpenHands venv is owned.

        Harbor's installer chowns ``/opt/openhands-venv`` to
        ``environment.default_user`` (root here). Create ``harbor-agent``
        first, then give that user the venv directory and install as that
        user.
        """
        await self.exec_as_root(
            environment,
            command=(
                "apt-get update && apt-get install -y --no-install-recommends "
                "ca-certificates curl git passwd util-linux"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.ensure_system_dependencies(
            environment, ("curl", "git", "build_tools", "tmux")
        )
        await self._ensure_isolated_agent_user(environment)
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p /opt/openhands-venv && "
                f"chown {AGENT_USER}:{AGENT_USER} /opt/openhands-venv"
            ),
        )
        if self._git_version:
            install_cmd = (
                "uv pip install "
                f"git+https://github.com/All-Hands-AI/OpenHands.git@{self._git_version}"
            )
        elif self._version:
            install_cmd = f"uv pip install openhands-ai=={self._version}"
        else:
            install_cmd = "uv pip install openhands-ai"
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                'if [ -f "$HOME/.local/bin/env" ]; then '
                'source "$HOME/.local/bin/env"; fi && '
                f"uv python install {self._python_version} && "
                f"uv venv /opt/openhands-venv --python {self._python_version} && "
                "source /opt/openhands-venv/bin/activate && "
                "export SKIP_VSCODE_BUILD=true && "
                f"{install_cmd} && "
                "/opt/openhands-venv/bin/python -m openhands.core.main --version"
            ),
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._route_env = await self._prepare_inference_env(environment)
        previous: dict[str, str | None] = {}
        for key, value in self._route_env.items():
            previous[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            await super().run(instruction, environment, context)
        finally:
            for key, old in previous.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
