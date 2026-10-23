# -*- coding: utf-8 -*-
"""见面照片墙实机验证（v6.88）：造照片 → 导入 → 墙面/拍立得/灯箱/卡片 → 截图

用法：.venv\\Scripts\\python.exe tools\\verify_photos.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：离屏下没有中文字体，
截图里全是豆腐块，看不出「拍立得相纸上的日期有没有字」。

验的东西：
1. 导入：照片被**复制**进 photos/<拍摄日期>/（原图删了也不裂），同一天自动归成一次见面
2. 去重：同一个文件再导一次 → 0 新增、N 重复
3. 缩略图：photos/.thumbs 里有 512px 缓存（墙面/拍立得都靠它）
4. 墙面：justified 行（一行等高、宽度铺满、单图不变形）
5. 拍立得 / 灯箱 / 桌宠小卡片：都能真画出来
6. 倒计时：设了 pw_next_date 后，卡片上出现「距下次见面还有 N 天」
7. 接线：导入后回忆日志里多一条 type='meeting'，好感度涨了

照片都造在临时目录里，跑完删掉；用户自己的 config.json / photos / meetings.json
一个字节都不碰（开头备份 pw_* 键，结尾原样还原或删掉）。
"""
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication

import desktop_pet as dp
import pet_photos as ph
import pet_photos_ui as ui
from affection_engine import AffectionEngine
from memory_events import MemoryEvents

WORK = os.path.join(tempfile.gettempdir(), 'petpw_verify')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)
SRC = os.path.join(WORK, 'src')
LIB = os.path.join(WORK, 'lib')

FAILS = []


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def make_samples():
    """造 10 张不同比例的照片，分 3 天（EXIF 拍摄日期），另外塞一个不是图片的文件

    比例特意做杂：3:2 横、2:3 竖、超宽、正方形 —— 墙面 justified 排得好不好一眼能看出来。
    """
    from PIL import Image
    os.makedirs(SRC, exist_ok=True)
    shapes = [(1200, 800), (900, 1200), (1600, 600), (1000, 1000), (1280, 720),
              (800, 1100), (1500, 700), (1100, 900), (960, 1280), (1400, 900)]
    days = ['2026:05:01', '2026:05:01', '2026:05:01', '2026:05:01',
            '2026:05:20', '2026:05:20', '2026:05:20',
            '2026:06:08', '2026:06:08', '2026:06:08']
    colors = [(70, 120, 190), (190, 120, 70), (90, 160, 120), (150, 110, 170),
              (200, 170, 90), (80, 150, 180), (170, 90, 110), (110, 170, 110),
              (120, 130, 200), (200, 140, 130)]
    made = []
    for i, ((w, h), day, col) in enumerate(zip(shapes, days, colors)):
        p = os.path.join(SRC, '照片%02d.jpg' % (i + 1))
        im = Image.new('RGB', (w, h), col)
        # 铺点渐变纹理，截图里不至于是一块纯色（也顺便让缩略图大小真实）
        px = im.load()
        for y in range(0, h, 7):
            for x in range(0, w, 7):
                px[x, y] = (min(255, col[0] + x % 60), min(255, col[1] + y % 50), col[2])
        exif = Image.Exif()
        exif[36867] = '%s 1%d:3%d:00' % (day, i % 10, i % 6)
        im.save(p, exif=exif, quality=88)
        made.append(p)
    txt = os.path.join(SRC, '不是图片.txt')
    with open(txt, 'w', encoding='utf-8') as f:
        f.write('这张不是照片，导入时该被跳过并说清原因')
    return made, txt


def pump(app, widget_attr_owner, name='_pump', secs=15):
    """等界面里的缩略图泵把队列吃完（不占后台线程，靠定时器一帧帧补）"""
    pump_obj = getattr(widget_attr_owner, name, None)
    t0 = time.time()
    while pump_obj is not None and pump_obj.busy() and time.time() - t0 < secs:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return time.time() - t0


def shot(app, widget, filename, note):
    app.processEvents()
    time.sleep(0.25)
    app.processEvents()
    pm = widget.grab()
    if not check(not pm.isNull() and pm.width() > 50 and pm.height() > 50,
                 '%s：画面 %dx%d' % (note, pm.width(), pm.height())):
        return
    path = os.path.join(SHOTS, filename)
    pm.save(path)
    print('    已存 screenshots/%s' % filename)


def main():
    app = QApplication(sys.argv[:1])
    pet = dp.PetWidget()
    pet.show()
    app.processEvents()

    # 别把用户真实数据写脏：回忆/好感度换成临时文件，配置保存掐掉
    pet._save_cfg_value = lambda *a, **k: True
    pet.memories = MemoryEvents(os.path.join(WORK, 'memories.json'))
    pet.affection = AffectionEngine(os.path.join(WORK, 'affection.json'))

    cfg_keys = (ph.CFG_TARGET_H, ph.CFG_NEXT_DATE, ph.CFG_NEXT_NOTE, ph.CFG_XP,
                ph.CFG_CARD, ph.CFG_CARD_POS)
    prev_cfg = {k: ph.cfg_read().get(k) for k in cfg_keys}
    # 验证脚本不该改用户设置：先按默认值跑，结尾再还原
    for k in (ph.CFG_TARGET_H, ph.CFG_NEXT_DATE, ph.CFG_NEXT_NOTE, ph.CFG_CARD_POS):
        ph.cfg_delete(k)

    photos, not_image = make_samples()
    print('素材：%d 张照片（3 天 / 10 种比例）+ 1 个非图片文件' % len(photos))

    ctrl = ui.PhotoController(pet, base_dir=LIB)
    ctrl.on_meetings = pet._record_meetings
    ctrl.notice.connect(lambda t: print('  消息：', t))
    pet._photos = ctrl                       # 让桌宠的 📷 走这个临时库

    # ---------- 1. 导入 ----------
    print('\n[1] 导入（含一个不是图片的文件）')
    res = ctrl.import_files(photos + [not_image])
    check(res.get('added') == 10, '加进来 10 张（实际 %s）' % res.get('added'))
    check(len(res.get('skipped') or []) == 1, '非图片文件被跳过并给了原因')
    check(ctrl.lib.count() == 3, '按拍摄日期分成 3 次见面（实际 %d）' % ctrl.lib.count())

    dates = sorted(m['date'] for m in ctrl.lib.meetings())
    check(dates == ['2026-05-01', '2026-05-20', '2026-06-08'], '日期分组正确：%s' % dates)

    rels = [p['file'] for m in ctrl.lib.meetings() for p in ph.meeting_photos(m)]
    copied = all(os.path.isfile(ph.abs_of(r, LIB)) for r in rels)
    check(copied and all(r.startswith('photos/') for r in rels),
          '10 张都复制进 photos/<日期>/（不是只记了个路径）')
    check(all(os.path.abspath(ph.abs_of(r, LIB)) not in [os.path.abspath(s) for s in photos]
              for r in rels), '存的是副本，不是原图路径（原图删了也不裂）')

    # ---------- 2. 去重 ----------
    print('\n[2] 再导一次（应该全是重复）')
    res2 = ctrl.import_files(photos)
    check(res2.get('added') == 0 and res2.get('dups') == 10,
          '0 新增 / %s 重复（实际 %s / %s）' % (10, res2.get('added'), res2.get('dups')))
    check(ctrl.lib.photo_count() == 10, '墙上还是 10 张（没被灌成 20 张）')

    # ---------- 3. 缩略图 ----------
    print('\n[3] 缩略图缓存')
    panel = ctrl.panel()
    panel.resize(980, 720)
    panel.show()
    app.processEvents()
    spent = pump(app, panel.wall)
    tdir = ctrl.lib.thumb_dir
    thumbs = os.listdir(tdir) if os.path.isdir(tdir) else []
    check(len(thumbs) >= 10, 'photos/.thumbs 里有 %d 个缩略图（用时 %.1fs）' % (len(thumbs), spent))
    from PySide6.QtGui import QImage
    ok_size = bool(thumbs) and all(QImage(os.path.join(tdir, t)).width() <= ph.THUMB_SIZE
                                   for t in thumbs)
    check(ok_size, '缩略图长边都不超过 %d px（墙面解码才够快）' % ph.THUMB_SIZE)

    # ---------- 4. justified 布局 ----------
    print('\n[4] 墙面布局（justified：一行等高、铺满宽度、单图不变形）')
    lay = ph.justified_rows([(1200, 800)], 900, 190, 8)
    one = lay['rows'][0]['items'][0]
    check(abs(one['h'] - 190) <= 1 and one['w'] < 900,
          '一张 3:2 的图按 190 高排（%dx%d），没被拉成整屏宽' % (one['w'], one['h']))
    rows = ph.justified_rows([(1200, 800), (900, 1200), (1600, 600), (1000, 1000)], 900, 190, 8)
    even = all(len({i['h'] for i in r['items']}) == 1 for r in rows['rows'])
    filled = all(abs(sum(i['w'] for i in r['items']) + 8 * (len(r['items']) - 1) - 900) <= 1
                 for r in rows['rows'][:-1])
    check(even, '同一行里的照片等高')
    check(filled or len(rows['rows']) == 1, '每一整行都正好铺满宽度')
    hits, heads = len(panel.wall._hits), len(panel.wall._heads)
    check(hits == 10, '墙面上有 10 个可点区域（实际 %d）' % hits)
    check(heads == 3, '3 个分组标题（实际 %d）' % heads)
    print('    统计行：', panel.lb_stats.text())
    shot(app, panel, 'v688_photos_wall.png', '墙面（justified 分组）')

    # ---------- 5. 单次见面（拍立得） ----------
    print('\n[5] 单次见面：拍立得贴墙')
    mid = ctrl.lib.meetings()[0]['id']
    meeting = ctrl.lib.get(mid)
    dlg = panel._open_meeting(mid)
    dlg.resize(860, 680)
    dlg.ed_title.setText('第一次一起看海')
    dlg.ed_place.setText('海边栈道')
    dlg.ed_mood.setText('开心')
    dlg.ed_note.setPlainText('风很大，她把外套借给我了。')
    dlg._save()
    app.processEvents()
    pump(app, dlg.pol)
    n_ph = len(ph.meeting_photos(ctrl.lib.get(mid)))
    check(len(dlg.pol._cells) == n_ph, '拍立得墙摆了 %d 张（这次见面有 %d 张）'
          % (len(dlg.pol._cells), n_ph))
    saved = ctrl.lib.get(mid)
    check(saved['title'] == '第一次一起看海' and saved['place'] == '海边栈道',
          '标题/地点保存成功：%s · %s' % (saved['title'], saved['place']))
    angles = {dlg.pol._angle(p['file']) for p in ph.meeting_photos(saved)}
    check(all(-3 <= a <= 3 for a in angles), '拍立得歪斜角度都在 ±3° 内：%s' % sorted(angles))
    check(dlg.pol._angle(ph.meeting_photos(saved)[0]['file'])
          == dlg.pol._angle(ph.meeting_photos(saved)[0]['file']), '同一张照片角度稳定（重绘不乱转）')
    shot(app, dlg, 'v688_photos_meeting.png', '单次见面（拍立得相纸）')

    # ---------- 6. 灯箱 ----------
    print('\n[6] 灯箱（看大图 / 翻页）')
    lb = ui.PhotoLightbox(ph.meeting_photos(saved), 0, title=saved['title'], parent=dlg)
    lb.resize(900, 700)
    lb.show()
    app.processEvents()
    first = lb.lb_info.text()
    lb.nav(1)
    app.processEvents()
    check(first != lb.lb_info.text() and ('2 / %d' % n_ph) in lb.lb_info.text(),
          '翻页正常：%s → %s' % (first[:12], lb.lb_info.text()[:12]))
    shot(app, lb, 'v688_photos_lightbox.png', '灯箱大图')

    # ---------- 7. 桌宠小卡片 + 倒计时 ----------
    print('\n[7] 桌宠旁边的小卡片 + 下次见面倒计时')
    future = time.strftime('%Y-%m-%d', time.localtime(time.time() + 30 * 86400))
    ph.cfg_write(ph.CFG_NEXT_DATE, future)
    ph.cfg_write(ph.CFG_NEXT_NOTE, '去植物园')
    card = ctrl.card()
    card.show()
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    check('次见面' in card.lb_stats.text(), '卡片统计：%s' % card.lb_stats.text())
    check('还有 30 天' in card.lb_down.text(), '卡片倒计时：%s（下次 %s）'
          % (card.lb_down.text(), future))
    days = ph.days_between(ph.today_str(), future)
    print('    距 %s 还有 %s 天（备注：%s）' % (future, days, ph.cfg_text(ph.CFG_NEXT_NOTE)))
    thumbs_ok = [not lb.pixmap().isNull() for lb in card.shots]
    check(any(thumbs_ok), '卡片上摆出了最近的照片：%s' % thumbs_ok)
    geo, pgeo = card.frameGeometry(), pet.frameGeometry()
    print('    卡片位置 (%d,%d)，桌宠 (%d,%d)' % (geo.x(), geo.y(), pgeo.x(), pgeo.y()))
    shot(app, card, 'v688_photos_card.png', '桌宠旁边的小卡片')

    # ---------- 8. 桌宠接线 ----------
    print('\n[8] 桌宠接线：回忆 + 好感')
    items = pet.memories.all(pet.current)
    meetings = [m for m in items if m.get('type') == 'meeting']
    check(len(meetings) >= 1, '回忆日志里多了 %d 条 📷 见面' % len(meetings))
    if meetings:
        print('    %s ｜ %s' % (meetings[0]['title'], meetings[0]['detail']))
    snap = pet.affection.snapshot(pet.current)
    check(snap['affection'] > 0, '好感度有涨：%d' % snap['affection'])
    _menu, acts = pet._build_context_menu()
    check('photos' in acts and 'photo_card' in acts, '右键菜单里有「📷 见面照片墙…」和「🖼 见面小卡片」')
    check('📷' in pet.chat_pw_btn.text(), '标题栏有照片入口：%r' % pet.chat_pw_btn.text())

    # ---------- 收尾 ----------
    for w in (lb, dlg, panel):
        try:
            w.close()
        except Exception:
            pass
    try:
        card.close()
    except Exception:
        pass
    app.processEvents()
    for k in cfg_keys:
        if prev_cfg.get(k) is None:
            ph.cfg_delete(k)          # 原来没这键：删干净，别在用户配置里留个 null
        else:
            ph.cfg_write(k, prev_cfg.get(k))
    shutil.rmtree(WORK, ignore_errors=True)

    print('\n结果：%s（%d 项没过）' % ('OK' if not FAILS else 'FAIL', len(FAILS)))
    for f in FAILS:
        print('  ✕', f)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
