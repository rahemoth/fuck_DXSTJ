# -*- coding: utf-8 -*-
"""
OpenAI 兼容 API 客户端:支持任何 OpenAI 格式的服务
(DeepSeek / Qwen / 自建 vLLM / Ollama openai 模式 等)。
"""
import time

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

    def list_models(self) -> list[str]:
        """获取服务端可用模型列表(/models 接口)"""
        resp = self.client.models.list()
        return sorted(m.id for m in resp.data)

    def ping(self) -> tuple[float, str]:
        """测试连接:发送一条极短对话,返回 (耗时秒, 模型回复)。
        验证 base_url / api_key / model 三要素均可用;失败抛异常。"""
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=8,
            messages=[{"role": "user", "content": "回复:ok"}],
        )
        elapsed = time.perf_counter() - t0
        content = (resp.choices[0].message.content or "").strip()
        return elapsed, content
