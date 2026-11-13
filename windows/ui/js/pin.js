/* LocPilot — 地图大头针状态机与渲染
 *
 * 与 macOS 版 PinAnimator / MapLocationPin 同构（时间线、阶段语义、视觉结构都对齐）：
 *   idle -> dropping（目标圆环浮现 + 从 80px 高处落下）-> landed（落地冲击 + 白色波纹）
 *        -> processing（200~400ms「系统确认」停留，等业务结果）
 *        -> success（红 -> 绿 morph + 白色对勾绘制 + 绿色波纹）
 *        -> error（白色感叹号淡入 + 以针尖为轴摇晃 + 红色波纹）
 *
 * 关键纪律：
 *  * 组件只管「怎么动」，业务只管「什么时候动」——成功/失败由业务显式调用 succeed()/fail()；
 *  * 所有单段动画时长 0.1~0.5s（DEFAULT_TIMINGS）；
 *  * 连续改点时旧时间线立刻失效（runId 令牌），两段动画不会互相打架；
 *  * 落针期间 processing 状态必须保持住，让用户感觉是「系统在确认」。
 *
 * 结构：PinAnimator 是纯状态机（node 可测）；PinView 是 DOM 渲染器（浏览器）。
 * 普通脚本，挂到 globalThis.LocPilotPin。
 */
(function (root) {
  'use strict';

  var DEFAULT_TIMINGS = {
    targetRing: 0.12,
    drop: 0.32,
    impact: 0.30,
    processing: 0.28,
    morph: 0.30,
    checkDraw: 0.22,
    shake: 0.30,
    ripple: 0.40
  };

  var PHASES = ['idle', 'dropping', 'landed', 'processing', 'success', 'error'];
  var SHARE_ANGLES = [-4.0, 4.0, -2.0, 2.0, 0.0];

  function mergeTimings(timings) {
    var out = {};
    Object.keys(DEFAULT_TIMINGS).forEach(function (key) {
      var value = timings && typeof timings[key] === 'number' ? timings[key] : DEFAULT_TIMINGS[key];
      out[key] = value;
    });
    return out;
  }

  function defaultSleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, Math.max(0, ms)); });
  }

  /* ------------------------------------------------------------------ *
   * PinAnimator —— 状态机（不碰 DOM）
   * ------------------------------------------------------------------ */
  function PinAnimator(options) {
    options = options || {};
    this.timings = mergeTimings(options.timings);
    this.sleep = typeof options.sleep === 'function' ? options.sleep : defaultSleep;
    this.listeners = typeof options.onChange === 'function' ? [options.onChange] : [];
    this.runId = 0;
    this.phase = 'idle';
    this.phaseLog = [];
    this.drop = 1;          // 1 = 针已落地（不是动画进度，是相对地面的位置）
    this.impact = 0;
    this.morph = 0;
    this.checkDraw = 0;
    this.exclamation = 0;
    this.shake = 0;
    this.ring = 0;
    this.ripple = 0;
    this.rippleKind = 'neutral';
  }

  PinAnimator.prototype.subscribe = function (listener) {
    var self = this;
    if (typeof listener !== 'function') { return function () {}; }
    this.listeners.push(listener);
    return function () {
      var index = self.listeners.indexOf(listener);
      if (index >= 0) { self.listeners.splice(index, 1); }
    };
  };

  PinAnimator.prototype.snapshot = function () {
    return {
      phase: this.phase,
      drop: this.drop,
      impact: this.impact,
      morph: this.morph,
      checkDraw: this.checkDraw,
      exclamation: this.exclamation,
      shake: this.shake,
      ring: this.ring,
      ripple: this.ripple,
      rippleKind: this.rippleKind,
      phaseLog: this.phaseLog.slice()
    };
  };

  PinAnimator.prototype._emit = function () {
    var state = this.snapshot();
    var listeners = this.listeners.slice();
    for (var i = 0; i < listeners.length; i += 1) {
      try { listeners[i](state); } catch (err) { /* 渲染层异常不能打断动画时间线 */ }
    }
  };

  PinAnimator.prototype._set = function (name, value) {
    this[name] = value;
    this._emit();
  };

  PinAnimator.prototype._enter = function (phase) {
    this.phase = phase;
    this.phaseLog.push(phase);
    this._emit();
  };

  PinAnimator.prototype._ms = function (seconds) {
    return Math.max(0, seconds * 1000);
  };

  /** 复位：清空动画进度与阶段记录，并让正在跑的时间线失效。 */
  PinAnimator.prototype.reset = function () {
    this.runId += 1;
    this.phase = 'idle';
    this.drop = 1;
    this.impact = 0;
    this.morph = 0;
    this.checkDraw = 0;
    this.exclamation = 0;
    this.shake = 0;
    this.ring = 0;
    this.ripple = 0;
    this.rippleKind = 'neutral';
    this.phaseLog = [];
    this._emit();
  };

  /**
   * 落针：圆环浮现 -> 下落 -> 落地冲击 -> 波纹 -> 进入 processing 等待业务结果。
   * resolve 时 phase 已是 processing；被更晚的落针/复位打断时返回 null。
   */
  PinAnimator.prototype.dropPin = async function () {
    this.reset();
    var token = this.runId;

    this._enter('dropping');
    this._set('ring', 1);

    // 先提交「悬空」状态，否则下一帧直接到落地会被合并
    this._set('drop', 0);
    await this.sleep(16);
    if (token !== this.runId) { return null; }

    // 重力加速下落：cubic-bezier(0.55, 0, 0.9, 0.45)
    this._set('drop', 1);
    await this.sleep(this._ms(this.timings.drop * 0.9));
    if (token !== this.runId) { return null; }

    this._enter('landed');
    this.rippleKind = 'neutral';
    this.ripple = 0;
    this._set('ripple', 1);

    // 落地冲击：压缩 -> 回弹 -> 收住（针尖不动，只有针体形变）
    var step = this.timings.impact / 3;
    this._set('impact', 1);
    await this.sleep(this._ms(step));
    if (token !== this.runId) { return null; }
    this._set('impact', -0.55);
    await this.sleep(this._ms(step));
    if (token !== this.runId) { return null; }
    this._set('impact', 0);
    await this.sleep(this._ms(step));
    if (token !== this.runId) { return null; }

    // 普通红针停留 200~400ms：让用户感觉是「系统在确认」，不是播完动画就出结果
    this._enter('processing');
    await this.sleep(this._ms(this.timings.processing));
    if (token !== this.runId) { return null; }
    return this.phase;
  };

  /** 定位成功：红 -> 绿 + 白色对勾绘制 + 绿色波纹。非 processing 状态调用是 no-op。 */
  PinAnimator.prototype.succeed = async function () {
    if (this.phase !== 'processing') { return false; }
    var token = this.runId;
    this._enter('success');
    this._set('morph', 1);
    await this.sleep(this._ms(this.timings.morph * 0.55));
    if (token !== this.runId) { return false; }
    this._set('checkDraw', 1);
    this._set('rippleKind', 'success');
    this._set('ripple', 0);
    this._set('ripple', 1);
    return true;
  };

  /** 定位失败：感叹号淡入 + 以针尖为轴摇晃 + 红色波纹。非 processing 状态调用是 no-op。 */
  PinAnimator.prototype.fail = async function () {
    if (this.phase !== 'processing') { return false; }
    var token = this.runId;
    this._enter('error');
    this._set('exclamation', 1);
    this._set('rippleKind', 'error');
    this._set('ripple', 0);
    this._set('ripple', 1);

    var step = this.timings.shake / 5;
    for (var i = 0; i < SHARE_ANGLES.length; i += 1) {
      this._set('shake', SHARE_ANGLES[i]);
      await this.sleep(this._ms(step));
      if (token !== this.runId) { return false; }
    }
    this._set('shake', 0);
    return true;
  };

  /* ------------------------------------------------------------------ *
   * PinView —— DOM 渲染器
   * ------------------------------------------------------------------ */
  var DROP_PX = 80;

  var PIN_HTML = [
    '<div class="lp-pin" data-phase="idle">',
    '  <div class="lp-pin-shadow"></div>',
    '  <div class="lp-pin-ring"></div>',
    '  <div class="lp-pin-ripple"></div>',
    '  <div class="lp-pin-fall">',
    '    <div class="lp-pin-shake">',
    '      <div class="lp-pin-body">',
    '        <svg class="lp-pin-shape" viewBox="0 0 44 52" width="44" height="52" aria-hidden="true">',
    '          <path class="lp-pin-tail" d="M15.5 36 L28.5 36 L22 51 Z"></path>',
    '          <circle class="lp-pin-head" cx="22" cy="19" r="19"></circle>',
    '        </svg>',
    '        <svg class="lp-pin-check" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">',
    '          <path pathLength="1" d="M4.6 12.7 L9.7 17.8 L19.5 6.2"></path>',
    '        </svg>',
    '        <span class="lp-pin-bang" aria-hidden="true"><i></i><i></i></span>',
    '      </div>',
    '    </div>',
    '  </div>',
    '</div>'
  ].join('\n');

  function PinView(options) {
    options = options || {};
    this.el = null;
    this.timings = mergeTimings(options.timings);
    this.prev = null;
    this.fallAnim = null;
    this.impactAnim = null;
    this.shakeAnim = null;
    this.suspended = false;
    this.reduceMotion = false;
    try {
      this.reduceMotion = typeof root.matchMedia === 'function' &&
        root.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (err) { this.reduceMotion = false; }
  }

  PinView.prototype.attach = function (element) {
    if (!element) { return; }
    this.el = element;
    this.shadowEl = element.querySelector('.lp-pin-shadow');
    this.ringEl = element.querySelector('.lp-pin-ring');
    this.rippleEl = element.querySelector('.lp-pin-ripple');
    this.fallEl = element.querySelector('.lp-pin-fall');
    this.shakeEl = element.querySelector('.lp-pin-shake');
    this.bodyEl = element.querySelector('.lp-pin-body');
    this.checkEl = element.querySelector('.lp-pin-check');
    this.applyTimingVars();
    this.prev = null;
    this.resetVisual();
  };

  PinView.prototype.detach = function () {
    this.cancelAnimations();
    this.el = null;
    this.prev = null;
  };

  PinView.prototype.applyTimingVars = function () {
    if (!this.el) { return; }
    var style = this.el.style;
    style.setProperty('--pin-morph', this.timings.morph.toFixed(3) + 's');
    style.setProperty('--ring-duration', this.timings.targetRing.toFixed(3) + 's');
    style.setProperty('--ripple-duration', this.timings.ripple.toFixed(3) + 's');
    style.setProperty('--check-duration', this.timings.checkDraw.toFixed(3) + 's');
  };

  PinView.prototype.cancelAnimations = function () {
    [this.fallAnim, this.impactAnim, this.shakeAnim].forEach(function (animation) {
      if (animation) { try { animation.cancel(); } catch (err) { /* 忽略 */ } }
    });
    this.fallAnim = null;
    this.impactAnim = null;
    this.shakeAnim = null;
  };

  PinView.prototype.resetVisual = function () {
    if (!this.el) { return; }
    this.cancelAnimations();
    var el = this.el;
    el.classList.remove('is-dropping', 'is-success', 'is-error', 'is-check');
    el.dataset.phase = 'idle';
    this.suspended = false;
    if (this.fallEl) { this.fallEl.style.transform = 'translateY(0px)'; }
    if (this.bodyEl) { this.bodyEl.style.transform = 'scale(1, 1)'; }
    if (this.shakeEl) { this.shakeEl.style.transform = 'rotate(0deg)'; }
    if (this.shadowEl) { this.shadowEl.style.opacity = '1'; this.shadowEl.style.transform = 'scale(1)'; }
    if (this.rippleEl) { this.rippleEl.className = 'lp-pin-ripple'; }
    this.applyTimingVars();
  };

  function hasAnimate(element) {
    return Boolean(element && typeof element.animate === 'function');
  }

  PinView.prototype.startFall = function () {
    if (!this.el) { return; }
    var duration = this.timings.drop * 1000;
    if (this.reduceMotion || !hasAnimate(this.fallEl)) {
      if (this.fallEl) { this.fallEl.style.transform = 'translateY(0px)'; }
      return;
    }
    var self = this;
    this.fallAnim = this.fallEl.animate(
      [{ transform: 'translateY(-' + DROP_PX + 'px)' }, { transform: 'translateY(0px)' }],
      { duration: duration, easing: 'cubic-bezier(0.55, 0.0, 0.9, 0.45)', fill: 'forwards' }
    );
    if (this.shadowEl && hasAnimate(this.shadowEl)) {
      this.shadowEl.animate(
        [{ opacity: 0.1, transform: 'scale(1.6)' }, { opacity: 0.26, transform: 'scale(1)' }],
        { duration: duration, easing: 'ease-out', fill: 'forwards' }
      );
    }
    this.fallAnim.onfinish = function () {
      self.fallAnim = null;
      if (self.fallEl) { self.fallEl.style.transform = 'translateY(0px)'; }
    };
    this.fallAnim.oncancel = function () { self.fallAnim = null; };
  };

  PinView.prototype.startImpact = function () {
    if (!this.el || !this.bodyEl || !hasAnimate(this.bodyEl) || this.reduceMotion) { return; }
    var duration = this.timings.impact * 1000;
    this.impactAnim = this.bodyEl.animate([
      { transform: 'scale(1, 1)', offset: 0, easing: 'ease-out' },
      { transform: 'scale(1.04, 0.93)', offset: 0.33, easing: 'ease-out' },
      { transform: 'scale(0.978, 1.0385)', offset: 0.66, easing: 'ease-in-out' },
      { transform: 'scale(1, 1)', offset: 1 }
    ], { duration: duration, fill: 'forwards' });
  };

  PinView.prototype.startShake = function () {
    if (!this.el || !this.shakeEl || !hasAnimate(this.shakeEl) || this.reduceMotion) { return; }
    var step = this.timings.shake / 5 * 1000;
    var frames = SHARE_ANGLES.map(function (angle) {
      return { transform: 'rotate(' + angle + 'deg)', easing: 'ease-in-out' };
    });
    this.shakeAnim = this.shakeEl.animate(frames, { duration: step * SHARE_ANGLES.length, fill: 'forwards' });
    var self = this;
    this.shakeAnim.onfinish = function () {
      self.shakeAnim = null;
      if (self.shakeEl) { self.shakeEl.style.transform = 'rotate(0deg)'; }
    };
  };

  PinView.prototype.restartRipple = function (kind) {
    if (!this.rippleEl) { return; }
    this.rippleEl.className = 'lp-pin-ripple';
    void this.rippleEl.offsetWidth;             // 强制重排，让动画能重新播放
    this.rippleEl.classList.add(kind === 'success' ? 'is-success' : kind === 'error' ? 'is-error' : 'is-neutral');
    this.rippleEl.classList.add('is-rippling');
  };

  /** 把状态机的快照映射到 DOM；只处理「阶段/数值发生变化」的边沿，避免每帧重排。 */
  PinView.prototype.sync = function (state) {
    if (!this.el) { this.prev = state; return; }
    var prev = this.prev || { phase: 'idle', drop: 1, checkDraw: 0, morph: 0 };
    this.el.dataset.phase = state.phase;

    // 目标圆环：落针阶段浮现，落地后淡出
    this.el.classList.toggle('is-dropping', state.phase === 'dropping');

    // 下落：drop=0 悬空；drop 回到 1 时播放一次下落动画
    if (state.drop <= 0.001 && prev.drop > 0.001) {
      this.cancelFallOnly();
      this.suspended = true;
      if (this.fallEl) { this.fallEl.style.transform = 'translateY(-' + DROP_PX + 'px)'; }
      if (this.shadowEl) { this.shadowEl.style.opacity = '0.1'; this.shadowEl.style.transform = 'scale(1.6)'; }
    } else if (state.drop >= 0.999 && this.suspended) {
      this.suspended = false;
      this.startFall();
    } else if (!this.suspended && !this.fallAnim && prev.drop < 0.001 && state.drop >= 0.999) {
      this.startFall();
    }

    // 落地：冲击形变 + 白色波纹
    if (state.phase === 'landed' && prev.phase !== 'landed') {
      this.startImpact();
      this.restartRipple('neutral');
    }

    // 成功：红 -> 绿（CSS fill transition）+ 对勾绘制 + 绿色波纹
    if (state.phase === 'success' && prev.phase !== 'success') {
      this.el.classList.add('is-success');
      this.restartRipple('success');
    }

    // 失败：感叹号 + 摇晃 + 红色波纹
    if (state.phase === 'error' && prev.phase !== 'error') {
      this.el.classList.add('is-error');
      this.startShake();
      this.restartRipple('error');
    }

    // 对勾在 morph 之后才开始画（animator 在 morph*0.55 后才把 checkDraw 置 1）
    if (state.checkDraw > 0.001 && !this.el.classList.contains('is-check')) {
      this.el.classList.add('is-check');
    }

    // 复位：回到普通红针、落地姿态
    if (state.phase === 'idle') {
      if (prev.phase !== 'idle') { this.resetVisual(); }
      else if (this.fallEl && !this.fallAnim && !this.suspended) {
        this.fallEl.style.transform = 'translateY(0px)';
      }
    }

    this.prev = state;
  };

  PinView.prototype.cancelFallOnly = function () {
    if (this.fallAnim) {
      try { this.fallAnim.cancel(); } catch (err) { /* 忽略 */ }
      this.fallAnim = null;
    }
  };

  PinView.createHTML = function () { return PIN_HTML; };

  root.LocPilotPin = {
    DEFAULT_TIMINGS: DEFAULT_TIMINGS,
    PHASES: PHASES,
    PinAnimator: PinAnimator,
    PinView: PinView,
    createPinHTML: function () { return PIN_HTML; },
    mergeTimings: mergeTimings
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
