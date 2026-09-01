# -*- coding: utf-8 -*-
"""
OpenAI 兼容 API 客户端:支持任何 OpenAI 格式的服务
(DeepSeek / Qwen / 自建 vLLM / Ollama openai 模式 等)。
"""
from openai import OpenAI

from core.log import get_logger

logger = get_logger("agent.llm")


class LLMClient:
    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg
        self.client = OpenAI(
            base_url=llm_cfg["base_url"].rstrip("/"),
            api_key=llm_cfg["api_key"],
            timeout=llm_cfg.get("timeout", 60),
        )
        self.model = llm_cfg["model"]

    def chat(self, system: str, user: str) -> str:
        """发送对话,返回模型回复文本"""
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.cfg.get("temperature", 0.1),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content
        logger.debug(f"LLM 原始回复: {content[:200]}")
        return content or ""
