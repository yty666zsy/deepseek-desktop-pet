# -*- coding: utf-8 -*-
"""「关掉人机声线」实机验证（v6.94）

使用者原话：
> 可是我已经安装了声音，你刚才说话的声音就是我专门设置的，但是一让你朗读就换成了人机，
> 有两套发生系统……把这些人机声音发声系统给关掉。

也就是：桌宠平时用**用户挑的声线**（本地 sherpa 模型 / 在线 edge），一旦朗读失败就
悄悄降级到 Windows 自带的 WinRT/SAPI 声线（微软慧慧/瑶瑶/康康 = 拼接式机械音），
听感从"人"变成"人机"。v6.94 起默认整条链路里没有它们。

用法（**必须在真实平台跑**，离屏下中文全是豆腐块、截图没法看）：
    .venv\\Scripts\\python.exe tools\\verify_no_system_tts.py

验的东西：
1. 三种配置（本地模型 / 在线 / 自建服务）算出来的引擎链里**都没有 offline**
2. `_synth_offline` 真拒绝：不启动 powershell、不产出音频
3. 老配置里存的 `offline:Huihui` → 启动时自动换成自然声线（否则升级了还是那个人机）
4. 手选/自带输入框填 `offline:` → 明确拒绝并解释，不静默换成别人
5. 声线下拉里没有「离线 · 慧慧/瑶瑶/康康」，设置页也没有「列出系统声线」按钮
6. 关掉系统声线之后**原来好用的声线照样能念**（本地模型真合成一句）
7. 想要回退的老用户仍可用 "voice_allow_system_tts": true 打开
8. 截图：设置·语音页（screenshots/v694_voice_page_no_robot.png）
"""
import json
import os
import sys
import time
import wave

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402

import desktop_pet as dp                     # noqa: E402
import voice_io as vio                       # noqa: E402

FAILS = []


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def pump(app, sec=0.4):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def voice_cfg(path):
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return {}
    keys = ('voice_name', 'voice_rate', 'voice_enabled', 'voice_content_mode',
            'voice_night_quiet', 'voice_local_url', 'voice_allow_system_tts')
    return {k: cfg.get(k) for k in keys}


def main():
    app = QApplication(sys.argv[:1])
    cfg_path = os.path.join(BASE, 'config.json')
    before = voice_cfg(cfg_path)

    # ---------- 1. 引擎链 ----------
    print('[1] 引擎链里不许再有系统声线')
    cases = (
        ('本地模型 sherpa:zh-ll/2', dict(voice='sherpa:zh-ll/2')),
        ('在线 edge 晓晓', dict(voice='edge:zh-CN-XiaoxiaoNeural')),
        ('自建服务 local:…', dict(voice='local:http://127.0.0.1:9880')),
        ('老配置 offline:Huihui', dict(voice='offline:Huihui')),
        ('强制 engine=offline', dict(voice='edge:zh-CN-XiaoxiaoNeural', engine='offline')),
    )
    for label, kw in cases:
        v = vio.VoiceIO(base_dir=BASE, enabled=True, night_quiet=False, **kw)
        plan = v.engine_plan()
        check('offline' not in plan, '%-24s → %s' % (label, plan))

    # ---------- 2. 真拒绝 ----------
    print('\n[2] _synth_offline 真拒绝（不启动 powershell）')
    v = vio.VoiceIO(base_dir=BASE, enabled=True, night_quiet=False,
                    voice='edge:zh-CN-XiaoxiaoNeural')
    called = {}
    _real_run = vio.subprocess.run

    def _spy(*a, **k):
        called['yes'] = True
        return _real_run(*a, **k)

    vio.subprocess.run = _spy
    try:
        out = os.path.join(BASE, 'logs', '_no_sys_check.wav')
        ok, err = v._synth_offline('这句不该被合成。', out)
        check(ok is False and '已关闭' in err, '拒绝并说明原因：%s' % err)
        check(not called.get('yes'), '没有启动任何子进程（powershell 根本没跑）')
        check(not os.path.exists(out), '没有产出音频文件')
        if os.path.exists(out):
            os.remove(out)
    finally:
        vio.subprocess.run = _real_run

    # ---------- 3. 老配置自动迁移 ----------
    print('\n[3] 老配置 offline:Huihui → 自动换成自然声线')
    pet = dp.PetWidget()
    pet.show()
    pump(app, 0.6)
    saved = {}
    notes = []
    pet._save_cfg_value = lambda k, v_: (saved.__setitem__(k, v_) or True)   # 不真写盘
    pet._notify = lambda m: notes.append(str(m))
    pet.voice_name = 'offline:Huihui'
    changed = pet._migrate_system_voice()
    check(changed is True, '识别出老配置里的系统声线并迁移')
    check(not str(pet.voice_name).startswith('offline:'),
          '新声线不再是系统声线：%s' % pet.voice_name)
    check(saved.get('voice_name') == pet.voice_name, '迁移结果写回了配置')
    check(any('已关闭' in n for n in notes), '状态条如实告诉用户：%s'
          % (notes[-1] if notes else '(无)'))

    # ---------- 4. 手选也被拒 ----------
    print('\n[4] 手填/手选 offline: → 明确拒绝')
    notes.clear()
    saved.clear()
    pet._voice_edit_apply('offline:Huihui')
    check(not saved.get('voice_name'), '没有把 offline:Huihui 写进配置')
    check(any('已关闭' in n for n in notes), '提示说清了为什么不行')
    notes.clear()
    pet._list_voices_dialog('system', pet)
    check(any('已关闭' in n for n in notes), '「列出系统声线」入口也不会再给你挑机械音')

    # ---------- 5. 列表/界面 ----------
    print('\n[5] 声线列表与设置页')
    choices = pet._voice_choices()
    specs = [c[0] for c in choices]
    check(not any(s.startswith('offline:') for s in specs),
          '下拉框里没有 offline 条目（共 %d 条）' % len(specs))
    check(any(s.startswith('sherpa:') for s in specs), '本地模型还在列表里（没误伤）')
    check(any(s.startswith('edge:') for s in specs), '在线声线还在列表里')
    check(not any('慧慧' in c[1] for c in choices), '列表里再也看不到「慧慧」')

    from settings_ui import SettingsDialog, PAGES
    dlg = SettingsDialog(pet, dp.MODEL_REGISTRY, parent=pet)
    dlg.show()
    dlg.nav.setCurrentRow(list(PAGES).index('语音'))
    pump(app, 0.7)
    labels = [dlg.cb_voice_name.itemText(i) for i in range(dlg.cb_voice_name.count())]
    check(not any('慧慧' in x or '瑶瑶' in x or '康康' in x for x in labels),
          '设置页下拉里没有慧慧/瑶瑶/康康（%d 条）' % len(labels))
    rows = dlg.stack.widget(list(PAGES).index('语音')).form.rowCount()
    check(rows <= 14, '语音页仍是 %d 行（没加行）' % rows)
    pm = dlg.grab()
    pm.save(os.path.join(SHOTS, 'v694_voice_page_no_robot.png'))
    check(pm.width() > 400, '设置·语音页截图 %dx%d → screenshots/v694_voice_page_no_robot.png'
          % (pm.width(), pm.height()))
    dlg.hide()

    # ---------- 6. 好声线照样能用 ----------
    print('\n[6] 关掉系统声线后，原来好用的声线照样能念')
    import pet_voicelib as vlib
    key = next((k for k in ('zh-ll', 'fanchen-c', 'kokoro') if vlib.is_installed(k)), '')
    if key:
        real = vio.VoiceIO(base_dir=BASE, enabled=True, night_quiet=False,
                           voice=vlib.spec(key, 0), rate='+15%')
        path = os.path.join(BASE, 'logs', '_no_sys_ok.wav')
        ok, err = real._synth_sherpa('这句用来确认好声线没被误伤。', path)
        check(ok, '本地模型 %s 合成成功%s' % (key, '' if ok else '（%s）' % err))
        if ok:
            with wave.open(path) as w:
                sec = w.getnframes() / float(w.getframerate() or 1)
            check(sec > 0.5, '音频时长 %.2fs（真出声，不是空文件）' % sec)
            os.remove(path)
    else:
        print('    （没装本地模型，跳过）')

    # ---------- 7. 还能开回来 ----------
    print('\n[7] 想要回退的老用户：voice_allow_system_tts = true')
    v2 = vio.VoiceIO(base_dir=BASE, enabled=True, night_quiet=False,
                     voice='edge:zh-CN-XiaoxiaoNeural', allow_system_tts=True)
    check(v2.engine_plan() == ['edge', 'offline'], '打开后才恢复「在线→系统」兜底：%s'
          % v2.engine_plan())

    pet.hide()
    pump(app, 0.3)
    print('\n[8] 收尾')
    after = voice_cfg(cfg_path)
    check(after == before, 'config.json 的语音键没被这次验证改动（voice_name=%s）'
          % after.get('voice_name'))
    print('\n退出码：%s' % ('0（全过）' if not FAILS else '1（%d 项没过）' % len(FAILS)))
    for m in FAILS:
        print('  ✕ %s' % m)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
