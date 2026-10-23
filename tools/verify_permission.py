# -*- coding: utf-8 -*-
"""聊天框权限档位实机验证（v6.91）

用法：.venv\\Scripts\\python.exe tools\\verify_permission.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：离屏后端中文字体全是豆腐块，
「🔒 严格 / 🛡 只问真危险 / 🔓 全放权」这几行字在截图里根本认不出来。

验的东西：
1. 按钮真长在输入框左边，三个档位的图标/颜色/提示都对
2. 三档菜单能弹出来，当前档打勾，底下写着"只对本轮对话有效"
3. **真的执行**（临时目录里跑真 PowerShell）：
   · 🔒 严格 + 写文件 → 弹确认框（我们替使用者点"允许"）→ 文件生成
   · 🛡 只问真危险 + 写文件 → **不弹框** → 文件生成
   · 🛡 只问真危险 + 删文件 → 弹框 → 我们点"拒绝" → 文件还在（放权不等于不设防）
   · 🔓 全放权 + 删文件 → 不弹框 → 文件没了，且补了一条"放权执行"的审计
4. 「✚ 新对话」把权限收回严格（拍板 3A：只对本轮对话有效）
5. 隐私：跑完用户 config.json / ledger.json / meetings.json 一个字节都没变
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402

import desktop_pet as dp                     # noqa: E402
import pet_permission as perm                # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), 'petperm_verify')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)

FAILS = []


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def pump(app, sec=0.25):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def sha(path):
    if not os.path.isfile(path):
        return ''
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def font(size=15):
    from PIL import ImageFont
    for p in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def qpixmap_to_pil(pm):
    """QPixmap → PIL.Image（PIL 只认 PIL 图，`grab()` 给的是 QPixmap）"""
    from io import BytesIO
    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pm.save(buf, 'PNG')
    buf.close()
    return Image.open(BytesIO(bytes(ba))).convert('RGB')


def clean_label(name):
    """PIL 的中文字体没有 emoji 字形（会画成豆腐块），说明文字里把它们去掉"""
    out = []
    for ch in name:
        cp = ord(ch)
        if cp < 0x0250 or 0x4E00 <= cp <= 0x9FFF or ch in '¥—·「」':
            out.append(ch)
    return ''.join(out).strip() or name


def compose_levels(rows, out_path, scale=3):
    """三档按钮放大对照图：左边写档位名，右边是按钮本身（放大 3 倍看清图标与颜色）"""
    from PIL import Image, ImageDraw
    f = font(16)
    label_w = 330
    pad = 10
    ims = [(clean_label(name), qpixmap_to_pil(im).resize((im.width() * scale, im.height() * scale),
                                                        Image.LANCZOS))
           for name, im in rows]
    W = label_w + max(i.width for _n, i in ims) + pad * 2
    H = sum(i.height + pad for _n, i in ims) + pad
    canvas = Image.new('RGB', (W, H), (246, 246, 248))
    dr = ImageDraw.Draw(canvas)
    y = pad
    for name, im in ims:
        dr.text((pad, y + im.height // 2 - 10), name, font=f, fill=(28, 28, 32))
        canvas.paste(im, (label_w, y))
        dr.rectangle([label_w - 1, y - 1, label_w + im.width, y + im.height],
                     outline=(180, 180, 188))
        y += im.height + pad
    canvas.save(out_path)
    return canvas.size


def main():
    app = QApplication(sys.argv[:1])

    print('[0] 隐私：先备份真实数据')
    watch = ('config.json', 'ledger.json', 'meetings.json')
    backup = {}
    for fn in watch:
        p = os.path.join(BASE, fn)
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                backup[fn] = f.read()
        print('    %-14s 存在=%s' % (fn, os.path.isfile(p)))

    pet = dp.PetWidget()
    pet.show()
    pump(app, 0.8)
    pet._save_cfg_value = lambda *a, **k: True        # 别把验证状态写进用户配置

    # 记录"要不要问"：把确认框换成可编程的假货
    asked = []
    answer = {'yes': True}

    def fake_confirm(message):
        asked.append(message)
        return answer['yes']

    pet._request_confirm = fake_confirm

    # 审计钩子换成记录器（审计本身可能关着，这里只验宿主有没有调）
    delegated, switches = [], []
    real_note_delegated, real_note_switch = perm.note_delegated, perm.note_switch
    perm.note_delegated = lambda level, what: delegated.append((level, what))
    perm.note_switch = lambda level, old=None: switches.append((old, level))

    # ---------- 1. 按钮本身 ----------
    print('\n[1] 按钮（输入框左边那颗锁）')
    row = pet.perm_btn.parent()
    check(pet.perm_btn is not None and pet.perm_btn.isVisible(), '按钮存在且可见')
    check(pet.perm_btn.width() == 30 and pet.perm_btn.height() == 30,
          '尺寸 30×30（跟 🎤 📎 一排对齐）')
    geoms = {'perm': pet.perm_btn.geometry(), 'input': pet.chat_input.geometry(),
             'mic': pet.mic_btn.geometry()}
    check(geoms['perm'].right() <= geoms['input'].left() + 4,
          '按钮在输入框左边（perm.right=%d ≤ input.left=%d）'
          % (geoms['perm'].right(), geoms['input'].left()))

    shots = []
    for lv in perm.LEVELS:
        pet._set_perm(lv)
        pump(app, 0.2)
        icon = pet.perm_btn.text()
        tip = pet.perm_btn.toolTip()
        color = pet._tk(perm.COLOR_TOKEN[lv])
        check(icon == perm.ICON[lv], '%s：按钮图标是 %s（实际 %r）' % (lv, perm.ICON[lv], icon))
        check(pet._t(perm.LABEL_KEY[lv]) in tip, '%s：提示里有档位名' % lv)
        check(pet._t('perm_scope') in tip, '%s：提示里写清只对本轮对话有效' % lv)
        check(color.lower() in pet.perm_btn.styleSheet().lower(),
              '%s：按钮颜色走主题 token %s' % (lv, perm.COLOR_TOKEN[lv]))
        shots.append(('%s  %s' % (perm.ICON[lv], pet._t(perm.LABEL_KEY[lv])), pet.perm_btn.grab()))
    check(len(switches) >= 2, '每次切档都记了审计（%d 次）' % len(switches))
    size = compose_levels(shots, os.path.join(SHOTS, 'v691_perm_levels.png'))
    print('    已存 screenshots/v691_perm_levels.png（%dx%d）' % size)

    # ---------- 2. 菜单 ----------
    print('\n[2] 三档菜单')
    pet._set_perm(perm.SMART)
    menu = pet._perm_build_menu()
    acts = [a for a in menu.actions() if a.isCheckable()]
    check(len(acts) == 3, '菜单里三档都在（实际 %d）' % len(acts))
    check([a.text() for a in acts][1].startswith(perm.ICON[perm.SMART]),
          '当前档（🛡）在第 2 项')
    check(sum(1 for a in acts if a.isChecked()) == 1, '只有一个档位打勾')
    check(any(pet._t('perm_scope') in a.text() for a in menu.actions() if not a.isCheckable()),
          '菜单底部写着"只对本轮对话有效"')
    menu.popup(pet.perm_btn.mapToGlobal(pet.perm_btn.rect().bottomLeft()))
    pump(app, 0.4)
    pm = menu.grab()
    check(pm.width() > 80 and pm.height() > 40, '菜单能画出来 %dx%d' % (pm.width(), pm.height()))
    pm.save(os.path.join(SHOTS, 'v691_perm_menu.png'))
    print('    已存 screenshots/v691_perm_menu.png')
    menu.close()
    pump(app, 0.15)

    # 顺手拍一张聊天面板（看按钮在真界面里的位置）
    pm2 = pet.chat_panel.grab()
    check(pm2.width() > 200, '聊天面板截图 %dx%d' % (pm2.width(), pm2.height()))
    pm2.save(os.path.join(SHOTS, 'v691_perm_in_chat.png'))

    # ---------- 3. 真跑 PowerShell ----------
    print('\n[3] 真执行（临时目录里跑真 PowerShell）')
    f_write = os.path.join(WORK, '严格档写的.txt')
    f_smart = os.path.join(WORK, '只问真危险写的.txt')
    f_del = os.path.join(WORK, '待删.txt')

    # ① 🔒 严格 + 写文件 → 必须问
    pet._set_perm(perm.STRICT)
    asked.clear()
    answer['yes'] = True
    out = pet._tool_run_powershell({'command': "Set-Content -Path '%s' -Value 'hi' -Encoding UTF8"
                                    % f_write})
    check(len(asked) == 1, '🔒 严格：写文件前问了（%d 次）' % len(asked))
    check(os.path.isfile(f_write), '🔒 严格：允许后文件真的写出来了')

    # ② 🛡 只问真危险 + 写文件 → 不问
    pet._set_perm(perm.SMART)
    asked.clear()
    delegated.clear()
    pet._tool_run_powershell({'command': "Set-Content -Path '%s' -Value 'hi' -Encoding UTF8"
                              % f_smart})
    check(len(asked) == 0, '🛡 只问真危险：写文件没弹框（这才是"不麻烦"的关键）')
    check(os.path.isfile(f_smart), '🛡 只问真危险：文件照样写出来了')

    # ③ 🛡 只问真危险 + 删文件 → 仍然问；拒绝就真的不删
    with open(f_del, 'w', encoding='utf-8') as f:
        f.write('x')
    asked.clear()
    answer['yes'] = False
    out = pet._tool_run_powershell({'command': "Remove-Item -Force '%s'" % f_del})
    check(len(asked) == 1, '🛡 只问真危险：删文件仍然问（%d 次）' % len(asked))
    check('拒绝' in out or not os.path.isfile(f_del), '拒绝后如实回报，未执行')
    check(os.path.isfile(f_del), '🛡 拒绝之后文件还在（放权不等于不设防）')

    # ④ 🔓 全放权 + 删文件 → 不问，且补审计
    pet._set_perm(perm.OPEN)
    asked.clear()
    delegated.clear()
    out = pet._tool_run_powershell({'command': "Remove-Item -Force '%s'" % f_del})
    check(len(asked) == 0, '🔓 全放权：删文件也没弹框')
    check(not os.path.isfile(f_del), '🔓 全放权：命令真的执行了（文件被删）')
    check(bool(delegated), '🔓 全放权：补了"放权执行"审计（%s）'
          % (delegated[0][1][:40] if delegated else '无'))

    # ---------- 4. 新对话收回权限 ----------
    print('\n[4] ✚ 新对话 → 权限收回严格')
    # 别让验证动到用户数据：存档/存记忆都短路
    pet._archive_chat_file = lambda *a, **k: ''
    pet._save_chat_memory = lambda *a, **k: None
    pet._save_memory = lambda *a, **k: True
    pom = pet.perm
    check(pom.level == perm.OPEN, '新对话之前是 🔓 全放权')
    pet._new_conversation(archive=False)
    pump(app, 0.3)
    check(pom.level == perm.STRICT, '新对话之后自动回到 🔒 严格（实际 %s）' % pom.level)
    check(pet.perm_btn.text() == perm.ICON[perm.STRICT], '按钮图标也跟着回 🔒')

    # ---------- 5. 收尾 ----------
    print('\n[5] 收尾')
    perm.note_delegated, perm.note_switch = real_note_delegated, real_note_switch
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
                changed.append(fn + '（跑之前没有）')
            continue
        with open(p, 'rb') as f:
            if f.read() != old:
                with open(p, 'wb') as f:
                    f.write(old)
                changed.append(fn)
    check(not changed, '用户真实数据一个字节都没变（改动过又还原：%s）' % (', '.join(changed) or '无'))

    print('\n退出码：%s' % ('0（全过）' if not FAILS else '1（%d 项没过）' % len(FAILS)))
    for m in FAILS:
        print('  ✕ %s' % m)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
