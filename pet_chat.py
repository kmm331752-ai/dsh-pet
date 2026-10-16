"""本地对话：接 OpenAI 兼容接口。

支持 Ollama / LM Studio / llama.cpp server / vLLM / LocalAI —— 它们都实现了
`POST {base}/chat/completions`。所以只要一个 base URL 和一个模型名，
不绑定任何一家。

**为什么不做成"随便聊聊"**：桌宠的定位是陪伴，不是一个聊天框。
所以这里给她的系统提示词是**她自己的口吻**（短句、口语、偶尔提一句
你的任务状态），而不是一个通用助手。想换人设就改 PERSONA。

**为什么不用流式**：桌宠的气泡一次显示一句，不需要逐字吐。
非流式实现简单、失败模式少（流式要处理半截 JSON、连接中断、
分块边界）。等真需要逐字效果再说。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

# 默认端点。Ollama 装好就是 11434；LM Studio 是 1234。
DEFAULT_ENDPOINTS = (
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:8080/v1",
)

# 她的口吻。要求短 —— 气泡放不下长句子，而且桌宠不该抢戏。
PERSONA = (
    "你是「大肥鱼」，一只住在用户电脑桌面上的小鲸鱼桌宠。"
    "你说话短、口语化、有点黏人，像熟人聊天，不像客服。"
    "一般一到两句就够，别用列表、别用 markdown、别长篇大论。"
    "用户摸你、喂你、拖你的时候你可以有反应。"
    "你不知道的事情就说不知道，不要编。"
)

TIMEOUT = 30.0
MAX_REPLY_CHARS = 120        # 气泡放得下的长度，超了截断
MAX_HISTORY = 8              # 保留几轮上下文


class ChatError(Exception):
    """连不上、模型不存在、返回格式不对 —— 消息直接给用户看。"""


def normalize_base(url: str) -> str:
    """把用户填的各种写法归一成 `http://host:port/v1`。

    容忍：
        http://127.0.0.1:11434
        http://127.0.0.1:11434/
        http://127.0.0.1:11434/v1/
        http://127.0.0.1:11434/v1/chat/completions
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    if not u.endswith("/v1"):
        u += "/v1"
    return u


def list_models(base: str, timeout: float = 5.0) -> list:
    """问端点有哪些模型。用于设置窗口里的下拉框。"""
    base = normalize_base(base)
    if not base:
        return []
    try:
        req = urllib.request.Request(base + "/models",
                                     headers={"User-Agent": "dsh-pet"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:                             # noqa: BLE001
        raise ChatError(f"连不上 {base}：{exc}") from exc
    out = []
    for item in (data.get("data") or []):
        mid = item.get("id") if isinstance(item, dict) else None
        if mid:
            out.append(str(mid))
    return sorted(out)


def detect_endpoint(timeout: float = 1.5) -> Optional[str]:
    """挨个试常见端口，返回第一个能用的 base URL。都没有就 None。"""
    for base in DEFAULT_ENDPOINTS:
        try:
            if list_models(base, timeout=timeout):
                return base
        except Exception:                                # noqa: BLE001
            continue
    return None


class ChatClient:
    """一个极小的 OpenAI 兼容客户端。只管对话，不管流式。"""

    def __init__(self, base: str = "", model: str = ""):
        self.base = normalize_base(base)
        self.model = (model or "").strip()
        self.history: list = []          # [{"role","content"}]

    @property
    def configured(self) -> bool:
        return bool(self.base and self.model)

    def reset(self) -> None:
        self.history = []

    def ask(self, text: str, context: str = "") -> str:
        """发一句话，返回她的回复。

        context 是「你现在在干什么」之类的背景（比如正在跑的任务），
        拼进系统提示词里 —— 这样她能接上一句「你在忙 XX 啊」。
        失败抛 ChatError。
        """
        if not self.base:
            raise ChatError("还没配本地模型的地址")
        if not self.model:
            raise ChatError("还没选模型")
        text = (text or "").strip()
        if not text:
            raise ChatError("没说话")

        sys_prompt = PERSONA
        if context:
            sys_prompt += f"\n（你现在看到的：{context}）"

        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(self.history[-MAX_HISTORY:])
        messages.append({"role": "user", "content": text})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # 桌宠说话要短，temperature 调低一点免得跑偏
            "temperature": 0.8,
            "max_tokens": 200,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "dsh-pet"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:                            # noqa: BLE001
                pass
            raise ChatError(f"模型返回 {exc.code}：{detail or exc.reason}") from exc
        except Exception as exc:                         # noqa: BLE001
            raise ChatError(f"连不上模型：{exc}") from exc

        try:
            data = json.loads(raw)
            reply = data["choices"][0]["message"]["content"]
        except Exception as exc:                         # noqa: BLE001
            raise ChatError(f"返回格式看不懂：{raw[:120]}") from exc

        reply = (reply or "").strip()
        if not reply:
            raise ChatError("模型没说话")
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS].rstrip() + "…"

        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply})
        if len(self.history) > MAX_HISTORY * 2:
            self.history = self.history[-MAX_HISTORY * 2:]
        return reply
