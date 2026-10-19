"""多语言。

**以中文原文当 key**，而不是发明一套 key 名。理由：
  · 源码里不用为了翻译把每个字面量改成 t("settings.title")，
    直接 t("设置…") 就行 —— 改起来小，读起来也还是原句
  · 翻译表本身就是「中文 → 英文」的对照，一眼能看出哪句漏了
  · 没翻译的自动退回中文，不会出现空白或 key 名漏到界面上

代价是中文字符串不能随便改 —— 改了就成了新 key，英文对不上。
所以有个测试守着：**翻译表里的每个 key 都必须在源码里出现**，
反过来源码里 t() 包住的每一句也都要有英文。

语言选择存在 pet_config.json 的 "lang"：zh / en / auto。
auto 就按 Windows 的界面语言判断。
"""

from __future__ import annotations

import pet_platform

import ctypes
import os

LANGS = ("auto", "zh", "en")
LANG_LABELS = {"auto": "跟随系统", "zh": "中文", "en": "English"}

# 当前生效的语言（"zh" 或 "en"）。auto 在 set_language 里解析成具体值。
_current = "zh"


def _system_lang() -> str:
    return pet_platform.system_language() or "zh"


def _system_lang_win32() -> str:
    """按 Windows 界面语言猜。猜不出来就当中文（这项目的用户大多是中文）。"""
    try:
        # GetUserDefaultUILanguage 返回 LANGID；中文是 0x0804/0x0404/0x0C04/0x1004
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = langid & 0x3FF
        return "zh" if primary == 0x04 else "en"
    except Exception:                                    # noqa: BLE001
        pass
    try:
        loc = (os.environ.get("LANG") or os.environ.get("LC_ALL") or "").lower()
        if loc:
            return "zh" if loc.startswith("zh") else "en"
    except Exception:                                    # noqa: BLE001
        pass
    return "zh"


def resolve(pref: str) -> str:
    """把配置里的语言偏好解析成具体语言。"""
    pref = (pref or "auto").strip().lower()
    if pref in ("zh", "en"):
        return pref
    return _system_lang()


def set_language(pref: str) -> str:
    """设置语言。传 auto/zh/en，返回实际生效的。"""
    global _current
    _current = resolve(pref)
    return _current


def current() -> str:
    return _current


def is_zh() -> bool:
    return _current == "zh"


def t(zh: str) -> str:
    """翻译一句。中文环境原样返回，没翻译的也原样返回。

    永远不抛 —— 它在渲染和界面构建的热路径上。
    """
    if _current == "zh":
        return zh
    try:
        return EN.get(zh, zh)
    except Exception:                                    # noqa: BLE001
        return zh


def t_lines(zh_lines: list) -> list:
    """翻译一组台词。"""
    if _current == "zh":
        return zh_lines
    return [t(x) for x in zh_lines]


# ── 翻译表 ──────────────────────────────────────────────────────────────────
# 按界面区域分组，方便对照着找漏。

EN = {
    # ── 右键菜单 ──
    "显示 / 隐藏": "Show / Hide",
    "展开详情": "Expand details",
    "收起详情": "Collapse details",
    "喂一口": "Feed",
    "番茄钟": "Pomodoro",
    "分钟": "min",
    "取消计时": "Cancel timer",
    "皮肤": "Skin",
    "从链接安装…": "Install from URL…",
    "删除皮肤…": "Remove skin…",
    "配饰": "Accessory",
    "隐私模式": "Privacy mode",
    "专注模式（安静待机不打扰）": "Focus mode (stay quiet)",
    "任务完成时提示音": "Sound on task finish",
    "陪伴模式（没任务时自己过日子）": "Companion mode (lives her own life)",
    "设置…": "Settings…",
    "和大肥鱼说话…": "Talk to her…",
    "导出相处记录…": "Export our record…",
    "检查更新…": "Check for updates…",
    "退出": "Quit",

    # ── 状态标签 ──
    "空闲": "Idle",
    "思考中": "Thinking",
    "执行中": "Working",
    "等你回答": "Waiting for you",
    "出错重试": "Failed, retrying",
    "已完成": "Done",
    "离线": "Offline",
    "溜达": "Wandering",
    "看你": "Watching you",
    "探头": "Peeking",
    "犯困": "Sleepy",
    "打盹": "Dozing",
    "睡着了": "Asleep",
    "刚醒": "Waking up",
    "好舒服": "Feels nice",

    # ── 设置窗口 ──
    "DSH 桌宠 · 设置": "DSH Pet · Settings",
    "行为": "Behaviour",
    "陪伴模式（DSH 没任务时自己过日子）": "Companion mode (lives her own life when idle)",
    "开机自启": "Start with Windows",
    "隐私模式（隐藏标题与提示词）": "Privacy mode (hide titles and prompts)",
    "任务完成时提示音": "Sound when a task finishes",
    "本地对话（可选）": "Local chat (optional)",
    "地址（Ollama 默认 http://127.0.0.1:11434）":
        "Endpoint (Ollama default: http://127.0.0.1:11434)",
    "模型名（Ollama 用 ollama list 看）": "Model name (see `ollama list`)",
    "保存": "Save",
    "测试连接": "Test connection",
    "和大肥鱼说话…": "Talk to her…",
    "相处记录": "Our record",
    "语言": "Language",
    "位置复位": "Reset position",
    "重置相处记录": "Reset our record",
    "出错日志": "Error log",
    "关闭": "Close",
    "位置已复位": "Position reset",
    "设置开机自启失败（见控制台）": "Could not set autostart (see console)",
    "填完地址和模型才能用": "Fill in both endpoint and model first",
    "已保存：": "Saved: ",
    "先填地址": "Enter an endpoint first",
    "正在连…": "Connecting…",
    "连上了。可用模型：": "Connected. Models: ",
    "连上了，但这个端点没报告任何模型": "Connected, but the endpoint lists no models",
    "连不上：": "Cannot connect: ",

    # ── 对话窗口 ──
    "和大肥鱼说话": "Talk to her",
    "我：": "Me: ",
    "她：": "Her: ",
    "说": "Send",
    "清空对话": "Clear",
    "…": "…",
    "说不出来…": "She can't answer…",
    "还没配本地模型。": "No local model configured yet.",
    "当前地址：": "Endpoint: ",
    "当前模型：": "Model: ",
    "（空）": "(empty)",
    "右键 → 设置 → 本地对话，填一个再试。":
        "Right-click → Settings → Local chat, fill it in and try again.",
    "还没配本地模型 —— 右键 → 设置 → 本地对话。":
        "No local model configured — right-click → Settings → Local chat.",

    # ── 皮肤安装 ──
    "安装皮肤": "Install skin",
    "皮肤压缩包链接，或本地文件路径：": "Skin archive URL, or a local file path:",
    "支持 zip；里面要有 theme.json 或 spritesheet.png / spritesheet.webp":
        "zip only; must contain theme.json or spritesheet.png / spritesheet.webp",
    "安装": "Install",
    "取消": "Cancel",
    "先填个链接或路径": "Enter a URL or path first",
    "正在下载安装…": "Downloading and installing…",
    "装好了": "Installed",
    "装不上": "Could not install",
    "删除皮肤": "Remove skin",
    "选一套删掉（内置皮肤不在列表里）：":
        "Pick one to remove (built-ins are not listed):",
    "删除": "Remove",
    "没有可删的皮肤": "Nothing to remove",
    "删掉了": "Removed",

    # ── 出错日志窗口 ──
    "DSH 桌宠 · 出错日志": "DSH Pet · Error log",
    "没有出错记录。": "No errors recorded.",
    "日志文件位置：": "Log file: ",
    "清空日志": "Clear log",

    # ── 气泡 / 提示 ──
    "有新版本 ": "New version ",
    "时间到！": "Time's up!",
    "该休息了": "Take a break",
    "这一轮结束了": "That round is over",
    "起来走两步吧": "Get up and stretch",
    "番茄钟取消了": "Pomodoro cancelled",
    "记好啦": "Saved it",

    # ── 台词 ──
    "唔…醒了醒了": "Mmh… I'm up",
    "你回来啦": "You're back",
    "别晃我…": "Stop shaking me…",
    "再睡五分钟…": "Five more minutes…",
    "我先眯一会儿": "I'll nap a bit",
    "撑不住了…": "Can't stay awake…",
    "晚安": "Good night",
    "困死了": "So sleepy",
    "啊——欠": "Yaaawn",
    "好无聊啊": "So bored",
    "你在忙什么": "What are you up to",
    "有点困了": "Getting sleepy",
    "溜达一圈": "Going for a stroll",
    "活动活动": "Stretch my fins",
    "看看窗外": "Looking outside",
    "嗯…走两步": "Mm… a few steps",
    "嘿嘿": "Hehe",
    "好舒服": "That's nice",
    "再摸摸": "Pet me again",
    "最喜欢你了": "You're my favourite",
    "别摸了…": "Stop poking…",
    "够了够了": "Enough, enough",
    "手拿开": "Hands off",
    "烦不烦": "So annoying",
    "晕…": "Dizzy…",
    "别甩了！": "Stop spinning me!",
    "天旋地转": "The world is spinning",
    "放我下来": "Put me down",
    "喂！": "Hey!",
    "放开我": "Let me go",
    "干嘛呀": "What are you doing",
    "别拎着我": "Don't dangle me",
    "有点饿了…": "A bit hungry…",
    "想吃东西": "Want a snack",
    "喂我一口嘛": "Feed me please",
    "好久没理我了": "You haven't talked to me in ages",
    "在忙吗": "Busy?",
    "唉…": "Sigh…",
    "早啊": "Morning",
    "今天也加油": "Let's do our best today",
    "睡醒啦": "Just woke up",
    "还不睡？": "Still awake?",
    "夜深了": "It's late",
    "抓到了": "Gotcha",
    "不松手": "Not letting go",
    "救命…": "Help…",
    "坐好久了，起来动动": "You've been sitting a while — move about",
    "腰要断了，站一会儿吧": "Your back will ache — stand up a bit",
    "别一直坐着啦": "Don't sit forever",
    "喝口水吧": "Have some water",
    "该补水了": "Time to hydrate",
    "杯子空了吧": "Your cup's probably empty",

    # ── 设置窗口底部的热键说明 ──
    "热键：Ctrl+Alt+P 显示/隐藏 · +F 喂食 · +S 设置\n"
    "滚轮缩放 · 双击开这个窗口 · 甩动/长按有惊喜":
        "Hotkeys: Ctrl+Alt+P show/hide · +F feed · +D focus · +H hide · +S settings\n"
        "Wheel to zoom · double-click opens this · try shaking or long-pressing her",

    # ── 状态行里拼接的片段 ──
    "个任务进行中": "tasks running",
    "执行 ": "Running ",
    "未发现 DSH 会话": "No DSH session found",
    "自动跟随活跃会话": "Auto-follow the active session",
    "复制会话 ID": "Copy session ID",

    # ── 信息卡 / 状态副行 ──
    "「": "\u201c",
    "」跑完了 · ": "\u201d finished · ",
    "」跑完了": "\u201d finished",
    "在等你回答": "Waiting for your answer",
    "请求出错，正在重试": "Request failed, retrying",
    "任务已完成": "Task complete",
    "个工具调用进行中": " tool calls running",
    "未命名会话": "Unnamed session",
    "工具调用进行中": "Tool call in progress",
    "模型正在生成": "Model is generating",
    "空闲（睡着了）": "Idle (asleep)",
    "等待你的下一条指令": "Waiting for your next instruction",
    "陪你第 ": "Day ",
    " 天": " together",
    "我们认识 ": "We\u2019ve known each other ",
    " 天啦": " days",
    "还在观察你的作息（": "Still learning your schedule (",
    " 个时段攒够样本）": "/6 hours have enough samples)",
    "特别黏你": "Very attached to you",
    "挺开心": "Pretty happy",
    "还不错": "Not bad",
    "有点蔫": "A bit down",
    "好久没理我了": "You haven\u2019t talked to me in ages",
    "… 另有 ": "\u2026 plus ",
    "个任务": " more tasks",
    "会话 ": "Session ",
    "第 ": "Turn ",
    " 轮 · 第 ": " · step ",
    " 步": "",
    "上下文 ": "context ",
    "最慢": "Slowest",
    "模型": "Model",
    "进度": "Progress",
    "输出": "Output",
    "大肥鱼（内置）": "Dafeiyu (built-in)",

    # ── 失败诊断 ──
    "刚才错在哪…": "What went wrong…",
    "刚才错在哪": "What went wrong",
    "最近没有工具出错": "No tool errors recently",
    "第": "turn",
    "步": "",
    "工具": "Tool",
    "没有细节": "no details",
    "出错": " failed",
    "第 ": "turn ",
    " 步": "",
    "复制全文": "Copy all",
    "已复制": "Copied",

    # ── 喂食 ──
    "喂她吃点什么": "Feed her something",
    "饱足": "Fullness",
    "♥ 爱吃的 · ✕ 不太爱": "♥ favourite · ✕ not a fan",
    "团子": "Dango", "小鱼": "Fish", "拉面": "Ramen", "饭团": "Rice ball",
    "米饭": "Rice", "蛋糕": "Cake", "饼干": "Cookie", "糖果": "Candy",
    "巧克力": "Chocolate", "奶茶": "Bubble tea", "草莓": "Strawberry",
    "茶": "Tea",
    "团子！": "Dango!", "这个我喜欢": "I like this one",
    "糯糯的": "So chewy",
    "鱼！": "Fish!", "同类……不对，好吃": "One of my own… no wait, tasty",
    "再来一条": "Another one",
    "好烫好烫": "Hot hot hot", "这个顶饱": "This really fills you up",
    "汤也喝掉": "Drink the broth too",
    "饭团": "Rice ball", "扎实": "Substantial", "海苔好吃": "The nori is good",
    "白米饭": "Plain rice", "香": "Fragrant", "吃饱了": "I'm full",
    "蛋糕！": "Cake!", "草莓留最后吃": "Save the strawberry for last",
    "甜甜的": "So sweet",
    "咔嚓": "Crunch", "饼干屑掉了": "Crumbs everywhere", "还有吗": "Any more?",
    "糖！": "Candy!", "含着慢慢化": "Let it melt slowly", "甜的": "Sweet",
    "巧克力": "Chocolate", "掰一块给你": "I'll break you off a piece",
    "苦的那种更好": "The dark kind is better",
    "奶茶！": "Bubble tea!", "珍珠要嚼": "Chew the pearls", "半糖就好": "Half sugar is fine",
    "草莓": "Strawberry", "酸酸甜甜": "Sweet and tart", "小小的": "So small",
    "喝茶": "Tea", "陪你坐会儿": "I'll sit with you a while",
    "烫，吹吹": "Hot — blow on it",
    "吃不下啦": "Can't eat another bite", "撑了…": "So full…",
    "等会儿再吃": "Maybe later", "肚子圆了": "My belly's round",
    "这个……不太行": "This one… not really", "唔": "Hmm",
    "下次换个吧": "Try something else next time", "勉勉强强": "I guess",
    "最爱你懂我": "You know me best", "这个最棒": "This is the best",
    "就等你喂这个": "I was waiting for this one",

    # ── 贴边隐藏 ──
    "贴边自动隐藏（鼠标靠近才出来）": "Auto-hide at screen edge (peek on hover)",

    # ── 皮肤预览 ──
    "预览…": "Preview…",
    "皮肤预览": "Skin preview",
    "点一下直接换": "Click one to switch",

    # ── 周报 ──
    "导出周报…": "Export weekly report…",
    "这周": "This week",
    "跑了": "Ran",
    "个任务": "tasks",
    "失败": "failed",
    "最慢的工具": "Slowest tools",
    "这周还没干什么，下周加油": "Nothing much this week — next week then",
    "写好啦": "Written",

    # ── 会话操作 ──
    "会话": "Sessions",
    "打开日志": "Open log",
    "复制 ID": "Copy ID",
    "复制摘要": "Copy summary",
    "把 DSH 切到前台": "Bring DSH to front",
    "现在没有活跃会话": "No active session right now",
    "已复制会话 ID": "Session ID copied",
    "已复制摘要": "Summary copied",
    "没找到 DSH 窗口": "Could not find the DSH window",
    "这个平台上做不了": "Not possible on this platform",
    "DSH 已切到前台": "DSH brought to front",
    "切不过去": "Could not switch",
    "DSH 没有「直接跳到某个会话」的接口，"
    "复制 ID 后在 DSH 里搜，或打开日志目录看原始记录。":
        "DSH has no API to jump straight to a session — "
        "copy the ID and search in DSH, or open the log folder.",

    # ── 托盘 ──
    "显示 / 隐藏桌宠": "Show / hide the pet",
    "展开 / 收起详情": "Expand / collapse details",
    "DSH 桌宠": "DSH Pet",
}
