"""Shared OpenAI-compatible NVIDIA client. Repository data is never trusted."""
from __future__ import annotations
import os
from openai import OpenAI

AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://integrate.api.nvidia.com/v1")
METADATA_MODEL = os.environ.get("AI_METADATA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
REVIEW_MODEL = os.environ.get("AI_REVIEW_MODEL", "deepseek-ai/deepseek-v4-flash-0731")

def client() -> OpenAI:
    return OpenAI(base_url=AI_BASE_URL, api_key=os.environ["NVIDIA_API_KEY"])
