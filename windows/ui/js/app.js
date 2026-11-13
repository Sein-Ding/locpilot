/* LocPilot Windows UI —— 主程序
 *
 * 架构：pywebview 打开 http://127.0.0.1:<port>/，页面与后端同源；本文件只通过相对
 * 路径 /api/... 与后端通信（HTTP + SSE），不写死端口、不依赖任何 CDN。
 *
 * 交互对齐 macOS 版：
 *   * 全屏高德地图（GCJ-02 纠偏），点击地图 -> 落针动画 -> POST /api/teleport(WGS-84)
 *     -> 成功变绿画勾 / 失败红针（失败绝不写死进动画，由业务结果驱动）；
 *   * 右上控件：设备状态（绿/黄/红，点开显示连接/断开）、跟随定位、恢复真实定位、缩放；
 *   * 左下角：坐标读数，与「高德地图」归属标注同一行；无定位时读数完全不出现；
 *   * 窗口可任意缩放，布局随视口自适应。
 */
(function (root, doc) {
  'use strict';

  var Geo = root.LocPilotGeo;
  var Model = root.LocPilotModel;
  var Api = root.LocPilotApi;
  var PinLib = root.LocPilotPin;

  var INITIAL_CENTER = [31.2304, 121.4737];   // 与 macOS 版初始相机一致（上海）
  var INITIAL_ZOOM = 12;
  var FOLLOW_ZOOM = 15;                        // 跟随定位时至少放到这个级别（≈1.5km 视野）
  var PIN_SIZE = [44, 52];
  var PIN_ANCHOR = [22, 52];
  var POSITION_EPSILON = 1e-9;

  var state = {
    snapshot: null,
    reachable: true,
    connecting: false,
    lastError: null,
    position: null,          // WGS-84 真值
    marker: null,
    markerPosition: null,
    provider: null,
    crs: 'gcj02',
    tileLayer: null,
    following: false,
    lastFollowTarget: null,
    didInitialCenter: false,
    selectionId: 0,
    teleportQueue: Promise.resolve(),
    popoverOpen: false,
    bootError: null,
    toastTimer: null
  };

  var map = null;
  var pin = null;
  var pinView = null;
  var client = null;
  var eventSource = null;
  var els = {};

  function $(id) { return doc.getElementById(id); }

  function cacheElements() {
    els.app = $('app');
    els.map = $('map');
    els.controls = $('map-controls');
    els.deviceBtn = $('device-btn');
    els.popover = $('device-popover');
    els.popoverDot = $('popover-dot');
    els.popoverTitle = $('popover-title');
    els.popoverSpinner = $('popover-spinner');
    els.popoverAction = $('popover-action');
    els.popoverError = $('popover-error');
    els.followBtn = $('follow-btn');
    els.clearBtn = $('clear-btn');
    els.zoomIn = $('zoom-in');
    els.zoomOut = $('zoom-out');
    els.attribution = $('attribution');
    els.readout = $('readout');
    els.toast = $('toast');
    els.mapError = $('map-error');
  }

  /* ---------- 顶部提示 ---------- */
  function cssVar(name) {
    try {
      return getComputedStyle(doc.documentElement).getPropertyValue(name).trim();
    } catch (err) { return ''; }
  }

  function showToast(message, isError) {
    if (!els.toast) { return; }
    els.toast.textContent = message;
    els.toast.className = isError ? 'err' : '';
    els.toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(function () {
      els.toast.hidden = true;
    }, isError ? 5200 : 2400);
  }

  /* ---------- 地图 ---------- */
  function leafletAvailable() {
    return root.L && typeof root.L.map === 'function';
  }

  function initMap() {
    if (!leafletAvailable()) {
      els.mapError.hidden = false;
      return false;
    }
    var provider = Model.providerConfig(null, null);
    var center = Geo.toDisplay(INITIAL_CENTER[0], INITIAL_CENTER[1], provider.crs);
    map = root.L.map('map', {
      center: center,
      zoom: INITIAL_ZOOM,
      zoomControl: false,
      attributionControl: false,   // 归属由左下角自绘，与坐标读数同一行
      scrollWheelZoom: true,
      worldCopyJump: true,
      zoomSnap: 1,
      zoomDelta: 1,
      minZoom: provider.minZoom,
      maxZoom: provider.maxZoom,
      fadeAnimation: true
    });
    applyProvider(provider);
    map.on('click', function (event) {
      var wgs = Geo.fromDisplay(event.latlng.lat, event.latlng.lng, state.crs);
      selectLocation(wgs[0], wgs[1]).catch(function (err) {
        console.warn('[LocPilot] 点击传送流程异常', err);
      });
    });
    return true;
  }

  function applyProvider(config) {
    if (!map || !config || !config.url) { return; }
    if (state.tileLayer) {
      map.removeLayer(state.tileLayer);
      state.tileLayer = null;
    }
    if (config.maxZoom) { map.setMaxZoom(config.maxZoom); }
    if (config.minZoom) { map.setMinZoom(config.minZoom); }
    state.tileLayer = root.L.tileLayer(config.url, {
      maxZoom: config.maxZoom,
      minZoom: config.minZoom,
      subdomains: config.subdomains || 'abc',
      attribution: '',
      crossOrigin: true,
      keepBuffer: 6,
      updateWhenZooming: true,
      updateWhenIdle: false
    }).addTo(map);
    state.provider = config;
    state.crs = config.crs;
    els.attribution.textContent = Model.attributionText(config.attribution);
    els.attribution.title = config.label || '';
    if (state.marker && state.markerPosition) {
      state.marker.setLatLng(displayLatLng(state.markerPosition));
    }
  }

  function displayLatLng(position) {
    var display = Geo.toDisplay(position.lat, position.lon, state.crs);
    return root.L.latLng(display[0], display[1]);
  }

  function centerOn(position, zoom) {
    if (!map) { return; }
    var target = displayLatLng(position);
    if (typeof zoom === 'number') {
      map.setView(target, zoom, { animate: true, duration: 0.45 });
    } else {
      map.panTo(target, { animate: true, duration: 0.45 });
    }
  }

  /* ---------- 大头针标记 ---------- */
  function createMarker(position) {
    var icon = root.L.divIcon({
      className: 'lp-pin-icon',
      html: PinLib.createPinHTML(),
      iconSize: PIN_SIZE,
      iconAnchor: PIN_ANCHOR
    });
    var marker = root.L.marker(displayLatLng(position), {
      icon: icon,
      interactive: false,
      keyboard: false,
      zIndexOffset: 1200
    }).addTo(map);
    state.marker = marker;
    state.markerPosition = { lat: position.lat, lon: position.lon };
    var element = marker.getElement();
    if (element) {
      pinView.attach(element.querySelector('.lp-pin'));
      pinView.prev = null;
      pinView.sync(pin.snapshot());
    }
    return marker;
  }

  function ensureMarker(position) {
    if (!map) { return; }
    if (!state.marker) {
      createMarker(position);
    } else {
      state.marker.setLatLng(displayLatLng(position));
      state.markerPosition = { lat: position.lat, lon: position.lon };
    }
  }

  function removeMarker() {
    if (state.marker && map) { map.removeLayer(state.marker); }
    state.marker = null;
    state.markerPosition = null;
    if (pinView) { pinView.detach(); }
  }

  function samePosition(a, b) {
    return Boolean(a && b) &&
      Math.abs(a.lat - b.lat) < POSITION_EPSILON &&
      Math.abs(a.lon - b.lon) < POSITION_EPSILON;
  }

  /** 快照里的位置 -> 地图标记；落针动画期间不动标记，避免和动画抢坐标。 */
  function syncPosition(position) {
    var busy = Model.isBusyPhase(pin.phase);
    if (position) {
      state.position = { lat: position.lat, lon: position.lon };
      if (!busy) {
        if (!state.marker || !samePosition(state.markerPosition, position)) {
          ensureMarker(position);
          if (!Model.isBusyPhase(pin.phase)) {
            pin.reset();                 // 外部改点（例如另一客户端）：直接显示，不播落针动画
            if (pinView) { pinView.prev = null; pinView.sync(pin.snapshot()); }
          }
        }
      }
    } else if (!busy) {
      // 失败红针要留在屏幕上给用户看；只有真正没有定位（清除/断开）才移除标记
      if (state.marker && pin.phase !== 'error') { removeMarker(); }
      state.position = null;
    }
    renderReadout();
    renderFollow();
  }

  /* ---------- 传送流程 ---------- */
  function enqueueTeleport(lat, lon) {
    var run = state.teleportQueue.catch(function () {}).then(function () {
      return client.teleport(lat, lon);
    });
    state.teleportQueue = run.catch(function () {});
    return run;
  }

  /**
   * 点击地图的完整语义（对齐 macOS AppState.selectLocation）：
   * 落针（圆环 -> 下落 -> 冲击 -> processing）完成后才下发定位，再按结果驱动成功/失败。
   */
  async function selectLocation(lat, lon) {
    var token = state.selectionId + 1;
    state.selectionId = token;
    ensureMarker({ lat: lat, lon: lon });
    pin.reset();
    if (pinView) { pinView.prev = null; pinView.sync(pin.snapshot()); }

    try {
      await pin.dropPin();
      if (token !== state.selectionId) { return; }
      await enqueueTeleport(lat, lon);
      if (token !== state.selectionId) { return; }
      state.position = { lat: lat, lon: lon };
      renderReadout();
      await pin.succeed();
    } catch (err) {
      if (token !== state.selectionId) { return; }
      console.warn('[LocPilot] 传送失败', err);
      var detail = err && err.message ? Model.truncate(err.message, 100) : '';
      showToast(detail ? '传送失败：' + detail : '传送失败', true);
      await pin.fail();
    }
  }

  /* ---------- 设备连接 ---------- */
  function engineChoice() {
    return Model.engineChoice(root.location ? root.location.search : '');
  }

  async function connectDevice() {
    if (state.connecting) { return; }
    state.connecting = true;
    state.lastError = null;
    renderDevice();
    try {
      var result = await client.connect(engineChoice());
      if (result && result.notes && result.notes.length) {
        showToast(result.notes.join(' '), false);
      }
      await refresh();
    } catch (err) {
      state.lastError = Model.truncate(err.message || String(err), 200);
      showToast('连接失败：' + state.lastError, true);
    } finally {
      state.connecting = false;
      renderDevice();
      renderStatus();
    }
  }

  async function disconnectDevice() {
    if (state.connecting) { return; }
    try {
      await client.disconnect();
      state.lastError = null;
      state.position = null;
      state.following = false;
      state.lastFollowTarget = null;
      removeMarker();
      renderReadout();
      renderFollow();
      await refresh();
    } catch (err) {
      showToast('断开失败：' + Model.truncate(err.message || String(err), 100), true);
    }
  }

  async function clearLocation() {
    try {
      await client.clear();
      state.position = null;
      state.following = false;
      state.lastFollowTarget = null;
      removeMarker();
      renderReadout();
      renderFollow();
      showToast('已恢复真实定位', false);
      await refresh();
    } catch (err) {
      showToast('恢复失败：' + Model.truncate(err.message || String(err), 100), true);
    }
  }

  /* ---------- 跟随定位 ---------- */
  function toggleFollow() {
    if (!state.position) {
      showToast('还没有设定位置，点一下地图即可', false);
      return;
    }
    state.following = !state.following;
    renderFollow();
    if (state.following) {
      state.lastFollowTarget = { lat: state.position.lat, lon: state.position.lon };
      centerOn(state.position, Math.max(map ? map.getZoom() : FOLLOW_ZOOM, FOLLOW_ZOOM));
    }
  }

  function followIfNeeded() {
    if (!state.following || !state.position || !map) { return; }
    if (samePosition(state.lastFollowTarget, state.position)) { return; }
    state.lastFollowTarget = { lat: state.position.lat, lon: state.position.lon };
    centerOn(state.position, Math.max(map.getZoom(), FOLLOW_ZOOM));
  }

  function initialCenterIfNeeded() {
    if (state.didInitialCenter || !state.position || !map) { return; }
    state.didInitialCenter = true;
    centerOn(state.position, Math.max(map.getZoom(), FOLLOW_ZOOM));
  }

  /* ---------- 缩放 ---------- */
  function zoomBy(delta) {
    if (!map) { return; }
    var target = Math.round(map.getZoom()) + delta;
    map.setZoom(target, { animate: true, duration: 0.18 });
  }

  /* ---------- 渲染 ---------- */
  function renderStatus() {
    var color = Model.statusColor(state.snapshot, state.reachable);
    var snapshot = state.snapshot || {};
    var opened = Boolean(snapshot.engine && snapshot.engine.opened);
    var text;
    if (state.reachable === false) {
      text = '后端未就绪';
    } else if (opened) {
      text = Model.deviceName(snapshot) || '已连接';
    } else {
      text = '设备未连接';
    }
    if (els.controls) { els.controls.style.setProperty('--status-color', 'var(--' + color + ')'); }
    els.deviceBtn.dataset.status = color;
    els.deviceBtn.title = '设备状态：' + text + (opened ? '（点击断开）' : '（点击连接）');
    els.deviceBtn.setAttribute('aria-label', '设备状态：' + text);
  }

  function renderReadout() {
    if (!els.readout) { return; }
    if (state.position) {
      els.readout.textContent = Model.formatCoord(state.position.lat, state.position.lon);
      els.readout.hidden = false;
    } else {
      els.readout.textContent = '';
      els.readout.hidden = true;          // 无定位：不画任何占位符
    }
  }

  function renderFollow() {
    if (!els.followBtn) { return; }
    els.followBtn.classList.toggle('active', state.following);
    els.followBtn.classList.toggle('dimmed', !state.position);
    els.followBtn.setAttribute('aria-pressed', state.following ? 'true' : 'false');
    els.followBtn.title = !state.position
      ? '还没有设定位置'
      : (state.following ? '正在跟随定位（点击停止）' : '跟随定位：镜头随定位自动移动');
  }

  function renderDevice() {
    if (!els.popover) { return; }
    var view = Model.deviceView({ snapshot: state.snapshot, connecting: state.connecting });
    els.popoverTitle.textContent = view.title;
    els.popoverSpinner.hidden = !view.spinner;
    if (view.action === 'none') {
      els.popoverAction.hidden = true;
      els.popoverAction.dataset.action = '';
    } else {
      els.popoverAction.hidden = false;
      els.popoverAction.textContent = view.actionLabel;
      els.popoverAction.dataset.action = view.action;
      els.popoverAction.disabled = false;
    }
    var error = !state.connecting && state.lastError ? state.lastError : '';
    els.popoverError.hidden = !error;
    els.popoverError.textContent = error;
  }

  function openPopover() {
    state.popoverOpen = true;
    els.popover.hidden = false;
    els.deviceBtn.setAttribute('aria-expanded', 'true');
    renderDevice();
  }

  function closePopover() {
    state.popoverOpen = false;
    els.popover.hidden = true;
    els.deviceBtn.setAttribute('aria-expanded', 'false');
  }

  function togglePopover() {
    if (state.popoverOpen) { closePopover(); } else { openPopover(); }
  }

  /* ---------- 快照 ---------- */
  function applySnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') { return; }
    state.snapshot = snapshot;
    state.reachable = true;

    var provider = Model.providerConfig(snapshot);
    if (!state.provider || state.provider.name !== provider.name) {
      applyProvider(provider);
    }
    if (snapshot.error) {
      state.lastError = Model.truncate(snapshot.error, 200);
    }
    syncPosition(Model.positionOf(snapshot));
    renderStatus();
    renderDevice();
    followIfNeeded();
    initialCenterIfNeeded();
  }

  async function refresh() {
    try {
      var snapshot = await client.status();
      applySnapshot(snapshot);
      return snapshot;
    } catch (err) {
      state.reachable = false;
      renderStatus();
      throw err;
    }
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    els.deviceBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      togglePopover();
    });
    els.popoverAction.addEventListener('click', function (event) {
      event.stopPropagation();
      var action = els.popoverAction.dataset.action;
      if (action === 'connect') { connectDevice(); }
      else if (action === 'disconnect') { disconnectDevice(); }
    });
    els.followBtn.addEventListener('click', function () { toggleFollow(); });
    els.clearBtn.addEventListener('click', function () { clearLocation(); });
    els.zoomIn.addEventListener('click', function () { zoomBy(1); });
    els.zoomOut.addEventListener('click', function () { zoomBy(-1); });

    els.app.addEventListener('click', function (event) {
      if (!state.popoverOpen) { return; }
      if (els.popover.contains(event.target) || els.deviceBtn.contains(event.target)) { return; }
      closePopover();
    });
    doc.addEventListener('keydown', function (event) {
      var meta = event.metaKey || event.ctrlKey;
      if (meta && (event.key === '=' || event.key === '+' || event.key === 'Add')) {
        event.preventDefault();
        zoomBy(1);
      } else if (meta && (event.key === '-' || event.key === '_' || event.key === 'Subtract')) {
        event.preventDefault();
        zoomBy(-1);
      } else if (event.key === 'Escape' && state.popoverOpen) {
        closePopover();
      }
    });

    var resizeQueued = false;
    root.addEventListener('resize', function () {
      if (resizeQueued || !map) { return; }
      resizeQueued = true;
      setTimeout(function () {
        resizeQueued = false;
        if (map) { map.invalidateSize(false); }
      }, 60);
    });
  }

  /* ---------- 启动 ---------- */
  function exposeHooks() {
    root.__locpilot = {
      ready: false,
      bootError: null,
      state: state,
      pin: pin,
      pinState: null,
      geo: {
        toDisplay: Geo.toDisplay,
        fromDisplay: Geo.fromDisplay,
        wgs2gcj: Geo.wgs2gcj,
        gcj2wgs: Geo.gcj2wgs,
        offsetMeters: Geo.offsetMeters
      },
      model: Model,
      map: function () { return map; },
      actions: {
        connect: connectDevice,
        disconnect: disconnectDevice,
        teleport: selectLocation,
        clear: clearLocation,
        follow: toggleFollow,
        zoomIn: function () { zoomBy(1); },
        zoomOut: function () { zoomBy(-1); },
        refresh: refresh
      }
    };
  }

  async function boot() {
    cacheElements();
    pin = new PinLib.PinAnimator();
    pinView = new PinLib.PinView({ timings: pin.timings });
    pin.subscribe(function (snapshotState) {
      if (root.__locpilot) { root.__locpilot.pinState = snapshotState; }
      pinView.sync(snapshotState);
    });
    exposeHooks();

    initMap();
    renderStatus();
    renderDevice();
    renderReadout();
    renderFollow();
    if (els.attribution && !els.attribution.textContent) {
      els.attribution.textContent = Model.attributionText(Model.DEFAULT_PROVIDER.attribution);
    }
    bind();

    client = Api.createClient({});
    try {
      var snapshot = await client.status();
      applySnapshot(snapshot);
    } catch (err) {
      state.reachable = false;
      state.bootError = String((err && err.message) || err);
      if (root.__locpilot) { root.__locpilot.bootError = state.bootError; }
      renderStatus();
      showToast('后端未就绪：' + Model.truncate(state.bootError, 120), true);
    }

    eventSource = client.events(function (snapshot) { applySnapshot(snapshot); }, function () {
      state.reachable = false;
      renderStatus();
    });

    // 调试/验收参数：?lat=&lon=&zoom=（不参与正常启动流程）
    var params = Model.viewParams(root.location ? root.location.search : '');
    if (map && params.lat !== null && params.lon !== null) {
      map.setView(Geo.toDisplay(params.lat, params.lon, state.crs), params.zoom || INITIAL_ZOOM);
      state.didInitialCenter = true;
    }
    if (params.autoconnect && !state.connecting) {
      connectDevice().catch(function (err) { console.warn('[LocPilot] 自动连接失败', err); });
    }

    root.__locpilot.ready = true;
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', function () {
      boot().catch(function (err) {
        root.__locpilot && (root.__locpilot.bootError = String((err && err.message) || err));
        showToast('启动失败：' + Model.truncate((err && err.message) || err, 120), true);
        console.error('[LocPilot] 启动失败', err);
      });
    });
  } else {
    boot().catch(function (err) {
      root.__locpilot && (root.__locpilot.bootError = String((err && err.message) || err));
      showToast('启动失败：' + Model.truncate((err && err.message) || err, 120), true);
      console.error('[LocPilot] 启动失败', err);
    });
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, document);
