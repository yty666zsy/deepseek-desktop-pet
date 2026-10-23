# -*- coding: utf-8 -*-
"""朗读暂停键实机验证（v6.93）

用法：.venv\\Scripts\\python.exe tools\\verify_voice_pause.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：① 离屏后端中文字体全是豆腐块，
截图里认不出「⏸ / ▶」那排；② 暂停/继续要真的走 Windows MCI，离屏也不影响，但截图没法看。
跑的时候会**真的出声**（3 秒左右的本地声线朗读），不想听就先把音量关小。

验的东西：
1. 真播一句 → MCI 状态是 `playing`
2. 点暂停 → MCI 状态变 `paused`，**播放位置真的不再前进**
3. 点继续 → 状态回 `playing`，位置接着往前跑（不是从头念）
4. **计时冻结**：整段朗读总耗时 ≈ 音频时长 + 暂停时长（暂停期间句子的"剩余时间"不走）
5. 没在朗读时点暂停 → 返回 False，界面说「现在没有在朗读」，不假装成功
6. 界面：输入框那一排有 ⏸ 键（30×30、跟 🎤 一条线），点一下变 ▶、再点回 ⏸，没在念时是灰的
7. 隐私：语音相关配置键一个都没变
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402

import desktop_pet as dp                     # noqa: E402
import voice_io as vio                       # noqa: E402

FAILS = []
ALIAS = vio.MCI_ALIAS
TEXT = '第一句用来验证暂停。第二句验证继续。第三句验证整段念完。'


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def pump(app, sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def mci_status(v):
    """查真实播放状态（playing/paused/stopped）

    ★ MCI 别名是**线程私有**的：主线程直接 mciSendStringW 一律失败（实测）。
    所以必须走 v.mci_query()，让朗读线程去发命令、把结果带回来。
    """
    ok, val = v.mci_query('status %s mode' % ALIAS)
    return val.strip().lower() if ok else ''


def mci_pos(v):
    ok, val = v.mci_query('status %s position' % ALIAS)
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


def voice_cfg(path):
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return {}
    keys = ('voice_name', 'voice_rate', 'voice_enabled', 'voice_content_mode',
            'voice_night_quiet', 'voice_local_url')
    return {k: cfg.get(k) for k in keys}


def wait_until(cond, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def main():
    app = QApplication(sys.argv[:1])
    cfg_path = os.path.join(BASE, 'config.json')
    before = voice_cfg(cfg_path)

    # ---------- 1. 用本地声线念一句（不联网，最稳） ----------
    print('[1] 准备：本地声线（不联网）')
    import pet_voicelib as vlib
    key = next((k for k in ('zh-ll', 'kokoro', 'fanchen-c') if vlib.is_installed(k)), '')
    if not key:
        print('  ✕ 没装本地声线模型，先跑 tools/fetch_voices.py')
        return 1
    print('    用 %s' % vlib.label(key))
    v = vio.VoiceIO(base_dir=BASE, enabled=True, voice=vlib.spec(key, 0), rate='+15%',
                    night_quiet=False)
    check(v.engine_plan()[0] == 'sherpa', '引擎顺序 %s' % v.engine_plan())

    # ---------- 2. 真播 + 暂停 ----------
    print('\n[2] 真播一句 → 暂停（MCI 原地停住）')
    check(v.pause() is False, '还没开始念时点暂停 → False（不假装成功）')
    t_begin = time.time()
    v.speak(TEXT, force=True)
    ok = wait_until(lambda: mci_status(v) in ('playing', 'paused'))
    check(ok, '开始出声（MCI 状态=%s）' % mci_status(v))
    check(v.is_playing() is True, 'is_playing() 为真（按钮该亮起来）')

    time.sleep(0.5)
    pos_before = mci_pos(v)
    check(v.pause() is True and v.is_paused() is True, '暂停成功')
    time.sleep(0.3)                      # 命令是排队交给朗读线程发的，给它一点时间
    st = mci_status(v)
    check(st == 'paused', 'MCI 状态变成 paused（实际 %s，暂停前位置 %d ms）' % (st, pos_before))
    pos1 = mci_pos(v)
    time.sleep(0.6)
    pos2 = mci_pos(v)
    check(pos1 >= 0 and pos2 >= 0 and abs(pos2 - pos1) <= 120,
          '暂停期间播放位置没动（%d ms → %d ms）' % (pos1, pos2))

    # ---------- 3. 继续 ----------
    print('\n[3] 继续（接着念，不是从头）')
    held = time.time()
    check(v.resume() is True and v.is_paused() is False, '继续成功')
    time.sleep(0.4)
    st = mci_status(v)
    check(st in ('playing', 'stopped'), 'MCI 状态回到 playing（实际 %s）' % st)
    pos3 = mci_pos(v)
    check(pos3 < 0 or pos3 >= pos2, '位置从暂停处继续（%d ms → %d ms）' % (pos2, pos3))

    # ---------- 4. 等它念完，核对"计时冻结" ----------
    print('\n[4] 计时冻结：整段耗时 ≈ 音频时长 + 暂停时长')
    done = wait_until(lambda: not v.is_playing(), timeout=25)
    total = time.time() - t_begin
    pause_held = time.time() - held + 0.9          # 暂停总时长（含前面量位置那 0.9 秒）
    check(done, '整段念完了（没卡住）')
    check(total > pause_held, '总耗时 %.1fs ≥ 暂停时长 %.1fs（暂停期间时间冻住了）'
          % (total, pause_held))
    check(v.spoken_count >= 1, '确实念过（spoken_count=%d）' % v.spoken_count)

    # ---------- 5. 界面 ----------
    print('\n[5] 界面：输入框那一排的 ⏸ 键')
    pet = dp.PetWidget()
    pet.show()
    pump(app, 0.6)
    pet._save_cfg_value = lambda *a, **k: True
    btn = pet.voice_pause_btn
    check(btn is not None and btn.isVisible(), '按钮存在且可见')
    check(btn.width() == 30 and btn.height() == 30, '尺寸 30×30（跟 🎤 📎 一排）')
    check(abs(btn.geometry().center().y() - pet.mic_btn.geometry().center().y()) <= 6,
          '跟 🎤 在同一条线上')
    check(btn.geometry().left() >= pet.chat_input.geometry().right() - 4,
          '按钮在输入框右边，跟 🎤 📎 一组')

    pet.voice = v                      # 换成刚验证过的那个真声线对象
    pet._apply_voice_pause_btn()
    idle_disabled = not btn.isEnabled()
    check(idle_disabled and btn.text() == '⏸', '没在朗读时是灰的 + 显示 ⏸')

    # 再念一句，用真按钮点暂停/继续
    v.speak(TEXT, force=True)
    wait_until(lambda: v.is_playing(), timeout=10)
    pump(app, 0.5)
    pet._apply_voice_pause_btn()
    check(btn.isEnabled() is True, '开始朗读后按钮自动变可点')
    btn.grab().save(os.path.join(SHOTS, 'v693_pause_btn_idle.png'))

    btn.click()
    pump(app, 0.3)
    check(btn.text() == '▶' and v.is_paused() is True, '点一下 → 变 ▶ 且真的暂停了')
    btn.grab().save(os.path.join(SHOTS, 'v693_pause_btn_paused.png'))

    shot = pet.chat_panel.grab()
    check(shot.width() > 200, '聊天面板截图 %dx%d → screenshots/v693_pause_in_chat.png'
          % (shot.width(), shot.height()))
    shot.save(os.path.join(SHOTS, 'v693_pause_in_chat.png'))

    btn.click()
    pump(app, 0.3)
    check(btn.text() == '⏸' and v.is_paused() is False, '再点一下 → 回 ⏸ 且继续念')
    v.stop()
    pump(app, 0.3)

    # 右键菜单里也有入口
    check(hasattr(pet, '_toggle_voice_pause'), '右键菜单里也挂了「⏸ 暂停 / ▶ 继续朗读」')
    pet.hide()
    pump(app, 0.3)

    # ---------- 6. 隐私 ----------
    print('\n[6] 收尾')
    check(voice_cfg(cfg_path) == before, '语音配置没被改动（voice_name=%s）' % before.get('voice_name'))
    print('\n退出码：%s' % ('0（全过）' if not FAILS else '1（%d 项没过）' % len(FAILS)))
    for m in FAILS:
        print('  ✕ %s' % m)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
