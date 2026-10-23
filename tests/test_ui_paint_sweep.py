# -*- coding: utf-8 -*-
"""v6.58 新增：UI「真绘制」扫荡测试（防"能构造但画不出来"这类 bug）

背景：小游戏界面曾因 `QPainter` 等绘图类**被误删的 import**，导致 `paintEvent` 抛
NameError —— 对话框能打开、样式表也有色，但**画面全白**（使用者实测「游戏界面没了」）。
当时所有测试只断言"能构造 / 样式表有色"，抓不到绘制期错误。

本测试把每个 UI 面都 **show + processEvents + grab**（真正触发 paintEvent），
并捕获 excepthook / stderr 里的异常 —— 任何绘制期错误都会让它失败。

运行：python -m pytest tests/test_ui_paint_sweep.py -q
"""
import contextlib
import io
import os
import sys
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def _paint(w):
    """show → 触发绘制 → grab 同步再绘一次；返回 stderr 文本"""
    app = _app()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        w.show()
        w.resize(360, 480)
        app.processEvents()
        pm = w.grab()
        app.processEvents()
    assert not pm.isNull(), '%s grab 返回空图（没画出来）' % type(w).__name__
    w.close()
    return buf.getvalue()


def _run(items):
    """items: [(说明, 构造并返回控件)] —— 逐个绘制，收集异常"""
    app = _app()
    errs = []
    old_hook = sys.excepthook

    def _hook(*a):
        errs.append('excepthook: ' + ''.join(traceback.format_exception(*a))[-400:])

    sys.excepthook = _hook
    try:
        for label, make in items:
            try:
                text = _paint(make())
            except Exception as e:
                errs.append('%s → %s: %s' % (label, type(e).__name__, e))
                continue
            if 'Traceback' in text or 'Error calling Python override' in text:
                errs.append('%s → 绘制期报错: %s' % (label, text.strip().splitlines()[-1][:160]))
        app.processEvents()
    finally:
        sys.excepthook = old_hook
    return errs


def test_minigames_paint():
    import pet_minigames as mg
    errs = _run([(name, (lambda c=cls: c(lambda *a, **k: None))) for name, cls in mg.GAMES.items()]
                + [('小游戏列表页', lambda: mg.GameWindow(lambda *a, **k: None))])
    assert not errs, f'小游戏绘制异常：{errs[:4]}'


def test_dialogs_paint():
    import desktop_pet as dp
    import settings_ui as su
    import affection_ui as au
    pet = dp.PetWidget()
    pet._save_cfg_value = lambda *a, **k: True
    items = [
        ('主窗口', lambda: pet),
        ('设置窗', lambda: su.SettingsDialog(pet, getattr(pet, 'registry', None))),
        ('关系面板', lambda: au.RelationDialog(pet.affection, 'flash', '测试角色')),
        ('回忆相册', lambda: au.MemoriesDialog(pet.memories, 'flash', '测试角色')),
    ]
    try:
        from model_registry import ModelRegistry
        import model_manager_ui as mmu
        reg = ModelRegistry(os.path.join(BASE, 'models.json'))
        items.append(('模型管理窗', lambda: mmu.ModelManagerDialog(reg, pet)))
    except Exception:
        pass
    errs = _run(items)
    assert not errs, f'对话框绘制异常：{errs[:4]}'


def test_cards_paint():
    import chat_cards as cc
    errs = _run([
        ('代码卡', lambda: cc.CodeCard('print(1)')),
        ('表格卡', lambda: cc.TableCard('|a|\n|-|\n|1|', '<table><tr><td>1</td></tr></table>')),
    ])
    assert not errs, f'卡片绘制异常：{errs[:4]}'


def test_photo_dialogs_paint():
    """v6.88 见面照片墙：空墙与有照片两种状态都要真画出来"""
    import tempfile
    from PIL import Image
    import pet_photos as ph
    import pet_photos_ui as ui

    base = tempfile.mkdtemp()
    srcs = []
    for i in range(2):
        src = os.path.join(base, 'p%d.jpg' % i)
        Image.new('RGB', (640 + 120 * i, 480), (40 * i, 120, 190)).save(src)
        srcs.append(src)
    lib = ph.PhotoLibrary(base_dir=base)
    lib.import_paths(srcs)
    mid = lib.meetings()[0]['id'] if lib.meetings() else ''
    ctrl = ui.PhotoController(pet=None, base_dir=os.path.join(base, 'empty'))
    errs = _run([
        ('墙面-有照片', lambda: ui.WallPanel(_ctrl_for(base))),
        ('墙面-空', lambda: ui.WallPanel(ctrl)),
        ('单次见面', lambda: ui.MeetingDialog(lib, mid)),
        ('灯箱', lambda: ui.PhotoLightbox(ph.meeting_photos(lib.get(mid)), 0, '测试')),
        ('见面卡片', lambda: ui.PhotoCard(_ctrl_for(base))),
        ('下次见面', lambda: ui.NextMeetingDialog(lib)),
    ])
    assert not errs, f'照片墙绘制异常：{errs[:4]}'


def _ctrl_for(base):
    import pet_photos_ui as ui
    return ui.PhotoController(pet=None, base_dir=base)


def test_ledger_dialogs_paint():
    """v6.89 记账：空账本与有账目两种状态、三个窗口都要真画出来"""
    import tempfile
    import pet_ledger as lg
    import pet_ledger_ui as ui

    base = tempfile.mkdtemp()
    empty = lg.LedgerBook(os.path.join(base, 'empty.json'))
    book = lg.LedgerBook(os.path.join(base, 'ledger.json'))
    for t in ('午饭 32', '打车 18.5 微信', '房租 3200', '发工资 12000', '收到红包 200'):
        r = lg.parse_entry(t)
        if r['ok']:
            book.add(r)
    book.set_budget('餐饮', 20)          # 故意设小，让预算条出现超支红条

    errs = _run([
        ('面板-有账目', lambda: ui.LedgerPanel(ui.LedgerController(book=book))),
        ('面板-空账本', lambda: ui.LedgerPanel(ui.LedgerController(book=empty))),
        ('记一笔', lambda: ui.EntryDialog(book, lg.parse_entry('昨天晚饭 85 微信'))),
        ('预算', lambda: ui.BudgetDialog(book, lg.today_str()[:7])),
    ])
    assert not errs, f'记账界面绘制异常：{errs[:4]}'

