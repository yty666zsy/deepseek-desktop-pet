# -*- coding: utf-8 -*-
"""本地预训练声线实机验证（v6.92）

用法：.venv\\Scripts\\python.exe tools\\verify_voices.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：离屏后端中文字体全是豆腐块，
「本地 · 花尘 C · 187 声线 #12」这类条目在截图里根本认不出来。

验的东西：
1. 引擎真在：sherpa-onnx 能 import，版本号打得出来
2. 清单完整：8 个免费中文模型 / 2000+ 说话人 / 总体积（这就是「能找的全部」）
3. 每个已装模型**真加载**（OfflineTts 起来，报耗时）
4. 每个已装模型**真合成**：一句中文真变成 wav（报采样率/时长/字节）
5. 同一个模型换说话人 → 音频字节**不一样**（证明真在换音色，不是换了个标签）
6. 模型坏了不哑：越界说话人号 → 自动降级 edge/offline 出声，且标记 fell_back
7. 下拉列表里真的出现本地条目（'sherpa:模型/号'），且不会爆成几千条
8. 界面出图：设置·语音页 / 「🎚 本地声线库」对话框 / 搜索过滤
9. 隐私：跑完用户**语音相关配置键**没变

   注意：桌宠如果在后台跑着，它会顺手往 config.json 写用量/状态，**整文件哈希会变**（不是我们改的），
   所以这里只核对语音那几个键。
"""
import hashlib
import os
import sys
import tempfile
import time
import wave

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402

import desktop_pet as dp                     # noqa: E402
import pet_voicelib as vlib                  # noqa: E402
import voice_io                              # noqa: E402

OUT = os.path.join(tempfile.gettempdir(), 'pet_voice_verify')
os.makedirs(OUT, exist_ok=True)
SENTENCE = '你好呀，这是本地预训练声线合成的验证语音。'

FAILS = []


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


def sha(path):
    if not os.path.isfile(path):
        return ''
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def wav_info(path):
    """(采样率, 秒, 字节)；读不了就返回 (0, 0, 大小)"""
    try:
        with wave.open(path, 'rb') as w:
            sr = w.getframerate()
            frames = w.getnframes()
        return sr, round(frames / float(sr or 1), 2), os.path.getsize(path)
    except Exception:
        return 0, 0.0, os.path.getsize(path) if os.path.isfile(path) else 0


def voice_cfg(path):
    """只取语音相关的配置键（桌宠在后台跑时会往 config.json 写用量统计，整文件哈希不可靠）"""
    try:
        import json
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return {}
    keys = ('voice_name', 'voice_rate', 'voice_enabled', 'voice_content_mode',
            'voice_night_quiet', 'voice_local_url', 'voice_local_ref', 'voice_local_prompt')
    return {k: cfg.get(k) for k in keys}


def main():
    app = QApplication(sys.argv[:1])

    print('[0] 隐私：先备份 config.json')
    cfg_path = os.path.join(BASE, 'config.json')
    cfg_hash = sha(cfg_path)
    cfg_voice = voice_cfg(cfg_path)
    print('    config.json 存在=%s sha256=%s 语音键=%s'
          % (os.path.isfile(cfg_path), (cfg_hash or '')[:12], cfg_voice.get('voice_name')))

    # ---------- 1. 引擎 ----------
    print('\n[1] 推理引擎')
    try:
        import sherpa_onnx
        ver = getattr(sherpa_onnx, '__version__', '') or 'ok'
        check(True, 'sherpa-onnx 能用（%s）' % ver)
    except Exception as e:                                       # noqa: BLE001
        check(False, 'sherpa-onnx 装不上：%s' % e)
        return 1

    # ---------- 2. 清单 ----------
    print('\n[2] 清单（免费中文预训练模型）')
    total_spk = sum(vlib.speakers(m['key']) for m in vlib.MODELS)
    for m in vlib.MODELS:
        got = '✔ 已装' if vlib.is_installed(m['key']) else '· 未下载'
        print('    %-2s %-12s %-24s %4d 声线  %4d MB  %s'
              % (got[:1], m['key'], m['title'], m['speakers'], m['mb'], m['note']))
    check(len(vlib.MODELS) >= 8, '清单里 %d 个模型' % len(vlib.MODELS))
    check(total_spk >= 2000, '合计 %d 个说话人（全部免费开源）' % total_spk)
    got = vlib.installed()
    check(bool(got), '已下载 %d/%d 个（%s）' % (len(got), len(vlib.MODELS), vlib.summary()))

    # ---------- 3+4. 真加载 + 真合成 ----------
    print('\n[3] 真加载 + 真合成（一句中文 → wav）')
    made = {}
    for key in got:
        t0 = time.time()
        try:
            vlib.get_tts(key)
        except Exception as e:                                   # noqa: BLE001
            check(False, '%s 加载失败：%s' % (key, str(e)[:90]))
            continue
        load_s = time.time() - t0
        spk = vlib.representative_speakers(key, 1)[0]
        path = os.path.join(OUT, '%s_%d.wav' % (key, spk))
        t1 = time.time()
        ok, err = vlib.synth(key, spk, SENTENCE, path, speed=1.0)
        synth_s = time.time() - t1
        sr, sec, size = wav_info(path)
        check(ok and sec > 0.4,
              '%-11s 加载 %.2fs / 合成 %.2fs → %.1f kHz %.2fs %d KB（%s #%d）'
              % (key, load_s, synth_s, sr / 1000.0, sec, size // 1024, key, spk))
        if not ok:
            print('        错误：%s' % err)
        else:
            made[key] = path

    # ---------- 5. 换说话人真的换音色 ----------
    print('\n[5] 换说话人 = 真换音色（同一句话，不同说话人）')
    multi = next((k for k in got if vlib.speakers(k) >= 5), '')
    if multi:
        a = os.path.join(OUT, '%s_spk0.wav' % multi)
        b = os.path.join(OUT, '%s_spk4.wav' % multi)
        ok1, _ = vlib.synth(multi, 0, SENTENCE, a)
        ok2, _ = vlib.synth(multi, 4, SENTENCE, b)
        ha, hb = sha(a), sha(b)
        check(ok1 and ok2 and ha != hb,
              '%s：#0 与 #4 音频不同（%s… vs %s…）' % (multi, ha[:8], hb[:8]))
    else:
        print('    （没有多说话人模型可测，跳过）')

    # ---------- 6. 坏模型不能把桌宠念哑 ----------
    print('\n[6] 降级：本地模型没了也不能哑（模型被删 / 没下载 / 文件损坏都走这条路）')
    real_root = vlib.ROOT
    vlib.ROOT = os.path.join(OUT, 'empty_root')
    os.makedirs(vlib.ROOT, exist_ok=True)          # 空目录 = 一个模型都没装
    vlib.drop_cache()
    try:
        v = voice_io.VoiceIO(base_dir=BASE, voice='sherpa:theresa/0')
        check(v.engine_plan() == ['sherpa', 'edge', 'offline'],
              '引擎顺序 %s（本地 → 在线 → 离线）' % v.engine_plan())
        p, kind, engine = v._synth_any('降级验证这一句。', os.path.join(OUT, 'fallback'))
        check(bool(p) and engine in ('edge', 'offline'),
              '本地模型不在 → 实际用 %s 出声（%s）' % (engine or '无', os.path.basename(p or '')))
        check(v.fell_back is True and '模型' in (v.last_error or ''),
              '标了 fell_back 并说明原因（%s）' % (v.last_error or '无'))
    finally:
        vlib.ROOT = real_root
        vlib.drop_cache()

    # ---------- 7+8. 界面 ----------
    print('\n[7] 宿主下拉 + 面板')
    pet = dp.PetWidget()
    pet.show()
    pump(app, 0.8)
    pet._save_cfg_value = lambda *a, **k: True        # 别把验证状态写进用户配置

    choices = pet._voice_choices()
    local = [c for c in choices if str(c[0]).startswith('sherpa:')]
    check(bool(local), '下拉里有 %d 条本地声线（总 %d 条）' % (len(local), len(choices)))
    now_installed = len(vlib.installed())
    cap = now_installed * vlib.REPRESENTATIVES + len(voice_io.VOICE_CHOICES)
    check(len(choices) <= cap,
          '下拉没有爆量（%d ≤ 上限 %d，%d 个模型）' % (len(choices), cap, now_installed))
    check(all(len(c) == 3 for c in choices), '条目格式统一（值 / 显示名 / 引擎）')

    from settings_ui import SettingsDialog, PAGES
    dlg = SettingsDialog(pet, dp.MODEL_REGISTRY, parent=pet)
    dlg.show()
    dlg.nav.setCurrentRow(list(PAGES).index('语音'))
    pump(app, 0.7)
    pm = dlg.grab()
    pm.save(os.path.join(SHOTS, 'v692_voice_page.png'))
    check(pm.width() > 400, '设置·语音页截图 %dx%d → screenshots/v692_voice_page.png'
          % (pm.width(), pm.height()))
    rows = dlg.stack.widget(list(PAGES).index('语音')).form.rowCount()
    check(rows <= 14, '语音页仍是 %d 行（没挤成一长条）' % rows)
    n_cb = dlg.cb_voice_name.count()
    check(n_cb == len(choices), '下拉框装了全部 %d 条（实际 %d）' % (len(choices), n_cb))

    import pet_voice_ui
    lib = pet_voice_ui.VoiceLibraryDialog(pet, parent=pet)
    lib.show()
    pump(app, 0.7)
    check(lib.lst_models.count() == len(vlib.MODELS),
          '本地声线库列出全部 %d 个模型' % lib.lst_models.count())
    # 选中第一个已装模型 → 右栏该有它的全部说话人
    for i in range(lib.lst_models.count()):
        if vlib.is_installed(lib.lst_models.item(i).data(0x0100)):
            lib.lst_models.setCurrentRow(i)
            break
    pump(app, 0.4)
    want = vlib.speakers(lib._key)
    check(lib.lst_speakers.count() == want,
          '%s 列出全部 %d 个说话人（不是只给代表）' % (lib._key, want))
    lib.grab().save(os.path.join(SHOTS, 'v692_voice_library.png'))
    print('    已存 screenshots/v692_voice_library.png')

    # 搜索过滤：抽一个靠后的说话人号
    pick = min(want - 1, 12)
    lib.ed_search.setText(str(pick))
    pump(app, 0.4)
    ok = lib.lst_speakers.count() == 1 and lib.lst_speakers.item(0).text() == '#%d' % pick
    check(ok, '搜索「%d」只剩 1 条（实际 %d 条）' % (pick, lib.lst_speakers.count()))
    lib.grab().save(os.path.join(SHOTS, 'v692_voice_search.png'))
    print('    已存 screenshots/v692_voice_search.png')

    # 双击 = 应用（真的写去 set_voice_name，但配置写入已被短路）
    applied = {}
    pet._set_voice_name = lambda name: applied.setdefault('name', name)
    lib._apply()
    want_spec = vlib.spec(lib._key, pick)
    check(applied.get('name') == want_spec,
          '「应用」把 %s 交给宿主（实际 %s）' % (want_spec, applied.get('name')))

    lib.close()
    dlg.close()
    pump(app, 0.3)
    pet.hide()
    pump(app, 0.3)

    # ---------- 9. 隐私 ----------
    print('\n[8] 收尾')
    check(voice_cfg(cfg_path) == cfg_voice,
          '用户语音配置没被改动（voice_name=%s）' % cfg_voice.get('voice_name'))
    check(os.path.isdir(vlib.ROOT), '模型目录在：%s（%s）' % (vlib.ROOT, vlib.summary()))

    print('\n合成出来的 wav 都在：%s' % OUT)
    print('\n退出码：%s' % ('0（全过）' if not FAILS else '1（%d 项没过）' % len(FAILS)))
    for m in FAILS:
        print('  ✕ %s' % m)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
