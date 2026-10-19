# 给改这个仓库的人（和 AI）看的

这不是风格指南，是**踩过的坑**。

每条规则后面都挂着真实事故 —— 没出过事的建议不会写在这里。
按「最贵的一次」排。

---

## 1. 加功能，默认先写一条走 `_poll` 的测试

**这条排第一，因为同一个错误犯了两次。**

测试分三层：

| 层 | 文件 | 测什么 |
|---|---|---|
| 单元 | `tests/test_pet.py` | 零件（函数输入输出） |
| 冒烟 | `tests/test_smoke.py` | 能不能起来、窗口和对话框在不在 |
| 端到端 | `tests/test_e2e.py` | **整条路**：写配置 → 建 App → 跑一轮 → 看推给屏幕的像素 |

新功能**至少**要有端到端那层的一条。端到端的核心工具是 `capture(app)`，
它截获 `display.push` 收到的画布 —— 那就是用户真正看到的东西。

### 两次事故

**第一次（配饰）**：测试直接调 `draw_accessory()` 验证它画得出来 —— 通过。
但真实路径上有两个 bug（配置被 BOM 吞掉、绘制污染缓存），**一个都没测到**，
最后是用户发现的。

**第二次（久坐提醒）**：测试直接调 `_health_check()` —— 通过。
但它挂在 `CompanionSource.poll()` 里，而那个函数**只在 DSH 空闲时**才被调用。
久坐恰恰发生在你干活的时候，结果是「你越忙她越不提醒」。
实测：DSH 是 `tools` / `waiting` 时 `_health_check` 被调用 **0 次**。

**第二次是在写完第一条规则之后犯的** —— 端到端那一层建好了，但新功能没往里加。

### 怎么写

```python
def test_xxx_reaches_the_screen(self):
    self.write_config(xxx=...)
    app = self.make_app()
    canvas = self.capture(app)
    self.assertIsNotNone(self.diff_box(before, canvas))
```

**断言像素，不是断言中间变量。**

### 装置上的两个坑

- **时间要冻住**：动画帧按 `time.time()` 选，两次截图之间墙钟在走，
  画面对不上 —— 会误判成「配饰没关掉」。`capture()` 里已经 `mock` 掉了。
- **`_poll` 要能关**：它会用 DSH 状态覆盖你手工塞的 `snapshot`。
  用 `capture(app, poll=False)`。

### 想清楚走哪条分支

`_poll` 有三条路：`args.demo` / `args.force_state` / `self.watcher`。

- `make_app()` 默认 `force_state="idle"` → 走 `_demo_snapshot`，**到不了 watcher**
- 要测 watcher 那条路，用 `make_app(force_state=None)`（会建真的 watcher 和
  companion，再用 `_stub_watcher()` 换掉）

### 第四次了：功能挂在只走一条分支的路径上

同一个形状，四次：

| 次 | 功能 | 挂在哪 | 现象 |
|---|---|---|---|
| 1 | 久坐提醒 | `CompanionSource.poll()` | 只在 DSH 空闲时跑 → **你越忙她越不提醒** |
| 2 | 音乐 / 应用感知 | `CompanionSource.poll()` | 同上 → **DSH 一忙就全失效** |
| 3 | quiet 抑制 | 压掉了**所有**气泡 | 连久坐提醒也没了 |
| 4 | `_health_tick` 的守卫 | `state not in ("idle","offline")` | 和 #3 一夹，气泡两边都没了 |

**#3 和 #4 是一对**：一个负责清，一个负责补，**清的那个不看清了什么，
补的那个不补它被清掉的**。单独看两边都对，合起来功能就没了。

**所以：加"清理/抑制"动作时，去查所有"补丁式重新贴上"的地方 ——
它们会互相抵消。** 反过来也一样。

**测法**：不要只测"她说了一句话"（`companion.bubble` 有值），
要测**最终 snapshot 上的 `bubble`** —— 中间变量有值、最后被清掉，
是最容易漏的一种。

### 第五次：一次性的状态修正，打不过每轮都跑的状态机

音乐那版在 `_music_tick` 里调了 `wake()` —— 把她从 sleeping 推到 waking。
**下一轮状态机看见 idle 很大，又把她哄睡了。** CI 报的：

    AssertionError: 'zzz' != 'music'

`wake()` 修一次，状态机每轮推翻它。

**修状态要用状态机自己的条件，不要用一次性调用。**

```python
elif self.state == "idle":
    # ✅ 写进条件里
    if self._music_playing():
        pass                       # 有人在放歌 = 有人在
    elif idle > sleep_after or ...:
        self._enter("sleeping")
```

这条和上面四次是一对：那四次是**没走到**，这次是**走到了但被推翻**。
共同点还是「你改的东西，和你以为在生效的地方，不是同一个」。

---

## 2. 属性在 `__init__` 里建，不要「用到才建」

```python
# ❌ 只在 _drop() 里赋值
self._drop_job = self.root.after(16, step)

# 别处直接读：
if self._drag is not None or self._drop_job is not None:   # AttributeError
```

**事故**：`_attention()` 这么写。全新启动、还没拖过她的时候抛
`AttributeError`，被 `_poll` 的 `except` 吞掉。后果是
**「卡在你这儿就走到屏幕中间」从来没生效过**，除非你先拖她一次。

这类 bug 不崩、不报错，只是某个功能**安静地不工作**。

`test_e2e.py::TestLazyAttributes` 会全文件扫描。判定「明确处理过」的方式是
搜 `getattr(self, "名字"` —— 不能只看用到的那一行，守卫常写在上一行。

---

## 3. 改渲染，**要看图**

**事故**：多语言第一版只包了菜单。测试全绿 —— 因为它只查
「`t()` 包住的有没有翻译」，而**根本没包**的信息卡不在检查范围里。
截图一看，英文那列的信息卡还是全中文。

> 测试绿只能说明「你检查的东西是对的」，不能说明「你检查全了」。

改完渲染，跑一下 `preview/` 里的生成脚本，或者直接截实机窗口看一眼。

---

## 4. 读本地 json 一律用 `utf-8-sig`

**事故**：用户会在记事本里改 `pet_config.json` / `pet_lines.json` /
`theme.json`，而记事本存「UTF-8」是**带 BOM** 的。用 `encoding="utf-8"`
读会抛 `UnicodeDecodeError`，而调用方一律 `except` 后返回默认值 ——
**整份配置被静默重置**。实测：带 BOM 的 `pet_mood.json` 读出来 `pokes=0`（真值 42）。

`utf-8-sig` 同时兼容有 BOM 和没 BOM，没有理由用 `utf-8`。

---

## 5. 别原地改缓存对象

**事故**：`frames` 是皮肤的缓存帧，`draw_accessory()` 是原地绘制。
不 `copy()` 的话配饰会烧进缓存 —— 关掉还在、换一个会叠加。

```python
sprite = frames[idx]
sprite = sprite.copy()          # ← 少了这行
pet_accessories.draw_accessory(sprite, ...)
```

写完问一句：**这个对象是别人缓存好的吗？**

---

## 6. 测试不许依赖机器状态

**事故**：健康提醒的测试只 `patch` 了 `pet_sources.idle_seconds`，
但 `dsh_pet` 里是 `from pet_sources import *` —— **另一份引用**，patch 无效。
于是真实系统空闲时间说了算：刚敲完命令时过，机器闲了 60 秒后就挂。
**跑三遍才发现。**

已经踩过的几种：

| 依赖 | 事故 |
|---|---|
| 系统空闲时间 | 上面这个 |
| 界面语言 | 冒烟测试断言 `"安装" in label`，CI runner 是英文环境就挂 |
| 时钟刻度 | 连调两次 `time.time()` 可能落在同一格，`>` 比较失败 |
| 动画相位 | 见规则 1 的「时间要冻住」 |

**不确定的输入就钉死。** 跑三遍再提交。

---

## 7. `from x import *` 的两个陷阱

**下划线开头的名字带不出来。** `_fmt_ms` / `_gear` / `_rgba` 这些要么显式引，
要么改成公开名。

```python
from pet_render import *
from pet_render import _fmt_ms      # 显式引
```

**可变的模块级常量会被复制一份。** 所以路径这类东西写成了**函数**
（`config_path()` / `skins_dir()`），而不是常量 —— 测试里 patch
`pet_base.data_dir` 才能影响所有模块。

同理，`patch` 的时候想清楚 patch 的是哪个模块的命名空间。

---

## 8. 别用 `name[:n]` 截断给人看的字符串

**事故**：皮肤预览的名字是 `s["display"][:14]`，界面上出现
「Hash Sage (cla」「Calico (clawd」这种半截词。

`_short_name()` 优先在括号断，其次在最后一个空格断，实在没地方断才硬切加省略号。

---

## 9. 报发现，别只报「做完了」

这个仓库的 README 里写着「实现上踩过的坑」，release notes 里也写。

**因为这些都是真发生的。** 用户问「XX 你做了吗」的时候，
答案经常是「做了，但没生效」—— 老实说，比含糊过去有用。

写 commit message 也一样：**写清楚为什么这么改，而不是改了什么**
（`git diff` 已经说了改了什么）。

---

## 提交前跑这个

```bash
python tests/test_pet.py
python tests/test_smoke.py
python tests/test_e2e.py
```

CI 三层都跑，发版时再跑一遍，**不过就不发**。

改动涉及渲染的话，再看一眼 `preview/` 里对应的图。
