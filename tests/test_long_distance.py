# -*- coding: utf-8 -*-
"""异地功能（v6.95）：距离、天气码、突变判断、缓存降级、提醒去重、面板渲染

不联网：所有网络函数都在测试里被替换。
运行：python -m pytest tests/test_long_distance.py -q
"""
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

import pet_long_distance as ld


@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _weather(code=0, maxs=(25, 24, 23), mins=(18, 17, 16), wind=2.0):
    return {'current': {'temp': 22.0, 'feels': 21.0, 'humidity': 50,
                        'code': code, 'wind': wind},
            'daily': {'max': list(maxs), 'min': list(mins)},
            'updated_at': int(time.time())}


def test_haversine_beijing_shanghai():
    km = ld.haversine_km(39.9042, 116.4074, 31.2304, 121.4737)
    assert 1030 < km < 1110


def test_weather_text():
    assert ld.weather_text(0) == '晴'
    assert ld.weather_text(61) == '小雨'
    assert ld.weather_text(95) == '雷阵雨'
    assert ld.weather_text(9999) == '未知天气'


def test_province_falls_back_to_capital():
    assert ld.canonical_city('山东') == '济南'
    assert ld.canonical_city('山东省') == '济南'
    assert ld.canonical_city('北京市') == '北京'
    assert ld.canonical_city('杭州') == '杭州'


def test_split_location_tokens():
    assert ld.split_location('青岛市崂山区青岛科技大学') == ['青岛市', '崂山区', '青岛科技大学']
    # 「校区」不能被当成行政区拆开
    assert ld.split_location('青岛科技大学崂山校区') == ['青岛科技大学崂山校区']
    assert ld.split_location('杭州') == ['杭州']


def test_photon_prefers_university_over_bus_stop(monkeypatch):
    features = [
        {'properties': {'name': '青岛科技大学', 'osm_key': 'highway', 'osm_value': 'bus_stop',
                        'district': '崂山区', 'city': '青岛市'},
         'geometry': {'coordinates': [120.4773, 36.1163]}},
        {'properties': {'name': '青岛科技大学（崂山校区）', 'osm_key': 'amenity', 'osm_value': 'university',
                        'district': '崂山区', 'city': '青岛市'},
         'geometry': {'coordinates': [120.4786, 36.1212]}},
    ]
    calls = []

    def fake_search(query):
        calls.append(query)
        return features

    monkeypatch.setattr(ld, '_photon_search', fake_search)
    geo = ld._photon_geocode('青岛市崂山区青岛科技大学')
    assert geo['name'] == '青岛科技大学（崂山校区）'
    assert geo['lat'] == pytest.approx(36.1212, abs=1e-4)
    assert calls[0] == '青岛科技大学 崂山区'


def test_photon_prefers_city_over_station(monkeypatch):
    features = [
        {'properties': {'name': '青岛', 'osm_key': 'railway', 'osm_value': 'station',
                        'city': '青岛市', 'state': '山东省', 'country': '中国'},
         'geometry': {'coordinates': [120.3075, 36.0631]}},
        {'properties': {'name': '青岛市', 'osm_key': 'place', 'osm_value': 'city',
                        'state': '山东省', 'country': '中国'},
         'geometry': {'coordinates': [120.3778, 36.0663]}},
    ]
    monkeypatch.setattr(ld, '_photon_search', lambda query: features)
    geo = ld._photon_geocode('青岛')
    assert geo['name'] == '青岛市'
    assert geo['lat'] == pytest.approx(36.0663, abs=1e-4)


def test_detect_rain_snow_drop_wind():
    assert ld.detect_alerts(_weather(code=61))[0][0] == 'rain'
    assert ld.detect_alerts(_weather(code=71))[0][0] == 'snow'
    assert ld.detect_alerts(_weather(code=95))[0][0] == 'storm'
    assert ld.detect_alerts(_weather(maxs=(30, 20, 18)))[0][0] == 'drop'
    assert ld.detect_alerts(_weather(wind=15.0))[0][0] == 'wind'
    assert ld.detect_alerts(_weather()) == []


def test_alert_dedup_and_persist(tmp_path, monkeypatch):
    svc = ld.LongDistance(str(tmp_path / 'cache.json'))
    monkeypatch.setattr(svc, 'weather', lambda city, force=False: _weather(code=61))
    first = svc.pop_alert('杭州')
    assert first and '带伞' in first
    # 同一天同类不重复；重启后也不重复
    svc2 = ld.LongDistance(str(tmp_path / 'cache.json'))
    monkeypatch.setattr(svc2, 'weather', lambda city, force=False: _weather(code=61))
    assert svc2.pop_alert('杭州') is None


def test_offline_uses_cached_geo(tmp_path, monkeypatch):
    path = str(tmp_path / 'cache.json')
    svc = ld.LongDistance(path)
    svc.cache['geo'] = {'杭州': {'name': '杭州', 'label': '浙江·杭州',
                                 'lat': 30.27, 'lon': 120.15, 'ts': 0}}
    monkeypatch.setattr(ld, 'geocode', lambda city: (_ for _ in ()).throw(ld.WeatherError('断网')))
    geo = svc.city_geo('杭州')
    assert geo['lat'] == pytest.approx(30.27)


def test_snapshot_distance(tmp_path, monkeypatch):
    geo = {'杭州': {'name': '杭州', 'label': '浙江·杭州', 'lat': 30.2741, 'lon': 120.1551},
           '成都': {'name': '成都', 'label': '四川·成都', 'lat': 30.5728, 'lon': 104.0668}}
    monkeypatch.setattr(ld, 'geocode', lambda city: dict(geo[city]))
    monkeypatch.setattr(ld, 'fetch_weather', lambda g: _weather())
    snap = ld.LongDistance(str(tmp_path / 'cache.json')).snapshot('杭州', '成都')
    assert 1500 < snap['distance_km'] < 1900
    assert snap['my']['geo']['label'] == '浙江·杭州'


def test_panel_renders(tmp_path, qapp):
    import pet_long_distance_ui as ui

    app = qapp

    class FakeSvc(object):
        def snapshot(self, *a, **kw):
            return {'my': {'geo': {'label': '浙江·杭州'}, 'weather': _weather()},
                    'ta': {'geo': {'label': '四川·成都'}, 'weather': _weather()},
                    'distance_km': 1667.4}

    class FakeHost(object):
        pet_city = '杭州'
        _cfg = {'ld_city': '', 'ld_alert': True}
        saved = {}

        def _long_distance(self):
            return FakeSvc()

        def _tk(self, key):
            return '#123456'

        def _save_cfg_value(self, key, value):
            self.saved[key] = value
            return True

    host = FakeHost()
    dlg = ui.LongDistanceDialog(host)
    dlg._apply(FakeSvc().snapshot())
    assert '1,667' in dlg.lbl_dist.text() and '非路程' in dlg.lbl_dist.text()
    assert '杭州' in dlg.lbl_my.text() and '成都' in dlg.lbl_ta.text()
    assert dlg.windowTitle().startswith('🌤')
    dlg.ed_ta.setText('成都')
    dlg._save()
    assert host.saved['ld_city'] == '成都' and host.saved['ld_alert'] is True
    dlg.close()
    app.processEvents()
