/* LocPilot — 纯函数层：快照 -> 界面状态
 *
 * 这一层不碰 DOM、不发请求，方便 node 直接做单元断言（tests/model.test.mjs）。
 * 语义对齐 macOS 版 AppState：
 *   * 状态色：后端不可达 = 红；设备已连接 = 绿；其余（未连接/连接失败）= 黄；
 *   * 设备弹框：已连接显示设备名 + 断开；未连接显示「重新连接」；连接中显示 loading；
 *   * 无定位时读数完全不画（不留 "—" 占位符）。
 */
(function (root) {
  'use strict';

  /* 后端拿不到状态时的兜底底图配置：与 locpilot/config.py 的 TILE_PROVIDERS.amap 一致。
   * 这样即使 /api/status 失败，页面也能先把高德底图铺出来（控件与定位接口照常可用）。 */
  var DEFAULT_PROVIDER = {
    name: 'amap',
    label: '高德地图',
    url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    subdomains: '1234',
    attribution: '&copy; 高德地图',
    crs: 'gcj02',
    max_zoom: 18
  };

  var BUSY_PHASES = { dropping: true, landed: true, processing: true };
  var MIN_ZOOM = 3;

  function isFiniteNumber(value) {
    return typeof value === 'number' && isFinite(value);
  }

  /** 快照里当前虚拟定位（WGS-84）；没有或非法返回 null。 */
  function positionOf(snapshot) {
    var position = snapshot && snapshot.position;
    if (!position || !isFiniteNumber(position.lat) || !isFiniteNumber(position.lon)) { return null; }
    return { lat: position.lat, lon: position.lon };
  }

  /** 坐标读数文本：6 位小数，与 macOS 版 AppState.format 一致。 */
  function formatCoord(lat, lon) {
    return Number(lat).toFixed(6) + ', ' + Number(lon).toFixed(6);
  }

  /** 读数文本；没有定位返回空串（调用方据此隐藏读数）。 */
  function readoutText(snapshot) {
    var position = positionOf(snapshot);
    return position ? formatCoord(position.lat, position.lon) : '';
  }

  /** 设备状态色：'green' | 'yellow' | 'red'。 */
  function statusColor(snapshot, reachable) {
    if (reachable === false) { return 'red'; }
    if (snapshot && snapshot.engine && snapshot.engine.opened) { return 'green'; }
    return 'yellow';
  }

  /** 已连接的设备名；未连接或无名返回 null。 */
  function deviceName(snapshot) {
    var device = snapshot && snapshot.engine && snapshot.engine.device;
    if (!device) { return null; }
    return device.name || device.udid || null;
  }

  /** 设备弹框内容：标题 + 动作。connecting 优先（连接请求在途）。 */
  function deviceView(options) {
    var snapshot = options && options.snapshot;
    if (options && options.connecting) {
      return { title: '正在连接…', action: 'none', actionLabel: '', spinner: true };
    }
    if (snapshot && snapshot.engine && snapshot.engine.opened) {
      return { title: deviceName(snapshot) || '已连接', action: 'disconnect', actionLabel: '断开连接', spinner: false };
    }
    return { title: '设备未连接', action: 'connect', actionLabel: '重新连接', spinner: false };
  }

  /** 是否处于落针动画的中间阶段（此时事件流不要改写标记位置）。 */
  function isBusyPhase(phase) {
    return Boolean(BUSY_PHASES[phase]);
  }

  function normalizeProvider(config, name) {
    var maxZoom = config.max_zoom || config.maxZoom || 19;
    return {
      name: name,
      label: config.label || name,
      url: config.url,
      subdomains: config.subdomains || 'abc',
      attribution: config.attribution || '',
      crs: config.crs === 'gcj02' ? 'gcj02' : 'wgs84',
      maxZoom: maxZoom,
      minZoom: MIN_ZOOM
    };
  }

  /** 从快照选择底图配置；快照缺配置时回落到高德（离线/后端未就绪也能铺图）。 */
  function providerConfig(snapshot, fallback) {
    var defaultConfig = fallback || DEFAULT_PROVIDER;
    var providers = (snapshot && snapshot.tile_providers) || {};
    var wanted = snapshot && snapshot.tile_provider;
    var name = null;
    var config = null;
    if (wanted && providers[wanted]) {
      name = wanted;
      config = providers[wanted];
    } else if (providers[defaultConfig.name]) {
      name = defaultConfig.name;
      config = providers[defaultConfig.name];
    } else {
      name = Object.keys(providers)[0] || defaultConfig.name;
      config = providers[name] || defaultConfig;
    }
    return normalizeProvider(config, name);
  }

  /** 归属标注文本：解掉后端配置里的 HTML 实体并去掉标签（不把原始 HTML 塞进 DOM）。 */
  function attributionText(raw) {
    var text = String(raw === null || raw === undefined ? '' : raw)
      .replace(/<[^>]*>/g, '')
      .replace(/&copy;/gi, '©')
      .replace(/&amp;/gi, '&')
      .replace(/&nbsp;/gi, ' ');
    return text.trim();
  }

  /** 自动化/调试：?engine=mock 时连接指定引擎，缺省 auto（与旧 Web UI 的约定一致）。 */
  function engineChoice(search) {
    try {
      var params = new URLSearchParams(search || '');
      var value = params.get('engine');
      return value || 'auto';
    } catch (err) {
      return 'auto';
    }
  }

  /** 启动参数：?lat=&lon=&zoom= 只用于调试/验收，正常由后端位置驱动。 */
  function viewParams(search) {
    var out = { lat: null, lon: null, zoom: null, autoconnect: false };
    try {
      var params = new URLSearchParams(search || '');
      var lat = Number(params.get('lat'));
      var lon = Number(params.get('lon'));
      var zoom = Number(params.get('zoom'));
      out.lat = isFiniteNumber(lat) && params.get('lat') !== null ? lat : null;
      out.lon = isFiniteNumber(lon) && params.get('lon') !== null ? lon : null;
      out.zoom = isFiniteNumber(zoom) && params.get('zoom') !== null ? zoom : null;
      out.autoconnect = params.get('autoconnect') === '1';
    } catch (err) { /* 忽略 */ }
    return out;
  }

  function truncate(text, max) {
    var value = String(text === null || text === undefined ? '' : text);
    var limit = max || 120;
    return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
  }

  root.LocPilotModel = {
    DEFAULT_PROVIDER: DEFAULT_PROVIDER,
    MIN_ZOOM: MIN_ZOOM,
    isFiniteNumber: isFiniteNumber,
    positionOf: positionOf,
    formatCoord: formatCoord,
    readoutText: readoutText,
    statusColor: statusColor,
    deviceName: deviceName,
    deviceView: deviceView,
    isBusyPhase: isBusyPhase,
    providerConfig: providerConfig,
    attributionText: attributionText,
    engineChoice: engineChoice,
    viewParams: viewParams,
    truncate: truncate
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
