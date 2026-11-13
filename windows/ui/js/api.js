/* LocPilot — 后端 API 客户端（HTTP + SSE）
 *
 * 页面与后端同源（pywebview 加载 http://127.0.0.1:<port>/，由后端静态服务提供本目录），
 * 所以一律用相对路径 /api/...：不写死端口、不拼绝对 URL、不需要 CORS。
 *
 * 契约以 locpilot/api.py 为准：
 *   GET  /api/status         -> 快照（engine / position / tile_providers / error ...）
 *   POST /api/connect        -> {engine: 'auto'|'mock'|...}
 *   POST /api/disconnect     -> {}
 *   POST /api/teleport       -> {lat, lon}（WGS-84！）
 *   POST /api/clear          -> {}
 *   GET  /api/events         -> SSE，event: snapshot
 * 错误体统一为 {"ok": false, "error": "..."}，客户端优先取 error 字段作为 message。
 *
 * 经典脚本（无打包），挂到 globalThis.LocPilotApi；fetch/EventSource 可注入，node 测试无依赖。
 */
(function (root) {
  'use strict';

  function createClient(options) {
    options = options || {};
    var fetchImpl = options.fetch || (typeof root.fetch === 'function' ? root.fetch.bind(root) : null);
    var eventSourceImpl = options.EventSource !== undefined
      ? options.EventSource
      : (typeof root.EventSource === 'function' ? root.EventSource : null);
    var base = options.base || '';

    function url(path) { return base + path; }

    function request(path, init) {
      if (!fetchImpl) {
        return Promise.reject(new Error('当前环境不支持 fetch'));
      }
      return fetchImpl(url(path), init || {}).then(function (response) {
        return response.text().then(function (text) {
          var data = null;
          if (text) {
            try { data = JSON.parse(text); } catch (err) { data = null; }
          }
          if (!response.ok) {
            var message = (data && (data.error || data.message)) || ('HTTP ' + response.status);
            var error = new Error(message);
            error.status = response.status;
            error.data = data;
            throw error;
          }
          return data;
        });
      });
    }

    function post(path, body) {
      return request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
      });
    }

    return {
      base: base,

      status: function () {
        return request('/api/status', { method: 'GET' });
      },

      connect: function (engine) {
        return post('/api/connect', { engine: engine || 'auto' });
      },

      disconnect: function () {
        return post('/api/disconnect', {});
      },

      teleport: function (lat, lon) {
        return post('/api/teleport', { lat: lat, lon: lon });
      },

      clear: function () {
        return post('/api/clear', {});
      },

      /** 订阅 SSE。onSnapshot(snapshot) 收到每个快照；onError() 在连接异常时回调。 */
      events: function (onSnapshot, onError) {
        if (!eventSourceImpl || typeof eventSourceImpl !== 'function') { return null; }
        var source = new eventSourceImpl(url('/api/events'));
        source.addEventListener('snapshot', function (event) {
          var data = null;
          try { data = JSON.parse(event.data); } catch (err) { return; }
          if (data) { onSnapshot(data); }
        });
        source.onerror = function () {
          if (typeof onError === 'function') { onError(); }
        };
        return source;
      }
    };
  }

  root.LocPilotApi = { createClient: createClient };
})(typeof globalThis !== 'undefined' ? globalThis : this);
