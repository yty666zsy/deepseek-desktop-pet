# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（全部来自本程序真实控件渲染）。

用法：.venv\\Scripts\\python.exe tools\\verify_readme.py
输出：screenshots/readme/*.png
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)
os.environ.setdefault('QT_ENABLE_HIGHDPI_SCALING', '0')

OUT = os.path.join(BASE, 'screenshots', 'readme')
TMP = tempfile.mkdtemp(prefix='petshot_')

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

import desktop_pet as dp


def shot(widget, name):
    """控件 → PNG（widget.grab 拿到的就是屏幕上的真实样子）"""
    path = os.path.join(TMP, name + '.png')
    widget.grab().save(path)
    return Image.open(path).convert('RGBA')


def rounded(im, radius=14):
    mask = Image.new('L', im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1),
                                           radius=radius, fill=255)
    out = im.copy()
    out.putalpha(mask)
    return out


def shadow_paste(canvas, im, xy, radius=14, blur=24, alpha=110):
    pad = blur * 2
    sh = Image.new('RGBA', (im.size[0] + pad * 2, im.size[1] + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    d.rounded_rectangle((pad, pad, pad + im.size[0], pad + im.size[1]),
                        radius=radius, fill=(0, 0, 0, alpha))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(sh, (xy[0] - pad, xy[1] - pad))
    canvas.alpha_composite(rounded(im, radius), xy)


def font(size, bold=False):
    p = r'C:\Windows\Fonts\msyhbd.ttc' if bold else r'C:\Windows\Fonts\msyh.ttc'
    try:
        return ImageFont.truetype(p, size)
    except Exception:
        return ImageFont.load_default()


def clear_chat(pet):
    lay = pet.chat_history_layout
    while lay.count() > 1:
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
    pet.display_msgs = []


def main():
    os.makedirs(OUT, exist_ok=True)
    pet = dp.PetWidget()
    pet.show()
    for _ in range(10):
        app.processEvents()

    # 统一截图条件：中文、100% 不透明、大一点的立绘（都不写回 config.json）
    pet.language = 'zh'
    pet._apply_pet_size(320, save=False)
    pet._apply_pet_opacity(1.0, save=False)
    for _ in range(5):
        app.processEvents()

    # ---------- 对话面板 ----------
    clear_chat(pet)
    pet._append_chat('我', '今天写代码卡住了，头好大……')
    pet._append_chat_md('桌宠', '先别硬扛，起来走两分钟吧。\n\n要不要我帮你把任务拆小一点？')
    pet._append_chat('我', '好，顺便记一下明天要交周报')
    pet._append_chat_md('桌宠', '记好了 ✅ 明天 09:00 提醒你。\n\n周报的事交给我盯着，你专心改 bug。')
    pet._append_chat('我', '对了，明天出门要带伞吗？')
    pet._append_chat_md('桌宠', '明天小雨转多云，18～24℃，记得带伞 ☔\n\n路面会滑，出门慢一点。')
    pet.chat_panel.setFixedSize(640, 640)
    pet._sync_window_to_panel()
    for _ in range(10):
        app.processEvents()
    chat_img = shot(pet.chat_panel, 'chat')

    # ---------- 右键菜单 ----------
    menu, _acts = pet._build_context_menu()
    menu.show()
    for _ in range(10):
        app.processEvents()
    menu_img = shot(menu, 'menu')
    menu.close()

    # ---------- 设置 · 模型页（改 Key 的入口） ----------
    from settings_ui import SettingsDialog
    dlg = SettingsDialog(pet, dp.MODEL_REGISTRY, parent=pet)
    dlg.nav.setCurrentRow(3)
    dlg.setFixedSize(1000, 660)
    dlg.show()
    for _ in range(10):
        app.processEvents()
    # 截图里用默认角色名，不带本机自定义模型档案（窗口显示后才有回填，故放在 show 之后）
    try:
        dlg.cb_char.blockSignals(True)
        dlg.cb_char.clear()
        dlg.cb_char.addItem('V4 Flash · deepseek-flash', 'flash')
        dlg.cb_char.addItem('V4 Pro · deepseek-v4-pro', 'pro')
        dlg.cb_char.setCurrentIndex(0)
        dlg.cb_char.blockSignals(False)
        dlg.lb_model.setText('deepseek-flash')
    except Exception:
        pass
    for _ in range(3):
        app.processEvents()
    settings_img = shot(dlg, 'settings')
    dlg.close()

    # ---------- 小游戏列表 ----------
    from pet_minigames import GameWindow
    gw = GameWindow(lambda *a: None, pet)
    gw.show()
    for _ in range(10):
        app.processEvents()
    games_img = shot(gw, 'games')
    gw.close()

    # ---------- 立绘 + 气泡 ----------
    pet.say_plain('今天也要一起加油呀～有想做的事尽管说！', immediate=True)
    for _ in range(20):
        app.processEvents()
    try:
        pet.bubble.setMinimumWidth(300)   # 让气泡按整句排版，不挤成小方块
        pet._place_bubble()
        app.processEvents()
    except Exception:
        pass
    pet_path = os.path.join(TMP, 'pet.png')
    pet.pet_label.pixmap().save(pet_path)      # 用 pixmap 而不是 grab()：保留透明背景
    pet_img = Image.open(pet_path).convert('RGBA')
    bubble_img = shot(pet.bubble, 'bubble') if pet.bubble.isVisible() else None

    # ---------- 合成 README 首图（真实控件 + 背景） ----------
    W, H = 1500, 840
    bg = Image.new('RGBA', (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        k = y / (H - 1)
        d.line((0, y, W, y), fill=(int(15 + k * 12), int(18 + k * 14), int(34 + k * 26), 255))
    glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((40, 260, 700, 920), fill=(79, 209, 197, 46))
    g.ellipse((700, 60, 1500, 860), fill=(124, 140, 255, 52))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(170)))

    d = ImageDraw.Draw(bg)
    d.text((84, 96), 'DeepSeek 桌宠', font=font(54, True), fill=(238, 243, 255))
    d.text((86, 176), '住在电脑里的小伙伴：会聊天 · 会记事 · 会提醒',
           font=font(25), fill=(176, 188, 220))
    d.text((86, 222), '右键菜单 / 拖到屏幕边缘贴边 / Ctrl+Alt+P 呼出',
           font=font(19), fill=(133, 146, 178))

    if bubble_img is not None:
        bg.alpha_composite(bubble_img, (150, 322))
    pglow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(pglow).ellipse((90, 430, 470, 800), fill=(79, 209, 197, 60))
    bg.alpha_composite(pglow.filter(ImageFilter.GaussianBlur(70)))
    bg.alpha_composite(pet_img, (110, 392))
    shadow_paste(bg, chat_img, (774, 100), radius=16, blur=26, alpha=120)

    bg.convert('RGB').save(os.path.join(OUT, '01-hero.png'), optimize=True)
    menu_img.convert('RGB').save(os.path.join(OUT, '02-menu.png'), optimize=True)
    chat_img.convert('RGB').save(os.path.join(OUT, '03-chat.png'), optimize=True)
    settings_img.convert('RGB').save(os.path.join(OUT, '04-settings.png'), optimize=True)
    games_img.convert('RGB').save(os.path.join(OUT, '05-games.png'), optimize=True)

    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print('已生成 screenshots/readme/%s  %dx%d' % (f, Image.open(p).size[0], Image.open(p).size[1]))

    app.quit()


if __name__ == '__main__':
    main()
