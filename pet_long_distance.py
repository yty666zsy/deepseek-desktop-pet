# -*- coding: utf-8 -*-
"""pet_long_distance.py — 异地：两城天气 + 直线距离 + 天气突变判断（v6.95）

纯数据层，不依赖 Qt：
- 城市解析 / 天气：Open-Meteo（免 key），失败时退回本地缓存
- 距离：Haversine 直线距离（明确不是路程）
- 突变：下雨/下雪、明天明显降温、大风；同一天同一类只提醒一次

缓存落在项目目录 long_distance_cache.json（原子写）。
"""
import datetime
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import pet_storage
from pet_log import get_logger

_log = get_logger('long_distance')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, 'long_distance_cache.json')

GEO_TTL = 30 * 86400       # 城市坐标 30 天
WEATHER_TTL = 30 * 60      # 天气 30 分钟
MAX_ALERTS_PER_DAY = 2

GEO_URL = 'https://geocoding-api.open-meteo.com/v1/search'
WEATHER_URL = 'https://api.open-meteo.com/v1/forecast'
PHOTON_URL = 'https://photon.komoot.io/api'
_UA = {'User-Agent': 'deepseek-desktop-pet/1.0'}

# 出现这些词就当「具体地点」，走 Photon（校区/小区/街道级别）；否则按城市查
_SPECIFIC_WORDS = ('区', '县', '镇', '乡', '街道', '路', '街', '号', '大学', '学院',
                   '学校', '校区', '公园', '机场', '车站', '广场', '大厦', '中心',
                   '小区', '村', '苑', '园', 'university', 'college', 'campus',
                   'street', 'road', 'airport', 'station', 'square', 'park')
_GOOD_AMENITY = {'university', 'college', 'school', 'hospital', 'airport',
                 'bus_station', 'train_station', 'townhall'}

WMO = {
    0: '晴', 1: '大部晴', 2: '多云', 3: '阴',
    45: '雾', 48: '雾凇',
    51: '小毛毛雨', 53: '毛毛雨', 55: '大毛毛雨',
    56: '冻毛毛雨', 57: '强冻毛毛雨',
    61: '小雨', 63: '中雨', 65: '大雨',
    66: '冻雨', 67: '强冻雨',
    71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '阵雨', 81: '强阵雨', 82: '暴雨',
    85: '阵雪', 86: '强阵雪',
    95: '雷阵雨', 96: '雷暴伴冰雹', 99: '强雷暴伴冰雹',
}

PRECIP_CODES = set(range(51, 68)) | {71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
DROP_ALERT_C = 8.0       # 明天最高温比今天低这么多 → 提醒
WIND_ALERT_MS = 13.9     # 7 级风（6 级太常见，容易天天提醒）

# 只写省份时 Open-Meteo 会把「山东」认成同名小村庄，统一换成省会再查
PROVINCE_CAPITAL = {
    '北京': '北京', '上海': '上海', '天津': '天津', '重庆': '重庆',
    '河北': '石家庄', '山西': '太原', '内蒙古': '呼和浩特', '辽宁': '沈阳',
    '吉林': '长春', '黑龙江': '哈尔滨', '江苏': '南京', '浙江': '杭州',
    '安徽': '合肥', '福建': '福州', '江西': '南昌', '山东': '济南',
    '河南': '郑州', '湖北': '武汉', '湖南': '长沙', '广东': '广州',
    '广西': '南宁', '海南': '海口', '四川': '成都', '贵州': '贵阳',
    '云南': '昆明', '西藏': '拉萨', '陕西': '西安', '甘肃': '兰州',
    '青海': '西宁', '宁夏': '银川', '新疆': '乌鲁木齐', '香港': '香港',
    '澳门': '澳门', '台湾': '台北',
}


def canonical_city(city):
    """省份名 → 省会（其余原样返回）。"""
    name = str(city or '').strip()
    for suffix in ('特别行政区', '自治区', '省', '市'):
        if name.endswith(suffix) and name[:-len(suffix)] in PROVINCE_CAPITAL:
            return PROVINCE_CAPITAL[name[:-len(suffix)]]
    return PROVINCE_CAPITAL.get(name, name)


class WeatherError(Exception):
    """天气/城市查询失败（带一句能直接给用户看的中文）"""


_SAVE_LOCK = threading.Lock()   # 面板与定时检查可能同时写缓存（同一个 .tmp）


def weather_text(code):
    try:
        return WMO.get(int(code), '未知天气')
    except Exception:
        return '未知天气'


def haversine_km(lat1, lon1, lat2, lon2):
    """两点球面直线距离（公里）。输入为十进制度。"""
    r = 6371.0088
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_json(url):
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        raise WeatherError('连不上天气服务：%s' % e)


def _is_specific(name):
    low = str(name or '').lower()
    return any(word in low for word in _SPECIFIC_WORDS)


def split_location(name):
    """把「青岛市崂山区青岛科技大学」拆成 ['青岛市', '崂山区', '青岛科技大学']"""
    parts, rest = [], str(name or '').strip()
    while rest:
        m = re.match(r'^(.{2,4}?(?:特别行政区|自治区|省|市|区|县|镇|乡|街道|村))', rest)
        if not m or m.end() >= len(rest):
            break
        parts.append(m.group(1))
        rest = rest[m.end():]
    parts.append(rest)
    return [p for p in parts if p]


def _photon_search(query):
    q = urllib.parse.urlencode({'q': query, 'limit': 10})
    return _get_json(PHOTON_URL + '?' + q).get('features') or []


def _label_bits(*vals):
    out = []
    for v in vals:
        v = str(v or '').strip()
        if v and not any(v in x for x in out):
            out.append(v)
    return out


def _norm_name(value):
    return re.sub(r'[\s（）()·.,，、\-—]+', '', str(value or ''))


def _name_matches(feature_name, query):
    a, b = _norm_name(feature_name), _norm_name(query)
    return bool(a and b and (a in b or b in a))


def _relevant(props, wanted, full):
    """结果的名字或街道能对上输入，才算相关（地址结果通常只有 street）"""
    for text in (props.get('name'), props.get('street')):
        if _name_matches(text, wanted):
            return True
        if text and full and _norm_name(text) in _norm_name(full):
            return True
    return False


def _feature_geo(feature, fallback):
    """Photon feature → 与 geocode() 相同的结构"""
    props = feature.get('properties') or {}
    coords = (feature.get('geometry') or {}).get('coordinates') or []
    if len(coords) < 2:
        return None
    lat, lon = _num(coords[1]), _num(coords[0])
    if lat is None or lon is None:
        return None
    name = str(props.get('name') or '').strip()
    if not name:
        name = str(props.get('street') or props.get('city') or fallback).strip()
        number = str(props.get('housenumber') or '').strip()
        if number:
            name = ('%s %s' % (name, number)).strip()
    bits = _label_bits(name, props.get('district'), props.get('city'), props.get('state'))
    country = str(props.get('country') or '').strip()
    if country and country not in ('中国', 'China'):
        bits = _label_bits(*(bits + [country]))
    return {'name': name, 'label': ' · '.join(bits[:3]),
            'admin1': str(props.get('city') or props.get('state') or ''),
            'country': country, 'lat': lat, 'lon': lon}


def _feature_score(feature, wanted, full):
    """具体地点排序：名字对上 > 大学/机场等 POI > 公交站/住宅；行政区也对上加分"""
    props = feature.get('properties') or {}
    name = str(props.get('name') or '')
    street = str(props.get('street') or '')
    score = 0
    if _name_matches(name, wanted) or _name_matches(street, wanted):
        score += 60
    if (str(props.get('osm_key') or '') == 'amenity'
            and str(props.get('osm_value') or '') in _GOOD_AMENITY):
        score += 30
    if str(props.get('osm_value') or '') in ('bus_stop', 'house', 'residential', 'dormitory'):
        score -= 25
    for field, bonus in (('district', 20), ('city', 10), ('state', 5)):
        val = str(props.get(field) or '')
        if val and val in full:
            score += bonus
    return score


def _city_score(feature, wanted, full):
    """城市排序：真·城市 > 镇/行政区，火车站/机场靠后"""
    props = feature.get('properties') or {}
    score = 0
    if _name_matches(props.get('name'), wanted):
        score += 50
    key = str(props.get('osm_key') or '')
    val = str(props.get('osm_value') or '')
    if key == 'place':
        score += {'city': 100, 'town': 70, 'administrative': 60, 'state': 40,
                  'suburb': 30, 'village': 20, 'hamlet': 10}.get(val, 30)
    elif key == 'boundary' and val == 'administrative':
        score += 50
    elif key == 'aeroway':
        score -= 30
    elif key == 'railway' and val in ('station', 'halt'):
        score += 5
    if str(props.get('country') or '') in ('中国', 'China'):
        score += 5
    return score


def _photon_geocode(name):
    """具体地点（校区/街道/小区）或城市 → Photon（OSM）。

    Open-Meteo 的中文城市库缺城市（「青岛」只剩同名村庄和岛），所以城市也走 Photon。
    """
    specific = _is_specific(name)
    if specific:
        parts = split_location(name)
        tail = parts[-1] if parts else name
        queries = []
        if len(parts) >= 2:
            queries.append('%s %s' % (tail, parts[-2]))
        queries.append(tail)
        if name not in queries:
            queries.append(name)
        scorer, strong = _feature_score, 70
    else:
        tail = name
        queries = [name]
        scorer, strong = _city_score, 120

    best, best_score = None, 0
    for query in queries:
        try:
            features = _photon_search(query)
        except WeatherError:
            continue
        for feat in features:
            props = feat.get('properties') or {}
            if not _relevant(props, tail, name):
                continue
            score = scorer(feat, tail, name)
            if score > best_score:
                best, best_score = feat, score
        if best_score >= strong:      # 名称和类别都对上了，不用再试
            break
    return _feature_geo(best, name) if best is not None else None


def _city_rows(name):
    q = urllib.parse.urlencode({'name': name, 'count': 5, 'language': 'zh', 'format': 'json'})
    return _get_json(GEO_URL + '?' + q).get('results') or []


def geocode(city):
    """城市名/具体地点 → {name, label, lat, lon, country, admin1}。查不到抛 WeatherError。"""
    name = str(city or '').strip()
    if not name:
        raise WeatherError('城市还没填')
    canonical = canonical_city(name)
    info = _photon_geocode(canonical)
    if info:
        return info
    name = canonical
    rows = _city_rows(name)
    if not rows:
        # Open-Meteo 不认「杭州市 / 崂山区」这类后缀，去掉再试一次
        short = name
        for suffix in ('特别行政区', '自治区', '街道', '省', '市', '区', '县', '镇', '乡', '村'):
            if short.endswith(suffix) and len(short) > len(suffix) + 1:
                short = short[:-len(suffix)]
                break
        if short != name:
            rows = _city_rows(short)
    if not rows:
        tip = '试试加个地标名（如「XX大学崂山校区」「XX小区」）' if _is_specific(name) else '换个写法试试（比如「杭州」）'
        raise WeatherError('没找到「%s」，%s' % (name, tip))
    row = rows[0]
    admin1 = str(row.get('admin1') or '').strip()
    short = str(row.get('name') or name).strip()
    label = short if (not admin1 or admin1 == short) else '%s·%s' % (admin1, short)
    country = str(row.get('country') or '').strip()
    if country and country not in ('中国', 'China'):
        label += '（%s）' % country
    return {
        'name': short, 'label': label, 'admin1': admin1, 'country': country,
        'lat': _num(row.get('latitude')), 'lon': _num(row.get('longitude')),
    }


def fetch_weather(geo):
    """按坐标取当前天气 + 未来 3 天高低温。"""
    q = urllib.parse.urlencode({
        'latitude': '%.4f' % float(geo['lat']),
        'longitude': '%.4f' % float(geo['lon']),
        'current': 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m',
        'daily': 'weather_code,temperature_2m_max,temperature_2m_min',
        'timezone': 'auto',
        'forecast_days': 3,
    })
    data = _get_json(WEATHER_URL + '?' + q)
    cur = data.get('current') or {}
    daily = data.get('daily') or {}
    temp = _num(cur.get('temperature_2m'))
    if temp is None:
        raise WeatherError('天气数据不完整，稍后再试')
    return {
        'current': {
            'temp': temp,
            'feels': _num(cur.get('apparent_temperature')),
            'humidity': _num(cur.get('relative_humidity_2m')),
            'code': int(_num(cur.get('weather_code'), 0) or 0),
            'wind': _num(cur.get('wind_speed_10m'), 0.0) or 0.0,
        },
        'daily': {
            'max': [_num(v) for v in (daily.get('temperature_2m_max') or [])],
            'min': [_num(v) for v in (daily.get('temperature_2m_min') or [])],
        },
        'timezone': data.get('timezone') or '',
        'updated_at': int(time.time()),
    }


def weather_line(weather):
    """一行天气文案（面板 / 气泡共用）"""
    if not weather:
        return '暂无天气'
    cur = weather.get('current') or {}
    daily = weather.get('daily') or {}
    temp = cur.get('temp')
    parts = ['%s %s' % (weather_text(cur.get('code')), '--' if temp is None else '%.0f℃' % temp)]
    feels = cur.get('feels')
    if feels is not None and temp is not None and abs(feels - temp) >= 2:
        parts.append('体感 %.0f℃' % feels)
    hum = cur.get('humidity')
    if hum is not None:
        parts.append('湿度 %d%%' % hum)
    wind = cur.get('wind')
    if wind:
        parts.append('风 %.1f m/s' % wind)
    line = ' · '.join(parts)
    highs, lows = daily.get('max') or [], daily.get('min') or []
    if highs and lows and highs[0] is not None and lows[0] is not None:
        line += '\n今天 %.0f~%.0f℃' % (lows[0], highs[0])
    return line


def detect_alerts(weather):
    """天气突变 → [(kind, 提醒文案)]，按重要性排序。同一天由调用方去重。"""
    out = []
    if not weather:
        return out
    cur = weather.get('current') or {}
    daily = weather.get('daily') or {}
    code = int(cur.get('code') or 0)

    if code in PRECIP_CODES:
        if code in SNOW_CODES:
            out.append(('snow', 'TA 那边在下雪，路滑，提醒 TA 慢点走。'))
        elif code >= 95:
            out.append(('storm', 'TA 那边在打雷下暴雨，提醒 TA 别在外面待着。'))
        else:
            out.append(('rain', 'TA 那边在下雨，提醒 TA 带伞。'))

    highs = daily.get('max') or []
    if len(highs) >= 2 and highs[0] is not None and highs[1] is not None:
        drop = highs[0] - highs[1]
        if drop >= DROP_ALERT_C:
            out.append(('drop', 'TA 那边明天最高温比今天低 %.0f℃（明天 %.0f℃），提醒 TA 加衣服。'
                        % (drop, highs[1])))

    wind = _num(cur.get('wind'), 0.0) or 0.0
    if wind >= WIND_ALERT_MS:
        out.append(('wind', 'TA 那边风很大（%.1f m/s），提醒 TA 注意。' % wind))
    return out


class LongDistance(object):
    """城市/具体地点 + 天气缓存 + 突变提醒去重。所有网络失败都优先退回旧缓存。"""

    def __init__(self, cache_path=None):
        self.path = cache_path or CACHE_PATH
        data = pet_storage.load_json(self.path, {})
        self.cache = data if isinstance(data, dict) else {}

    def _save(self):
        try:
            with _SAVE_LOCK:
                pet_storage.atomic_write_json(self.path, self.cache)
        except Exception as e:
            _log.debug('异地缓存写入失败：%s', e)

    @staticmethod
    def _fresh(slot, ttl):
        return bool(slot) and (time.time() - float(slot.get('ts') or 0)) < ttl

    def city_geo(self, city, force=False):
        key = str(city or '').strip()
        if not key:
            raise WeatherError('城市还没填')
        geo = self.cache.setdefault('geo', {})
        if not isinstance(geo, dict):
            geo = self.cache['geo'] = {}
        slot = geo.get(key)
        if not force and self._fresh(slot, GEO_TTL):
            return slot
        try:
            info = geocode(key)
        except WeatherError:
            if slot and slot.get('lat') is not None:
                return slot          # 断网时沿用旧坐标
            raise
        info['ts'] = int(time.time())
        geo[key] = info
        self._save()
        return info

    def weather(self, city, force=False):
        key = str(city or '').strip()
        weathers = self.cache.get('weather')
        if not isinstance(weathers, dict):
            weathers = self.cache['weather'] = {}
        slot = weathers.get(key)
        if not force and self._fresh(slot, WEATHER_TTL):
            return slot.get('data')
        try:
            data = fetch_weather(self.city_geo(key, force=False))
        except WeatherError:
            if slot and slot.get('data'):
                return slot['data']      # 断网时用上次的天气
            raise
        weathers[key] = {'ts': int(time.time()), 'data': data}
        self._save()
        return data

    def snapshot(self, my_city, ta_city, force=False):
        """两地天气 + 直线距离。任一地点失败整体抛 WeatherError。"""
        if not str(ta_city or '').strip():
            raise WeatherError('先填 TA 的地点')
        my_geo = self.city_geo(my_city, force)
        ta_geo = self.city_geo(ta_city, force)
        my_w = self.weather(my_city, force)
        ta_w = self.weather(ta_city, force)
        km = haversine_km(my_geo['lat'], my_geo['lon'], ta_geo['lat'], ta_geo['lon'])
        return {'my': {'geo': my_geo, 'weather': my_w},
                'ta': {'geo': ta_geo, 'weather': ta_w},
                'distance_km': km}

    def pop_alert(self, ta_city, now=None):
        """取一条今天还没发过的突变提醒；没有/超上限返回 None。"""
        city = str(ta_city or '').strip()
        if not city:
            return None
        weather = self.weather(city)
        day = datetime.datetime.fromtimestamp(now or time.time()).strftime('%Y-%m-%d')
        alerts = self.cache.get('alerts')
        if not isinstance(alerts, dict) or alerts.get('date') != day or alerts.get('city') != city:
            alerts = {'date': day, 'city': city, 'sent': [], 'n': 0}
            self.cache['alerts'] = alerts
        if int(alerts.get('n') or 0) >= MAX_ALERTS_PER_DAY:
            return None
        for kind, text in detect_alerts(weather):
            if kind in alerts['sent']:
                continue
            alerts['sent'].append(kind)
            alerts['n'] = int(alerts.get('n') or 0) + 1
            self._save()
            return text
        return None
