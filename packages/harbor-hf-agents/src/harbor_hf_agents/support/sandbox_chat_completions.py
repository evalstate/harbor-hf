"""Shared Chat Completions loopback wrapper for Harbor installed agents."""

from __future__ import annotations

import os
from typing import Any, ClassVar

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.support.isolated_user import IsolatedProviderAgent
from harbor_hf_agents.support.sandbox_inference_route import (
    use_sandbox_inference_route,
)


def allowed_model_id(model_name: str | None) -> str:
    """Return the Harbor model id after the provider prefix."""
    if not model_name or "/" not in model_name:
        raise ValueError("Model name must be in the format provider/model_name")
    return model_name.split("/", 1)[1]


class SandboxChatCompletionsAgent(IsolatedProviderAgent):
    """Inject the locked sandbox Chat Completions route into an installed agent."""

    route_base_url_key: ClassVar[str]
    route_api_key_key: ClassVar[str]
    route_label: ClassVar[str]
    install_packages: ClassVar[tuple[str, ...]] = (
        "ca-certificates",
        "curl",
        "passwd",
        "util-linux",
    )
    inject_route_into_process: ClassVar[bool] = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor API
        super().__init__(*args, **kwargs)
        self._route_env: dict[str, str] | None = None

    def allowed_model_id(self) -> str:
        """Return the locked model id after the provider prefix."""
        return allowed_model_id(self.model_name)

    def extend_route_env(self, env: dict[str, str]) -> None:
        """Add harness-specific aliases to the loaded loopback environment."""

    async def after_route_prepared(self) -> None:
        """Hook after the loopback route is loaded and stored on the agent."""

    async def prepare_route_env(self, environment: BaseEnvironment) -> dict[str, str]:
        """Load the sandbox loopback route. Fail if it is missing."""
        env: dict[str, str] = {}
        bridged = await use_sandbox_inference_route(
            self,
            environment,
            env,
            base_url_key=self.route_base_url_key,
            api_key_key=self.route_api_key_key,
            api="chat-completions",
            allowed_model=self.allowed_model_id(),
        )
        if not bridged:
            raise RuntimeError(
                f"{self.route_label} requires the Sandbox inference route"
            )
        if self.route_base_url_key not in env or self.route_api_key_key not in env:
            raise RuntimeError(
                "Sandbox inference route did not provide "
                f"{self.route_label} credentials"
            )
        self.extend_route_env(env)
        return env

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

    async def install_runtime_packages(self, environment: BaseEnvironment) -> None:
        """Install the apt packages the isolated agent user needs."""
        packages = " ".join(self.install_packages)
        await self.exec_as_root(
            environment,
            command=(
                "apt-get update && apt-get install -y --no-install-recommends "
                f"{packages}"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

    async def install(self, environment: BaseEnvironment) -> None:
        await self.install_runtime_packages(environment)
        await super().install(environment)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._route_env = await self.prepare_route_env(environment)
        await self.after_route_prepared()
        if not self.inject_route_into_process:
            await super().run(instruction, environment, context)
            return
        previous: dict[str, str | None] = {}
        for key, value in self._route_env.items():
            previous[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            await super().run(instruction, environment, context)
        finally:
            for key, old in previous.items():
                if old is None:
                    del os.environ[key]
                else:
                    os.environ[key] = old
