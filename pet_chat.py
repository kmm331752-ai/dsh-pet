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
import time
import urllib.error
import urllib.request
from typing import Optional

from pet_base import chat_history_path

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
MAX_SAVED_HISTORY = 40       # 存盘保留几轮（比上下文多，重启后还能接上）
MAX_FACTS = 40               # 最多记多少条


# ── 记忆 ────────────────────────────────────────────────────────────────────
# 分两种，来源不同，可靠程度也不同：
#
# **观察型**：从她本来就看得到的数据里推出来 —— 你常跑什么任务、几点在、
# 用得多不多。这些是真的，不用猜。
#
# **对话型**：你说「我叫小明」这种自我披露。用模式匹配抠出来，**是尽力而为** ——
# 中文没有格标记，模式必然漏也会错。所以：
#   · 抠错了用户能在对话窗口里删
#   · 宁可少记：只认几个明确的句式，模糊的一律不记
FACT_PATTERNS = (
    (r"我(?:的名字)?叫\s*([^\s，。！？、,.!?]{1,12})", "名字"),
    (r"我是(?:一个|一名)?\s*([^\s，。！？、,.!?]{2,16})", "身份"),
    (r"我在(?:做|搞|写|弄)\s*([^\s，。！？、,.!?]{2,20})", "在做"),
    (r"我(?:喜欢|爱|最爱)\s*([^\s，。！？、,.!?]{2,16})", "喜欢"),
    # 负向断言：别让「我在做 X」被「我在 X」再匹配一遍
    (r"我(?:住在|在)(?![做搞写弄])\s*([^\s，。！？、,.!?]{2,16})", "在"),
    (r"我(?:用|使)的?是\s*([^\s，。！？、,.!?]{2,20})", "在用"),
)


def extract_facts(text: str) -> list:
    """从一句话里抠出自我披露。返回 [(类型, 内容)]。

    故意保守：只认上面那几个句式。抠不出来就返回空 —— 宁可不记，别记错。
    """
    import re
    out = []
    text = (text or "").strip()
    if not text or len(text) > 200:
        return out
    for pat, kind in FACT_PATTERNS:
        for m in re.finditer(pat, text):
            val = (m.group(1) or "").strip()
            # 太短或明显是虚词的丢掉
            if len(val) < 1 or val in ("谁", "什么", "这个", "那个", "你", "我"):
                continue
            out.append((kind, val))
    return out


class ChatClient:
    """一个极小的 OpenAI 兼容客户端。只管对话，不管流式。"""

    def __init__(self, base: str = "", model: str = ""):
        self.base = normalize_base(base)
        self.model = (model or "").strip()
        self.history: list = []          # [{"role","content"}]
        self.facts: list = []            # [{"kind","value","at"}] 她记得的事
        self.notes: list = []            # 观察型记忆（一行一条，由外面喂）
        self._loaded = False

    # -- 记忆的存与取 ------------------------------------------------------
    def load(self) -> None:
        """从磁盘读回对话和记忆。没有就留空，不报错。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(chat_history_path(), encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except Exception:                                # noqa: BLE001
            data = {}
        if isinstance(data, dict):
            h = data.get("history")
            if isinstance(h, list):
                self.history = [m for m in h if isinstance(m, dict)
                                and m.get("role") in ("user", "assistant")
                                and isinstance(m.get("content"), str)]
                self.history = self.history[-MAX_SAVED_HISTORY * 2:]
            f = data.get("facts")
            if isinstance(f, list):
                self.facts = [x for x in f if isinstance(x, dict)
                              and x.get("value")][:MAX_FACTS]

    def save(self) -> None:
        try:
            payload = {
                "history": self.history[-MAX_SAVED_HISTORY * 2:],
                "facts": self.facts[:MAX_FACTS],
            }
            with open(chat_history_path(), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    def remember(self, kind: str, value: str) -> bool:
        """记一条。重复的不记。返回是否是新记的。"""
        value = (value or "").strip()
        if not value:
            return False
        for f in self.facts:
            if f.get("kind") == kind and f.get("value") == value:
                return False
        self.facts.append({"kind": kind, "value": value,
                           "at": time.strftime("%Y-%m-%d")})
        if len(self.facts) > MAX_FACTS:
            del self.facts[:-MAX_FACTS]
        return True

    def forget(self, index: int) -> None:
        if 0 <= index < len(self.facts):
            del self.facts[index]

    def memory_brief(self) -> str:
        """把记忆拼成给模型看的一段话。没有记忆就返回空串。"""
        bits = []
        if self.facts:
            bits.append("；".join(f"{f['kind']}：{f['value']}" for f in self.facts[:12]))
        if self.notes:
            bits.append("；".join(self.notes[:6]))
        return "。".join(b for b in bits if b)

    @property
    def configured(self) -> bool:
        return bool(self.base and self.model)

    def reset(self) -> None:
        self.history = []
        self.save()

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

        self.load()
        sys_prompt = PERSONA
        mem = self.memory_brief()
        if mem:
            sys_prompt += f"\n（你记得的事：{mem}）"
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
        # 顺手记下她刚知道的关于你的事。
        # 模式匹配是**尽力而为** —— 中文没有格标记，必然漏也会错，
        # 所以只认几个明确句式，抠错了用户能在对话窗口里删。
        for kind, value in extract_facts(text):
            self.remember(kind, value)
        self.save()
        return reply


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
