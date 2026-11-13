/* LocPilot — WGS-84 <-> GCJ-02 纠偏（显示层专用）
 *
 * 后端与 API 全部使用 WGS-84（设备真值）；高德底图使用 GCJ-02，直接叠加会偏 300~800 米。
 * 这里只在渲染底图 / 把用户点选的底图坐标换算回设备坐标时做转换，绝不污染下发给后端的数据。
 *
 * 与 locpilot/core/coord.py **完全同构**：同一组克拉索夫斯基椭球常数、同一组三次多项式扰动、
 * 同样的境外判定矩形（72.004~137.8347 / 0.8293~55.8271）与同样的 8 次不动点迭代。
 * 这样 JS 与 Python 在同一坐标上得到一致结果（见 tests/geo.test.mjs 的 Python 金标用例）。
 *
 * 经典脚本，无模块系统：挂到 globalThis.LocPilotGeo，浏览器与 node 测试共用。
 */
(function (root) {
  'use strict';

  var A = 6378245.0;                    // 克拉索夫斯基椭球长半轴
  var EE = 0.00669342162296594323;      // 第一偏心率平方
  var EARTH_RADIUS_M = 6371008.8;       // 与 locpilot/core/geo.py 一致

  function outOfChina(lat, lon) {
    return !(lon >= 72.004 && lon <= 137.8347 && lat >= 0.8293 && lat <= 55.8271);
  }

  function transformLat(x, y) {
    var ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320.0 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return ret;
  }

  function transformLon(x, y) {
    var ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return ret;
  }

  /** WGS-84 -> GCJ-02（把设备真值画到国内底图上）。返回 [lat, lon]。 */
  function wgs2gcj(lat, lon) {
    lat = Number(lat);
    lon = Number(lon);
    if (outOfChina(lat, lon)) { return [lat, lon]; }
    var dLat = transformLat(lon - 105.0, lat - 35.0);
    var dLon = transformLon(lon - 105.0, lat - 35.0);
    var radLat = lat / 180.0 * Math.PI;
    var magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * Math.PI);
    dLon = (dLon * 180.0) / (A / sqrtMagic * Math.cos(radLat) * Math.PI);
    return [lat + dLat, lon + dLon];
  }

  /** GCJ-02 -> WGS-84（把用户在国内底图上点的位置换算成设备坐标）。返回 [lat, lon]。 */
  function gcj2wgs(lat, lon) {
    lat = Number(lat);
    lon = Number(lon);
    if (outOfChina(lat, lon)) { return [lat, lon]; }
    var wLat = lat;
    var wLon = lon;
    for (var i = 0; i < 8; i += 1) {
      var pair = wgs2gcj(wLat, wLon);
      var dLat = pair[0] - lat;
      var dLon = pair[1] - lon;
      if (Math.abs(dLat) < 1e-11 && Math.abs(dLon) < 1e-11) { break; }
      wLat -= dLat;
      wLon -= dLon;
    }
    return [wLat, wLon];
  }

  /** 两点大圆距离（米），用于自检偏移量。 */
  function distanceMeters(a, b) {
    var phi1 = a[0] / 180.0 * Math.PI;
    var phi2 = b[0] / 180.0 * Math.PI;
    var dPhi = (b[0] - a[0]) / 180.0 * Math.PI;
    var dLambda = (b[1] - a[1]) / 180.0 * Math.PI;
    var h = Math.sin(dPhi / 2) * Math.sin(dPhi / 2) +
            Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) * Math.sin(dLambda / 2);
    return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  /** 某点 WGS-84 与 GCJ-02 的偏移距离（米）。 */
  function offsetMeters(lat, lon) {
    var g = wgs2gcj(lat, lon);
    return distanceMeters([lat, lon], g);
  }

  /** WGS-84 坐标 -> 当前底图坐标系下的显示坐标（crs: 'gcj02' | 'wgs84'）。 */
  function toDisplay(lat, lon, crs) {
    return crs === 'gcj02' ? wgs2gcj(lat, lon) : [Number(lat), Number(lon)];
  }

  /** 底图显示坐标 -> WGS-84（下发后端前必须经过这里）。 */
  function fromDisplay(lat, lon, crs) {
    return crs === 'gcj02' ? gcj2wgs(lat, lon) : [Number(lat), Number(lon)];
  }

  root.LocPilotGeo = {
    A: A,
    EE: EE,
    EARTH_RADIUS_M: EARTH_RADIUS_M,
    outOfChina: outOfChina,
    wgs2gcj: wgs2gcj,
    gcj2wgs: gcj2wgs,
    distanceMeters: distanceMeters,
    offsetMeters: offsetMeters,
    toDisplay: toDisplay,
    fromDisplay: fromDisplay
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
