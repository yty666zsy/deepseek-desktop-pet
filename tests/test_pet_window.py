# -*- coding: utf-8 -*-
"""所有窗口的「—」隐藏键（v6.90）

需求：「给所有的窗口都加一个隐藏的按钮放在关闭按钮旁边，跟浏览器一样」。
这里锁三件事：
1. `add_hide_button()` 真的把标题栏该有的位配齐了（最小化 + 系统菜单 + 关闭 + 标题），
   并且把 `Qt.Tool` 摘掉 —— **实测 Tool 窗口系统根本不画最小化键**，不摘等于没加
2. `present()` 能把**已隐藏（最小化）**的窗口还原到前台（否则点入口看着像没反应）
3. **源码护栏**：应用里每个 `QDialog` 窗口类的 `__init__` 都必须挂上「—」，
   以后新增窗口忘挂会直接红 —— 不然"所有窗口"迟早退化成"大部分窗口"

运行：python -m pytest tests/test_pet_window.py -q
"""
import ast
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

# 有窗口的应用模块（桌宠本体 desktop_pet.py 的动态对话框单独用源码计数锁）
WINDOW_MODULES = ('settings_ui.py', 'model_manager_ui.py', 'pet_music_ui.py',
                  'pet_download_ui.py', 'pet_convert_ui.py', 'pet_photos_ui.py',
                  'affection_ui.py', 'pet_minigames.py', 'pet_ledger_ui.py')


@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------
# 1. 标志位
# --------------------------------------------------------------------------
def test_adds_minimize_and_sysmenu(qapp):
    """「—」要能用，光有 WindowMinimizeButtonHint 不够（Windows 会画成灰的/禁用）"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    import pet_window as pwin

    d = QDialog()
    pwin.add_hide_button(d)
    f = d.windowFlags()
    assert f & Qt.WindowMinimizeButtonHint, '没挂最小化键'
    assert f & Qt.WindowSystemMenuHint, '缺系统菜单位 → 最小化键会画成禁用态（实测）'
    assert f & Qt.WindowCloseButtonHint and f & Qt.WindowTitleHint
    assert not (f & Qt.WindowMaximizeButtonHint), '面板都是定宽布局，别给最大化'
    # 值一条断言：PySide6 里 `~Qt.WindowMaximizeButtonHint` 求补只在枚举已定义位上做，
    # 会把 Close 位（0x8000000）顺手擦掉 —— 表现就是「✕ 没了」
    assert int(f) & int(Qt.WindowCloseButtonHint), '✕ 被 ~枚举 的坑擦掉了'
    d.close()


def test_drops_tool_hint(qapp):
    """Qt.Tool 的窗口系统不画最小化键 —— 必须换成普通窗口"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    import pet_window as pwin

    d = QDialog()
    d.setWindowFlag(Qt.Tool, True)
    # 注意：Qt.Tool == 0xb 里含 Window|Dialog 的位，直接 `f & Qt.Tool` 恒真 ——
    # 必须先把类型位剥出来再比（这是本文件第一版测试自己踩的坑）
    assert (int(d.windowFlags()) & int(Qt.WindowType_Mask)) == int(Qt.Tool)
    pwin.add_hide_button(d)
    f = int(d.windowFlags())
    assert (f & int(Qt.WindowType_Mask)) == int(Qt.Dialog), 'Tool 标志没摘掉，最小化键长不出来'
    assert f & Qt.WindowMinimizeButtonHint
    d.close()


def test_keeps_stays_on_top_if_any(qapp):
    """别的窗口标志不能被顺手擦掉（比如置顶）"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    import pet_window as pwin

    d = QDialog()
    d.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    pwin.add_hide_button(d)
    assert d.windowFlags() & Qt.WindowStaysOnTopHint
    d.close()


# --------------------------------------------------------------------------
# 2. 隐藏 / 还原
# --------------------------------------------------------------------------
def test_hide_then_present(qapp):
    """按「—」= 隐藏（最小化，状态全留）；再从入口回来 = 还原到前台"""
    from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout
    import pet_window as pwin

    d = QDialog()
    pwin.add_hide_button(d)
    ed = QLabel('窗口里的内容')
    QVBoxLayout(d).addWidget(ed)
    d.resize(300, 160)
    d.show()
    qapp.processEvents()
    assert d.isVisible() and not pwin.is_hidden(d)

    pwin.hide_window(d)
    qapp.processEvents()
    assert pwin.is_hidden(d), 'showMinimized() 之后应处于隐藏/最小化态'
    # 不断言 isVisible()：offscreen 后端照样报 True（真机上才是 False），这里只锁状态位
    assert d.layout().itemAt(0).widget() is ed, '隐藏不该销毁里面的东西'

    pwin.present(d)
    qapp.processEvents()
    assert not pwin.is_hidden(d), 'present() 要把最小化的窗口还原'
    assert d.isVisible()
    d.close()


def test_present_none_and_dead(qapp):
    """present() 对 None / 已销毁窗口要闭嘴，不能把宿主搞崩"""
    import pet_window as pwin

    assert pwin.present(None) is None
    assert pwin.is_hidden(object()) is False        # 不是窗口 → 老实返回 False


# --------------------------------------------------------------------------
# 3. 源码护栏：每个窗口类都得挂「—」
# --------------------------------------------------------------------------
def _has_hide_call(func):
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('add_hide_button',)):
            return True
    return False


def test_every_dialog_class_has_hide_button():
    missing = []
    for name in WINDOW_MODULES:
        path = os.path.join(BASE, name)
        tree = ast.parse(open(path, encoding='utf-8-sig').read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if 'QDialog' not in bases:
                continue            # QWidget 子类（自绘卡片/画布）不在此列
            init = next((n for n in node.body
                         if isinstance(n, ast.FunctionDef) and n.name == '__init__'), None)
            if init is None:
                continue            # 没有自己的 __init__ → 继承父类（如 BaseGame 的子类）
            if not _has_hide_call(init):
                missing.append('%s:%s' % (name, node.name))
    assert not missing, '这些窗口没挂「—」隐藏键：%s' % ', '.join(missing)


def test_no_tool_window_left():
    """面板不该再挂 Qt.Tool —— 挂了系统就不画最小化键（实测）"""
    bad = []
    for name in WINDOW_MODULES:
        src = open(os.path.join(BASE, name), encoding='utf-8-sig').read()
        for i, line in enumerate(src.splitlines(), 1):
            if 'setWindowFlag(Qt.Tool' in line.replace(' ', ''):
                bad.append('%s:%d' % (name, i))
    assert not bad, '这些地方还留着 Qt.Tool：%s' % ', '.join(bad)


def test_desktop_pet_dynamic_dialogs_have_hide_button():
    """宿主里那几个临时造的 QDialog（待办/导出/记忆/提醒/人物大小）也都要有"""
    src = open(os.path.join(BASE, 'desktop_pet.py'), encoding='utf-8-sig').read()
    made = src.count('QDialog(self)')
    hooked = src.count('pwin.add_hide_button(dlg)')
    assert made == 5, '宿主动态对话框数量变了（%d），检查是否新加了没挂「—」的窗口' % made
    assert hooked >= made, '有 %d 个动态对话框没挂「—」（挂了 %d 个）' % (made, hooked)


def test_reopen_paths_use_present():
    """常驻面板的入口必须走 present()，否则隐藏后点入口像没反应"""
    for name, needle in (('pet_music_ui.py', 'pwin.present(self._panel)'),
                         ('pet_download_ui.py', 'pwin.present(self._panel)'),
                         ('pet_convert_ui.py', 'pwin.present(self._panel)'),
                         ('pet_photos_ui.py', 'pwin.present(self._panel)'),
                         ('pet_ledger_ui.py', 'pwin.present(self.panel(parent))')):
        src = open(os.path.join(BASE, name), encoding='utf-8-sig').read()
        assert needle in src, '%s 的打开路径没走 present()' % name
