# -*- coding: utf-8 -*-
"""tools/make_xiaoyu_assets.py — 把「小雨」10 格精灵图转成本项目要的立绘。

本项目立绘约定：assets/<角色>/<角色>_<状态>.png，状态缺图时自动回退 <角色>_idle.png。
小雨原图是一张 10 格横排精灵图，每格一个静态姿势，需要抠掉洋红背景并拆成状态图。

抠图公式与 xiaoyu-desktop-pet/src/renderer/renderer.ts 完全一致（同一张图，
两边的视觉必须一致）：excess = min(r,b) - g，excess<=30 留原样，否则按 (160-excess)/130 递减 alpha。

所有输出统一裁到「同一窗口」（各姿势内容的并集包围盒），这样：
  ① 尺寸一致 → 项目里 blink 整图叠 idle 的眨眼对得上（BLINK_OFFSETS=(0,0)）；
  ② 人物在画面里占满，不会被原图 217x724 的比例压成细条。

用法：python tools/make_xiaoyu_assets.py [精灵图路径]
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SRC = r'F:\桌宠\xiaoyu-desktop-pet\assets\xiaoyu-sprites-v3.png'
ROLE = 'flash'          # 复用 flash 槽位：默认角色、记忆/好感度的角色键都写死 flash/pro，新增角色要改多处
CELLS = 10

# 姿势序号（0 起，从左到右）：
# 0 站立睁眼 / 1 蹦跳 / 2 闭眼抱手 / 3 挥手 / 4 举手欢呼 / 5 惊讶
# 6 气鼓鼓 / 7 拿苹果 / 8 拿蛋糕 / 9 蜷缩睡觉
STATE_MAP = {
    'idle': 0, 'blink': 2, 'thinking': 2, 'shy': 2, 'shy_hug': 2,
    'sleep': 9, 'peek_sleep': 9, 'peek_bottom_sleep': 9,
    'peek': 0, 'peek_top': 0, 'peek_bottom': 0,
    'happy': 4, 'excited': 4, 'victory': 4, 'dance': 4, 'pat': 4, 'proud': 4,
    'surprised': 5,
    'angry': 6, 'cry': 6, 'sad': 6, 'defeat': 6,
    'waving': 3, 'sing': 3, 'music': 3,
    'eating': 7, 'hungry': 7, 'coffee': 7,
    'gift': 8, 'flower': 8, 'flower_give': 8, 'kiss': 8, 'hug_whale': 8, 'reading': 8,
    'exercise': 1,
    'lying': 9,
    'phone': 0, 'typing': 0, 'umbrella': 0,
}


def key_out(cell):
    """洋红背景 → 透明（alpha 与 Electron 版逐像素一致）"""
    cell = cell.convert('RGBA')
    px = cell.load()
    w, h = cell.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            excess = min(r, b) - g
            if excess <= 30:
                continue
            alpha = max(0.0, min(1.0, (160 - excess) / 130.0))
            # 半透明边缘像素反解掉洋红底色，否则人物轮廓会留一圈粉边
            if 0.02 < alpha < 1:
                r = max(0, min(255, int((r - 255 * (1 - alpha)) / alpha)))
                b = max(0, min(255, int((b - 255 * (1 - alpha)) / alpha)))
            px[x, y] = (r, g, b, int(round(a * alpha)))
    return cell


def main(src=DEFAULT_SRC):
    sheet = Image.open(src).convert('RGB')
    w, h = sheet.size
    bounds = [round(i * w / CELLS) for i in range(CELLS + 1)]

    cells = [key_out(sheet.crop((bounds[i], 0, bounds[i + 1], h))) for i in range(CELLS)]

    # 统一窗口 = 各姿势内容包围盒的并集（全透明格不参与）
    boxes = [c.getchannel('A').getbbox() for c in cells]
    solid = [b for b in boxes if b]
    if not solid:
        raise SystemExit('精灵图里没有非背景像素，抠图参数要检查')
    win = (min(b[0] for b in solid), min(b[1] for b in solid),
           max(b[2] for b in solid), max(b[3] for b in solid))
    print('源图 %dx%d，统一窗口 %s' % (w, h, win))

    out_dir = os.path.join(ROOT, 'assets', ROLE)
    os.makedirs(out_dir, exist_ok=True)
    cw = min(bounds[i + 1] - bounds[i] for i in range(CELLS))
    win = (win[0], win[1], min(win[2], cw), win[3])

    written = {}
    for state, idx in STATE_MAP.items():
        img = cells[idx].crop(win)
        path = os.path.join(out_dir, '%s_%s.png' % (ROLE, state))
        img.save(path)
        written.setdefault(idx, []).append(state)
    for idx in sorted(written):
        print('姿势 %d → %s' % (idx, '、'.join(written[idx])))
    print('已写入 %d 个状态图 → %s' % (len(STATE_MAP), out_dir))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC)