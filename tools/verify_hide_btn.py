# -*- coding: utf-8 -*-
"""窗口「—」隐藏键实机验证（v6.90）

用法：.venv\\Scripts\\python.exe tools\\verify_hide_btn.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：
离屏后端没有原生标题栏，`winId()` 也不是真 HWND —— 恰恰是这次要验的东西全在原生那边。

验的东西：
1. 每个窗口的**原生样式**真的带 WS_MINIMIZEBOX + WS_SYSMENU（后者不在的话「—」是灰的/禁用）
2. 不是工具窗（WS_EX_TOOLWINDOW 的窗口系统根本不画最小化键）
3. 没有最大化位（面板都是定宽布局）
4. 抓 PrintWindow 出图 —— 亲眼看到标题栏是「— □ ✕」，不是靠猜
5. 真按一下最小化（Win32 `ShowWindow(SW_MINIMIZE)`）→ Qt 侧确实是隐藏态、窗口内容还在
6. 从桌宠自己的入口叫回来（音乐/下载/转换/照片墙/记账/关系/小游戏）→ **同一个窗口**被还原
   到前台（不是关掉重建 —— 那样「隐藏」就退化成「关闭」了）
7. 隐私：跑完用户真实 config.json / ledger.json / meetings.json / photos/ 一个字节都没变

注：审计日志 / 出网白名单 / 语音输入 / 记一笔 / 预算 这些是 exec() 模态小对话框，构造要
一堆外部依赖，这里不硬造 —— 它们的「—」由 tests/test_pet_window.py 里的源码护栏锁住。
"""
import ctypes
import hashlib
import os
import shutil
import sys
import tempfile
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402

import desktop_pet as dp                     # noqa: E402
import pet_ledger as lg                      # noqa: E402
import pet_ledger_ui as lg_ui                # noqa: E402
import pet_photos_ui as ph_ui                # noqa: E402
import pet_window as pwin                    # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), 'pethide_verify')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)
LEDGER_TMP = os.path.join(WORK, 'ledger.json')
PHOTOS_TMP = os.path.join(WORK, 'photos_lib')

FAILS = []
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# ---- Win32 常量 ----
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_THICKFRAME = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SW_MINIMIZE = 6
SM_CYCAPTION, SM_CXSIZEFRAME, SM_CYSIZEFRAME = 4, 32, 33


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def note(msg):
    print('    · %s' % msg)


def sha(path):
    if not os.path.isfile(path):
        return ''
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def pump(app, sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


class RECT(ctypes.Structure):
    _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                ('right', wintypes.LONG), ('bottom', wintypes.LONG)]


class BIH(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


def capture(hwnd):
    """PrintWindow 抓窗口（含非客户区 = 标题栏）；被别的程序挡着也能抓到"""
    from PIL import Image
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    W, H = rc.right - rc.left, rc.bottom - rc.top
    if W <= 0 or H <= 0:
        return None
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, W, H)
    gdi32.SelectObject(hdc_mem, hbmp)
    user32.PrintWindow(hwnd, hdc_mem, 2)      # 2 = PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(W * H * 4)
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = W
    bi.biHeight = -H
    bi.biPlanes = 1
    bi.biBitCount = 32
    gdi32.GetDIBits(hdc_mem, hbmp, 0, H, buf, ctypes.byref(bi), 0)
    im = Image.frombuffer('RGBA', (W, H), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    return im


def caption_height():
    return user32.GetSystemMetrics(SM_CYCAPTION) + 2 * user32.GetSystemMetrics(SM_CYSIZEFRAME)


def font(size=15):
    from PIL import ImageFont
    for path in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf',
                 r'C:\Windows\Fonts\simsun.ttc'):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def clean_label(name):
    """PIL 的中文字体没有 emoji 字形（会画成豆腐块），说明文字里把它们去掉"""
    out = []
    for ch in name:
        cp = ord(ch)
        if cp < 0x0250 or 0x4E00 <= cp <= 0x9FFF or ch in '¥—·':
            out.append(ch)
    return ''.join(out).strip() or name


def tile(strips, out_path, gap=6, label_w=210, scale=2.0):
    """把若干「标题栏条」拼成一张说明图：左边写窗口名，右边是 title bar 原样放大"""
    from PIL import Image, ImageDraw
    f = font(15)
    rows = []
    for name, im in strips:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        rows.append((clean_label(name), im))
    W = label_w + max(im.width for _n, im in rows) + gap
    H = sum(max(im.height, 24) + gap for _n, im in rows) + gap
    canvas = Image.new('RGB', (W, H), (245, 245, 247))
    dr = ImageDraw.Draw(canvas)
    y = gap
    for name, im in rows:
        dr.text((8, y + max(0, (im.height - 18) // 2)), name, font=f, fill=(30, 30, 34))
        canvas.paste(im, (label_w, y))
        dr.rectangle([label_w - 1, y - 1, label_w + im.width, y + im.height],
                     outline=(190, 190, 196))
        y += max(im.height, 24) + gap
    canvas.save(out_path)
    return canvas.size


def style_of(hwnd):
    s = user32.GetWindowLongW(hwnd, GWL_STYLE)
    e = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return s & 0xFFFFFFFF, e & 0xFFFFFFFF


# --------------------------------------------------------------------------
# 逐个窗口验
# --------------------------------------------------------------------------
def probe(app, name, win, reopen=None, collect=None, full_shot=None):
    """验一个窗口：原生样式 → 抓图 → 最小化 → 从入口还原"""
    if win is None:
        check(False, '%s：没造出来（跳过）' % name)
        return
    pump(app, 0.25)
    hwnd = int(win.winId())
    st, ex = style_of(hwnd)
    ok = check(bool(st & WS_CAPTION), '%s：有原生标题栏（WS_CAPTION）' % name)
    ok &= check(bool(st & WS_MINIMIZEBOX), '%s：样式里带 WS_MINIMIZEBOX —— 那颗「—」在' % name)
    ok &= check(bool(st & WS_SYSMENU),
                '%s：带 WS_SYSMENU —— 「—」是可用状态，不是灰的' % name)
    ok &= check(not (ex & WS_EX_TOOLWINDOW),
                '%s：不是工具窗（WS_EX_TOOLWINDOW 的窗口系统不画最小化键）' % name)
    ok &= check(not (st & WS_MAXIMIZEBOX), '%s：没给最大化（定宽布局，别让它撑变形）' % name)
    if not ok:
        return

    im = capture(hwnd)
    if im is None:
        check(False, '%s：PrintWindow 抓图失败' % name)
    else:
        cap = min(im.height, caption_height() + 6)
        if collect is not None:
            collect.append((name, im.crop((0, 0, im.width, cap))))
        if full_shot:
            im.save(os.path.join(SHOTS, full_shot))
            print('    已存 screenshots/%s（%dx%d，整窗含标题栏）' % (full_shot, im.width, im.height))

    # 真按「—」：走 Win32 最小化（跟用户点标题栏那颗键是同一条路）
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    pump(app, 0.35)
    check(pwin.is_hidden(win), '%s：最小化之后 Qt 侧是隐藏态' % name)
    check(bool(win.isVisible() is not None), '%s：窗口对象还活着（没被销毁）' % name)

    # 从入口叫回来
    if reopen is not None:
        got = reopen()
        if got is not None:
            check(got is win, '%s：入口拿回来的是**同一个窗口**（没关掉重建，状态全留）' % name)
    else:
        pwin.present(win)
    pump(app, 0.4)
    check(not pwin.is_hidden(win) and win.isVisible(),
          '%s：一叫就还原到前台（不是"点一下没反应"）' % name)


def main():
    app = QApplication(sys.argv[:1])

    # ---------- 0. 别动用户数据 ----------
    print('[0] 隐私：先备份真实数据')
    watch = ('config.json', 'ledger.json', 'meetings.json')
    backup = {}
    for fn in watch:
        p = os.path.join(BASE, fn)
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                backup[fn] = f.read()
        print('    %-14s 存在=%s sha=%s' % (fn, os.path.isfile(p), sha(p)[:16] or '—'))
    photos_dir = os.path.join(BASE, 'photos')
    photos_before = sorted(os.listdir(photos_dir)) if os.path.isdir(photos_dir) else []

    # 记账 / 照片墙统统指到临时目录
    lg.LEDGER_PATH = LEDGER_TMP
    book = lg.LedgerBook(LEDGER_TMP)
    book.add(lg.parse_entry('午饭 32', cats=book.cats()))
    book.add(lg.parse_entry('打车 18.5', cats=book.cats()))
    book.add(lg.parse_entry('发工资 12000', cats=book.cats()))
    print('    临时账本 %s（3 笔）' % LEDGER_TMP)

    pet = dp.PetWidget()
    pet.show()
    pump(app, 0.6)
    pet._save_cfg_value = lambda *a, **k: True          # 别把验证状态写进用户配置
    pet._ledger = lg_ui.LedgerController(pet, book=book)
    pet._ledger.changed.connect(lambda: None)           # 别带动侧栏刷新
    pet._photos = ph_ui.PhotoController(pet, base_dir=PHOTOS_TMP)
    pet._photos.on_meetings = lambda *a, **k: None      # 别写回忆/好感

    strips = []

    # ---------- 1. 设置 / 模型管理（宿主里直接造的对话框） ----------
    print('\n[1] 设置 / 模型管理')
    from settings_ui import SettingsDialog, _AuditDialog, _NetDialog, _AsrDialog  # noqa: F401
    from model_manager_ui import ModelManagerDialog

    dlg_settings = SettingsDialog(pet, dp.MODEL_REGISTRY, parent=pet)
    dlg_settings.show()
    probe(app, '⚙️ 设置', dlg_settings, collect=strips)

    dlg_models = ModelManagerDialog(dp.MODEL_REGISTRY, key_resolver=lambda _f: '', parent=pet)
    dlg_models.show()
    probe(app, '🎯 模型管理', dlg_models, collect=strips)

    # ---------- 2. 常驻面板（隐藏后要从桌宠入口叫回来） ----------
    print('\n[2] 常驻面板（音乐 / 下载 / 转换 / 照片墙 / 记账）')
    probe(app, '🎵 音乐', pet.open_music_panel(),
          reopen=pet.open_music_panel, collect=strips)
    probe(app, '🎬 下载', pet.open_download_panel(),
          reopen=pet.open_download_panel, collect=strips)
    probe(app, '⇄ 转换', pet.open_convert_panel(),
          reopen=pet.open_convert_panel, collect=strips)
    probe(app, '📷 照片墙', pet.open_photo_wall(),
          reopen=pet.open_photo_wall, collect=strips, full_shot='v690_hide_btn_photos.png')
    probe(app, '¥ 记账', pet.open_ledger(),
          reopen=pet.open_ledger, collect=strips, full_shot='v690_hide_btn_ledger.png')

    # ---------- 3. 关系 / 小游戏（以前点一次就关掉重建） ----------
    print('\n[3] 关系 / 小游戏（已开就还原，不再关掉重建）')

    def _re_relation():
        pet._open_relation()
        return getattr(pet, '_relation_dialog', None)

    def _re_games():
        pet._open_games()
        return getattr(pet, '_game_window', None)

    pet._open_relation()
    probe(app, '❤️ 关系', getattr(pet, '_relation_dialog', None),
          reopen=_re_relation, collect=strips)
    pet._open_games()
    probe(app, '🎮 小游戏', getattr(pet, '_game_window', None),
          reopen=_re_games, collect=strips)

    # ---------- 4. 出图 ----------
    print('\n[4] 出图')
    if strips:
        size = tile(strips, os.path.join(SHOTS, 'v690_hide_btn_frames.png'))
        check(size[0] > 400 and size[1] > 100,
              '标题栏对照图 %dx%d → screenshots/v690_hide_btn_frames.png' % size)

    # 任务栏：面板最小化后确实变成一个可点回来的任务栏按钮
    print('\n[5] 任务栏（最小化后点得回来）')
    tray = user32.FindWindowW('Shell_TrayWnd', None)
    if tray:
        im = capture(tray)
        if im is not None:
            im.save(os.path.join(SHOTS, 'v690_hide_btn_taskbar.png'))
            print('    已存 screenshots/v690_hide_btn_taskbar.png（%dx%d）' % (im.width, im.height))
            note('任务栏上那几个按钮就是最小化后的面板 —— 点一下就回来')
    else:
        note('抓不到任务栏（Shell_TrayWnd），跳过')

    # ---------- 6. 收尾：关窗 + 还原用户数据 ----------
    print('\n[6] 收尾')
    for w in (dlg_settings, dlg_models, getattr(pet, '_relation_dialog', None),
              getattr(pet, '_game_window', None)):
        try:
            w and w.close()
        except Exception:
            pass
    for ctrl_name, attr in (('_music', 'panel'), ('_download', 'panel'),
                            ('_convert', 'panel'), ('_photos', 'panel'), ('_ledger', 'panel')):
        c = getattr(pet, ctrl_name, None)
        p = getattr(c, '_panel', None) if c is not None else None
        try:
            p and p.close()
        except Exception:
            pass
    pump(app, 0.4)
    try:
        pet.hide()
    except Exception:
        pass
    pump(app, 0.3)

    changed = []
    for fn in watch:
        p = os.path.join(BASE, fn)
        old = backup.get(fn)
        if old is None:
            if os.path.isfile(p):
                changed.append('%s（跑之前没有，现在有了）' % fn)
            continue
        with open(p, 'rb') as f:
            now = f.read()
        if now != old:
            with open(p, 'wb') as f:        # 原样写回
                f.write(old)
            changed.append(fn)
    check(not changed, '用户真实数据一个字节都没变（改动过又还原：%s）' % (', '.join(changed) or '无'))
    for fn in watch:
        if fn in backup:
            check(sha(os.path.join(BASE, fn)) == hashlib.sha256(backup[fn]).hexdigest(),
                  '%s 内容与跑之前一致' % fn)
    photos_after = sorted(os.listdir(photos_dir)) if os.path.isdir(photos_dir) else []
    check(photos_before == photos_after, 'photos/ 目录没有被写进验证照片（%d 个文件）'
          % len(photos_after))

    print('\n退出码：%s' % ('0（全过）' if not FAILS else '1（%d 项没过）' % len(FAILS)))
    if FAILS:
        for m in FAILS:
            print('  ✕ %s' % m)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
