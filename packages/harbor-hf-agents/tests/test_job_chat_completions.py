"""Tests for the shared Chat Completions loopback wrapper."""

import pytest

from harbor_hf_agents.support.job_chat_completions import (
    allowed_model_id,
    inference_max_output_tokens,
)


def test_allowed_model_id_strips_the_provider_prefix() -> None:
    assert allowed_model_id("openai/gpt-oss-20b:together") == "gpt-oss-20b:together"


def test_allowed_model_id_rejects_a_bare_name() -> None:
    with pytest.raises(ValueError, match="provider/model_name"):
        allowed_model_id("gpt-oss-20b")


def test_reads_locked_inference_output_limit() -> None:
    assert inference_max_output_tokens("32768") == 32768


@pytest.mark.parametrize("value", [None, "", "0", "-1", "invalid"])
def test_rejects_invalid_inference_output_limit(
    value: str | None,
) -> None:
    with pytest.raises(RuntimeError, match="positive integer|required"):
        inference_max_output_tokens(value)
