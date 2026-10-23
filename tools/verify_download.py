# -*- coding: utf-8 -*-
"""下载功能实机验证：开面板 → 解析链接 → 真下载 → 截图

用法：.venv\\Scripts\\python.exe tools\\verify_download.py [链接] [画质 best|1080|720|480|audio]
会真的下一遍（存进 downloads/，验完删掉），并在 screenshots/ 里留一张面板图。
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication

import desktop_pet as dp
import pet_download as dl

url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.bilibili.com/video/BV1GJ411x7h7'
quality = sys.argv[2] if len(sys.argv) > 2 else '720'

app = QApplication(sys.argv[:1])
p = dp.PetWidget()
p.show()
app.processEvents()

ok, hint = dl.status()
print('内核:', hint)
if not ok:
    print('FAIL: 内核不可用')
    sys.exit(1)

panel = p.open_download_panel()
if panel is None:
    print('FAIL: 面板没打开')
    sys.exit(1)

panel.url_edit.setText(url)
panel.quality_box.setCurrentIndex(panel.quality_box.findData(quality))
print('画质下拉:', panel.quality_box.currentText(), '|', panel.quality_box.currentData())
panel.on_probe()
t0 = time.time()
while time.time() - t0 < 90 and panel.info.text().startswith('把链接'):
    app.processEvents()
    time.sleep(0.1)
print('解析:', panel.info.text().replace('<br>', ' | ')[:200])
if not panel.dl_btn.isEnabled():
    print('FAIL: 解析失败')
    p._download.shutdown()
    sys.exit(1)

panel.grab().save(os.path.join(SHOTS, 'v685_download_panel.png'))
print('已存 screenshots/v685_download_panel.png')

panel.on_download()
t0 = time.time()
job = None
while time.time() - t0 < 300:
    app.processEvents()
    time.sleep(0.2)
    job = p._download.current_job()
    if job and job.state in ('done', 'error', 'canceled'):
        break
print('结果:', job.state if job else '?', '| 进度', panel.bar.value(), '|', panel.status.text())
# 让「✅ 下好了」这类收尾事件有机会处理完，截图才是真样子
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
print('收尾状态:', panel.status.text(), '| 任务列表:', [panel.tasks.item(i).text() for i in range(panel.tasks.count())])
panel.grab().save(os.path.join(SHOTS, 'v685_download_done.png'))
print('已存 screenshots/v685_download_done.png')

path = job.file if job else ''
if job and job.state == 'done' and path and os.path.isfile(path):
    size = os.path.getsize(path)
    print('文件:', os.path.basename(path), '%.1f MB' % (size / 1024.0 / 1024.0))
    try:
        os.remove(path)          # 验证产物不留在用户目录里
        print('（验证完已删除）')
    except Exception:
        pass
    p._download.shutdown()
    app.processEvents()
    print('OK')
    sys.exit(0)

print('FAIL:', job.error if job else '没有任务')
p._download.shutdown()
app.processEvents()
sys.exit(1)
