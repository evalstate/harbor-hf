"""Unit tests for the OpenHands sandbox inference wrapper."""

from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.openhands import agent as openhands_agent
from harbor_hf_agents.openhands.agent import OpenHandsAgent


@pytest.fixture(autouse=True)
def no_sandbox_inference_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openhands_agent,
        "use_sandbox_inference_route",
        AsyncMock(return_value=False),
    )


def _run_call(exec_calls: list) -> object:
    for call in exec_calls:
        if "openhands.core.main" in call.kwargs["command"]:
            return call
    raise AssertionError("No OpenHands run command found in exec calls")


@pytest.mark.asyncio
async def test_sandbox_route_injects_loopback_env(
    temp_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def use_route(_agent, _environment, env, **kwargs):
        assert kwargs["base_url_key"] == "LLM_BASE_URL"
        assert kwargs["api_key_key"] == "LLM_API_KEY"
        assert kwargs["api"] == "chat-completions"
        assert kwargs["allowed_model"] == "openai/gpt-oss-20b:together"
        env["LLM_BASE_URL"] = "http://127.0.0.1:18080/v1"
        env["LLM_API_KEY"] = "harbor-local-inference-bridge"
        return True

    monkeypatch.setattr(openhands_agent, "use_sandbox_inference_route", use_route)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    agent = OpenHandsAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.11.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.run("solve the task", mock_env, AgentContext())

    run_call = _run_call(mock_env.exec.call_args_list)
    assert "solve the task" in run_call.kwargs["command"]
    assert run_call.kwargs["env"]["LLM_BASE_URL"] == "http://127.0.0.1:18080/v1"
    assert run_call.kwargs["env"]["LLM_API_KEY"] == "harbor-local-inference-bridge"


@pytest.mark.asyncio
async def test_missing_sandbox_route_fails(temp_dir) -> None:
    agent = OpenHandsAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.11.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="Sandbox inference route"):
        await agent.run("solve the task", mock_env, AgentContext())


@pytest.mark.asyncio
async def test_install_creates_isolated_agent_user(temp_dir) -> None:
    agent = OpenHandsAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.11.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.install(mock_env)

    first = mock_env.exec.call_args_list[0]
    assert "passwd util-linux" in first.kwargs["command"]
    assert any(
        "chown harbor-agent:harbor-agent /opt/openhands-venv" in call.kwargs["command"]
        for call in mock_env.exec.call_args_list
    )
    assert any(
        "openhands-ai==1.11.0" in call.kwargs["command"]
        for call in mock_env.exec.call_args_list
    )
