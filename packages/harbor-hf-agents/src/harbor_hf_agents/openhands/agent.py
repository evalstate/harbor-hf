"""OpenHands using Harbor's direct model connection."""

from typing import override

from harbor.agents.installed.openhands import OpenHands
from harbor.environments.base import BaseEnvironment

from harbor_hf_agents.support.direct_inference import (
    DirectChatCompletionsAgent,
)
from harbor_hf_agents.support.isolated_user import AGENT_USER


class OpenHandsAgent(DirectChatCompletionsAgent, OpenHands):
    """Harbor OpenHands with direct OpenAI-compatible inference settings."""

    base_url_key = "LLM_BASE_URL"
    api_key_key = "LLM_API_KEY"
    agent_label = "OpenHands"
    install_packages = (
        "ca-certificates",
        "curl",
        "git",
        "passwd",
        "util-linux",
    )
    inject_environment_into_process = True

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Create the isolated user before the OpenHands venv is owned.

        Harbor 0.21.0 starts OpenHands with ``openhands.core.main``. That
        module is the V0 CLI. Create ``harbor-agent`` first, then give that
        user the venv directory and install as that user.
        """
        await self.install_runtime_packages(environment)
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
