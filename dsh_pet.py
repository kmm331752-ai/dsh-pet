"""DSH 桌面宠物 —— 主程序。

拆出来的模块：
    pet_base      路径、常量、Win32 分层窗口、字体、几何、出错日志
    pet_types     TaskInfo / Snapshot / Mood
    pet_render    角色渲染、叠加图形、皮肤、信息卡
    pet_sources   读 DSH 会话日志 + 陪伴行为
    pet_system    全局热键、更新检查、开机自启

这个文件只剩 PetApp（窗口 + 交互）和命令行入口。
原来是一个 3300 行的单文件 —— 拆开是为了能改得动。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import random
import sys
import threading
import time

import tkinter as tk

from PIL import Image, ImageDraw

from pet_base import *
from pet_render import *
from pet_sources import *          # 也带出 Mood
from pet_system import *
from pet_types import Snapshot, TaskInfo

import pet_accessories
import pet_chat
from pet_i18n import t

# ─────────────────────────────────────────────────────────────────────────────

class PetApp:
    def __init__(self, args):
        import tkinter as tk
        self.tk = tk
        self.args = args
        self.cfg = self._load_config()

        self.forced_state: Optional[str] = None
        self.demo_i = 0
        self.last_demo = 0.0

        # DPI 缩放。main() 里已经声明了 PerMonitorV2 感知，所以窗口尺寸
        # 是物理像素 —— 200% 缩放的屏上不乘这个，桌宠只有一半大。
        # 测试里直接构造 PetApp 时没走 main()，所以这里自己取一次；
        # 取不到就是 1.0，不影响低 DPI。
        self.dpi = dpi_scale()

        self.root = tk.Tk()
        self.root.title("DSH Pet")
        # Tk 回调（事件绑定、after 任务）里抛的异常默认只打到 stderr ——
        # exe 模式下那是黑洞。接过来落盘。
        self.root.report_callback_exception = lambda *a: log_error("Tk 回调异常", a[1])
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        self.root.resizable(False, False)

        dx, dy = default_position()
        x = int(self.cfg.get("x", dx))
        y = int(self.cfg.get("y", dy))
        # 缩放要在几何之前算好 —— 窗口尺寸由它决定。
        # dpi_scale 乘进来：声明了 DPI 感知之后，窗口尺寸是**物理像素**，
        # 不乘的话 200% 缩放的屏上桌宠只有一半大。
        self.scale = max(SCALE_MIN, min(SCALE_MAX, float(self.cfg.get("scale", 1.0))))
        self.win_w = max(60, int(round(WIN_W * self.scale * self.dpi)))
        self.win_h = max(80, int(round(WIN_H * self.scale * self.dpi)))
        x, y = clamp_position(x, y)                      # 拔掉副屏后可能已经在屏幕外
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

        self.display, self.backend = make_display(self.root, self.win_w, self.win_h)

        self.watcher = None if (args.demo or args.force_state) else \
            StatusWatcher(pinned=args.session)
        # 陪伴模式：DSH 没活干时接管。--demo/--force-state 下不用，那是纯预览。
        self.companion = None if (args.demo or args.force_state or args.no_companion) \
            else CompanionSource()
        self.in_companion = False

        self.detail = bool(self.cfg.get("show_detail", False))
        self.privacy = bool(self.cfg.get("privacy", False))
        self.sound = bool(self.cfg.get("sound", True))
        # 配饰 id。配置里存了个不认识的（改过文件、降级过版本）就当没戴。
        _acc = str(self.cfg.get("accessory", pet_accessories.DEFAULT_ACCESSORY))
        self.accessory = _acc if _acc in pet_accessories.ACCESSORIES \
            else pet_accessories.DEFAULT_ACCESSORY
        # 本地对话的端点与模型（空 = 没配，功能关着）
        self.chat_url = str(self.cfg.get("chat_url", "") or "")
        self.chat_model = str(self.cfg.get("chat_model", "") or "")
        self.chat = pet_chat.ChatClient(self.chat_url, self.chat_model)
        # 语言。auto 按 Windows 界面语言解析，所以这里存偏好、算具体值。
        self.lang_pref = str(self.cfg.get("lang", "auto") or "auto")
        self.lang = pet_i18n.set_language(self.lang_pref)
        self.frame_i = 0
        self.snap = Snapshot(state=args.force_state or "idle")
        self._card_cache = (None, None)
        self._drag = None
        self._moved = False
        self.poke_until = 0.0
        self._tray_actions: list = []
        # 每个事件各自节流 —— 完成和出错能先后响，同一种不会连响
        self._last_sound: dict = {}
        self._last_state = ""

        # ── 交互状态 ──
        # 缩放（scale / win_w / win_h）已在几何之前初始化，这里只补其余交互状态。
        self.hover_since = 0.0        # 鼠标停在她身上的起始时刻
        self.hover_until = 0.0        # 悬停反应持续到什么时候
        self.dizzy_until = 0.0        # 被甩晕
        self.annoyed_until = 0.0      # 被摸烦了
        self.petted_until = 0.0       # 摸得开心
        self.pet_streak = 0           # 连续摸的次数（超了就烦）
        self.pet_streak_at = 0.0
        self.grabbed = False          # 长按"抱起来"
        self.grab_edge = None         # 抓住屏幕边缘时贴的是哪一边
        self._press_at = 0.0
        self._press_xy = (0, 0)
        self._shake = []              # 甩动检测：最近几次拖动的方向与时刻
        self._mouse = (0, 0)          # 最近一次鼠标位置（用来看向鼠标）
        self._mouse_at = 0.0
        self._cursor_prev = (0, 0)    # 全局鼠标位置的上一次采样（判断"刚动过"）
        self._cursor_at = 0.0
        # 番茄钟 / 专注模式 / 截图模式
        self.pomo_until = 0.0         # 计时结束时刻（0 = 没在计时）
        self.pomo_total = 0
        self.focus_mode = bool(self.cfg.get("focus", False))
        self._ghost = False           # 截图模式：连托盘一起藏
        self._autohide = bool(self.cfg.get("autohide", False))
        self._peek_until = 0.0        # 贴边隐藏后探出来的时刻
        self._attention_at = 0.0      # 上次"等待提醒"挪窗口的时刻
        self._long_press_job = None
        self.wander_x = 0             # 溜达时窗口的水平偏移

        self.skin = load_skin(getattr(args, "skin", None) or self.cfg.get("skin", "dafeiyu"))
        self._build_sprites()
        self._bind_events()
        self._start_tray()

        self.root.after(60, self._poll)
        self.root.after(60, self._tick)

    # -- 配置 ---------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            with open(config_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:                                # noqa: BLE001
            return {}

    def _save_config(self) -> None:
        cfg = dict(self.cfg)
        cfg["x"], cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        cfg["show_detail"] = self.detail
        cfg["privacy"] = self.privacy
        cfg["sound"] = self.sound
        cfg["skin"] = self.skin.name
        cfg["scale"] = round(self.scale, 3)
        cfg["accessory"] = self.accessory
        cfg["chat_url"] = self.chat_url
        cfg["chat_model"] = self.chat_model
        cfg["lang"] = self.lang_pref
        try:
            with open(config_path(), "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    # -- 精灵缓存 -----------------------------------------------------------
    def _ensure_state(self, st: str) -> None:
        """按需渲染某个状态的帧。

        不能启动时全建：whale-chan 七个状态就要 6.4 秒，现在状态涨到 13 个，
        全建得十几秒，那段时间窗口是死的。改成用到哪个建哪个，
        启动只等 idle。
        """
        if st in self.sprites:
            return
        try:
            items = self.skin.frames(st)
        except Exception as exc:                         # noqa: BLE001
            log_error(f"状态 {st} 渲染失败", exc)
            items = []
        self.sprites[st] = [im for im, _ in items]
        self.durations[st] = [d for _, d in items]
        self._loop_ms[st] = sum(self.durations[st])

    def _build_sprites(self) -> None:
        """预渲染 idle，其余状态等用到再建（见 _ensure_state）。"""
        self.sprites: dict = {}
        self.durations: dict = {}
        self._loop_ms: dict = {}
        t0 = time.time()
        self._ensure_state("idle")
        # 交互反应素材（clawd 主题自带；内置皮肤程序化生成）
        self.reaction_sprites: dict = {}
        self.reaction_durs: dict = {}
        for kind in REACTION_KINDS:
            try:
                items = self.skin.reaction_frames(kind)
            except Exception as exc:                     # noqa: BLE001
                log_error(f"反应素材 {kind} 加载失败", exc)
                items = []
            self.reaction_sprites[kind] = [im for im, _ in items]
            self.reaction_durs[kind] = [d for _, d in items]
        have = [k for k in REACTION_KINDS if self.reaction_sprites[k]]
        print(f"[skin] {self.skin.display}: 启动预渲染 "
              f"{len(self.sprites['idle'])} 帧，{time.time() - t0:.1f}s"
              f"；其余状态按需加载；交互反应 {', '.join(have) if have else '无'}")

    # -- 事件 ---------------------------------------------------------------
    def _bind_events(self) -> None:
        r = self.root
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_drag)
        r.bind("<ButtonRelease-1>", self._on_release)
        r.bind("<Button-3>", self._on_menu)
        r.bind("<Motion>", self._on_motion)               # 悬停反应 + 看向鼠标
        r.bind("<Leave>", self._on_leave)
        r.bind("<MouseWheel>", self._on_wheel)            # 滚轮缩放
        r.bind("<Double-Button-1>", self._on_double)      # 双击开设置

        self._build_menu()

    def _build_menu(self) -> None:
        """建右键菜单。抽出来是为了切语言时能整个重建 ——
        Tk 的菜单项文字建完就改不了（entryconfigure 能改 label，
        但级联子菜单和 radiobutton 的变量要一起重做，不如重建干净）。
        """
        r = self.root
        if getattr(self, "menu", None) is not None:
            try:
                self.menu.destroy()
            except Exception:                            # noqa: BLE001
                pass
        self.menu = self.tk.Menu(r, tearoff=0)
        self.menu.add_command(label=t("喂一口") + " 🍡", command=self._feed)
        self.menu.add_command(label=t("展开详情") + "/" + t("收起详情"), command=self._toggle_detail)
        self.menu.add_separator()
        self.menu.add_command(label=t("自动跟随活跃会话"), command=self._follow_auto)
        self.menu.add_command(label=t("复制会话 ID"), command=self._copy_sid)
        self.menu.add_separator()
        self.var_privacy = self.tk.BooleanVar(value=self.privacy)
        self.var_sound = self.tk.BooleanVar(value=self.sound)
        self.var_companion = self.tk.BooleanVar(
            value=self.companion is not None and bool(self.cfg.get("companion", True)))
        self.menu.add_checkbutton(label=t("隐私模式（隐藏标题与提示词）"),
                                  variable=self.var_privacy, command=self._toggle_privacy)
        self.menu.add_checkbutton(label=t("任务完成时提示音"),
                                  variable=self.var_sound, command=self._toggle_sound)
        self.menu.add_checkbutton(label=t("陪伴模式（没任务时自己过日子）"),
                                  variable=self.var_companion, command=self._toggle_companion)
        self.var_focus = self.tk.BooleanVar(value=self.focus_mode)
        self.menu.add_checkbutton(label=t("专注模式（安静待机不打扰）"),
                                  variable=self.var_focus, command=self._toggle_focus)
        self.menu.add_separator()
        self._pomo_menu = self.tk.Menu(self.menu, tearoff=0)
        for mins in POMODORO_CHOICES:
            self._pomo_menu.add_command(label=f"{mins} {t('分钟')}",
                                        command=lambda m=mins: self._start_pomodoro(m))
        self._pomo_menu.add_separator()
        self._pomo_menu.add_command(label=t("取消计时"), command=self._cancel_pomodoro)
        self.menu.add_cascade(label=t("番茄钟") + " 🍅", menu=self._pomo_menu)
        self.menu.add_separator()
        self._skin_menu = self.tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label=t("皮肤"), menu=self._skin_menu)
        self._rebuild_skin_menu()
        self._acc_menu = self.tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label=t("配饰"), menu=self._acc_menu)
        self._rebuild_acc_menu()
        self.menu.add_separator()
        self.menu.add_command(label=t("设置…"), command=self._open_settings)
        self.menu.add_command(label=t("和大肥鱼说话…"), command=self._open_chat)
        self.menu.add_command(label=t("导出相处记录…"), command=self._export_record)
        self.menu.add_command(label=t("检查更新…"), command=self._check_update_async)
        self.menu.add_command(label=t("退出"), command=self._quit)

    def _rebuild_menus(self) -> None:
        """切语言后重建菜单。"""
        self._build_menu()
        self._rebuild_skin_menu()
        self._rebuild_acc_menu()

    def _rebuild_skin_menu(self) -> None:
        self._skin_menu.delete(0, "end")
        self.var_skin = self.tk.StringVar(value=self.skin.name)
        for s in discover_skins():
            self._skin_menu.add_radiobutton(
                label=s["display"], value=s["id"], variable=self.var_skin,
                command=lambda sid=s["id"]: self._switch_skin(sid))
        self._skin_menu.add_separator()
        self._skin_menu.add_command(label=t("从链接安装…"), command=self._install_skin_dialog)
        self._skin_menu.add_command(label=t("删除皮肤…"), command=self._remove_skin_dialog)

    # -- 皮肤安装 / 删除 ---------------------------------------------------
    def _install_skin_dialog(self) -> None:
        """要一个链接或本地路径，后台装，装完刷新菜单。"""
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(t("安装皮肤"))
        win.geometry("520x190")
        win.attributes("-topmost", True)
        tk.Label(win, text=t("皮肤压缩包链接，或本地文件路径："),
                 anchor="w").pack(fill="x", padx=14, pady=(14, 4))
        entry = tk.Entry(win)
        entry.pack(fill="x", padx=14)
        entry.focus_set()
        tk.Label(win, text=t("支持 zip；里面要有 theme.json 或 spritesheet.png / spritesheet.webp"),
                 anchor="w", fg="#888").pack(fill="x", padx=14, pady=(4, 0))
        status = tk.Label(win, text="", anchor="w", wraplength=480, justify="left")
        status.pack(fill="x", padx=14, pady=(8, 0))

        def go():
            src = entry.get().strip().strip('"')
            if not src:
                status.configure(text=t("先填个链接或路径"))
                return
            status.configure(text=t("正在下载安装…"))
            win.update_idletasks()

            def work():
                try:
                    import pet_skin_install
                    skin_id, msg = pet_skin_install.install_skin(src)
                    self._tray_actions.append(f"skin-installed:{skin_id}:{msg}")
                except Exception as exc:                 # noqa: BLE001
                    log_error("安装皮肤失败", exc)
                    self._tray_actions.append(f"skin-error:{exc}")

            threading.Thread(target=work, name="dsh-pet-skin", daemon=True).start()

        bar = tk.Frame(win)
        bar.pack(fill="x", padx=14, pady=(10, 12))
        tk.Button(bar, text=t("安装"), command=go).pack(side="right")
        tk.Button(bar, text=t("取消"), command=win.destroy).pack(side="right", padx=6)
        entry.bind("<Return>", lambda _e: go())

    def _remove_skin_dialog(self) -> None:
        """列出能删的皮肤（自己装的），选一个删掉。"""
        tk = self.tk
        import pet_skin_install
        removable = [s for s in discover_skins() if s["id"] not in pet_skin_install.BUILTIN]
        if not removable:
            self._say(t("没有可删的皮肤"), 2.5)
            return
        win = tk.Toplevel(self.root)
        win.title(t("删除皮肤"))
        win.geometry("360x300")
        win.attributes("-topmost", True)
        tk.Label(win, text=t("选一套删掉（内置皮肤不在列表里）："),
                 anchor="w").pack(fill="x", padx=14, pady=(14, 6))
        box = tk.Listbox(win)
        box.pack(fill="both", expand=True, padx=14)
        for s in removable:
            box.insert("end", f"{s['display']}  ({s['id']})")

        def do():
            sel = box.curselection()
            if not sel:
                return
            skin_id = removable[sel[0]]["id"]
            try:
                pet_skin_install.remove_skin(skin_id)
                if self.skin.name == skin_id:
                    self._switch_skin("dafeiyu")          # 删的是当前用的，换回内置
                self._rebuild_skin_menu()
                win.destroy()
                self._say(t("删掉了"), 2.5)
            except Exception as exc:                     # noqa: BLE001
                log_error("删除皮肤失败", exc)
                tk.Label(win, text=str(exc), fg="#c33").pack(padx=14)

        bar = tk.Frame(win)
        bar.pack(fill="x", padx=14, pady=(8, 12))
        tk.Button(bar, text=t("删除"), command=do).pack(side="right")
        tk.Button(bar, text=t("取消"), command=win.destroy).pack(side="right", padx=6)

    def _rebuild_acc_menu(self) -> None:
        self._acc_menu.delete(0, "end")
        self.var_acc = self.tk.StringVar(value=self.accessory)
        for aid in pet_accessories.accessory_ids():
            self._acc_menu.add_radiobutton(
                label=pet_accessories.accessory_label(aid), value=aid,
                variable=self.var_acc,
                command=lambda a=aid: self._switch_accessory(a))

    def _switch_accessory(self, aid: str) -> None:
        self.accessory = aid if aid in pet_accessories.ACCESSORIES else "none"
        self._save_config()

    def _switch_skin(self, skin_id: str) -> None:
        new = load_skin(skin_id)
        if new.name == self.skin.name and new.kind == self.skin.kind:
            return
        self.skin = new
        self._build_sprites()
        self._card_cache = (None, None)
        self._save_config()

    def _toggle_privacy(self) -> None:
        self.privacy = bool(self.var_privacy.get())
        self._card_cache = (None, None)
        self._save_config()

    def _toggle_sound(self) -> None:
        self.sound = bool(self.var_sound.get())
        self._save_config()

    def _toggle_companion(self) -> None:
        on = bool(self.var_companion.get())
        self.cfg["companion"] = on
        if on and self.companion is None:
            self.companion = CompanionSource()
        elif not on:
            if self.companion is not None:
                self.companion.save()
            self.companion = None
            self.in_companion = False
        self._save_config()

    # -- 设置窗口 -----------------------------------------------------------
    def _open_settings(self) -> None:
        tk = self.tk
        if getattr(self, "_settings_win", None) is not None:
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                return
            except Exception:                            # noqa: BLE001
                self._settings_win = None

        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title(t("DSH 桌宠 · 设置"))
        win.resizable(False, False)
        win.attributes("-topmost", True)
        # 这些参数是给 grid() 的 —— 左对齐要写 sticky，不是 anchor。
        # 写成 anchor 会让第一个 Checkbutton 就抛 TclError，
        # 整个设置窗口建不起来（而且异常被 Tk 吞在回调里，看着就是"点了没反应"）。
        pad = dict(padx=14, pady=6, sticky="w")

        row = 0
        tk.Label(win, text=t("行为"), font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
        row += 1

        v_comp = tk.BooleanVar(value=self.companion is not None)
        v_priv = tk.BooleanVar(value=self.privacy)
        v_sound = tk.BooleanVar(value=self.sound)
        v_auto = tk.BooleanVar(value=autostart_enabled())

        def apply_companion():
            self.var_companion.set(v_comp.get())
            self._toggle_companion()

        def apply_priv():
            self.var_privacy.set(v_priv.get())
            self._toggle_privacy()

        def apply_sound():
            self.var_sound.set(v_sound.get())
            self._toggle_sound()

        def apply_auto():
            ok = set_autostart(v_auto.get())
            if not ok:
                v_auto.set(autostart_enabled())
                status.config(text=t("设置开机自启失败（见控制台）"), fg="#B93B37")

        tk.Checkbutton(win, text=t("陪伴模式（DSH 没任务时自己过日子）"), variable=v_comp,
                       command=apply_companion).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text=t("开机自启"), variable=v_auto,
                       command=apply_auto).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text=t("隐私模式（隐藏标题与提示词）"), variable=v_priv,
                       command=apply_priv).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text=t("任务完成时提示音"), variable=v_sound,
                       command=apply_sound).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        # 语言。切换后要重建界面才生效 —— Tk 的菜单文字改不了，
        # 只能重建菜单；设置窗口自己重建一次即可。
        lang_row = tk.Frame(win)
        lang_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=6)
        tk.Label(lang_row, text=t("语言")).pack(side="left")
        v_lang = tk.StringVar(value=self.lang_pref)

        def apply_lang(*_a):
            self.lang_pref = v_lang.get()
            self.lang = pet_i18n.set_language(self.lang_pref)
            self._save_config()
            self._rebuild_menus()
            # 设置窗口自己重建（不然标签还是旧语言）
            win.destroy()
            self._settings_win = None
            self._open_settings()

        om_lang = tk.OptionMenu(lang_row, v_lang, *pet_i18n.LANGS,
                                command=apply_lang)
        om_lang.pack(side="left", padx=(6, 0))
        row += 1

        tk.Frame(win, height=1, bg="#DDD").grid(row=row, column=0, columnspan=2,
                                                sticky="ew", padx=14, pady=8)
        row += 1
        tk.Label(win, text=t("皮肤"), font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 2))
        row += 1

        v_skin = tk.StringVar(value=self.skin.name)
        labels = {s["id"]: s["display"] for s in discover_skins()}
        om = tk.OptionMenu(win, v_skin, *labels.keys(),
                           command=lambda sid: self._switch_skin(sid))
        om.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=4)
        row += 1

        tk.Frame(win, height=1, bg="#DDD").grid(row=row, column=0, columnspan=2,
                                                sticky="ew", padx=14, pady=8)
        row += 1
        tk.Label(win, text=t("本地对话（可选）"), font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 2))
        row += 1

        v_url = tk.StringVar(value=self.chat_url)
        v_model = tk.StringVar(value=self.chat_model)
        tk.Label(win, text=t("地址（Ollama 默认 http://127.0.0.1:11434）"),
                 anchor="w", fg="#888").grid(row=row, column=0, columnspan=2,
                                             sticky="w", padx=14)
        row += 1
        tk.Entry(win, textvariable=v_url, width=34).grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=14)
        row += 1
        tk.Label(win, text=t("模型名（Ollama 用 ollama list 看）"),
                 anchor="w", fg="#888").grid(row=row, column=0, columnspan=2,
                                             sticky="w", padx=14, pady=(4, 0))
        row += 1
        tk.Entry(win, textvariable=v_model, width=34).grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=14)
        row += 1

        chat_status = tk.Label(win, text="", anchor="w", fg="#666", wraplength=300,
                               justify="left")
        chat_status.grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 0))
        row += 1

        def save_chat():
            self.chat_url = v_url.get().strip()
            self.chat_model = v_model.get().strip()
            self.chat.base = pet_chat.normalize_base(self.chat_url)
            self.chat.model = self.chat_model
            self._save_config()
            if not self.chat.configured:
                chat_status.configure(text=t("填完地址和模型才能用"), fg="#B93B37")
            else:
                chat_status.configure(
                    text=t("已保存：") + f"{self.chat.model} @ {self.chat.base}", fg="#2E9E63")

        def probe():
            """探一下端点，顺便把模型名列出来。"""
            url = v_url.get().strip()
            if not url:
                chat_status.configure(text=t("先填地址"), fg="#B93B37")
                return
            chat_status.configure(text=t("正在连…"), fg="#666")
            win.update_idletasks()

            def work():
                try:
                    models = pet_chat.list_models(url)
                    if models:
                        self._tray_actions.append(
                            f"chat-models:{','.join(models[:12])}")
                    else:
                        self._tray_actions.append("chat-probe-ok:")
                except Exception as exc:                 # noqa: BLE001
                    self._tray_actions.append(f"chat-probe-fail:{exc}")

            threading.Thread(target=work, name="dsh-pet-probe", daemon=True).start()

        chatbar = tk.Frame(win)
        chatbar.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 6))
        tk.Button(chatbar, text=t("保存"), command=save_chat).pack(side="left")
        tk.Button(chatbar, text=t("测试连接"), command=probe).pack(side="left", padx=6)
        tk.Button(chatbar, text=t("和大肥鱼说话…"),
                  command=self._open_chat).pack(side="left")
        row += 1

        tk.Frame(win, height=1, bg="#DDD").grid(row=row, column=0, columnspan=2,
                                                sticky="ew", padx=14, pady=8)
        row += 1
        tk.Label(win, text=t("相处记录"), font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 2))
        row += 1

        m = self.companion.mood if self.companion else Mood()
        info = (f"陪你第 {m.days_together} 天 · {m.mood_label}\n"
                f"被戳 {m.pokes} 次 · 被拖 {m.drags} 次 · 被喂 {m.feeds} 次\n"
                f"亲密度 {m.affection * 100:.0f}% · 精力 {m.energy * 100:.0f}%\n"
                f"{m.schedule_text()}")
        tk.Label(win, text=info, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))
        row += 1

        def reset_pos():
            dx, dy = default_position()
            self.root.geometry(f"{WIN_W}x{WIN_H}+{dx}+{dy}")
            self._save_config()
            status.config(text=t("位置已复位"), fg="#2E9E63")

        def forget_me():
            if self.companion is not None:
                self.companion.mood = Mood()
                self.companion.mood.first_seen = time.time()
                self.companion.save()
            win.destroy()
            self._settings_win = None
            self._open_settings()

        btns = tk.Frame(win)
        btns.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 6))
        tk.Button(btns, text=t("位置复位"), command=reset_pos).pack(side="left")
        tk.Button(btns, text=t("重置相处记录"), command=forget_me).pack(side="left", padx=6)
        tk.Button(btns, text=t("出错日志"), command=self._show_error_log).pack(side="left")
        row += 1

        status = tk.Label(win, text="", fg="#666")
        status.grid(row=row, column=0, columnspan=2, sticky="w", padx=14)
        row += 1
        tk.Label(win, text=t("热键：Ctrl+Alt+P 显示/隐藏 · +F 喂食 · +S 设置\n滚轮缩放 · 双击开这个窗口 · 甩动/长按有惊喜"),
                 justify="left", fg="#777").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 0))
        row += 1
        tk.Button(win, text=t("关闭"), width=10, command=lambda: (
            win.destroy(), setattr(self, "_settings_win", None))).grid(
            row=row, column=0, columnspan=2, pady=(6, 14))

    # -- 托盘图标 -----------------------------------------------------------
    def _start_tray(self) -> None:
        self.tray = None
        self._tray_key = None
        self._hidden = False
        # 全局热键：和托盘共用动作队列，都在主线程消费
        self.hotkeys = None
        if not getattr(self.args, "no_hotkeys", False):
            try:
                self.hotkeys = HotKeys(sink=self._tray_actions.append)
                self.hotkeys.start()
            except Exception as exc:                     # noqa: BLE001
                log_error("全局热键注册失败", exc)
                self.hotkeys = None
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem(t("显示 / 隐藏桌宠"),
                                 lambda: self._tray_actions.append("toggle"), default=True),
                pystray.MenuItem(t("展开 / 收起详情"),
                                 lambda: self._tray_actions.append("detail")),
                pystray.MenuItem(t("隐私模式"), lambda: self._tray_actions.append("privacy"),
                                 checked=lambda _i: self.privacy),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(t("退出"), lambda: self._tray_actions.append("quit")),
            )
            self.tray = pystray.Icon("dsh_pet", self._tray_image(), t("DSH 桌宠"), menu)
            # pystray 自带消息循环，放后台线程；菜单回调只投递动作，主线程执行
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as exc:                          # noqa: BLE001
            log_error("托盘图标不可用", exc)
            self.tray = None

    def _tray_image(self) -> Image.Image:
        """托盘图标 = 当前状态的首帧缩到 64px。"""
        st = self.snap.state
        frames = self.sprites.get(st) or self.sprites.get("idle") or []
        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        if not frames:
            return canvas
        im = frames[0].copy()
        bb = im.getbbox()
        if bb:
            im = im.crop(bb)
        im.thumbnail((60, 60), Image.LANCZOS)
        canvas.alpha_composite(im, ((64 - im.width) // 2, (64 - im.height) // 2))
        return canvas

    def _tray_sync(self) -> None:
        if self.tray is None:
            return
        key = (self.snap.state, len(self.snap.tasks), self.privacy)
        if key == self._tray_key:
            return
        self._tray_key = key
        try:
            self.tray.icon = self._tray_image()
            self.tray.title = f"DSH 桌宠 · {PALETTE[self.snap.state]['label']}"
            self.tray.update_menu()
        except Exception:                                 # noqa: BLE001
            pass

    def _drain_tray(self) -> None:
        """托盘回调在别的线程，必须回到主线程操作界面。"""
        while self._tray_actions:
            act = self._tray_actions.pop(0)
            if act == "toggle":
                self._hidden = not self._hidden
                if self._hidden:
                    self.root.withdraw()
                else:
                    self.root.deiconify()
            elif act == "detail":
                self._toggle_detail()
            elif act == "privacy":
                self.var_privacy.set(not self.privacy)
                self._toggle_privacy()
            elif act == "feed":
                self._feed()
            elif act == "settings":
                self._open_settings()
            elif act == "ghost":
                self._toggle_ghost()
            elif act == "focus":
                self.var_focus.set(not self.focus_mode)
                self._toggle_focus()
            elif act == "quit":
                self._quit()
            elif isinstance(act, str) and act.startswith("update:"):
                tag = act.split(":", 1)[1]
                if self.companion is not None:
                    self.companion.say(t("有新版本 ") + tag, 6.0)
                print(f"[update] 有新版本 {tag} -> {RELEASES_URL}")
            elif isinstance(act, str) and act.startswith("skin-installed:"):
                _tag, skin_id, msg = act.split(":", 2)
                self._rebuild_skin_menu()
                self._switch_skin(skin_id)               # 装完直接用上
                self._say(t("装好了"), 2.6)
                print(f"[skin] {msg}")
            elif isinstance(act, str) and act.startswith("skin-error:"):
                self._say(t("装不上"), 2.6)
                print(f"[skin] 安装失败：{act.split(':', 1)[1]}", file=sys.stderr)
            elif isinstance(act, str) and act.startswith("chat-probe-ok:"):
                self._set_chat_status(t("连上了，但这个端点没报告任何模型"), "#C97A22")
            elif isinstance(act, str) and act.startswith("chat-models:"):
                names = act.split(":", 1)[1]
                self._set_chat_status(t("连上了。可用模型：") + names, "#2E9E63")
                # 只有一个模型且还没填，就顺手填上
                if not self.chat_model and "," not in names:
                    self.chat_model = names
                    self.chat.model = names
                    self._save_config()
            elif isinstance(act, str) and act.startswith("chat-probe-fail:"):
                self._set_chat_status(t("连不上：") + act.split(":", 1)[1], "#B93B37")
            elif isinstance(act, str) and act.startswith("chat-reply:"):
                reply = act.split(":", 1)[1]
                self._say(reply, max(5.0, min(14.0, len(reply) * 0.28)))
                w = getattr(self, "_chat_win", None)
                if w is not None:
                    try:
                        # 把"…"那行换成真回复
                        txt = [c for c in w.winfo_children()
                               if c.winfo_class() == "Text"][0]
                        txt.configure(state="normal")
                        txt.delete("end-3l", "end-1l")
                        txt.insert("end", f"她：{reply}\n\n", "her")
                        txt.see("end")
                        txt.configure(state="disabled")
                    except Exception:                    # noqa: BLE001
                        pass
            elif isinstance(act, str) and act.startswith("chat-error:"):
                self._say(t("说不出来…"), 3.0)
                w = getattr(self, "_chat_win", None)
                if w is not None:
                    try:
                        txt = [c for c in w.winfo_children()
                               if c.winfo_class() == "Text"][0]
                        txt.configure(state="normal")
                        txt.delete("end-3l", "end-1l")
                        txt.insert("end", f"（{act.split(':', 1)[1]}）\n\n", "sys")
                        txt.see("end")
                        txt.configure(state="disabled")
                    except Exception:                    # noqa: BLE001
                        pass

    # -- 番茄钟 / 专注 / 截图模式 -------------------------------------------
    def _toggle_focus(self) -> None:
        """专注模式：她安静待机，不冒泡、不溜达、不主动说话。"""
        self.focus_mode = bool(self.var_focus.get())
        if self.companion is not None and self.focus_mode:
            self.companion.bubble_until = 0.0
            self.companion.bubble = ""
        self._save_config()

    def _toggle_ghost(self) -> None:
        """截图模式：连窗口带托盘一起藏掉，录屏时完全不碍事。"""
        self._ghost = not getattr(self, "_ghost", False)
        if self._ghost:
            self.root.withdraw()
            if self.tray is not None:
                try:
                    self.tray.visible = False
                except Exception:                        # noqa: BLE001
                    pass
        else:
            self.root.deiconify()
            if self.tray is not None:
                try:
                    self.tray.visible = True
                except Exception:                        # noqa: BLE001
                    pass

    def _start_pomodoro(self, minutes: int) -> None:
        self.pomo_until = time.time() + minutes * 60
        self.pomo_total = minutes * 60
        if self.companion is not None:
            self.companion.say(f"{minutes} 分钟，开始专注", 3.0)
        self._save_config()

    def _cancel_pomodoro(self) -> None:
        self.pomo_until = 0.0
        if self.companion is not None:
            self.companion.say(t("番茄钟取消了"), 2.5)

    # -- 相处记录导出 / 更新检查 -------------------------------------------
    def _export_record(self) -> None:
        """把相处记录画成一张图，方便分享。"""
        m = self.companion.mood if self.companion else Mood()
        W, H = 420, 260
        img = Image.new("RGBA", (W, H), (26, 29, 38, 255))
        d = ImageDraw.Draw(img)
        try:
            f_big = load_font(26, bold=True)
            f_mid = load_font(15)
            f_small = load_font(13)
        except Exception:                                # noqa: BLE001
            f_big = f_mid = f_small = None
        d.rounded_rectangle([10, 10, W - 10, H - 10], radius=14,
                            outline=(70, 78, 96, 255), width=2)
        d.text((28, 26), f"陪你第 {m.days_together} 天", font=f_big, fill=(240, 243, 248, 255))
        d.text((28, 62), m.mood_label, font=f_mid, fill=(150, 200, 255, 255))
        # 亲密度条
        d.rounded_rectangle([28, 92, W - 28, 100], radius=4, fill=(52, 56, 66, 255))
        fw = max(4, int((W - 56) * max(0.0, min(1.0, m.affection))))
        d.rounded_rectangle([28, 92, 28 + fw, 100], radius=4, fill=(255, 128, 168, 255))
        d.text((28, 108), f"亲密度 {m.affection * 100:.0f}%   精力 {m.energy * 100:.0f}%",
               font=f_small, fill=(152, 160, 176, 255))
        rows = [
            ("被摸", f"{m.pokes} 次"),
            ("被拖", f"{m.drags} 次"),
            ("被喂", f"{m.feeds} 次"),
        ]
        y = 140
        for k, v in rows:
            d.text((28, y), k, font=f_small, fill=(152, 160, 176, 255))
            vw = d.textlength(v, font=f_small)
            d.text((W - 28 - vw, y), v, font=f_small, fill=(240, 243, 248, 255))
            y += 22
        d.text((28, H - 40), "DSH 桌宠", font=f_small, fill=(110, 118, 134, 255))
        out = record_path()
        try:
            img.convert("RGB").save(out)
            print(f"[export] 相处记录 -> {out}")
            if self.companion is not None:
                self.companion.say(t("记好啦"), 2.5)
        except Exception as exc:                         # noqa: BLE001
            log_error("导出相处记录失败", exc)

    # -- 本地对话 -----------------------------------------------------------
    def _set_chat_status(self, text: str, color: str = "#666") -> None:
        """把「测试连接」的结果写到设置窗口那行状态上。窗口关了就算了。"""
        w = getattr(self, "_settings_win", None)
        if w is None:
            return
        try:
            for child in w.winfo_children():
                if child.winfo_class() == "Label" and child.cget("wraplength") == 300:
                    child.configure(text=text, fg=color)
                    return
        except Exception:                                # noqa: BLE001
            pass

    def _chat_context(self) -> str:
        """给她一点「你现在在干什么」的背景，好接得上话。"""
        try:
            s = self.snap
            bits = []
            if s.state and s.state != "idle":
                bits.append(f"状态是{PALETTE.get(s.state, {}).get('label', s.state)}")
            if s.title:
                bits.append(f"在忙「{s.title[:30]}」")
            m = self.companion.mood if self.companion else None
            if m:
                bits.append(f"认识 {m.days_together} 天")
            return "，".join(bits)
        except Exception:                                # noqa: BLE001
            return ""

    def _open_chat(self) -> None:
        tk = self.tk
        if getattr(self, "_chat_win", None) is not None:
            try:
                self._chat_win.deiconify()
                self._chat_win.lift()
                return
            except Exception:                            # noqa: BLE001
                self._chat_win = None

        win = tk.Toplevel(self.root)
        self._chat_win = win
        win.title(t("和大肥鱼说话"))
        win.geometry("440x420")
        win.attributes("-topmost", True)

        log = tk.Text(win, wrap="word", state="disabled", height=14,
                      bg="#1a1d26", fg="#e8ecf2", relief="flat",
                      font=("Microsoft YaHei UI", 10))
        log.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        log.tag_configure("me", foreground="#7FB3E8", justify="right")
        log.tag_configure("her", foreground="#F2A65A")
        log.tag_configure("sys", foreground="#8a92a4")

        def put(who: str, text: str) -> None:
            log.configure(state="normal")
            tag = {"me": "me", "her": "her"}.get(who, "sys")
            prefix = {"me": t("我："), "her": t("她：")}.get(who, "")
            log.insert("end", f"{prefix}{text}\n\n", tag)
            log.see("end")
            log.configure(state="disabled")

        bar = tk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 4))
        entry = tk.Entry(bar, font=("Microsoft YaHei UI", 10))
        entry.pack(side="left", fill="x", expand=True)
        entry.focus_set()

        def send(_e=None):
            text = entry.get().strip()
            if not text:
                return
            entry.delete(0, "end")
            put("me", text)
            if not self.chat.configured:
                put("sys", t("还没配本地模型。") + "\n" + t("当前地址：")
                           + (self.chat.base or t("（空）")) + "\n"
                           + t("当前模型：") + (self.chat.model or t("（空）")) + "\n"
                           + t("右键 → 设置 → 本地对话，填一个再试。"))
                return
            put("sys", t("…"))
            win.update_idletasks()

            def work():
                try:
                    reply = self.chat.ask(text, self._chat_context())
                    self._tray_actions.append(f"chat-reply:{reply}")
                except Exception as exc:                 # noqa: BLE001
                    log_error("本地对话失败", exc)
                    self._tray_actions.append(f"chat-error:{exc}")

            threading.Thread(target=work, name="dsh-pet-chat", daemon=True).start()

        entry.bind("<Return>", send)
        tk.Button(bar, text=t("说"), width=6, command=send).pack(side="left", padx=(6, 0))

        foot = tk.Frame(win)
        foot.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(foot, text=t("清空对话"),
                  command=lambda: (self.chat.reset(), log.configure(state="normal"),
                                   log.delete("1.0", "end"),
                                   log.configure(state="disabled"))).pack(side="left")
        tk.Button(foot, text=t("关闭"), command=lambda: (
            win.destroy(), setattr(self, "_chat_win", None))).pack(side="right")

        if self.chat.configured:
            put("sys", f"{self.chat.model} @ {self.chat.base}")
        else:
            put("sys", t("还没配本地模型 —— 右键 → 设置 → 本地对话。"))

    def _show_error_log(self) -> None:
        tk = self.tk
        text = read_error_log(60)
        win = tk.Toplevel(self.root)
        win.title(t("DSH 桌宠 · 出错日志"))
        win.geometry("720x420")
        win.attributes("-topmost", True)
        body = (text if text.strip()
                else t("没有出错记录。") + f"\n\n{t('日志文件位置：')}\n{error_log_path()}")
        box = tk.Text(win, wrap="none", font=("Consolas", 9))
        box.insert("1.0", body)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        bar = tk.Frame(win)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(bar, text=t("清空日志"), command=lambda: (
            self._clear_error_log(), win.destroy())).pack(side="left")
        tk.Button(bar, text=t("关闭"), command=win.destroy).pack(side="right")

    @staticmethod
    def _clear_error_log() -> None:
        try:
            open(error_log_path(), "w").close()
        except OSError:
            pass

    def _check_update_async(self) -> None:
        """后台查一次新版本，结果塞进动作队列 —— 不阻塞界面。"""
        def work():
            tag = check_update()
            if tag:
                self._tray_actions.append(f"update:{tag}")
        try:
            threading.Thread(target=work, name="dsh-pet-update", daemon=True).start()
        except Exception:                                # noqa: BLE001
            pass

    def _attention(self) -> None:
        """卡在「等你回答」时，把她挪到屏幕中间晃一下 —— 别让她安静地举着手被忽略。

        只在状态刚变成 waiting 时挪一次，之后就待在中间，不来回跳。
        """
        now = time.time()
        if now - self._attention_at < 20.0:              # 20 秒内不重复
            return
        self._attention_at = now
        if self._drag is not None or self._drop_job is not None:
            return
        vx, vy, vw, vh = virtual_screen()
        if vw <= 0:
            return
        cx = vx + (vw - self.win_w) // 2
        cy = vy + int(vh * 0.28)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{cx}+{cy}")
        self.root.attributes("-topmost", True)
        self.root.lift()

    def _pomodoro_tick(self, now: float) -> None:
        """番茄钟计时与到点提醒。"""
        if not self.pomo_until:
            return
        if now < self.pomo_until:
            remain = int(self.pomo_until - now)
            self.snap.detail_line = f"🍅 {remain // 60:02d}:{remain % 60:02d}"
            self.snap.bubble = True                      # 用强调色，跟状态文案区分
            return
        # 到点了
        self.pomo_until = 0.0
        self._play_sound("pomo")
        if self.companion is not None:
            self.companion.say(say_line("POMO_DONE_LINES"), 6.0)

    def _play_sound(self, event: str = "done") -> None:
        """响一声。

        音效来源按优先级：
          1. <程序目录>/sounds/<event>.wav —— 用户自己放的音效包
          2. Windows 系统事件音（按事件选不同的）
          3. 静音

        同一事件 3 秒内只响一次（多个任务同时完成不该连响）。
        不同事件各有各的节流，所以"完成"和"出错"能先后响。
        """
        if not self.sound:
            return
        now = time.time()
        if now - self._last_sound.get(event, 0.0) < 3.0:
            return
        self._last_sound[event] = now
        try:
            import winsound
            # 用户音效包优先
            wav = os.path.join(data_dir(), "sounds", f"{event}.wav")
            if os.path.exists(wav):
                winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            alias = SOUND_ALIASES.get(event, SOUND_ALIASES["done"])
            winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:                                 # noqa: BLE001
            pass

    # -- 缩放 ---------------------------------------------------------------
    def _overlay_kind(self, now: float) -> str:
        """当前该画哪个交互装饰。交互优先于状态 —— 你碰她的时候先回应你。"""
        if now < self.dizzy_until:
            return "stars"
        if self.grabbed:
            return "sweat"
        if now < self.annoyed_until:
            return "angry"
        if now < self.petted_until:
            return "heart"
        if self.hover_since and now - self.hover_since > HOVER_DELAY:
            return "question"
        if self.snap.state == "sleeping":
            return "zzz"
        if self.snap.state == "celebrating":
            return "note"
        return ""

    def _apply_scale(self, new_scale: float) -> None:
        """改变缩放：重建显示层与窗口几何，以窗口中心为锚点。"""
        new_scale = max(SCALE_MIN, min(SCALE_MAX, new_scale))
        if abs(new_scale - self.scale) < 0.005:
            return
        old_w, old_h = self.win_w, self.win_h
        self.scale = new_scale
        self.win_w = max(60, int(round(WIN_W * self.scale * self.dpi)))
        self.win_h = max(80, int(round(WIN_H * self.scale * self.dpi)))
        x = self.root.winfo_x() + (old_w - self.win_w) // 2
        y = self.root.winfo_y() + (old_h - self.win_h) // 2
        self.display, self.backend = make_display(self.root, self.win_w, self.win_h)
        x, y = clamp_position(x, y)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")
        self._card_cache = (None, None)
        self._save_config()

    # -- 鼠标交互 -----------------------------------------------------------
    def _on_motion(self, e):
        """鼠标在窗口内移动：记录位置（她会看向鼠标），并开始计悬停。"""
        self._mouse = (e.x_root, e.y_root)
        self._mouse_at = time.time()
        if self.hover_since == 0.0:
            self.hover_since = time.time()

    def _on_leave(self, _e):
        self.hover_since = 0.0

    def _on_wheel(self, e):
        """滚轮缩放。Windows 上 delta 是 120 的整数倍。"""
        self._apply_scale(self.scale + (0.1 if e.delta > 0 else -0.1))

    def _on_double(self, _e):
        """双击打开设置。标记 _moved 免得 release 又当成一次「摸」。"""
        self._moved = True
        self._cancel_long_press()
        self._open_settings()

    def _cancel_long_press(self) -> None:
        if self._long_press_job is not None:
            try:
                self.root.after_cancel(self._long_press_job)
            except Exception:                            # noqa: BLE001
                pass
            self._long_press_job = None

    def _long_press_fired(self) -> None:
        """按住不动够久 —— 把她「抱起来」，她会挣扎。"""
        self._long_press_job = None
        if self._drag is None or self._moved:
            return
        self.grabbed = True
        self._say(say_line("GRAB_LINES"), 2.4)

    def _say(self, text: str, secs: float = 3.0) -> None:
        """让陪伴模式冒一句话。没开陪伴模式（或专注模式）就什么也不做。"""
        if not text or self.companion is None or self.focus_mode:
            return
        self.companion.say(text, secs)

    def _drop(self) -> None:
        """松手：她往下掉，落到屏幕底部。"""
        self.grabbed = False
        if getattr(self, "_drop_job", None) is not None:
            try:
                self.root.after_cancel(self._drop_job)
            except Exception:                            # noqa: BLE001
                pass
        floor = self.root.winfo_screenheight() - self.win_h - TASKBAR_MARGIN
        vel = 0.0

        def step():
            nonlocal vel
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if y >= floor:
                self.root.geometry(f"+{x}+{floor}")
                self._drop_job = None
                self._save_config()
                return
            vel += GRAVITY
            ny = min(floor, y + int(vel))
            self.root.geometry(f"+{x}+{ny}")
            self._drop_job = self.root.after(16, step)

        step()

    def _on_press(self, e):
        self._cancel_long_press()
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())
        self._moved = False
        self.grabbed = False
        self._shake = []
        self._press_at = time.time()
        self._long_press_job = self.root.after(LONG_PRESS_MS, self._long_press_fired)

    def _on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) > 3 or abs(dy) > 3:
            if not self._moved:
                self._cancel_long_press()                # 动了就不算长按
                if self.companion is not None:
                    self.companion.touch("drag")         # 拖动也记一笔
            self._moved = True
        # 甩动检测：记最近几次水平方向，来回换够多次就算甩
        now = time.time()
        self._shake = [(t, d) for t, d in self._shake if now - t < SHAKE_WINDOW]
        if abs(dx) > 12:
            self._shake.append((now, 1 if dx > 0 else -1))
        dirs = [d for _, d in self._shake]
        flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
        if flips >= SHAKE_DIRS:
            self._shake = []
            self.dizzy_until = now + SHAKE_DIZZY_S
            self.poke_until = 0.0
            self._say(say_line("DIZZY_LINES"), 2.6)
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_release(self, e):
        was_grab = self.grabbed
        self._cancel_long_press()
        self._drag = None
        if was_grab:
            self._drop()                                 # 抱起来后松手 → 掉下去
            return
        if not self._moved:
            now = time.time()
            # 单击 = 戳一下：播一次反应动画（皮肤没提供就只切详情卡）
            durs = self.reaction_durs.get("poke") or []
            if durs:
                self.poke_until = now + sum(durs) / 1000.0
            # 陪伴模式记一笔：亲密度和精力都会涨，睡着的话会醒
            if self.companion is not None:
                self.companion.touch("poke")
                # 连着摸太多次她会烦
                if now - self.pet_streak_at > ANNOY_WINDOW:
                    self.pet_streak = 0
                self.pet_streak_at = now
                self.pet_streak += 1
                if self.pet_streak >= ANNOY_AFTER:
                    self.annoyed_until = now + ANNOY_S
                    self.pet_streak = 0
                    self.companion.say(say_line("ANNOY_LINES"), 2.4)
                else:
                    self.petted_until = now + 1.8
                    if self.in_companion and random.random() < 0.3:
                        self.companion.say(say_line("PET_LINES"))
            self._toggle_detail()
        else:
            # 拖到屏幕边缘附近 —— 她会抓住不放
            x, y = self.root.winfo_x(), self.root.winfo_y()
            vx, vy, vw, vh = virtual_screen()
            edge = None
            if x <= vx + EDGE_SNAP:
                edge, x = "left", vx
            elif x + self.win_w >= vx + vw - EDGE_SNAP:
                edge, x = "right", vx + vw - self.win_w
            elif y <= vy + EDGE_SNAP:
                edge, y = "top", vy
            self.grab_edge = edge
            if edge is not None:
                self.root.geometry(f"+{x}+{y}")
                self._say(say_line("EDGE_LINES"), 2.4)
        self._save_config()

    def _feed(self):
        """喂一口：精力大涨，亲密度涨得最多。"""
        if self.companion is None:
            return
        self.companion.touch("feed")
        self.companion.say(say_line("FEED_LINES"), 3.5)
        durs = self.reaction_durs.get("poke") or []
        if durs:
            self.poke_until = time.time() + sum(durs) / 1000.0

    def _toggle_detail(self):
        self.detail = not self.detail
        self._card_cache = (None, None)
        self._save_config()

    def _on_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _follow_auto(self):
        if self.watcher:
            self.watcher.pinned = None

    def _copy_sid(self):
        sid = self.snap.session_id
        if not sid:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(sid)
        except Exception:                                # noqa: BLE001
            pass

    def _quit(self):
        self._save_config()
        if self.companion is not None:
            self.companion.save()                        # 记下这一程
        if getattr(self, "hotkeys", None) is not None:
            try:
                self.hotkeys.stop()
            except Exception:                            # noqa: BLE001
                pass
        try:
            self.root.destroy()
        except Exception:                                # noqa: BLE001
            pass

    # -- 循环 ---------------------------------------------------------------
    def _poll(self):
        try:
            self._drain_tray()
            if self.args.demo:
                now = time.time()
                if now - self.last_demo > 3.0:
                    self.last_demo = now
                    self.forced_state = STATE_ORDER[self.demo_i % len(STATE_ORDER)]
                    self.demo_i += 1
                self.snap = self._demo_snapshot(self.forced_state)
            elif self.args.force_state:
                self.snap = self._demo_snapshot(self.args.force_state)
            elif self.watcher:
                # 全局鼠标位置：她要看的是整个桌面上的鼠标，不只是窗口内的
                cx, cy = cursor_pos()
                if (cx, cy) != self._cursor_prev:
                    self._cursor_prev = (cx, cy)
                    self._cursor_at = time.time()
                if self.companion is not None:
                    self.companion.mouse_x = cx
                    self.companion.mouse_at = self._cursor_at
                    self.companion.pet_x = self.root.winfo_x()

                snap = self.watcher.poll()
                if self.watcher._new_completion:          # 有任务刚跑完
                    self.watcher._new_completion = False
                    self._play_sound("done")
                # 出错也响一声 —— 但不是"完成"那个音，免得听混
                if snap.state == "failed" and self._last_state != "failed":
                    self._play_sound("failed")
                self._last_state = snap.state
                # 喂作息统计：DSH 有活在跑就说明你在。她据此学到你几点在、几点不在，
                # 而不是死按 23:00~07:00 判断夜晚（夜班的人凌晨也在）。
                if self.companion is not None:
                    self.companion.observe(snap.state in ACTIVE_STATES)
                # DSH 那边没活干（空闲或压根没会话）→ 交给陪伴模式，她自己过日子。
                # 有活在跑时反过来把她叫醒，免得 DSH 都开工了她还在睡。
                if snap.state in ("idle", "offline") and self.companion is not None:
                    snap = self.companion.poll()
                    self.in_companion = True
                else:
                    self.in_companion = False
                    if self.companion is not None:
                        self.companion.wake()
                self.snap = snap
                # 番茄钟 / 专注模式 / 等待提醒
                self._pomodoro_tick(time.time())
                if self.focus_mode:
                    self.snap.bubble = False                 # 专注时不冒泡
                    if self.companion is not None:
                        self.companion.bubble_until = 0.0
                if self.snap.state == "waiting" and not self.focus_mode:
                    self._attention()                        # 卡在你这儿就走到屏幕中间
                # 溜达：把窗口朝目标位置挪一点。拖动中不抢方向盘。
                if (self.in_companion and self.snap.wander and self._drag is None
                        and getattr(self, "_drop_job", None) is None):
                    target = int(self.snap.wander)
                    wx = self.root.winfo_x()
                    if abs(wx - target) > WANDER_STEP:
                        wx += WANDER_STEP if target > wx else -WANDER_STEP
                        self.root.geometry(f"+{wx}+{self.root.winfo_y()}")
        except Exception as exc:                         # noqa: BLE001
            log_error("poll 轮询异常", exc)
        self.root.after(POLL_MS, self._poll)

    def _demo_snapshot(self, state: str) -> Snapshot:
        import random
        base = dict(
            state=state, session_id="demo-0000-0000", title="生成一个桌面宠物",
            provider="opencode-go", model="deepseek-v4.1-flash",
            turn=38, step=8, context_pct=13.6, surface_tokens=136054,
            context_window=1000000, tokens_out=70695,
            prompt="你帮我生成一个桌面宠物，宠物的状态是显示你的任务运行状态",
        )
        if state == "tools":
            base.update(started_ms=time.time() * 1000 - 3200, tool_names=["pwsh"],
                        detail_line="1 个工具调用进行中")
        elif state == "thinking":
            base.update(started_ms=time.time() * 1000 - 1400, detail_line=base["prompt"][:44])
        elif state == "idle":
            base.update(idle_hint="空闲", detail_line="生成一个桌面宠物")
        else:
            base.update(detail_line="等待你的下一条指令")
        snap = Snapshot(**base)
        snap.phase = random.random()
        return snap

    def _tick(self):
        try:
            st = self.snap.state
            self._ensure_state(st)                       # 用到才渲染（见 _ensure_state）
            frames = self.sprites.get(st) or self.sprites.get("idle") or []
            durs = self.durations.get(st) or [TICK_MS]
            if not frames:
                self.root.after(TICK_MS, self._tick)
                return

            # 逐帧时长不同（精灵图契约给了每帧毫秒），所以按时间选帧而不是按计数
            now = time.time()
            # 交互反应优先于任务状态：拖动中显示拖动姿态，刚被戳则播一次反应。
            # 反应素材皮肤没提供就自动跳过，不影响原来的动画。
            react = "drag" if self._drag else ("poke" if now < self.poke_until else None)
            if react and self.reaction_sprites.get(react):
                frames = self.reaction_sprites[react]
                durs = self.reaction_durs[react]
                loop = sum(durs) or TICK_MS
                if react == "poke":
                    # 一次性：按「剩余时间」倒推播放位置，播完自然回落到状态动画
                    t = max(0.0, min(loop - (self.poke_until - now) * 1000, loop - 1))
                else:
                    t = (now * 1000) % loop          # 拖动姿态循环播
            else:
                loop = self._loop_ms.get(st) or sum(durs) or TICK_MS
                t = (now * 1000) % loop

            acc, idx = 0.0, 0
            for i, dur in enumerate(durs):
                acc += dur
                if t < acc:
                    idx = i
                    break
            else:
                idx = len(frames) - 1
            sprite = frames[idx]
            self.snap.phase = t / loop
            self.frame_i += 1

            # 交互装饰画在所有皮肤之上 —— 契约/clawd 的帧是预渲染的，
            # 画在 render_pet 里就只有内置皮肤能用了。
            ov_kind = self._overlay_kind(now)
            if ov_kind:
                ov = overlay_for(ov_kind, self.snap.phase)
                if ov is not None:
                    sprite = Image.alpha_composite(sprite, ov)
            # 配饰同理，也画在所有皮肤之上。
            # 位置从角色**本体**的包围盒推（character_box 用 alpha 阈值把光晕
            # 和落影排除掉）—— 用 getbbox() 的话光晕会比头顶高 2~10px
            # （各皮肤不一样），配饰就全飘在头上。
            if self.accessory and self.accessory != "none":
                bb = pet_accessories.character_box(sprite)
                if bb:
                    pet_accessories.draw_accessory(
                        sprite, self.accessory,
                        cx=(bb[0] + bb[2]) / 2.0,
                        top=float(bb[1]),
                        bh=float(bb[3] - bb[1]),
                        s=1.0,                           # 帧已是最终分辨率
                        phase=self.snap.phase)
            if self.snap.mirror:                         # 看向左边：水平翻转
                sprite = ImageOps.mirror(sprite)
            dots = session_dots(self.snap.tasks)         # 多任务时脚边排一排圆点
            if dots is not None:
                sprite = Image.alpha_composite(sprite, dots)

            card_key = (st, self.snap.detail_line, self.snap.idle_hint, self.detail,
                        self.privacy,
                        self.snap.tool_names and tuple(self.snap.tool_names),
                        int((self.snap.elapsed_ms or 0) / 100),
                        tuple((t_.session_id, t_.state, t_.short_state,
                               int((t_.elapsed_ms or 0) / 1000)) for t_ in self.snap.tasks))
            if self._card_cache[0] != card_key:
                self._card_cache = (card_key,
                                    render_card(self.snap, self.detail, self.privacy))
            card = self._card_cache[1]

            canvas = Image.new("RGBA", (WIN_W, WIN_H), (0, 0, 0, 0))
            canvas.alpha_composite(sprite, (0, 0))
            canvas.alpha_composite(card, (0, PET_AREA_H))
            if self.scale != 1.0:                        # 滚轮缩放：整幅一起缩
                canvas = canvas.resize((self.win_w, self.win_h), Image.LANCZOS)
            self.display.push(canvas)
            self._tray_sync()
        except Exception as exc:                         # noqa: BLE001
            log_error("tick 渲染异常", exc)
        # 刷新间隔跟着当前皮肤的帧时长走：帧短就刷快点（whale-chan 是 17ms/帧），
        # 帧长就慢下来省 CPU（内置皮肤是 95ms）。范围夹在 33~100ms。
        try:
            step = min(self.durations.get(self.snap.state) or [TICK_MS])
        except Exception:                                # noqa: BLE001
            step = TICK_MS
        self.root.after(int(max(33, min(step, 100))), self._tick)

    def run(self):
        self.root.mainloop()


def demo_tasks() -> list:
    """用于预览与回归检查的多任务样例。"""
    now = time.time() * 1000
    return [
        TaskInfo(session_id="a1", state="tools", title="电商商品复制软件",
                 tool_names=["pwsh"], started_ms=now - 8100),
        TaskInfo(session_id="b2", state="thinking", title="介绍一下你",
                 started_ms=now - 4200),
        TaskInfo(session_id="c3", state="thinking", title="网站视觉分析",
                 started_ms=now - 900),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def do_shot(prefix: str, skin_id: str = "dafeiyu", privacy: bool = False) -> None:
    """不建窗口，把各状态渲染成 PNG，便于预览与回归检查。"""
    skin = load_skin(skin_id)
    print(f"皮肤: {skin.display}")

    def compose(st: str, snap: "Snapshot", detail: bool) -> Image.Image:
        canvas = Image.new("RGBA", (WIN_W, WIN_H), (0, 0, 0, 0))
        items = skin.frames(st)
        if items:
            canvas.alpha_composite(items[0][0], (0, 0))
        canvas.alpha_composite(render_card(snap, detail, privacy), (0, PET_AREA_H))
        return canvas

    for st in STATE_ORDER:
        snap = Snapshot(state=st, title="生成一个桌面宠物",
                        provider="opencode-go", model="deepseek-v4.1-flash",
                        turn=38, step=8, context_pct=13.6, surface_tokens=136054,
                        context_window=1000000, tokens_out=70695,
                        prompt="你帮我生成一个桌面宠物，宠物的状态是显示你的任务运行状态")
        if st == "tools":
            snap.tool_names = ["pwsh"]
            snap.detail_line = "1 个工具调用进行中"
            snap.started_ms = time.time() * 1000 - 3200
        elif st == "thinking":
            snap.detail_line = snap.prompt[:44]
            snap.started_ms = time.time() * 1000 - 1400
        elif st == "idle":
            snap.idle_hint, snap.detail_line = "空闲", "生成一个桌面宠物"
        elif st == "celebrating":
            snap.detail_line = "任务已完成"
            snap.started_ms = time.time() * 1000 - 2000
        elif st == "waiting":
            snap.detail_line = "在等你回答"
            snap.started_ms = time.time() * 1000 - 5100
        elif st == "failed":
            snap.detail_line = "请求出错，正在重试"
            snap.started_ms = time.time() * 1000 - 2400
        else:
            snap.detail_line = "等待你的下一条指令"
        compose(st, snap, False).save(f"{prefix}-{st}.png")
        compose(st, snap, True).save(f"{prefix}-{st}-detail.png")
        print(f"  {st:12s} -> {prefix}-{st}.png")

    multi = Snapshot(state="tools", tasks=demo_tasks(), title="电商商品复制软件",
                     provider="opencode-go", model="deepseek-v4.1-flash",
                     turn=161, step=4, context_pct=13.6, surface_tokens=136054,
                     context_window=1000000, tokens_out=70695, tool_names=["pwsh"],
                     started_ms=time.time() * 1000 - 8100)
    compose("tools", multi, False).save(f"{prefix}-multi.png")
    compose("tools", multi, True).save(f"{prefix}-multi-detail.png")
    print(f"  multi        -> {prefix}-multi.png  ({len(multi.tasks)} 个任务)")


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH 桌面宠物")
    ap.add_argument("--demo", action="store_true", help="每 3 秒轮换状态预览")
    ap.add_argument("--force-state", choices=STATE_ORDER, help="固定显示某个状态")
    ap.add_argument("--session", help="固定监视指定会话 ID")
    ap.add_argument("--shot", metavar="PREFIX", help="渲染各状态到 PNG 后退出")
    ap.add_argument("--skin", default=None, help="指定皮肤 id（默认用配置里的）")
    ap.add_argument("--list-skins", action="store_true", help="列出可用皮肤后退出")
    ap.add_argument("--privacy", action="store_true", help="隐私模式渲染")
    ap.add_argument("--no-companion", action="store_true",
                    help="关掉陪伴模式（DSH 没任务时就显示空闲，不自己过日子）")
    ap.add_argument("--no-hotkeys", action="store_true",
                    help="不注册全局热键（Ctrl+Alt+P 显示隐藏 / +F 喂食 / +S 设置）")
    args = ap.parse_args()

    # 尽早装上 —— 之后任何未捕获异常都会落盘，而不是消失在 exe 的 stderr 黑洞里
    install_excepthook()
    # 必须在建任何窗口之前声明 DPI 感知，否则 Windows 会先按老的缩放方式
    # 把窗口位图拉伸，4K 屏上整只桌宠和字都是糊的。
    set_dpi_awareness()

    if args.list_skins:
        for s in discover_skins():
            print(f"  {s['id']:<16} {s['display']:<24} {s['kind']}")
        return 0

    if args.shot:
        do_shot(args.shot, args.skin or "dafeiyu", args.privacy)
        return 0

    if sys.platform != "win32":
        print("本程序仅支持 Windows（依赖分层窗口）。", file=sys.stderr)
        return 1

    # 单实例锁：绑一个本地端口，进程活着就占着，退出时系统自动释放。
    # 比 PID 文件干净 —— 崩溃也不会留下需要手动清的残留。
    # 插件、快捷方式、手动启动都走这里，所以不会叠出第二只桌宠。
    #
    # 注意：进程收到终止信号后，端口大约还要 2 秒才真正释放。这期间起来的
    # 新实例会 bind 失败 —— 所以这里必须【报错并非零退出】，不能静默 return 0，
    # 否则调用方（DSH 插件）分不清「已经在跑」和「起来又死了」。
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        lock.listen(1)
    except OSError as exc:
        print(f"桌宠启动失败：单实例锁端口 {SINGLE_INSTANCE_PORT} 被占用"
              f"（上一只可能还在退出中，等两秒再试）。{exc}", file=sys.stderr)
        return 2

    PetApp(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
