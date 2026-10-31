"""OpenStreetMap のタイル地図に現在位置と軌跡を表示する。

このビューを作った時点で初めて地図サーバへ接続する。
表示する範囲のタイルを取得するので、位置の概略が地図サーバに伝わる。
"""
import json

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView

# OSM のタイルサーバは Referer の無い要求を拒否するので、基底 URL を与える
BASE_URL = "http://localhost/mosaic_gnss_recorder/"
MAX_TRACK_POINTS = 2000
PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html, body, #map { height: 100%; margin: 0; }</style>
</head><body><div id="map"></div>
<script>
var map = L.map('map').setView([36, 138], 5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);
var track = L.polyline([], {color: '#2980b9', weight: 3}).addTo(map);
var marker = null;
function updatePosition(points) {
  if (!points.length) { return; }
  var last = points[points.length - 1];
  track.setLatLngs(points);
  if (marker === null) {
    marker = L.circleMarker(last, {radius: 7, color: '#c0392b', fillOpacity: 0.9}).addTo(map);
    map.setView(last, 17);
  } else {
    marker.setLatLng(last);
    if (!map.getBounds().contains(last)) { map.panTo(last); }
  }
}
</script></body></html>
"""


def track_points(points):
    """測位履歴を [緯度, 経度] のリストにする。多すぎるときは間引く。"""
    fixes = [(p.latitude, p.longitude) for _t, p in points if p.latitude is not None]
    if not fixes:
        return []
    step = max(1, len(fixes) // MAX_TRACK_POINTS)
    sampled = fixes[::step]
    if sampled[-1] != fixes[-1]:
        sampled.append(fixes[-1])
    return [[round(lat, 7), round(lon, 7)] for lat, lon in sampled]


class MapView(QWebEngineView):
    """地図のビュー。"""

    def __init__(self):
        super().__init__()
        self._ready = False
        self.loadFinished.connect(self._on_loaded)  # pylint: disable=no-member
        self.setHtml(PAGE, QUrl(BASE_URL))

    def _on_loaded(self, success):
        """ページの読み込み完了を記録する。"""
        self._ready = success

    def set_track(self, points):
        """測位履歴を地図へ反映する。"""
        if self._ready:
            self.page().runJavaScript(f"updatePosition({json.dumps(track_points(points))});")
