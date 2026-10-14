# 素材来源与授权

## 角色形象

`assets/front.png`、`assets/side.png`、`assets/back.png`（以及 `*_238.png` 备用尺寸）
来自开源项目：

- **项目**：[1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet) —— 「大肥鱼桌宠」
- **协议**：MIT License（Copyright (c) 2026）
- **形象**：DeepSeek V4 Pro 二创形象「鲸鱼娘·大肥鱼」

上游对素材做了两道处理，本项目直接使用其成品：

1. **白底抠图** —— 泛洪填充去背景，再按 bbox 裁切
2. **边缘去污 + 预乘 alpha 缩放** —— 对半透明边缘像素做白底去混合
   （`fg = (pixel - 255*(1-a)) / a`），消除移动/旋转时的白边

因此这三张 PNG 是已经干净、可直接合成的透明图。

### 原始文件名对照

| 本项目 | 上游文件 |
|---|---|
| `assets/front.png` | `sprites/正面_306.png` |
| `assets/side.png` | `sprites/侧面_306.png` |
| `assets/back.png` | `sprites/背面_306.png` |
| `assets/front_238.png` 等 | `sprites/正面_238.png` 等（备用小尺寸） |
| `assets/icon.png` | `sprites/icon.png` |

### 需要重新下载时

```
https://raw.githubusercontent.com/1190fasheqi/dafeiyu-pet/main/sprites/正面_306.png
https://raw.githubusercontent.com/1190fasheqi/dafeiyu-pet/main/sprites/侧面_306.png
https://raw.githubusercontent.com/1190fasheqi/dafeiyu-pet/main/sprites/背面_306.png
```

（文件名含中文，URL 需按 UTF-8 百分号编码。）

## 皮肤素材

### `assets/skins/kun-like/spritesheet.webp`

- **项目**：[liyupi/dsh-kun-like-pet](https://github.com/liyupi/dsh-kun-like-pet) —— 「Kun Like 桌宠」
- **协议**：MIT License（Copyright (c) 2026 liyupi）
- **规格**：`1536 × 1872`，8 列 × 9 行，单格 `192 × 208` —— 完全符合
  **Codex 桌宠精灵图契约**（上游注明素材沿用 Codex 官方契约，未重新设计）

它是本项目**契约兼容性的实测样本**：每行非空格子数（6/8/8/4/5/8/6/6/6）
与契约声明的「使用列」逐一吻合，也因此验证了「帧数从图集自动探测」这条实现路径。

### 支持任意契约皮肤

除内置皮肤外，程序扫描这两个目录，把任何符合契约的包列为可选皮肤：

- `assets/skins/<名称>/spritesheet.{webp,png}`
- `~/.codex/pets/<名称>/spritesheet.{webp,png}` ← Codex 桌宠的官方目录

从 clawd-on-desk 等项目的主题里拿到的精灵图，直接放进去就能用，不需要改代码。
用 `--list-skins` 查看当前可用皮肤。

### `assets/skins/openpets/spritesheet.webp`

- **项目**：[OpenPetsHQ/openpets](https://github.com/OpenPetsHQ/openpets) —— 桌面伴侣平台
- **规格**：`1536 × 2288` —— 8 列 × **11 行**，单格同为 `192 × 208`

它是**契约「向前兼容」的实测样本**：前 9 行与 Codex 契约逐行吻合
（挥手 4 帧、跳跃 5 帧、等待 6 帧、出错 8 帧……），第 10、11 行是它自己的扩展状态。
因此本项目把尺寸校验放宽为「宽固定 1536、高为 208 的整数倍且不少于 9 行」，
既接受标准契约包，也接受这类扩展包。

### `assets/skins/matcha-cheeky/` · `matcha-gentle/` · `matcha-sunny/`

- **项目**：[CaptainRong/MatchaDandan](https://github.com/CaptainRong/MatchaDandan) —— 抹茶蛋蛋的多画风 Codex 宠物合集
- **作者**：RongCao
- **许可**：**MIT License**（`Copyright (c) 2026 RongCao`）
- **规格**：`1536 × 2288` —— 8 列 × 11 行，每行帧数 `[7, 8, 8, 4, 5, 8, 6, 6, 6]`

**MIT 是本项目里授权最干净的一类** —— 可自由使用、修改、分发，只需保留版权声明。
三个变体是同一角色的不同画风（Cheeky 棱角分明 / Gentle 圆润大眼 / Sunny 介于两者）。

## clawd 主题素材：一套都没有打包

本项目**支持** clawd 主题格式（加载器是仓库里的代码，MIT），
但**仓库里不含任何 clawd 素材**。原因是授权，逐条说明：

### calico / cloudling —— 权利未明确

- **项目**：[clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk)（rullerzhou-afk）
- **作者**：calico 为 rullerzhou；cloudling 为 stlabslogo

上游源码是 AGPL-3.0-only，但仓库 README 明确写道，该源代码许可
**「不自动适用于」**角色设计、美术、动画、品牌元素或主题资源包 ——
同时也**没有为这些美术单独给出许可**。

也就是说这两套美术的权利状态是**未明确**的：不是「明确允许」，也不是「明确禁止」。
**本地自用是一回事，公开再分发是另一回事** —— 后者风险明确，所以仓库排除它们。

### 鲸鱼娘 Whale-chan —— 许可允许，但与本仓库的 MIT 冲突

- **主题作者**：鹿鹿（[rullerzhou-afk](https://github.com/rullerzhou-afk)）
- **上游素材作者**：ZipZipPipe（<https://space.bilibili.com/4168597>）、
  上善无形（<https://space.bilibili.com/4456176>）
- **上游项目**：[Neko3000/deepseek-whalechan](https://github.com/Neko3000/deepseek-whalechan)
- **许可**：[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)
- **归档 sha256**：`1e37403b21570f5d6850caade7ec48e3d97da3f9d5223071461e6c202ad7e0d8`

这条许可**允许非商业移植**（条件是署名 + 注明修改 + 相同方式共享），
但 **SA 有传染性**：包含它的衍生作品必须同样以 CC BY-NC-SA 4.0 发布，
并因此**不能商用**。这与本仓库的 MIT 许可直接冲突。

所以它被 `.gitignore` 排除。**你自己放进去完全没问题** —— 只要你接受
「用了它的那份作品不能商用」这个条件，并保留主题包内的 `LICENSE`。

### 哈希仙人 Hash Sage —— 明确禁止

`LicenseRef-Hash-Sage-Art`，保留全部权利。LICENSE 原文：

> Except for personal use of the Clawd on Desk application as distributed by this
> project, you may not copy, modify, repackage, distribute, resell, or **use these
> materials in another project** without explicit written permission from the
> copyright holder.

注意「个人使用」**是绑定在 Clawd on Desk 这个应用里的**，不是通用的个人使用许可。
移植到本项目属于 "use these materials in another project"，需要权利人书面许可。
**本项目不收录，也不建议你这么做。**

### 想用 clawd 主题怎么办

加载器已经写好了，你把主题目录放进去就能用，见 README 的「格式二：clawd 主题」。
扫描目录：

```
<程序目录>/assets/skins/clawd/<名称>/     ← 自己放这里
%APPDATA%/clawd-on-desk/themes/<名称>/    ← 或 clawd 自己的主题目录
```

**建议只放你有权使用的主题**：自己做的、作者明确授权的、
或者许可兼容的（CC0 / CC-BY / CC-BY-SA / MIT 等）。

## 本项目的角色相关代码

`dsh_pet.py` 里的角色渲染（呼吸、弹跳、摇摆、状态光晕、配件）为本项目原创，
素材本身按上游 MIT 协议使用。

## 参考过的其他同类项目

- [liyupi/dsh-kun-like-pet](https://github.com/liyupi/dsh-kun-like-pet) —— DSH Cordis 插件形态，
  用 `agents` 服务 + `tools/execute` 等事件推导状态；本项目借用其精灵图作契约样本
- [CLICGGER-TYPES/dsh-piggy](https://github.com/CLICGGER-TYPES/dsh-piggy) —— DSH 插件 + Electron 桌面双形态
- [rullerzhou-afk/clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk) —— 状态机设计参考
  （睡觉 / 思考 / 打字 / 多子代理 / 审批气泡 / 庆祝）
- [OpenPetsHQ/openpets](https://github.com/OpenPetsHQ/openpets) —— 「气泡不显示提示词、代码、路径」的隐私原则
- [ntd4996/agentpet](https://github.com/ntd4996/agentpet) —— 托盘图标显示各 agent 状态

本项目与它们的路线不同：**独立进程 + 只读会话投影缓存**，
因此不需要安装 DSH 插件，也不改动 DSH 本身。
