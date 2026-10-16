# 🐟 DSH 桌宠

一个悬浮在桌面上的透明小宠物。**有 DSH 任务时她显示任务状态，没有任务时她过自己的日子。**

思考时她托腮，跑工具时她转身拿齿轮，卡在等你回答时她举手，任务跑完她庆祝。
同时开着几个会话，她会一次全列出来，而不是只挑一个显示。

而当你没在跑任务、或者根本没开 DSH 的时候 —— 她会自己溜达、犯困、打盹、睡着，
你一动键盘鼠标她就醒。你还能喂她、摸她，她会记住你。

![十三种状态](preview/states-13.png)

单文件 Python + Pillow，没有网络请求，没有遥测。它只读 DSH 自己写的会话文件，
不改配置、不装插件、不碰 DSH 本身。

## 🚀 跑起来

**方式一：直接用 exe（不用装 Python）**

下载 [Releases](../../releases) 里的 `DSH-Pet.exe`，双击就跑。
配置和相处记录会写在 exe 旁边。

**或者用安装包**：`DSH-Pet-Setup-x.y.z.exe` —— 带开始菜单项、桌面快捷方式和
卸载程序，安装时可勾选开机自启。自己编译安装包：

```bash
python build_exe.py
iscc installer.iss        # 需要 Inno Setup 6
```

**方式二：从源码跑**

```bash
git clone https://github.com/kmm331752-ai/dsh-pet
cd dsh-pet
python dsh_pet.py
```

需要 Python 3.10+ 和 Pillow。可选依赖两个：`pystray`（托盘图标）、
`numpy`（只在读 clawd 主题的 SVG 时才用得上）。

Windows 上双击 `启动桌宠.vbs` 可以静默启动，不弹控制台；
出问题就用 `调试启动.bat`，能看到报错。

**自己打包 exe：**

```bash
pip install pyinstaller
python build_exe.py        # 产物在 dist/DSH-Pet.exe
```

它会先用 `git archive` 导出已提交的文件再打包 —— 这样本地自用的受限素材
（比如被 `.gitignore` 排除的 clawd 主题）不会被塞进 exe 里分发出去。

```bash
python dsh_pet.py --demo                # 每 3 秒轮换状态，看效果用
python dsh_pet.py --list-skins          # 列出可用皮肤
python dsh_pet.py --skin kun-like       # 指定皮肤
python dsh_pet.py --session <id>        # 只盯一个会话
python dsh_pet.py --shot 前缀           # 不建窗口，把各状态渲成 PNG
```

左键点她一下会播个反应动画，顺便展开详情卡；拖动移动位置；右键是菜单。

## 🎭 任务状态（七种）

| 状态 | 什么时候 | 长什么样 |
|---|---|---|
| 🟦 空闲 | 没有任何会话在生成或跑工具 | 正面站着 |
| 🟪 思考中 | `sessionStats.openStep` 有值 | 正面 + 三个跳动的点 |
| 🟧 执行中 | `sessionStats.pendingCalls` 非空 | 侧面转身 + 转齿轮 |
| 🩷 等你回答 | `userQuestions.questions.active` 非空 | 正面 + 粉色光晕 |
| 🟥 出错重试 | `llmRetry` 非空 | 正面 + 红色光晕 |
| 🟩 已完成 | `turnBoundary.openTurnStartSeq` 从有值变 null | 庆祝动作 |
| ⬜ 离线 | 一个 DSH 会话都找不到 | 背面 |

（左边那些方块就是她脚下的光晕颜色）

优先级是 等你回答 > 出错 > 执行 > 思考 > 已完成 > 空闲。把「等你回答」排第一
是因为卡在你这里最该被看见 —— 其他状态再热闹，也不如这一条值得打断你。

「已完成」比较特殊：它不是某个字段的值，而是一次**变化**。盯着
`openTurnStartSeq` 从有值变成 null 的那一刻，就知道那个会话刚跑完一个回合，庆祝 6 秒。

## 🐾 陪伴模式

DSH 那边一个活跃会话都没有的时候，她不会傻站着显示「空闲」—— 她开始过自己的日子。

| 状态 | 什么时候 | 长什么样 |
|---|---|---|
| 🟦 溜达 | 白天有精神，随机触发 | 侧身走两步 |
| 🟪 犯困 | 45 秒没碰键盘鼠标 | 正面 + 紫色光晕 |
| 🟦 打盹 | 90 秒没互动 | 正面，动画放慢 3 倍 |
| ⬜ 睡着了 | 4 分钟没互动，或者深夜 | **背面**，缩成一团 |
| 🟨 刚醒 | 你动了鼠标 / 精力睡满 | 正面 + 暖黄光晕 |
| 🩷 好舒服 | 被摸 / 被喂 | 正面 + 粉色光晕 |

深夜（23:00–07:00）只要安静 30 秒就直接睡。你一碰键盘鼠标她就醒，
有时还会说一句「唔…醒了醒了」。

**她会记住你。** 相处记录存在 `pet_mood.json`：

- 陪你第几天、亲密度、精力
- 被戳 / 被拖 / 被喂各多少次
- 亲密度影响她的说法（「特别黏你」→「好久没理我了」）

**能对她做的事：**

| 操作 | 效果 |
|---|---|
| 左键单击 | 摸一下 —— 亲密度 +2%，精力 +5%，睡着会醒 |
| 拖动 | 也算互动，亲密度 +1% |
| 右键 → 喂一口 🍡 | 精力 +25%，亲密度 +6% |

偶尔她会自己冒一句话（文案表在 `dsh_pet.py` 顶部的 `WAKE_LINES` 那几个常量里，想改直接改）。

不想让她自己过日子的话，右键菜单或设置窗口里关掉「陪伴模式」就行，
也可以用 `--no-companion` 启动。

## 🖱️ 能对她做什么

![交互动作](preview/interactions.png)

| 操作 | 她的反应 |
|---|---|
| **左键单击** | 摸一下 —— 亲密度 +2%，精力 +5%，头顶冒心；睡着会醒 |
| **鼠标停在她身上 1 秒** | 她注意到你，抬头冒一个「？」 |
| **拖动** | 移动位置，亲密度 +1% |
| **快速来回甩** | **晕头转向** —— 头顶转星星，持续 3 秒 |
| **按住不动 2 秒** | **把她抱起来** —— 她会挣扎冒汗，松手后**掉下去**落到屏幕底部 |
| **拖到屏幕边缘松手** | 她**抓住边缘**贴住不放 |
| **滚轮** | 缩放大小（60%~200%），设置会记住 |
| **双击** | 打开设置窗口 |
| **连续摸 5 次以上** | 她会**烦** —— 冒怒气符号说「别摸了…」，冷一会儿 |
| **右键** | 菜单：喂一口 🍡 / 展开详情 / 隐私模式 / 提示音 / 陪伴模式 / 皮肤 / 设置 |

**甩动**判定的是最近 0.6 秒内水平方向来回换了 5 次以上；**抱起来**是按住不动
2 秒（一移动就取消，所以不会跟拖动打架）。

这些装饰（星星、问号、心、怒气、汗、z）全是**程序化画出来的**，不依赖素材 ——
所以契约皮肤和 clawd 主题一样能用。

## ⌨️ 全局热键

| 热键 | 作用 |
|---|---|
| `Ctrl+Alt+P` | 显示 / 隐藏她 |
| `Ctrl+Alt+F` | 喂一口 |
| `Ctrl+Alt+D` | 专注模式开关（安静待机不冒泡） |
| `Ctrl+Alt+H` | 截图模式：连托盘一起藏掉，录屏时不碍事 |
| `Ctrl+Alt+S` | 打开设置 |

不想用可以 `--no-hotkeys` 启动。注册失败（比如被别的程序占了）只会在控制台
警告一句，不影响桌宠本身。

## 🍅 番茄钟

右键 → **番茄钟**，选 25 / 45 / 60 分钟。计时期间信息卡副行显示剩余时间
（`🍅 12:34`，用强调色），到点她会庆祝 + 响提示音 + 说一句。

**专注模式**（`Ctrl+Alt+D`）会让所有气泡静音，适合开会或录屏。

## 🧩 自定义台词

所有文案都在文件顶部，但不用改代码也能换 —— 在程序目录放一个 `pet_lines.json`：

```json
{
  "PET_LINES": ["嘿嘿", "再摸摸"],
  "FEED_LINES": ["谢谢投喂"],
  "YAWN_LINES": ["啊——欠", "老板什么时候下班"]
}
```

可用的键：`WAKE_LINES` / `SLEEP_LINES` / `YAWN_LINES` / `ROAM_LINES` /
`PET_LINES` / `FEED_LINES` / `ANNOY_LINES` / `DIZZY_LINES` / `GRAB_LINES` /
`HUNGRY_LINES` / `LONELY_LINES` / `MORNING_LINES` / `NIGHT_LINES` /
`EDGE_LINES` / `POMO_DONE_LINES`。

**只覆盖你写的那几个键**，其余继续用默认。值必须是**非空字符串列表**，
写错了会被忽略（不会让桌宠变成一句话都不说）。

## 📤 导出相处记录

右键 → **导出相处记录…**，在程序目录生成 `相处记录.png`：陪了几天、
亲密度进度条、被摸/被拖/被喂次数。可以直接发出去。

## 🔔 自动更新

右键 → **检查更新…**，后台查一次 GitHub Releases（不阻塞界面、不自动下载）。
有新版本她会说一句「有新版本 v1.2.0」，控制台也会打印下载地址。

网络不通或限流都只算「查不到」，绝不会因此报错或影响启动。

## 🎵 音效

不同事件响不同的系统音（不占体积，换系统主题时音效也跟着变）：

| 事件 | 系统音 |
|---|---|
| 任务完成 | `SystemAsterisk` |
| 出错 | `SystemHand` |
| 等你回答 | `SystemExclamation` |
| 番茄钟到点 | `SystemNotification` |

**想换成自己的音效**：在程序目录建 `sounds/`，放 `<事件>.wav`（事件名就是上表左列）：

```
sounds/done.wav
sounds/failed.wav
sounds/pomo.wav
```

同名文件存在就用你的，不存在就退回系统音。同一种音 3 秒内只响一次
（多个任务同时完成不会连响），但不同事件各算各的。

## 🖥️ 高 DPI

程序启动时声明 **PerMonitorV2** DPI 感知，窗口尺寸按系统缩放倍数放大 ——
4K 屏 200% 缩放下不会糊，也不会只有半个手掌大。老系统自动退回
System DPI 感知，再不行就不声明（模糊但能用）。

## 🏃 她会自己做什么

| 动作 | 什么时候 |
|---|---|
| **看向鼠标** | 鼠标在附近动过，且离她 90px 以上 → 她转头朝那边（会镜像翻转） |
| **溜达** | 白天精力够，随机每 35~90 秒走两步 —— **窗口真的会移动** |
| **换显示器** | 溜达时约 1/8 概率跑到另一块屏幕 |
| **探头** | 每 2~5 分钟，侧身探头看一会儿 |
| **犯困 / 打盹 / 睡着** | 闲置 45s / 90s / 240s（深夜 30s 直接睡） |
| **数羊** | 睡着时头顶飘 z z z |

**情绪与情境：**

| 触发 | 反应 |
|---|---|
| 精力低于 22% | 主动要吃的：「有点饿了…」 |
| 超过 2 小时没互动 | 叹气「好久没理我了」，亲密度缓慢下降 |
| 早上 5~11 点 / 晚上 22 点后 | 各问候一次：「早啊」/「还不睡？」 |
| 陪满 7 天 | 解锁一句：「我们认识 7 天啦」 |

## ⚙️ 设置窗口

右键菜单 → **设置…**，或者改配置文件。里面有：

- 陪伴模式 / 开机自启 / 隐私模式 / 提示音 四个开关
- 皮肤下拉选择
- 相处记录一览 + 「重置相处记录」按钮
- 「位置复位」按钮（窗口跑到屏幕外时用这个）

**开机自启**写的是 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，
不需要管理员权限。打包成 exe 之后自启的就是 exe 本身。

**多显示器**：位置按绝对坐标记，拔掉副屏之后如果窗口落到已经不存在的区域，
启动时会自动夹回可见范围（至少留 60px 让你能抓住它）。

## 📋 多个会话

一个 DSH 会话算一个任务。桌宠每轮扫描全部会话，把在跑的挑出来排序后全列出。

角色显示的是**聚合状态** —— 只要有一个任务在跑工具，她就转身拿齿轮。
折叠的信息卡里，单任务显示状态、工具名和耗时；多任务显示「N 个任务进行中」，
下面逐行列出每个任务的标题、状态、耗时。展开详情面板还能看到主任务的模型、
token 和上下文占用。

排序按「状态优先级，同类按开始时间」，所以顺序是稳的，不会来回跳。

## 🎨 皮肤

仓库带六套，右键菜单切换：

| 皮肤 | 来源 | 格式 |
|---|---|---|
| 大肥鱼 | 内置 | 三视图 + 程序化姿态 |
| kun-like | [dsh-kun-like-pet](https://github.com/liyupi/dsh-kun-like-pet) | Codex 契约 |
| openpets | [openpets](https://github.com/OpenPetsHQ/openpets) | Codex 契约（11 行扩展） |
| matcha-cheeky / gentle / sunny | [MatchaDandan](https://github.com/CaptainRong/MatchaDandan) | Codex 契约（11 行扩展） |

![契约皮肤](preview/skins-3.png)
![MatchaDandan 三画风](preview/skins-matcha.png)

六套的授权都是 MIT 或等价宽松许可，可以放心用 ✅

### ➕ 加自己的皮肤

支持两种格式，都是丢进目录就能认，不用改代码。

**Codex 桌宠精灵图契约** —— 一张图集，单格 `192×208`，8 列、至少 9 行。
行序是固定的：待机、向右跑、向左跑、挥手、跳跃、出错、等待回复、专注干活、思考审阅。
第 9 行往后随便加，openpets 就多画了两行。

```
<程序目录>/assets/skins/<名称>/spritesheet.webp
~/.codex/pets/<名称>/spritesheet.webp        ← Codex 桌宠自己的目录
```

**clawd 主题** —— 目录顶层放 `theme.json`，资源在 `assets/` 下，
每个状态一个文件（`.apng` / `.gif` / `.webp` / `.png` / `.svg` 都行）。

```
<程序目录>/assets/skins/clawd/<名称>/
%APPDATA%/clawd-on-desk/themes/<名称>/       ← clawd 自己的主题目录
```

clawd 的状态名会映射到本项目的状态：`idle`/`dozing`/`yawning` → 空闲，
`thinking`/`sweeping` → 思考中，`working`/`carrying`/`juggling` → 执行中，
`attention`/`notification` → 等你回答，`error` → 出错，`sleeping`/`collapsing` → 离线。

APNG、GIF、WebP、PNG 走 Pillow，动画完整，逐帧时长也读得到。
静态 SVG 会先光栅化再用。

**不想手动解压？** 右键 → 皮肤 → **从链接安装…**，填一个 zip 的 URL
或者本地路径，下载、解压、校验、装上一步到位，装完直接切过去。

安装器会自己判断压缩包里哪一层是皮肤根（套一层两层目录都认得），
装完还会**重新扫一遍确认认得出来**，认不出就回滚删掉 —— 不会留下
半个坏皮肤让桌宠下次启动时崩。

压缩包里的路径穿越（`../../`）、绝对路径、压缩炸弹都会被拒绝。
装错了想删：皮肤 → **删除皮肤…**（内置的三套删不掉）。

⚠️ **脚本驱动的 SVG 只能拿到静止帧。** clawd 的不少 SVG 是 JS 在运行时算动画的
（cloudling 自己管这叫 "scripted SVG object-channel assets"），光栅化只截得到一帧。
实测 cloudling：云朵、眼睛、出错态的 X 眼都在，但打字的手之类就没了，几个状态只剩一团云。
Calico 不受影响 —— 它只有 idle 用 SVG，而且是眼球追踪用的 `-follow.svg`，
加载器会自动换成同名的 APNG，动画是完整的。

### ⚖️ 仓库里为什么没有 clawd 的素材

加载器在（是代码，MIT），素材一套都没打包。原因都是授权，不是技术：

- 🚫 **calico / cloudling** 的美术权利状态**未明确**。上游源码是 AGPL-3.0，
  但 README 明说该许可不自动适用于美术，也没给这些美术单独授权。
  本地自用没问题，公开再分发就是另一回事了。
- 🚫 **鲸鱼娘** 是 CC BY-NC-SA 4.0，允许非商业移植，但 SA 有传染性 ——
  会要求整个衍生作品都用同样的许可发布，跟本仓库的 MIT 冲突。它还有 157MB。
- 🚫 **哈希仙人** 保留全部权利，许可里明确写了不许 `use these materials in another project`。

所以你自己放主题进去就行，上面两种目录都可以 ✅ 完整说明在
[ATTRIBUTION.md](assets/ATTRIBUTION.md)。

## 🕶️ 隐私模式

跟 [openpets](https://github.com/OpenPetsHQ/openpets) 学的：**气泡里不放提示词、代码和路径**。

开着的时候，任务行标题从会话标题（由你的提问生成）变成「会话 1」「会话 2」，
副行文案也从提示词片段变成「模型正在生成」这种通用说法。
右键菜单或 `--privacy` 切换，记在 `pet_config.json` 里。

![隐私模式对比](preview/privacy-preview.png)

## 🔔 托盘和提示音

桌宠被窗口挡住时，托盘图标仍然显示当前状态（图标就是该状态首帧缩到 64px，
悬停能看到状态名）。任务跑完会响一声系统提示音，多个任务同时完成只响一次。

两个都能在右键菜单里关掉。

## 🔍 状态是怎么读到的

只读两份 DSH 自己写的文件。

**会话投影缓存**（主要来源，明文 JSON）：

```
<DSH_HOME>/storages/session_projcache/sessions/session-<id>.json
```

`rows` 下面那些字段就是判断依据 —— `sessionStats.openStep` 非 null 说明在生成，
`pendingCalls` 非空说明有工具在跑，`userQuestions.questions.active` 非空说明在等你，
`llmRetry` 非空说明在重试，`turnBoundary.openTurnStartSeq` 由有转无说明刚跑完。
另外还读了 `title`、`turnOutline`、`modelSelection`、`contextPressure`、
`tokenUsage`、`plan`、`goal` 这些用来填详情面板。

**会话事件日志**（只为了拿工具名）：

```
<DSH_HOME>/sessions/*/session-<id>/session*.jsonl.zstd
```

这是追加写的 zstd 流，Python 没法一直挂着解压流读（`decompressobj` 不能复用，
`stream_reader` 也读不到后面追加的数据）。所以按 zstd 帧魔数 `28 B5 2F FD` 切开，
一帧一帧独立解，只解上次之后新增的那些 —— 每轮开销恒定，跟日志总长无关。
从 `tool/call` 事件里取 `callId → name`，就能把 `pendingCalls` 里的 ID 还原成
`pwsh`、`read`、`grep` 这种工具名。

投影缓存按 mtime + size 缓存，文件没变就不重新解析。实测 10 个会话下，
一轮轮询中位 0.8ms、最差 14ms，轮询间隔是 350ms，很宽裕。
工具名的增量读取器只给活跃会话建，最多留 6 个，不会去读遍所有日志。

## 🕳️ 实现上踩过的坑

**透明窗口。** tkinter 自己做不了逐像素透明，所以用 `WS_EX_LAYERED` +
`UpdateLayeredWindow` 直接推一张带 alpha 的 32 位位图。这样边缘是平滑的，
而不是 `-transparentcolor` 抠色那种带色边的效果。

两个坑：

- `UpdateLayeredWindow` 的 `pptDst` 参数**会移动窗口**。传坐标进去的话每帧都把窗口
  搬到那儿，Tk 设的 `geometry` 全被覆盖 —— 必须传 `NULL`。
- 预乘 alpha 别用 Python 逐像素循环，每帧五万次迭代太慢。用 `ImageChops.multiply`
  逐通道乘，再让 PIL 以 `"BGRA"` 原始模式吐字节。

**动画。** 素材只有三张静态三视图，动感全靠变换：呼吸（纵向缩放）、弹跳、轻微旋转、
状态光晕、配件旋转。所有帧预渲染后缓存，每帧只做合成和推送，CPU 占用很低。

万一分层窗口不可用，会退回 `-transparentcolor` 并打条警告。

## 📁 文件

| 路径 | 是什么 |
|---|---|
| `dsh_pet.py` | 主程序：`PetApp`（窗口 + 交互）和命令行入口 |
| `pet_base.py` | 路径、常量、Win32 分层窗口、字体、几何、出错日志 |
| `pet_types.py` | `TaskInfo` / `Snapshot` |
| `pet_render.py` | 角色渲染、叠加图形、皮肤、信息卡 |
| `pet_sources.py` | 读 DSH 会话日志、状态监视、陪伴行为、台词表 |
| `pet_system.py` | 全局热键、更新检查、开机自启 |
| `pet_skin_install.py` | 皮肤安装（下载 / 解压 / 校验 / 回滚） |
| `build_exe.py` | 打包脚本，产出 `dist/DSH-Pet.exe` |
| `installer.iss` | Inno Setup 安装包脚本（`iscc installer.iss`） |
| `tests/` | 回归测试，`python tests/test_pet.py` 直接跑 |
| `assets/` | 角色三视图，以及 `skins/` 下的契约皮肤包 |

模块之间是单向依赖：`base → types → render / sources / system → dsh_pet`。
`import dsh_pet` 会把所有符号带出来，所以老用法不变。
| `dsh-plugin/` | DSH 插件（可选，见下） |
| `启动桌宠.vbs` / `调试启动.bat` | 静默启动 / 带控制台启动 |
| `preview/` | 各状态渲染图和实机截图 |
| `pet_config.json` | 窗口位置与偏好（运行时生成，已在 .gitignore 里） |
| `pet_mood.json` | 相处记录：亲密度、精力、互动次数（同上） |
| `pet_lines.json` | 可选，自定义台词（同上） |
| `.github/workflows/` | CI：push 跑测试；打 tag 自动构建并发 Release |

## 🧪 测试

```bash
python tests/test_pet.py        # 61 项单元测试，3 秒
python tests/test_smoke.py      # 14 项冒烟测试，真的把 PetApp 建起来
```

不需要 pytest，直接跑就行。

**单元测试**覆盖状态机、渲染、日志解析、几何、热键表、台词表、出错日志、
皮肤安装（含安全边界：路径穿越 / 绝对路径 / 压缩炸弹）。

**冒烟测试**真的 `new` 一个 `PetApp`、跑一轮事件循环、检查窗口和菜单、
再干净退出。这一层是拦「启动即崩」的 —— 有过两次发版事故
（`self.greeted` 属性不存在、`_skin_menu` 没创建）都是启动就崩，
而单元测试一个都没拦住，因为它们根本不构造 `PetApp`。

副作用控制：临时目录当配置目录，托盘从 `sys.modules` 摘掉（不弹图标、不起线程）。

CI 在 Windows + Python 3.10/3.12 上跑这两套，push 和 PR 都触发。

## 🔌 DSH 插件（可选）

`dsh-plugin/` 里有个 DSH 插件，作用是**随 DSH 启动自动拉起桌宠**，
以及在侧边栏底部加一个开关按钮。

**目前只有 Host 半能用** ⚠️：自动启动没问题，`/dsh-pet/status` 和 `/dsh-pet/toggle`
两条路由实测正常。但**侧边栏那个按钮没出现**，还在查 —— 怀疑是本包没被扫进
客户端模块图，需要看 `window.__DSH_BOOT__` 才能确认。插件里留了条
`/dsh-pet/debug-boot` 路由专门用来查这个。

桌宠自己带单实例锁（绑本地端口 51937，进程退出由系统释放），
所以插件、快捷方式、手动启动三条路随便走，不会叠出两只。

安装方式见 [dsh-plugin/cordis.patch.yml](dsh-plugin/cordis.patch.yml) 里的注释
（Electron 桌面版独占管理 profile，`dsh plugin add` 会被拒，得手工链接）。

不用插件也完全没关系，双击 `启动桌宠.vbs` 一样跑 👌

## ⚠️ 已知限制

**状态有 3~5 秒延迟。** DSH 的投影缓存不是工具一发就更新：

```
t=0.0s  pendingCalls={}                  openStep=step 71   ← 工具已经在跑了
t=2.5s  pendingCalls={}                  openStep=step 71   ← 投影还是没更新
t=5.0s  pendingCalls={call_00_ecsx...}   openStep=None      ← 这才切到「执行中」
```

所以短于 5 秒左右的工具调用可能捕捉不到，桌宠会一直显示「思考中」。
这是读投影缓存的固有限制，不是渲染的问题。

**只看本机。** 读的是本地 DSH 数据目录，远程或容器里的 DSH 看不到。

**一个会话 = 一个任务。** DSH 投影缓存里的 `subagentCatalog` 基本是空的，
后台任务更是完全没暴露，所以同一个会话里并行跑的子代理不会各占一行，
都算在那个会话这一个任务里。clawd-on-desk 那种「多个子代理时抛接」的动作，
这个数据源做不到。

**两个状态的逻辑还没在真实触发时验证过：**

| 状态 | 依据字段 | 验证到什么程度 |
|---|---|---|
| 🟩 已完成 | `turnBoundary.openTurnStartSeq` | ✅ 两种状态都实测到过 |
| 🩷 等你回答 | `userQuestions.questions.active` | ⚠️ 结构确认了，但只见过空值 |
| 🟥 出错重试 | `llmRetry` | ⚠️ 同上 |

也就是说后两个的判定是照着字段结构写的，还没在真触发时看过。
你哪天正好遇到（我向你提问时、或模型重试时）可以瞄一眼是不是如期亮起 👀
渲染和状态机本身用 `--shot` 逐个验证过了。

## 🙏 素材与致谢

角色形象「鲸鱼娘·大肥鱼」来自 [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet)，
契约皮肤来自 [liyupi/dsh-kun-like-pet](https://github.com/liyupi/dsh-kun-like-pet) 和
[OpenPetsHQ/openpets](https://github.com/OpenPetsHQ/openpets)、
[CaptainRong/MatchaDandan](https://github.com/CaptainRong/MatchaDandan)，都是 MIT。
上游已经把抠图和边缘去污做完了，这边直接拿来用。逐个素材的来源见
[assets/ATTRIBUTION.md](assets/ATTRIBUTION.md)。

写的时候参考过这几个同类项目：

- [liyupi/dsh-kun-like-pet](https://github.com/liyupi/dsh-kun-like-pet) —— DSH Cordis 插件形态
- [CLICGGER-TYPES/dsh-piggy](https://github.com/CLICGGER-TYPES/dsh-piggy) —— 插件 + Electron 双形态
- [rullerzhou-afk/clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk) —— 状态机设计
- [OpenPetsHQ/openpets](https://github.com/OpenPetsHQ/openpets) —— 隐私原则
- [ntd4996/agentpet](https://github.com/ntd4996/agentpet) —— 托盘状态显示

它们大多走插件路线，这个项目选了另一条：**独立进程 + 只读投影缓存**，
所以不装插件也能用，也不用改 DSH。

## 📄 许可

代码 MIT，见 [LICENSE](LICENSE)。随附素材各自归属原作者，也都是 MIT 或等价的宽松许可。
