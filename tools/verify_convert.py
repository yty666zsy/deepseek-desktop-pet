# -*- coding: utf-8 -*-
"""文件转换实机验证：开面板 → 塞真文件 → 真转 → 截图

用法：.venv\\Scripts\\python.exe tools\\verify_convert.py
会在临时目录里造几份素材（图片/视频/PDF/CSV/Word/文件夹），真转一遍看产物，最后删掉。
截图留在 screenshots/ 里。

v6.87 起多验四条：页码范围、PDF 拆分、PDF 压缩、质量/时间戳/文件夹递归。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication

import desktop_pet as dp
import pet_convert as cv

WORK = os.path.join(tempfile.gettempdir(), 'petcv_verify')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)


def make_samples():
    """造素材：两张图、一段带声音的 mp4、一份 3 页 PDF + 两份 1 页 PDF、CSV、docx、一棵小文件夹树"""
    from PIL import Image
    made = {}
    for name, color, size in (('海边.jpg', (30, 140, 200), (320, 200)),
                              ('截图.png', (220, 60, 60), (160, 160))):
        p = os.path.join(WORK, name)
        Image.new('RGB', size, color).save(p)
        made[name] = p
    pdfs = []
    for i, color in enumerate(((255, 255, 255), (240, 240, 220))):
        p = os.path.join(WORK, '扫描页%d.pdf' % (i + 1))
        Image.new('RGB', (300, 300), color).save(p, 'PDF')
        pdfs.append(p)
    made['pdfs'] = pdfs

    # 3 页的「手册」—— 拆分/页码/压缩都拿它试
    pages = []
    for i, color in enumerate(((250, 120, 120), (120, 240, 120), (120, 120, 250))):
        p = os.path.join(WORK, '_pg%d.png' % i)
        Image.new('RGB', (320, 420), color).save(p)
        pages.append(p)
    manual = os.path.join(WORK, '手册.pdf')
    Image.open(pages[0]).save(manual, 'PDF', save_all=True,
                              append_images=[Image.open(p) for p in pages[1:]])
    made['manual'] = manual

    # 有噪声的图：比「质量 20 vs 95」的体积差异用（纯色图看不出差别）
    noise = os.path.join(WORK, '噪声.png')
    im = Image.new('RGB', (420, 320))
    px = im.load()
    for y in range(320):
        for x in range(420):
            px[x, y] = ((x * 7 + y * 13) % 256, (x * 11 + y * 3) % 256, (x * 5 + y * 17) % 256)
    im.save(noise)
    made['noise'] = noise

    csvp = os.path.join(WORK, '成绩.csv')
    with open(csvp, 'w', encoding='utf-8') as f:
        f.write('名字,科目,分数\n小明,数学,95\n小红,语文,88\n')
    made['csv'] = csvp

    docx = os.path.join(WORK, '笔记.docx')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>桌宠的转换功能测试</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>第二段文字</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(docx, 'w') as z:
        z.writestr('word/document.xml', doc)
    made['docx'] = docx

    # 一棵小文件夹树：验证「拖文件夹 + 递归子目录」
    tree = os.path.join(WORK, '相册')
    deeper = os.path.join(tree, '子目录')
    os.makedirs(deeper, exist_ok=True)
    Image.new('RGB', (100, 80), (60, 60, 60)).save(os.path.join(tree, '一.jpg'))
    Image.new('RGB', (100, 80), (90, 90, 90)).save(os.path.join(tree, '二.png'))
    Image.new('RGB', (100, 80), (120, 120, 120)).save(os.path.join(deeper, '三.jpg'))
    made['tree'] = tree

    ff = cv.find_ffmpeg()
    if ff:
        mp4 = os.path.join(WORK, '演示.mp4')
        try:
            subprocess.run([ff, '-y', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'testsrc=size=320x240:rate=15:duration=3',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                            '-shortest', mp4], check=True, capture_output=True, timeout=120)
            made['mp4'] = mp4
        except Exception as exc:
            print('（造 mp4 失败，跳过音视频那条：%s）' % exc)
    return made


def wait_job(app, ctl, panel, what, limit=180):
    t0 = time.time()
    while time.time() - t0 < limit:
        app.processEvents()
        time.sleep(0.15)
        job = ctl.current_job()
        if job and job.state in ('done', 'error', 'canceled'):
            for _ in range(20):        # 收尾事件走完，界面才是最终样子
                app.processEvents()
                time.sleep(0.02)
            return job
    return None


def run_round(app, p, files, target, title, wait_target=20,
              quality=None, keep_dates=None, pages=None, recurse=None):
    """面板里点一次完整流程：清空 → 设参数 → 塞文件 → 选格式 → 转 → 等结束"""
    panel = p.open_convert_panel()
    panel.on_clear()
    # 每轮都从默认值开始，免得上一轮的设置偷偷影响这一轮
    panel.quality_slider.setValue(90 if quality is None else int(quality))
    if keep_dates is not None:
        panel.keepdates_chk.setChecked(bool(keep_dates))
    else:
        panel.keepdates_chk.setChecked(False)
    panel.pages_edit.setText('' if pages is None else str(pages))
    if recurse is not None:
        panel.recurse_chk.setChecked(bool(recurse))
    panel.add_files(files)
    app.processEvents()
    # Filee 的目标格式是后台线程现问的，冷缓存时等它把下拉填完
    t0 = time.time()
    while panel.target_box.findData(target) < 0 and time.time() - t0 < wait_target:
        app.processEvents()
        time.sleep(0.1)
    idx = panel.target_box.findData(target)
    if idx < 0:
        print('  ✕ %s：下拉里没有 %s（有 %s）'
              % (title, target, [panel.target_box.itemData(i) for i in range(panel.target_box.count())]))
        return None
    panel.target_box.setCurrentIndex(idx)
    extra = []
    if quality is not None:
        extra.append('质量 %d' % quality)
    if keep_dates:
        extra.append('保留时间戳')
    if pages:
        extra.append('页码 %s' % pages)
    print('  %s：%d 个文件 → %s（%s）%s' % (title, panel.files.count(), target,
                                          panel.target_box.currentText(),
                                          ('｜' + '，'.join(extra)) if extra else ''))
    panel.on_convert()
    job = wait_job(app, p._convert, panel, title)
    if job is None:
        print('  ✕ %s：超时没结束' % title)
        return None
    state = job.state
    ok = sum(1 for r in job.results if r.get('ok'))
    print('  %s → %s（%d/%d 成功）%s' % (title, state, ok, len(job.results),
                                        ('｜' + job.error) if job.error else ''))
    for r in job.results:
        outs = r.get('outs') or []
        label = ('%d 个：%s' % (len(outs), os.path.basename(outs[0]))) if len(outs) > 1 \
            else (os.path.basename(r.get('out') or '') or r.get('error', ''))
        eng = r.get('engine') or ''
        print('     %s %s%s%s' % ('✔' if r.get('ok') else '✕', label,
                                 ('［%s］' % eng) if eng else '',
                                 ('（%s）' % r['note']) if r.get('note') else ''))
    return job


def main():
    app = QApplication(sys.argv[:1])
    p = dp.PetWidget()
    p.show()
    app.processEvents()

    ok, hint = cv.status()
    print('引擎:', hint)
    if not ok:
        print('FAIL: 一个引擎都没有')
        return 1

    made = make_samples()
    keys = ('cv_unify', 'cv_dir', 'cv_overwrite', 'cv_quality', 'cv_keepdates',
            'cv_recurse', 'cv_pages')
    prev_cfg = {k: cv.cfg_read().get(k) for k in keys}
    panel = p.open_convert_panel()
    if panel is None:
        print('FAIL: 面板没打开')
        return 1
    print('面板:', panel.windowTitle())
    # 截图要的是「默认长相」：跟原文件放一起、不勾统一输出
    panel.unify_chk.setChecked(False)
    panel.dir_edit.setText('')
    panel.overwrite_chk.setChecked(False)
    panel.keepdates_chk.setChecked(False)
    panel.quality_slider.setValue(90)
    panel.pages_edit.setText('')
    panel.add_files([made['海边.jpg'], made['截图.png']])
    app.processEvents()
    panel.target_box.setCurrentIndex(panel.target_box.findData('webp'))
    app.processEvents()
    panel.grab().save(os.path.join(SHOTS, 'v687_convert_panel.png'))
    print('已存 screenshots/v687_convert_panel.png')

    all_ok = True
    jobs = []
    # 统一输出到临时目录，免得把用户目录弄脏
    out_dir = os.path.join(WORK, 'out')
    os.makedirs(out_dir, exist_ok=True)
    panel.unify_chk.setChecked(True)
    panel.dir_edit.setText(out_dir)

    if made.get('mp4'):
        jobs.append(run_round(app, p, [made['mp4']], 'mp3', '视频 MP4 → MP3'))
        jobs.append(run_round(app, p, [made['mp4']], 'gif', '视频 MP4 → GIF'))
    jobs.append(run_round(app, p, made['pdfs'], cv.MERGE_PDF, '两个 PDF 合并'))
    jobs.append(run_round(app, p, made['pdfs'][:1], 'txt', 'PDF → 文本'))
    jobs.append(run_round(app, p, [made['csv']], 'xlsx', 'CSV → XLSX'))
    jobs.append(run_round(app, p, [made['csv']], 'json', 'CSV → JSON'))
    jobs.append(run_round(app, p, [made['docx']], 'txt', 'Word → 文本'))

    # ---- v6.87 新能力：页码范围 / 拆分 / 压缩 / 质量 / 时间戳 / 文件夹递归 ----
    if cv._has_module('pypdf') or cv.find_filee():
        presets = cv.filee_presets() if cv.find_filee() else {}
        print('  Filee 预设里有 PDF 三件套：%s'
              % [pid for pid in cv.PDF_PRESETS.values() if pid in presets])
        jobs.append(run_round(app, p, [made['manual']], cv.PAGES_PDF,
                              'PDF 取页（只要第 1、3 页）', pages='1,3'))
        jobs.append(run_round(app, p, [made['manual']], cv.SPLIT_PDF,
                              'PDF 拆分（每页一个）'))
        jobs.append(run_round(app, p, [made['manual']], cv.COMPRESS_PDF,
                              'PDF 压缩（有预设走 Filee）'))
        jobs.append(run_round(app, p, [made['manual']], cv.COMPRESS_PDF,
                              'PDF 压缩（带页码 1-3 → pypdf，同样三页）', pages='1-3'))
        jobs.append(run_round(app, p, made['pdfs'], cv.MERGE_PDF,
                              '合并（没页码 → Filee 预设）'))
        jobs.append(run_round(app, p, made['pdfs'], cv.MERGE_PDF,
                              '合并只取每份第 1 页 → pypdf', pages='1'))
        panel.grab().save(os.path.join(SHOTS, 'v687_convert_pdf.png'))
        print('已存 screenshots/v687_convert_pdf.png')

    # 质量：同一张图分别用 20 / 95 转 JPG，比大小
    if cv.find_filee() or cv._has_module('PIL'):
        lo_dir = os.path.join(WORK, 'q20')
        hi_dir = os.path.join(WORK, 'q95')
        os.makedirs(lo_dir, exist_ok=True)
        os.makedirs(hi_dir, exist_ok=True)
        panel.dir_edit.setText(lo_dir)
        panel.overwrite_chk.setChecked(True)
        lo = run_round(app, p, [made['noise']], 'jpg', '图片质量 20', quality=20)
        panel.dir_edit.setText(hi_dir)
        hi = run_round(app, p, [made['noise']], 'jpg', '图片质量 95', quality=95)
        jobs += [lo, hi]
        if lo and hi and lo.results and hi.results:
            a = os.path.getsize(lo.results[0]['out'])
            b = os.path.getsize(hi.results[0]['out'])
            print('     质量 20 = %d B，质量 95 = %d B → %s'
                  % (a, b, '低质量更小 ✔' if a < b else '没差 / 反了 ✕'))
            if a >= b:
                all_ok = False
        panel.overwrite_chk.setChecked(False)
        panel.dir_edit.setText(out_dir)

    # 保留原时间戳
    old = time.time() - 86400 * 30
    os.utime(made['海边.jpg'], (old, old))
    kd_dir = os.path.join(WORK, 'keepdate')
    os.makedirs(kd_dir, exist_ok=True)
    panel.dir_edit.setText(kd_dir)
    kd = run_round(app, p, [made['海边.jpg']], 'png', '保留原时间戳', keep_dates=True)
    jobs.append(kd)
    if kd and kd.results and kd.results[0].get('ok'):
        got = os.stat(kd.results[0]['out']).st_mtime
        same = abs(got - old) < 3
        print('     源修改时间 %.0f / 产物 %.0f → %s' % (old, got, '一致 ✔' if same else '不一致 ✕'))
        if not same:
            all_ok = False
    panel.dir_edit.setText(out_dir)
    # 用户手动拖文件夹：面板里勾「包含子文件夹」
    tree_out = os.path.join(WORK, 'tree_out')
    os.makedirs(tree_out, exist_ok=True)
    panel.dir_edit.setText(tree_out)
    tree_job = run_round(app, p, [made['tree']], 'webp', '文件夹递归（拖整个目录）',
                         recurse=True)
    jobs.append(tree_job)
    if tree_job:
        got = len([r for r in tree_job.results if r.get('ok')])
        print('     文件夹里收了 %d 个文件（应当是 3：两层共 3 张图）' % got)
        if got != 3:
            all_ok = False
    panel.dir_edit.setText(out_dir)

    # Filee 在的话，再验几条内置引擎做不到的（这才是「双引擎」的意义）
    if cv.find_filee():
        jobs.append(run_round(app, p, [made['docx']], 'pdf', 'Word → PDF（Filee）'))
        jobs.append(run_round(app, p, [made['海边.jpg']], 'heic', 'JPG → HEIC（Filee）'))
        jobs.append(run_round(app, p, [made['pdfs'][0]], 'png', 'PDF → PNG 图片（Filee）'))
    # 最后一轮留个多文件的，截图里结果列表才好看（一个真转、一个「已经是 png」跳过）
    jobs.append(run_round(app, p, [made['海边.jpg'], made['截图.png']], 'png', '批量图片 → PNG'))

    panel.grab().save(os.path.join(SHOTS, 'v687_convert_done.png'))
    print('已存 screenshots/v687_convert_done.png')

    for job in jobs:
        if job is None or job.state != 'done' or job.fail_count:
            all_ok = False

    print('\n产物：')
    for root, _dirs, files in os.walk(WORK):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, WORK)
            print('  %-30s %8.1f KB' % (rel, os.path.getsize(full) / 1024.0))

    try:
        p._convert.shutdown()
    except Exception:
        pass
    app.processEvents()
    # 验证脚本不该改用户设置：把面板偏好恢复原样
    try:
        for k in keys:
            if prev_cfg.get(k) is None:
                cv.cfg_delete(k)          # 原来根本没这键：删掉，别留个 null 在用户配置里
            else:
                cv.cfg_write(k, prev_cfg.get(k))
    except Exception:
        pass
    shutil.rmtree(WORK, ignore_errors=True)
    print('\nOK' if all_ok else '\nFAIL')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
