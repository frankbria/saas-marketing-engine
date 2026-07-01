"""Publishing adapters (TECH_SPEC §7). API-first: owned blog + Reddit in v1."""

from app.channels.base import ChannelAdapter, PublishResult, Retryable, get_adapter

__all__ = ["ChannelAdapter", "PublishResult", "Retryable", "get_adapter"]
