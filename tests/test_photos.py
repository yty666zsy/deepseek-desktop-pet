# -*- coding: utf-8 -*-
"""见面照片墙（v6.88）：数据层 + 布局算法 + 界面

这里锁的是「内核算得对 + 界面画得出来」，不联网、不动用户真实数据：
1. 日期归一（2026/10/21、2026年10月21日、EXIF 的 2026:10:21 …都认）与「差几天」
2. 照片导入：**复制**进 photos/<日期>/、按拍摄日期自动分组、重复照片不重复进墙
3. 缩略图缓存：同文件同尺寸复用一份；原图改了自动换新（键含修改时间）
4. justified 行布局：一行等高、宽度铺满、末行不把一张图拉变形
5. 见面记录：增删改查、坏 json 不炸、删见面连带删副本
6. 统计与倒计时：距上次见面 / 距下次见面（含「日子填在未来」这种）
7. 界面：墙面（分组标题 + 照片命中区）、拍立得、灯箱翻页、桌宠小卡片
8. 桌宠接线：标题栏 📷 + 右键「📷 见面照片墙…」+ 导入后写回忆（type='meeting'）并加好感

运行：python -m pytest tests/test_photos.py -q
"""
import json
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def make_jpg(path, size=(800, 600), color=(60, 120, 200), exif_date=''):
    """造一张真照片（可选写 EXIF 拍摄日期）"""
    from PIL import Image
    im = Image.new('RGB', size, color)
    if exif_date:
        exif = Image.Exif()
        exif[36867] = exif_date
        im.save(path, exif=exif)
    else:
        im.save(path)
    return str(path)


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """配置写到临时目录，别把真 config.json 写脏"""
    import pet_photos as ph
    monkeypatch.setattr(ph, 'CONFIG_PATH', str(tmp_path / 'config.json'), raising=False)
    yield


@pytest.fixture
def lib(tmp_path):
    import pet_photos as ph
    return ph.PhotoLibrary(base_dir=str(tmp_path / 'lib'))


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


# --------------------------------------------------------------------------
# 1. 日期
# --------------------------------------------------------------------------
def test_norm_date_accepts_common_shapes():
    import pet_photos as ph
    assert ph.norm_date('2026-10-21') == '2026-10-21'
    assert ph.norm_date('2026/1/2') == '2026-01-02'
    assert ph.norm_date('2026年10月21日') == '2026-10-21'
    assert ph.norm_date('20261021') == '2026-10-21'
    assert ph.norm_date('2026:10:21 18:30:00') == '2026-10-21'


def test_norm_date_rejects_garbage():
    import pet_photos as ph
    assert ph.norm_date('') == ''
    assert ph.norm_date('乱写的') == ''
    assert ph.norm_date('2026-13-40') == ''
    assert ph.norm_date('乱写的', 'x') == 'x'


def test_days_between():
    import pet_photos as ph
    assert ph.days_between('2026-10-21', '2026-10-25') == 4
    assert ph.days_between('2026-10-25', '2026-10-21') == -4
    assert ph.days_between('2026-10-21', '2026-10-21') == 0
    assert ph.days_between('', '2026-10-21') is None


def test_pretty_date_has_weekday():
    import pet_photos as ph
    assert ph.pretty_date('2026-10-21') == '2026年10月21日 周三'
    assert ph.pretty_date('') == ''


def test_weekday_cn():
    import pet_photos as ph
    assert ph.weekday_cn('2026-10-19') == '周一'
    assert ph.weekday_cn('2026-10-25') == '周日'
    assert ph.weekday_cn('xx') == ''


# --------------------------------------------------------------------------
# 2. 图片基本信息 / EXIF
# --------------------------------------------------------------------------
def test_ext_and_is_image():
    import pet_photos as ph
    assert ph.ext_of('a/b/c.JPG') == 'jpg'
    assert ph.is_image('x.HEIC') is True
    assert ph.is_image('x.mp4') is False
    assert ph.is_image('') is False


def test_probe_reads_real_size(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'a.jpg', (321, 123))
    assert ph.probe(p) == (321, 123)
    assert ph.probe(str(tmp_path / 'nope.jpg')) == (0, 0)


def test_exif_date(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'e.jpg', exif_date='2026:10:21 18:30:00')
    assert ph.exif_date(p) == '2026-10-21'


def test_exif_date_missing_returns_empty(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'ne.jpg')
    assert ph.exif_date(p) == ''
    assert ph.exif_date(str(tmp_path / 'ghost.jpg')) == ''


def test_photo_date_prefers_exif_then_mtime(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'x.jpg', exif_date='2020:01:02 03:04:05')
    assert ph.photo_date(p) == '2020-01-02'
    q = make_jpg(tmp_path / 'y.jpg')
    os.utime(q, (time.mktime(time.strptime('2021-05-06', '%Y-%m-%d')),) * 2)
    assert ph.file_date(q) == '2021-05-06'
    assert ph.photo_date(q) == '2021-05-06'


def test_group_by_date_splits_and_sorts(tmp_path):
    import pet_photos as ph
    a = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:22 10:00:00')
    b = make_jpg(tmp_path / 'b.jpg', exif_date='2026:10:20 10:00:00')
    c = make_jpg(tmp_path / 'c.jpg', exif_date='2026:10:22 20:00:00')
    groups = ph.group_by_date([a, b, c, str(tmp_path / 'not-image.txt')])
    assert [d for d, _v in groups] == ['2026-10-20', '2026-10-22']
    assert len(groups[1][1]) == 2


# --------------------------------------------------------------------------
# 3. 哈希 / 缩略图 / 路径
# --------------------------------------------------------------------------
def test_sha1_file(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'a.jpg')
    assert len(ph.sha1_file(p)) == 40
    assert ph.sha1_file(p) == ph.sha1_file(p)
    assert ph.sha1_file(str(tmp_path / 'nope')) == ''


def test_thumb_cache_reuses_and_invalidates(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'a.jpg')
    tdir = str(tmp_path / 'thumbs')
    t1 = ph.ensure_thumb(p, 256, tdir)
    assert t1 and os.path.isfile(t1)
    size1 = os.path.getsize(t1)
    assert ph.ensure_thumb(p, 256, tdir) == t1        # 第二次复用同一个文件
    assert os.path.getsize(t1) == size1
    # 换个尺寸 → 另一个缓存文件
    assert ph.ensure_thumb(p, 128, tdir) != t1
    # 原图改了（修改时间变）→ 缓存键变，不会拿旧图骗人
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert ph.thumb_path(p, 256, tdir) != t1


def test_ensure_thumb_missing_source(tmp_path):
    import pet_photos as ph
    assert ph.ensure_thumb(str(tmp_path / 'ghost.jpg'), 256, str(tmp_path / 't')) == ''


def test_clear_thumbs(tmp_path):
    import pet_photos as ph
    p = make_jpg(tmp_path / 'a.jpg')
    tdir = str(tmp_path / 'thumbs')
    assert ph.ensure_thumb(p, 256, tdir)
    assert ph.clear_thumbs(tdir) == 1
    assert os.listdir(tdir) == []


def test_rel_and_abs_roundtrip(tmp_path):
    import pet_photos as ph
    d = tmp_path / 'photos' / '2026-10-21'
    d.mkdir(parents=True)
    p = make_jpg(d / 'a.jpg')
    rel = ph.rel_to_base(p, str(tmp_path))
    assert rel == 'photos/2026-10-21/a.jpg'
    assert os.path.abspath(ph.abs_of(rel, str(tmp_path))) == os.path.abspath(p)


# --------------------------------------------------------------------------
# 4. justified 布局
# --------------------------------------------------------------------------
def test_justified_rows_fills_width_and_equal_height():
    import pet_photos as ph
    sizes = [(4000, 3000), (3000, 4000), (1920, 1080), (800, 1200)]
    lay = ph.justified_rows(sizes, 900, 190, 8)
    assert lay['rows'], '至少得排出一行'
    for row in lay['rows'][:-1]:                       # 除末行：宽度正好铺满
        total = sum(it['w'] for it in row['items']) + 8 * (len(row['items']) - 1)
        assert abs(total - 900) <= 1, total
    for row in lay['rows']:
        assert len({it['h'] for it in row['items']}) == 1, '同一行必须等高'
        assert row['h'] > 0


def test_justified_rows_single_photo_not_stretched():
    """一张 4:3 的照片不该被拉成整屏宽（v6.88 实测踩过：末行硬撑满 → 变形）"""
    import pet_photos as ph
    it = ph.justified_rows([(4000, 3000)], 900, 190, 8)['rows'][0]['items'][0]
    assert it['h'] == 190
    assert abs(it['w'] - 190 * 4 / 3) <= 2


def test_justified_rows_keeps_aspect_ratio():
    import pet_photos as ph
    lay = ph.justified_rows([(1000, 1000), (2000, 1000)], 600, 150, 0)
    for row in lay['rows']:
        for it in row['items']:
            src = [(1000, 1000), (2000, 1000)][it['i']]
            assert abs((it['w'] / it['h']) - (src[0] / src[1])) < 0.05


def test_justified_rows_empty_and_broken_sizes():
    import pet_photos as ph
    assert ph.justified_rows([], 900) == {'rows': [], 'height': 0}
    lay = ph.justified_rows([(0, 0), (None, 5), ('x', 2)], 500, 120, 6)
    assert lay['rows'], '坏尺寸也要排出来，不能崩'


def test_justified_rows_no_overlap():
    import pet_photos as ph
    lay = ph.justified_rows([(800, 600)] * 9, 700, 160, 8)
    ys = [(r['y'], r['y'] + r['h']) for r in lay['rows']]
    for (y1, e1), (y2, e2) in zip(ys, ys[1:]):
        assert y2 >= e1, '行不能压行'
    assert lay['height'] >= ys[-1][1]


def test_justified_rows_row_height_near_target():
    import pet_photos as ph
    sizes = [(1200, 800)] * 12
    lay = ph.justified_rows(sizes, 900, 180, 8)
    assert all(120 <= r['h'] <= 400 for r in lay['rows']), [r['h'] for r in lay['rows']]


# --------------------------------------------------------------------------
# 5. 见面记录
# --------------------------------------------------------------------------
def test_create_meeting_defaults(lib):
    import pet_photos as ph
    m = lib.create_meeting('2026-10-21')
    assert m['date'] == '2026-10-21'
    assert m['title'] == '第 1 次见面'
    assert lib.count() == 1
    assert lib.get(m['id'])['id'] == m['id']


def test_create_meeting_ids_never_collide(lib):
    m1 = lib.create_meeting('2026-10-21')
    m2 = lib.create_meeting('2026-10-21')
    assert m1['id'] != m2['id']
    assert lib.count() == 2


def test_update_meeting_normalizes_date(lib):
    m = lib.create_meeting('2026-10-21')
    got = lib.update_meeting(m['id'], date='2026/11/1', title='一起看电影', place='影院')
    assert got['date'] == '2026-11-01'
    assert got['title'] == '一起看电影'
    assert got['place'] == '影院'
    assert lib.get(m['id'])['note'] == ''


def test_update_missing_meeting_returns_none(lib):
    assert lib.update_meeting('nope', title='x') is None


def test_meetings_sorted_newest_first(lib):
    lib.create_meeting('2026-01-01')
    lib.create_meeting('2026-10-21')
    lib.create_meeting('2026-05-05')
    assert [m['date'] for m in lib.meetings()] == ['2026-10-21', '2026-05-05', '2026-01-01']


def test_bad_json_tolerated(tmp_path):
    import pet_photos as ph
    base = tmp_path / 'lib'
    base.mkdir()
    (base / 'meetings.json').write_text('{ 这不是 json', encoding='utf-8')
    lib = ph.PhotoLibrary(base_dir=str(base))
    assert lib.count() == 0
    lib.create_meeting('2026-10-21')
    assert lib.count() == 1, '坏文件清掉后应能正常重写'


def test_non_list_json_tolerated(tmp_path):
    import pet_photos as ph
    base = tmp_path / 'lib'
    base.mkdir()
    (base / 'meetings.json').write_text('{"a": 1}', encoding='utf-8')
    assert ph.PhotoLibrary(base_dir=str(base)).count() == 0


def test_meeting_photos_tolerates_junk():
    import pet_photos as ph
    assert ph.meeting_photos({}) == []
    assert ph.meeting_photos({'photos': None}) == []
    assert len(ph.meeting_photos({'photos': [{'file': 'a.jpg'}, 'junk', {}, None]})) == 1


def test_save_has_no_tmp_leftover(lib, tmp_path):
    lib.create_meeting('2026-10-21')
    names = os.listdir(os.path.dirname(lib.meetings_path))
    assert 'meetings.json' in names
    assert not [n for n in names if n.endswith('.tmp')]


# --------------------------------------------------------------------------
# 6. 导入
# --------------------------------------------------------------------------
def test_import_copies_into_date_folder(lib, tmp_path):
    import pet_photos as ph
    src = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:21 09:00:00')
    res = lib.import_paths([src])
    assert res['added'] == 1 and not res['skipped']
    m = lib.meetings()[0]
    assert m['date'] == '2026-10-21'
    rel = ph.meeting_photos(m)[0]['file']
    assert rel.startswith('photos/2026-10-21/'), rel
    saved = ph.abs_of(rel, lib.base_dir)
    assert os.path.isfile(saved), '照片必须复制进项目目录（原图被删也不裂）'
    assert os.path.abspath(saved) != os.path.abspath(src)
    assert ph.meeting_photos(m)[0]['w'] == 800


def test_import_dedupes_same_file(lib, tmp_path):
    src = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:21 09:00:00')
    assert lib.import_paths([src])['added'] == 1
    res2 = lib.import_paths([src])
    assert res2['added'] == 0 and res2['dups'] == 1
    assert lib.photo_count() == 1


def test_import_groups_by_day(lib, tmp_path):
    a = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:20 09:00:00')
    b = make_jpg(tmp_path / 'b.jpg', exif_date='2026:10:21 09:00:00')
    c = make_jpg(tmp_path / 'c.jpg', exif_date='2026:10:21 20:00:00')
    res = lib.import_paths([a, b, c])
    assert res['added'] == 3
    assert lib.count() == 2, '同一天的两张应归到同一次见面'
    assert lib.photo_count() == 3


def test_import_skips_non_image(lib, tmp_path):
    txt = tmp_path / 'note.txt'
    txt.write_text('hi', encoding='utf-8')
    res = lib.import_paths([str(txt)])
    assert res['added'] == 0
    assert res['skipped'], '得说明为什么没加'
    assert res['skipped'][0][1]


def test_import_empty_gives_error(lib):
    res = lib.import_paths([])
    assert res['added'] == 0 and res['error']


def test_import_folder_recursive(lib, tmp_path):
    import pet_photos as ph
    d = tmp_path / 'trip' / 'day1'
    d.mkdir(parents=True)
    make_jpg(d / 'x.jpg', exif_date='2026:10:21 09:00:00')
    make_jpg(tmp_path / 'trip' / 'y.jpg', exif_date='2026:10:21 10:00:00')
    assert len(ph.expand_image_paths([str(tmp_path / 'trip')])) == 2
    assert lib.import_paths([str(tmp_path / 'trip')])['added'] == 2


def test_import_fixed_date_overrides_exif(lib, tmp_path):
    src = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:21 09:00:00')
    lib.import_paths([src], date='2026-12-24')
    assert lib.meetings()[0]['date'] == '2026-12-24'


def test_add_photos_to_existing_meeting(lib, tmp_path):
    m = lib.create_meeting('2026-10-21')
    a = make_jpg(tmp_path / 'a.jpg')
    b = make_jpg(tmp_path / 'b.jpg', color=(1, 2, 3))
    res = lib.add_photos(m['id'], [a, b])
    assert res['added'] == 2
    assert len([p for p in lib.get(m['id'])['photos']]) == 2
    assert lib.count() == 1, '加照片不该新建见面'


def test_add_photos_missing_meeting(lib, tmp_path):
    res = lib.add_photos('nope', [make_jpg(tmp_path / 'a.jpg')])
    assert res['added'] == 0 and res['error']


def test_remove_photo_deletes_file(lib, tmp_path):
    import pet_photos as ph
    src = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:21 09:00:00')
    lib.import_paths([src])
    m = lib.meetings()[0]
    rel = ph.meeting_photos(m)[0]['file']
    assert lib.remove_photo(m['id'], rel, delete_file=True) is True
    assert ph.meeting_photos(lib.get(m['id'])) == []
    assert not os.path.isfile(ph.abs_of(rel, lib.base_dir))
    assert lib.remove_photo(m['id'], rel) is False, '重复移除应返回 False'


def test_delete_meeting_removes_copies(lib, tmp_path):
    import pet_photos as ph
    src = make_jpg(tmp_path / 'a.jpg', exif_date='2026:10-21 09:00:00'.replace('-', ':', 1))
    lib.import_paths([src])
    m = lib.meetings()[0]
    rel = ph.meeting_photos(m)[0]['file']
    assert lib.delete_meeting(m['id'], delete_files=True) is True
    assert lib.count() == 0
    assert not os.path.isfile(ph.abs_of(rel, lib.base_dir))
    assert lib.delete_meeting('nope') is False


def test_move_photo_between_meetings(lib, tmp_path):
    import pet_photos as ph
    a = lib.create_meeting('2026-10-20')
    b = lib.create_meeting('2026-10-21')
    lib.add_photos(a['id'], [make_jpg(tmp_path / 'x.jpg')])
    rel = ph.meeting_photos(lib.get(a['id']))[0]['file']
    assert lib.move_photo(rel, a['id'], b['id']) is True
    assert ph.meeting_photos(lib.get(a['id'])) == []
    assert len(ph.meeting_photos(lib.get(b['id']))) == 1


def test_search_finds_by_title_place_note(lib):
    m = lib.create_meeting('2026-10-21', title='看电影', place='万达', note='爆米花很甜')
    lib.create_meeting('2026-11-11', title='散步')
    assert [x['id'] for x in lib.search('电影')] == [m['id']]
    assert [x['id'] for x in lib.search('万达')] == [m['id']]
    assert [x['id'] for x in lib.search('爆米花')] == [m['id']]
    assert len(lib.search('')) == 2
    assert lib.search('不存在的东西') == []


def test_find_by_date(lib):
    m = lib.create_meeting('2026-10-21')
    assert lib.find_by_date('2026/10/21')['id'] == m['id']
    assert lib.find_by_date('2027-01-01') is None


# --------------------------------------------------------------------------
# 7. 统计 / 倒计时
# --------------------------------------------------------------------------
def test_stats_line_empty(lib):
    assert '拖进来' in lib.stats_line()


def test_stats_and_countdown(lib, monkeypatch):
    import pet_photos as ph
    lib.create_meeting('2026-10-01')
    lib.create_meeting('2026-10-20')
    ph.cfg_write(ph.CFG_NEXT_DATE, '2026-10-25')
    ph.cfg_write(ph.CFG_NEXT_NOTE, '去植物园')
    s = lib.stats(today='2026-10-21')
    assert s['meetings'] == 2
    assert s['first'] == '2026-10-01' and s['last'] == '2026-10-20'
    assert s['days_since_last'] == 1
    assert s['days_to_next'] == 4
    assert s['days_together'] == 20
    line = lib.stats_line(today='2026-10-21')
    assert '2 次见面' in line and '还有 4 天' in line


def test_stats_next_is_today(lib):
    import pet_photos as ph
    lib.create_meeting('2026-10-21')
    ph.cfg_write(ph.CFG_NEXT_DATE, '2026-10-21')
    assert '就是今天' in lib.stats_line(today='2026-10-21')


def test_stats_next_in_the_past(lib):
    import pet_photos as ph
    lib.create_meeting('2026-10-01')
    ph.cfg_write(ph.CFG_NEXT_DATE, '2026-10-10')
    assert '已过 11 天' in lib.stats_line(today='2026-10-21')


def test_stats_photo_date_in_future_no_negative_wording(lib):
    """照片日期填在未来（提前记好约好的那次）时，不能说「距上次见面 -3 天」"""
    lib.create_meeting('2026-10-24')
    line = lib.stats_line(today='2026-10-21')
    assert '-' not in line.split('·')[-1]


def test_cfg_helpers(tmp_path, monkeypatch):
    import pet_photos as ph
    assert ph.cfg_read() == {}
    assert ph.cfg_write('a', 1) is True
    assert ph.cfg_read()['a'] == 1
    assert ph.cfg_int('a', 0) == 1
    assert ph.cfg_int('missing', 7) == 7
    assert ph.cfg_int('a', 0) == 1
    ph.cfg_write('flag', False)
    assert ph.cfg_bool('flag', True) is False
    assert ph.cfg_bool('missing_flag', True) is True
    assert ph.cfg_delete('a') is True
    assert 'a' not in ph.cfg_read()


# --------------------------------------------------------------------------
# 8. 界面
# --------------------------------------------------------------------------
def test_photo_tokens_all_exist():
    """本模块用到的 token 必须真的在唯一源里（否则渲染成洋红兜底色）"""
    import pet_theme
    import pet_photos_ui as ui
    missing = [k for k in ui.PHOTO_TOKENS if k not in pet_theme.DEFAULT_THEME]
    assert not missing, missing
    grouped = {k for keys in pet_theme.TOKEN_GROUPS.values() for k in keys}
    assert set(ui.PHOTO_TOKENS) <= grouped, set(ui.PHOTO_TOKENS) - grouped


def test_wall_view_lays_out_hits(app, lib, tmp_path):
    import pet_photos_ui as ui
    lib.import_paths([make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:20 10:00:00'),
                      make_jpg(tmp_path / 'b.jpg', exif_date='2026:10:21 10:00:00',
                               color=(9, 9, 9))])
    view = ui.WallView()
    view.resize(800, 600)
    view.set_meetings(lib.meetings())
    assert len(view._heads) == 2
    assert len(view._hits) == 2
    assert all(h['rect'].width() > 0 and h['rect'].height() > 0 for h in view._hits)
    assert view.minimumHeight() > 100
    view.deleteLater()


def test_wall_view_empty_state_paints(app):
    import pet_photos_ui as ui
    view = ui.WallView()
    view.resize(600, 400)
    view.set_meetings([])
    pm = view.grab()
    assert not pm.isNull()
    view.deleteLater()


def test_wall_panel_constructs_and_paints(app, tmp_path):
    import pet_photos_ui as ui
    ctrl = ui.PhotoController(pet=None, base_dir=str(tmp_path / 'lib'))
    panel = ui.WallPanel(ctrl)
    panel.resize(880, 620)
    panel.show()
    app.processEvents()
    pm = panel.grab()
    assert not pm.isNull()
    assert '拖照片' in panel.lb_hint.text()
    assert panel.lb_stats.text()
    panel.close()
    panel.deleteLater()


def test_wall_panel_search_filters(app, lib, tmp_path):
    import pet_photos_ui as ui
    lib.create_meeting('2026-10-21', title='看电影')
    lib.create_meeting('2026-11-11', title='散步')
    ctrl = ui.PhotoController(pet=None, base_dir=lib.base_dir)
    panel = ui.WallPanel(ctrl)
    panel._on_search('电影')
    assert len(panel.wall._heads) == 1
    panel._on_search('')
    assert len(panel.wall._heads) == 2
    panel.close()
    panel.deleteLater()


def test_meeting_dialog_saves_edits(app, lib, tmp_path):
    import pet_photos_ui as ui
    m = lib.create_meeting('2026-10-21')
    dlg = ui.MeetingDialog(lib, m['id'], pet=None)
    dlg.ed_title.setText('第一次一起看电影')
    dlg.ed_place.setText('万达')
    dlg.ed_mood.setText('开心')
    dlg.ed_note.setPlainText('她买了爆米花')
    dlg._save()
    got = lib.get(m['id'])
    assert got['title'] == '第一次一起看电影'
    assert got['place'] == '万达'
    assert got['note'] == '她买了爆米花'
    assert got['mood'] == '开心'
    dlg.close()
    dlg.deleteLater()


def test_meeting_dialog_bad_date_falls_back(app, lib):
    import pet_photos_ui as ui
    m = lib.create_meeting('2026-10-21')
    dlg = ui.MeetingDialog(lib, m['id'], pet=None)
    dlg.ed_date.setText('乱写')
    dlg._save()
    assert lib.get(m['id'])['date'] == '2026-10-21'
    dlg.close()
    dlg.deleteLater()


def test_polaroid_view_cells_and_stable_angle(app, tmp_path):
    import pet_photos_ui as ui
    import pet_photos as ph
    lib = ph.PhotoLibrary(base_dir=str(tmp_path / 'lib'))
    lib.import_paths([make_jpg(tmp_path / 'a.jpg'), make_jpg(tmp_path / 'b.jpg', color=(3, 3, 3))])
    photos = ph.meeting_photos(lib.meetings()[0])
    view = ui.PolaroidView()
    view.resize(700, 500)
    view.set_photos(photos)
    assert len(view._cells) == 2
    a1 = view._angle(photos[0]['file'])
    assert -3 <= a1 <= 3
    assert view._angle(photos[0]['file']) == a1, '歪斜角度必须稳定（重绘不能乱转）'
    assert not view.grab().isNull()
    view.deleteLater()


def test_lightbox_nav_clamps(app, lib, tmp_path):
    import pet_photos_ui as ui
    import pet_photos as ph
    lib.import_paths([make_jpg(tmp_path / 'a.jpg'), make_jpg(tmp_path / 'b.jpg', color=(5, 5, 5))])
    photos = ph.meeting_photos(lib.meetings()[0])
    lb = ui.PhotoLightbox(photos, 0, '测试')
    assert lb._i == 0
    lb.nav(-1)
    assert lb._i == 0, '第一张再往前还是第一张'
    lb.nav(1)
    assert lb._i == 1
    lb.nav(1)
    assert lb._i == 1
    assert '2 / 2' in lb.lb_info.text()
    lb.close()
    lb.deleteLater()


def test_photo_card_texts(app, lib):
    import pet_photos_ui as ui
    import pet_photos as ph
    lib.import_paths([make_jpg(__import__('tempfile').mkdtemp() + '/a.jpg')])
    ph.cfg_write(ph.CFG_NEXT_DATE, '2027-01-01')
    ctrl = ui.PhotoController(pet=None, base_dir=lib.base_dir)
    card = ui.PhotoCard(ctrl)
    card.refresh()
    assert '次见面' in card.lb_stats.text()
    assert '还有' in card.lb_down.text()
    assert not card.grab().isNull()
    card.deleteLater()


def test_controller_import_reports_and_calls_back(app, tmp_path):
    import pet_photos_ui as ui
    seen = []
    ctrl = ui.PhotoController(pet=None, base_dir=str(tmp_path / 'lib'))
    ctrl.on_meetings = lambda mids: seen.append(list(mids))
    notices = []
    ctrl.notice.connect(lambda t: notices.append(t))
    res = ctrl.import_files([make_jpg(tmp_path / 'a.jpg')])
    assert res['added'] == 1
    assert seen and seen[0], '导入后要通知宿主（写回忆 + 加好感）'
    assert notices and notices[0].startswith('📷')


def test_controller_import_bad_path_is_graceful(app, tmp_path):
    import pet_photos_ui as ui
    ctrl = ui.PhotoController(pet=None, base_dir=str(tmp_path / 'lib'))
    notices = []
    ctrl.notice.connect(lambda t: notices.append(t))
    res = ctrl.import_files([str(tmp_path / 'ghost.jpg')])
    assert res is not None and res['added'] == 0
    assert notices


def test_next_meeting_dialog_saves(app, lib, monkeypatch):
    import pet_photos_ui as ui
    import pet_photos as ph
    from PySide6.QtCore import QDate
    dlg = ui.NextMeetingDialog(lib)
    dlg.date_edit.setDate(QDate(2027, 3, 14))
    dlg.ed_note.setText('去看樱花')
    dlg._save()
    assert ph.cfg_text(ph.CFG_NEXT_DATE) == '2027-03-14'
    assert ph.cfg_text(ph.CFG_NEXT_NOTE) == '去看樱花'
    dlg.deleteLater()


def test_next_meeting_dialog_clear(app, lib):
    import pet_photos_ui as ui
    import pet_photos as ph
    ph.cfg_write(ph.CFG_NEXT_DATE, '2027-03-14')
    dlg = ui.NextMeetingDialog(lib)
    dlg._clear()
    assert ph.cfg_text(ph.CFG_NEXT_DATE) == ''
    dlg.deleteLater()


# --------------------------------------------------------------------------
# 9. 桌宠接线
# --------------------------------------------------------------------------
def test_pet_has_photo_entry_points(app, tmp_path, monkeypatch):
    import desktop_pet as dp
    pet = dp.PetWidget()
    pet._save_cfg_value = lambda *a, **k: True
    try:
        assert hasattr(pet, 'open_photo_wall'), '桌宠要有照片墙入口'
        assert hasattr(pet.chat_pw_btn, 'text') and '📷' in pet.chat_pw_btn.text(), \
            '标题栏入口要带 📷（还带「照片」两个字，光一个图标太小扫不到）'
        _menu, acts = pet._build_context_menu()
        assert 'photos' in acts and 'photo_card' in acts
        assert '见面照片墙' in acts['photos'].text()
    finally:
        try:
            pet.hide()
            pet.deleteLater()
        except Exception:
            pass


def test_pet_records_meeting_memory_and_affection(app, tmp_path):
    """导入照片 → 写一条 type='meeting' 的回忆 + 触发「见面」好感事件"""
    import desktop_pet as dp
    import pet_photos_ui as ui
    from memory_events import MemoryEvents
    from affection_engine import AffectionEngine

    pet = dp.PetWidget()
    pet._save_cfg_value = lambda *a, **k: True
    pet.memories = MemoryEvents(str(tmp_path / 'mem.json'))
    pet.affection = AffectionEngine(str(tmp_path / 'aff.json'))
    pet._notify = lambda *a, **k: None
    try:
        ctrl = ui.PhotoController(pet, base_dir=str(tmp_path / 'lib'))
        ctrl.on_meetings = pet._record_meetings
        before = pet.affection.snapshot(pet.current)['affection']
        ctrl.import_files([make_jpg(tmp_path / 'a.jpg', exif_date='2026:10:21 18:00:00')])
        items = pet.memories.all(pet.current)
        assert items and items[0]['type'] == 'meeting', items
        assert '见面' in items[0]['title']
        assert '1 张照片' in items[0]['detail']
        after = pet.affection.snapshot(pet.current)['affection']
        assert after > before, '见面该加好感（(好感 %d → %d)' % (before, after)
    finally:
        try:
            pet.hide()
            pet.deleteLater()
        except Exception:
            pass


def test_memory_events_accepts_meeting_type(tmp_path):
    from memory_events import MemoryEvents
    ev = MemoryEvents(str(tmp_path / 'm.json'))
    e = ev.add('flash', 'meeting', '见面：2026年10月21日', '影院，2 张照片')
    assert e['type'] == 'meeting'
    assert ev.all('flash')[0]['type'] == 'meeting'


def test_affection_has_meeting_event():
    from affection_engine import EVENTS
    assert 'meeting' in EVENTS
    aff, xp, _cd, daily_key, daily_limit = EVENTS['meeting']
    assert aff > 0 and xp > 0 and daily_key and daily_limit, '见面事件要有每日上限，别刷分'


def test_memories_dialog_shows_camera_emoji(app, tmp_path):
    import affection_ui as au
    from memory_events import MemoryEvents
    ev = MemoryEvents(str(tmp_path / 'm.json'))
    ev.add('flash', 'meeting', '见面：2026年10月21日', '2 张照片')
    dlg = au.MemoriesDialog(ev, 'flash', '测试角色')
    assert '📷' in dlg.list.item(0).text()
    dlg.deleteLater()
