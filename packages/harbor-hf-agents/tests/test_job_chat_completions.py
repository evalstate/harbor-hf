"""Tests for the shared Chat Completions loopback wrapper."""

import pytest

from harbor_hf_agents.support.job_chat_completions import allowed_model_id


def test_allowed_model_id_strips_the_provider_prefix() -> None:
    assert allowed_model_id("openai/gpt-oss-20b:together") == "gpt-oss-20b:together"


def test_allowed_model_id_rejects_a_bare_name() -> None:
    with pytest.raises(ValueError, match="provider/model_name"):
        allowed_model_id("gpt-oss-20b")
