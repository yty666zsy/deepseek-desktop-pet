# -*- coding: utf-8 -*-
"""pet_window.py — 所有窗口的「隐藏」按钮（v6.90）

需求原话：「给所有的窗口都加一个隐藏的按钮放在关闭按钮旁边，跟浏览器一样」。
也就是每个窗口的标题栏在「✕」左边多一个「—」，按下去**隐藏（最小化）**而不是关掉——
面板状态全留着，从任务栏/桌宠入口回来还是原样。

为什么不自己画标题栏
--------------------
自绘标题栏意味着 20 多个窗口都要重做边框、拖动、缩放、多屏、暗色主题、DPI 缩放，
回归面太大；而「—」本来就是系统按钮，位置/外观/DPI/高对比度主题全由 Windows 管好，
**加一个标志位就长得跟浏览器一模一样**。所以这里只做一件事：把标志位配齐。

实测踩到的两个 Win32 坑（都在真实平台上验过，别再踩一遍）
--------------------------------------------------------
1) **只给 `Qt.WindowMinimizeButtonHint` 不够**：Windows 只有在 **WS_SYSMENU** 也在场时
   才把最小化键画成「可用」状态，否则会画出一个小灰点（禁用态，看着像坏了）。
   → 必须同时给 `Qt.WindowSystemMenuHint`。
2) **`Qt.Tool` 的窗口（WS_EX_TOOLWINDOW）系统根本不画最小化键**，标志加了也没用（实测）。
   → 音乐 / 下载 / 转换 / 照片墙这些面板原来是 `Qt.Tool`，得换成普通窗口；
   代价是它们会进任务栏，而这正好就是「跟浏览器一样」：最小化后点任务栏就回来，
   桌宠自己的入口（标题栏按钮 / 右键菜单）也会把它还原（见 `present()`）。
3) **别拿 `~Qt.WindowXxxHint` 做 PySide6 的位运算**：`Qt.WindowType` 是 flag 枚举，
   `~Qt.WindowMaximizeButtonHint` 只在**枚举已定义的位**上求补（得 `0x1ff7fff`），
   会把 `0x8000000`（`WindowCloseButtonHint`）顺手一起擦掉 —— 表现就是「✕ 没了」。
   → 本模块全程用 `int()` 做位运算，最后 `Qt.WindowType(f)` 包回去再交给
   `setWindowFlags()`（它只收 `Qt.WindowType`，直接传 int 会 TypeError）。

顺带一个视觉事实：不加最大化时 Windows 仍会在「—」和「✕」之间画一个**灰掉的 □ 占位**，
这是系统画标题栏的固定行为（浏览器就是 — □ ✕ 三个键），不是我们画坏的。
"""
from PySide6.QtCore import Qt

# 标题栏需要的那几位：最小化（要的那颗「—」）+ 系统菜单（让「—」从灰变可用）+ 关闭 + 标题
# 注意：因为是运算符在枚举上拼的，这里统一转成 int 存着（见模块头部第 3 条坑）
HIDE_TITLE_BITS = int(Qt.WindowMinimizeButtonHint | Qt.WindowSystemMenuHint
                      | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
_TYPE_MASK = int(Qt.WindowType_Mask)      # 0xff：窗口类型（Window/Dialog/Tool…）占的低 8 位
_MAXIMIZE_BIT = int(Qt.WindowMaximizeButtonHint)


def add_hide_button(win, drop_tool=True, allow_maximize=False):
    """给窗口标题栏加一个原生「—」隐藏键（位置就在 ✕ 左边）。

    **必须在 show() 之前调用**：`setWindowFlags()` 会重建原生窗口句柄，
    对着已经显示的窗口改会闪一下、丢焦点，还会把模态状态弄乱。

    drop_tool=True：把 `Qt.Tool` 换成普通对话框（否则系统不画最小化键）。
    allow_maximize：这些面板都是定宽布局，最大化只会更难看，默认不给。
    """
    try:
        f = int(win.windowFlags())
        if drop_tool and (f & _TYPE_MASK) == int(Qt.Tool):
            f = (f & ~_TYPE_MASK) | int(Qt.Dialog)     # Tool → 普通窗口
        f |= HIDE_TITLE_BITS
        if not allow_maximize:
            f &= ~_MAXIMIZE_BIT
        win.setWindowFlags(Qt.WindowType(f))
    except Exception:
        pass
    return win


def hide_window(win):
    """隐藏到任务栏（＝系统最小化）：不关窗口，里面的状态一个字都不丢"""
    try:
        win.showMinimized()
    except Exception:
        pass
    return win


def is_hidden(win):
    """窗口现在是"隐藏/最小化"着的吗"""
    try:
        return bool(win.isMinimized() or (win.windowState() & Qt.WindowMinimized))
    except Exception:
        return False


def present(win):
    """把窗口端到最前面；**最小化着就先还原**。

    宿主的所有入口（标题栏 🎵/⬇/⇄/📷/¥、右键菜单、侧栏页签）都走这里 ——
    不然窗口藏着的时候点入口只会 `show()` 一下，看起来像"没反应"（其实是还在任务栏里）。
    """
    if win is None:
        return win
    try:
        if is_hidden(win):
            win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        win.show()
        win.raise_()
        win.activateWindow()
    except RuntimeError:
        pass            # 底层 C++ 对象已经销毁
    except Exception:
        pass
    return win
