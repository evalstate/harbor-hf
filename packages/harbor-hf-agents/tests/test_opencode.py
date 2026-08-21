"""Unit tests for the OpenCode sandbox inference wrapper."""

from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.opencode import agent as opencode_agent
from harbor_hf_agents.opencode.agent import OpenCodeAgent


@pytest.fixture(autouse=True)
def no_sandbox_inference_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        opencode_agent,
        "use_sandbox_inference_route",
        AsyncMock(return_value=False),
    )


def _run_call(exec_calls: list) -> object:
    for call in exec_calls:
        if "opencode --model=" in call.kwargs["command"]:
            return call
    raise AssertionError("No opencode run command found in exec calls")


def _config_call(exec_calls: list) -> object:
    for call in exec_calls:
        if "opencode.json" in call.kwargs["command"]:
            return call
    raise AssertionError("No opencode config command found in exec calls")


@pytest.mark.asyncio
async def test_sandbox_route_injects_loopback_env(
    temp_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def use_route(_agent, _environment, env, **kwargs):
        assert kwargs["base_url_key"] == "OPENAI_BASE_URL"
        assert kwargs["api_key_key"] == "OPENAI_API_KEY"
        assert kwargs["api"] == "chat-completions"
        assert kwargs["allowed_model"] == "openai/gpt-oss-20b:together"
        env["OPENAI_BASE_URL"] = "http://127.0.0.1:18080/v1"
        env["OPENAI_API_KEY"] = "harbor-local-inference-bridge"
        return True

    monkeypatch.setattr(opencode_agent, "use_sandbox_inference_route", use_route)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    agent = OpenCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.18.20",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.run("solve the task", mock_env, AgentContext())

    run_call = _run_call(mock_env.exec.call_args_list)
    assert "--model=openai/openai/gpt-oss-20b:together" in run_call.kwargs["command"]
    assert "solve the task" in run_call.kwargs["command"]
    assert run_call.kwargs["env"]["OPENAI_BASE_URL"] == "http://127.0.0.1:18080/v1"
    assert run_call.kwargs["env"]["OPENAI_API_KEY"] == "harbor-local-inference-bridge"
    config_call = _config_call(mock_env.exec.call_args_list)
    assert "http://127.0.0.1:18080/v1" in config_call.kwargs["command"]
    assert "openai/gpt-oss-20b:together" in config_call.kwargs["command"]


@pytest.mark.asyncio
async def test_missing_sandbox_route_fails(temp_dir) -> None:
    agent = OpenCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.18.20",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="Sandbox inference route"):
        await agent.run("solve the task", mock_env, AgentContext())


@pytest.mark.asyncio
async def test_install_creates_isolated_agent_user(temp_dir) -> None:
    agent = OpenCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="1.18.20",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.install(mock_env)

    first = mock_env.exec.call_args_list[0]
    assert "passwd util-linux" in first.kwargs["command"]
    assert any(
        "opencode-ai@1.18.20" in call.kwargs["command"]
        for call in mock_env.exec.call_args_list
    )
