"""Unit tests for the Kimi Code job inference wrapper."""

from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext

from harbor_hf_agents.kimi_code.agent import KimiCodeAgent

_ROUTE = "harbor_hf_agents.support.job_chat_completions.use_job_inference_route"


@pytest.fixture(autouse=True)
def no_job_inference_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ROUTE, AsyncMock(return_value=False))


def _run_call(exec_calls: list) -> object:
    for call in exec_calls:
        if "kimi " in call.kwargs["command"] and "--prompt" in call.kwargs["command"]:
            return call
    raise AssertionError("No kimi run command found in exec calls")


@pytest.mark.asyncio
async def test_job_route_injects_loopback_env(
    temp_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def use_route(_agent, _environment, env, **kwargs):
        assert kwargs["base_url_key"] == "KIMI_MODEL_BASE_URL"
        assert kwargs["api_key_key"] == "KIMI_MODEL_API_KEY"
        assert kwargs["api"] == "chat-completions"
        assert kwargs["allowed_model"] == "openai/gpt-oss-20b:together"
        env["KIMI_MODEL_BASE_URL"] = "http://127.0.0.1:18080/v1"
        env["KIMI_MODEL_API_KEY"] = "harbor-local-inference-bridge"
        env["JOB_INFERENCE_MAX_OUTPUT_TOKENS"] = "32768"
        return True

    monkeypatch.setattr(_ROUTE, use_route)
    monkeypatch.delenv("HARBOR_HF_INFERENCE_MAX_OUTPUT_TOKENS", raising=False)
    agent = KimiCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="0.38.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.run("solve the task", mock_env, AgentContext())

    run_call = _run_call(mock_env.exec.call_args_list)
    assert "solve the task" in run_call.kwargs["env"].values()
    assert run_call.kwargs["env"]["KIMI_MODEL_BASE_URL"] == "http://127.0.0.1:18080/v1"
    assert (
        run_call.kwargs["env"]["KIMI_MODEL_API_KEY"] == "harbor-local-inference-bridge"
    )
    assert run_call.kwargs["env"]["KIMI_MODEL_NAME"] == "openai/gpt-oss-20b:together"
    assert run_call.kwargs["env"]["KIMI_MODEL_MAX_COMPLETION_TOKENS"] == "32768"


@pytest.mark.asyncio
async def test_missing_job_route_fails(temp_dir) -> None:
    agent = KimiCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="0.38.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="Job inference route"):
        await agent.run("solve the task", mock_env, AgentContext())


@pytest.mark.asyncio
async def test_install_creates_isolated_agent_user(temp_dir) -> None:
    agent = KimiCodeAgent(
        logs_dir=temp_dir,
        model_name="openai/openai/gpt-oss-20b:together",
        version="0.38.0",
    )
    mock_env = AsyncMock()
    mock_env.exec.return_value = AsyncMock(return_code=0, stdout="", stderr="")

    await agent.install(mock_env)

    first = mock_env.exec.call_args_list[0]
    assert "passwd util-linux" in first.kwargs["command"]
    assert any(
        "@moonshot-ai/kimi-code@0.38.0" in call.kwargs["command"]
        for call in mock_env.exec.call_args_list
    )
