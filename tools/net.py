# -*- coding: utf-8 -*-
"""
出站 HTTP 的公共约定。

只有一个约定，但它很关键：**本机地址不走代理**。

原因很实在：大量开发机会设置 `HTTP_PROXY` / `HTTPS_PROXY` 指向本地抓包工具
或公司代理。httpx 默认 `trust_env=True`，于是对 `http://127.0.0.1:11434`
（Ollama）的请求也会被送进代理；代理不认识这个地址，直接回 502。
现场表现是「Ollama 明明开着，程序却连不上，报错还像服务端故障」，很难往代理上想。

本机地址判定的用途仅此一处：决定要不要读取代理等环境变量。
远程地址（GLM / OpenAI / 远端 MCP）仍然应该走代理——那是它们的正常工作方式。
"""
from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["is_loopback", "trust_env_for", "LOOPBACK_HOSTS"]

#: 明确的本机主机名。`127.*` 前缀单独用前缀匹配处理。
LOOPBACK_HOSTS = frozenset({"localhost", "localhost.", "::1", "0.0.0.0", "[::1]"})


def is_loopback(url: str) -> bool:
    """
    判断 URL 是否指向本机。

    解析失败时保守地返回 False（当作远程）——宁可让一个坏 URL 走代理，
    也不要把真正的远程服务误判成本机而绕过代理。
    """
    try:
        host = (urlsplit(str(url or "")).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


def trust_env_for(url: str) -> bool:
    """
    该目标是否应当读取代理等环境变量。

    :return: 本机地址 → False（忽略代理）；远程地址 → True（尊重用户代理配置）
    """
    return not is_loopback(url)
