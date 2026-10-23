# -*- coding: utf-8 -*-
"""朗读暂停 / 继续（v6.93）

使用者要求：「加一个朗读暂停按钮」。

这里守四件事：
1. **真暂停**：当前这一句停在原处（MCI `pause`），不是"停掉再从头念"；
2. **计时冻结**：暂停期间时间不走，恢复后正好接着念完剩下的时长（不需要重新合成）；
3. **不假装**：没在朗读时点暂停 → 返回 False，界面提示「现在没有在朗读」，而不是按钮假装成功；
4. **按钮真在**：输入框那一排有 ⏸ 键、在输入框左边、没在念时是灰的、点了会变成 ▶。

为什么要换播放器：标准库 `winsound` 只能"播 / 停"，**没有暂停续播能力**，
所以 WAV 也从 winsound 改成 MCI（`type waveaudio`）—— 这是暂停键能成立的前提。
"""
import os
import sys
import threading
import time
import wave

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

import voice_io as vio      # noqa: E402


def _silent_wav(path, seconds=0.2, rate=16000):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x00\x00' * int(rate * seconds))
    return str(path)


def _voice(tmp_path, **kw):
    return vio.VoiceIO(base_dir=str(tmp_path), **kw)


# ---------------------------------------------------------------- 计时冻结

def test_wait_sentence_runs_full_length(tmp_path):
    """没有暂停时，等一句话就是等 dur 秒（这里验逻辑，不等真的音频）"""
    v = _voice(tmp_path)
    t0 = time.time()
    assert v._wait_sentence(0.2, time.time()) is True
    assert 0.15 <= time.time() - t0 < 1.0


def test_wait_sentence_frozen_while_paused(tmp_path, monkeypatch):
    """★ 核心：暂停期间计时不走 —— 恢复后还要等完剩下的时长"""
    v = _voice(tmp_path)
    v._playing = True
    monkeypatch.setattr(vio, '_mci', lambda cmd: (True, ''))    # 别真去动播放器
    t_start = time.time()
    done = {}
    th = threading.Thread(target=lambda: done.setdefault('ok', v._wait_sentence(0.35, t_start)))
    th.start()
    time.sleep(0.1)
    assert v.pause() is True and v.is_paused() is True
    held = time.time()
    time.sleep(0.5)                        # 暂停超过本句剩余时长
    assert 'ok' not in done, '暂停期间不该"念完"——时间必须冻住'
    assert v.resume() is True and v.is_paused() is False
    th.join(timeout=3)
    assert done.get('ok') is True
    assert time.time() - held > 0.15, '恢复后要接着等剩下的时长，而不是立刻算播完'


def test_stop_breaks_the_wait(tmp_path):
    """stop() 要能立刻打断等待（暂停状态也算）——返回 False 让 runner 掐断"""
    v = _voice(tmp_path)
    v._playing = True
    done = {}
    th = threading.Thread(target=lambda: done.setdefault('ok', v._wait_sentence(5.0, time.time())))
    th.start()
    time.sleep(0.1)
    v.stop()
    th.join(timeout=3)
    assert done.get('ok') is False


# ---------------------------------------------------------------- 不假装

def test_pause_returns_false_when_not_speaking(tmp_path):
    v = _voice(tmp_path)
    assert v.is_playing() is False
    assert v.pause() is False, '没在朗读时说"暂停成功"就是骗人'
    assert v.is_paused() is False


def test_resume_without_pause_returns_false(tmp_path):
    v = _voice(tmp_path)
    assert v.resume() is False


def test_toggle_pause_flips_both_ways(tmp_path, monkeypatch):
    v = _voice(tmp_path)
    v._playing = True
    monkeypatch.setattr(vio, '_mci', lambda cmd: (True, ''))
    assert v.toggle_pause() is True and v.is_paused() is True
    assert v.toggle_pause() is True and v.is_paused() is False


def test_pause_command_runs_on_player_thread(tmp_path, monkeypatch):
    """MCI 句柄是**线程私有**的（实测：主线程发 pause 返回失败）——界面点的暂停
    必须排队交给朗读线程去发，否则按钮亮着、声音照放。"""
    v = _voice(tmp_path)
    v._q.put(None)                    # 让内置朗读线程退出，这段由测试自己当"播放线程"
    time.sleep(0.4)
    v._playing = True
    seen = []
    monkeypatch.setattr(vio, '_mci', lambda cmd: (seen.append(cmd) or (True, 'playing')))
    assert v.pause() is True
    assert seen == [], '主线程不许直接发 MCI 命令（发了也不生效）'
    v._drain_ctl()
    assert seen and seen[0].startswith('pause'), '排队到朗读线程后才真正发 pause'


def test_mci_query_runs_on_player_thread(tmp_path, monkeypatch):
    """验证脚本靠它在朗读线程里查真实播放状态（playing/paused/position）"""
    v = _voice(tmp_path)
    v._q.put(None)
    time.sleep(0.4)
    monkeypatch.setattr(vio, '_mci', lambda cmd: (True, 'paused'))

    def player():
        t0 = time.time()
        while time.time() - t0 < 3:
            v._drain_ctl()
            time.sleep(0.02)

    th = threading.Thread(target=player, daemon=True)
    th.start()
    assert v.mci_query('status %s mode' % vio.MCI_ALIAS) == (True, 'paused')
    th.join(timeout=3)


def test_stop_clears_paused(tmp_path, monkeypatch):
    v = _voice(tmp_path)
    v._playing = True
    monkeypatch.setattr(vio, '_mci', lambda cmd: (True, ''))
    v.pause()
    assert v.is_paused() is True
    v.stop()
    assert v.is_paused() is False, '停止之后不能还挂在"暂停"态'


# ---------------------------------------------------------------- 播放器能力

def test_wav_playback_uses_mci_so_it_can_pause(tmp_path):
    """WAV 必须走 MCI —— winsound 停得下来但暂停不了，加了暂停键就只能换播放器"""
    v = _voice(tmp_path)
    path = _silent_wav(tmp_path / 'a.wav', 0.15)
    dur = v._play_wav(path)
    assert dur > 0.1
    assert v._played_by == 'mci', 'WAV 应该由 MCI 播（否则暂停只能掐断）'
    v._purge()


def test_pause_falls_back_gracefully_when_not_mci(tmp_path, monkeypatch):
    """极少数打不开 MCI 的 wav 由 winsound 播：暂停退化成"掐断这一句"，但不报错、不丢队列"""
    v = _voice(tmp_path)
    v._playing = True
    v._played_by = 'winsound'
    called = {}
    monkeypatch.setattr(v, '_purge', lambda: called.setdefault('purged', True))
    monkeypatch.setattr(vio, '_mci', lambda cmd: (False, ''))     # 假装 MCI 也发不出去
    assert v.pause() is True and v.is_paused() is True
    v._drain_ctl()                       # 朗读线程执行排队来的 pause → 发现没 MCI 就掐断
    assert called.get('purged') is True


# ---------------------------------------------------------------- 界面

@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    a = QApplication.instance() or QApplication(sys.argv[:1])
    yield a


def test_pause_button_lives_in_chat_input_row(app):
    """按钮得长在输入框那一排（跟 🎤 📎 🔒 一起），平时是灰的，暂停后变 ▶"""
    import desktop_pet as dp
    pet = dp.PetWidget()
    pet.show()
    try:
        from PySide6.QtWidgets import QApplication
        pet._apply_chat_fold(False)        # v6.77 折叠：正文默认可能收着，先展开再判定
        pet.chat_panel.show()
        QApplication.processEvents()
        btn = pet.voice_pause_btn
        assert btn is not None
        assert btn.isVisible(), '按钮得在显示出来的输入行里'
        assert btn.width() == 30 and btn.height() == 30
        assert btn.text() == '⏸'
        assert btn.isEnabled() is False, '没在朗读时应该是灰的（别让人点了没反应）'
        geoms = {'btn': btn.geometry(), 'input': pet.chat_input.geometry(),
                 'mic': pet.mic_btn.geometry()}
        assert geoms['btn'].left() >= geoms['input'].right() - 4, '按钮要在输入框那一排（右边跟 🎤 📎 一组）'
        assert abs(geoms['btn'].center().y() - geoms['mic'].center().y()) <= 6, '要跟 🎤 在同一条线上'

        class _FakeVoice:
            def __init__(self):
                self.paused = False
                self.playing = True

            def is_paused(self):
                return self.paused

            def is_playing(self):
                return self.playing

            def pause(self):
                self.paused = True
                return True

            def resume(self):
                self.paused = False
                return True

        fake = _FakeVoice()
        pet.voice = fake
        pet._toggle_voice_pause()
        pet._apply_voice_pause_btn()
        assert fake.paused is True and btn.text() == '▶' and btn.isEnabled()
        pet._toggle_voice_pause()
        pet._apply_voice_pause_btn()
        assert fake.paused is False and btn.text() == '⏸'

        fake.playing = False                    # 念完了 → 自己灰掉
        pet._apply_voice_pause_btn()
        assert btn.isEnabled() is False and '没有在朗读' in btn.toolTip()
    finally:
        pet.hide()
        pet.deleteLater()
