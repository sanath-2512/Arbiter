"""Model client factory. The credential is read here and nowhere else."""

from __future__ import annotations

import os

from arbiter.config import ConfigError, ModelConfig
from arbiter.models.base import ModelClient


def read_api_key(cfg: ModelConfig, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    key = (env.get(cfg.api_key_env) or "").strip()
    if not key:
        raise ConfigError(f"{cfg.api_key_env} is not set; it must contain the API credential for the prescribed model.")
    return key


def make_client(cfg: ModelConfig, env: dict[str, str] | None = None) -> ModelClient:
    if cfg.provider == "fake":
        from arbiter.models.fake import FakeClient, load_script

        client: ModelClient = FakeClient(load_script(cfg.script), model_name=cfg.name or "fake-scripted")
    elif cfg.provider == "openai_chat":
        from arbiter.models.openai_chat import OpenAIChatClient

        client = OpenAIChatClient(cfg, read_api_key(cfg, env))
    elif cfg.provider == "anthropic_messages":
        from arbiter.models.anthropic import AnthropicClient

        client = AnthropicClient(cfg, read_api_key(cfg, env))
    else:
        raise ConfigError(f"unknown provider {cfg.provider!r}")
    if cfg.tool_protocol == "text":
        from arbiter.models.textproto import TextProtocolClient

        client = TextProtocolClient(client)
    return client
