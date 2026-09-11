# -*- coding: utf-8 -*-
"""
OpenAI 兼容 API 客户端:支持任何 OpenAI 格式的服务
(DeepSeek / Qwen / 自建 vLLM / Ollama openai 模式 等)。
"""
import time

import concurrent.futures

from openai import OpenAI

from core.log import get_logger

logger = get_logger("agent.llm")


class LLMClient:
    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg
        self._build_client()
        self.model = llm_cfg["model"]

    def _build_client(self):
        self.client = OpenAI(
            base_url=self.cfg["base_url"].rstrip("/"),
            api_key=self.cfg["api_key"],
            timeout=self.cfg["timeout"],
        )

    def chat(self, system: str, user: str) -> str:
        """发送对话,返回模型回复文本。
        网关对长生成会周期发送心跳字节,单次读超时(timeout)永远不触发,
        实测请求可无限挂起(2026-09-08 E2E 因此卡死 1 小时+),
        故在工作线程中调用并设硬超时封顶:超时后关闭并重建底层连接
        强制解除阻塞的读调用,异常向上抛出由 executor 跳过该题。"""
        hard = float(self.cfg["timeout"]) * 3
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(self._chat_blocking, system, user)
            try:
                return fut.result(timeout=hard)
            except concurrent.futures.TimeoutError:
                self.client.close()      # 断开 socket,解除阻塞线程
                self._build_client()     # 重建,后续请求继续可用
                raise TimeoutError(f"LLM 响应超过硬超时 {hard:.0f}s,已强制断开重连")
        finally:
            ex.shutdown(wait=False)      # 泄漏线程在 close() 后自行退出

    def _chat_blocking(self, system: str, user: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.cfg["temperature"],
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
