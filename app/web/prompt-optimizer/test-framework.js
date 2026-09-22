/* ============================================================
 * 提示词优化系统 · 轻量测试框架（零依赖，浏览器 / Node 双端运行）
 * ------------------------------------------------------------
 *  - describe / it / beforeEach（支持 async）
 *  - expect 链式断言（含 toEqual 深比较、toThrow、toMatch 等）
 *  - 函数级覆盖率采集：installCoverage 包装命名空间方法与类原型方法
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  /* ---------------- 断言 ---------------- */
  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b) return false;
    if (a && b && typeof a === "object") {
      if (Array.isArray(a) !== Array.isArray(b)) return false;
      var ka = Object.keys(a), kb = Object.keys(b);
      if (ka.length !== kb.length) return false;
      for (var i = 0; i < ka.length; i++) {
        if (!Object.prototype.hasOwnProperty.call(b, ka[i])) return false;
        if (!deepEqual(a[ka[i]], b[ka[i]])) return false;
      }
      return true;
    }
    return false;
  }

  function fmt(v) {
    try { return typeof v === "string" ? v : JSON.stringify(v); } catch (e) { return String(v); }
  }

  function Expect(actual, negate) {
    this._actual = actual;
    this._negate = !!negate;
  }
  Expect.prototype._check = function (cond, msg) {
    var ok = this._negate ? !cond : cond;
    if (!ok) throw new Error((this._negate ? "期望不成立：" : "断言失败：") + msg);
  };
  Expect.prototype.toBe = function (expected) {
    this._check(this._actual === expected, fmt(this._actual) + " === " + fmt(expected));
  };
  Expect.prototype.toEqual = function (expected) {
    this._check(deepEqual(this._actual, expected), fmt(this._actual) + " 深等于 " + fmt(expected));
  };
  Expect.prototype.toBeClose = function (expected, delta) {
    this._check(Math.abs(this._actual - expected) <= (delta || 1e-6), this._actual + " ≈ " + expected);
  };
  Expect.prototype.toContain = function (sub) {
    var ok = typeof this._actual === "string"
      ? this._actual.indexOf(sub) !== -1
      : Array.isArray(this._actual) && this._actual.indexOf(sub) !== -1;
    this._check(ok, "结果包含 " + fmt(sub));
  };
  Expect.prototype.toMatch = function (re) {
    this._check(re.test(this._actual), "匹配 " + re + "，实际：" + fmt(String(this._actual).slice(0, 120)));
  };
  Expect.prototype.toBeTruthy = function () { this._check(!!this._actual, "结果为真，实际：" + fmt(this._actual)); };
  Expect.prototype.toBeFalsy = function () { this._check(!this._actual, "结果为假，实际：" + fmt(this._actual)); };
  Expect.prototype.toBeNull = function () { this._check(this._actual === null, "为 null"); };
  Expect.prototype.toBeDefined = function () { this._check(this._actual !== undefined, "已定义"); };
  Expect.prototype.toBeUndefined = function () { this._check(this._actual === undefined, "为 undefined"); };
  Expect.prototype.toBeGreaterThan = function (n) { this._check(this._actual > n, this._actual + " > " + n); };
  Expect.prototype.toBeGreaterThanOrEqual = function (n) { this._check(this._actual >= n, this._actual + " >= " + n); };
  Expect.prototype.toBeLessThan = function (n) { this._check(this._actual < n, this._actual + " < " + n); };
  Expect.prototype.toBeLessThanOrEqual = function (n) { this._check(this._actual <= n, this._actual + " <= " + n); };
  Expect.prototype.toHaveLength = function (n) { this._check(this._actual && this._actual.length === n, "length=" + (this._actual && this._actual.length) + " 期望 " + n); };
  Expect.prototype.toBeInstanceOf = function (Ctor) { this._check(this._actual instanceof Ctor, "是 " + (Ctor.name || "构造函数") + " 的实例"); };
  Expect.prototype.toThrow = function () {
    var threw = false;
    try { this._actual(); } catch (e) { threw = true; }
    this._check(threw, "函数抛出异常");
  };
  Object.defineProperty(Expect.prototype, "not", {
    get: function () { return new Expect(this._actual, !this._negate); },
  });

  function expect(actual) { return new Expect(actual, false); }

  /* ---------------- 套件注册表 ---------------- */
  var suites = [];
  var currentSuite = null;

  function describe(name, fn) {
    var suite = { name: name, cases: [], beforeEach: [] };
    suites.push(suite);
    var prev = currentSuite;
    currentSuite = suite;
    fn();
    currentSuite = prev;
  }
  function it(name, fn) {
    if (!currentSuite) throw new Error("it() 必须在 describe() 内调用");
    currentSuite.cases.push({ name: name, fn: fn });
  }
  function beforeEach(fn) { currentSuite.beforeEach.push(fn); }

  async function run() {
    var startedAt = Date.now();
    for (var s = 0; s < suites.length; s++) {
      var suite = suites[s];
      suite.passed = 0;
      suite.failed = 0;
      for (var i = 0; i < suite.cases.length; i++) {
        var c = suite.cases[i];
        try {
          for (var b = 0; b < suite.beforeEach.length; b++) await suite.beforeEach[b]();
          await c.fn();
          c.ok = true;
          suite.passed++;
        } catch (e) {
          c.ok = false;
          c.error = (e && e.stack) ? e.stack.split("\n").slice(0, 4).join("\n") : String(e);
          suite.failed++;
        }
      }
    }
    var totals = suites.reduce(function (acc, su) {
      acc.passed += su.passed; acc.failed += su.failed; return acc;
    }, { passed: 0, failed: 0 });
    return {
      suites: suites,
      totals: totals,
      totalCases: totals.passed + totals.failed,
      durationMs: Date.now() - startedAt,
      coverage: reportCoverage(),
    };
  }

  function reset() { suites.length = 0; }

  /* ---------------- 函数级覆盖率 ---------------- */
  var coverageGroups = {};

  function wrapFn(group, name, fn) {
    var wrapped = function () {
      coverageGroups[group].called[name] = true;
      return fn.apply(this, arguments);
    };
    wrapped.__original = fn;
    return wrapped;
  }

  /* 构造函数包装：必须保留 prototype 链，否则实例访问原型方法会失败 */
  function wrapCtor(group, name, Ctor) {
    var wrapped = function () {
      coverageGroups[group].called[name] = true;
      var inst = Object.create(Ctor.prototype);
      var ret = Ctor.apply(inst, arguments);
      return (ret && (typeof ret === "object" || typeof ret === "function")) ? ret : inst;
    };
    wrapped.prototype = Ctor.prototype;
    wrapped.__original = Ctor;
    return wrapped;
  }

  var CTOR_NAMES = { ContextCapture: true, PromptOptimizer: true };

  /** 包装一个命名空间对象的全部函数方法 */
  function instrumentObject(groupName, obj) {
    if (!obj || coverageGroups[groupName]) return;
    var group = coverageGroups[groupName] = { called: {}, total: {}, constructors: {} };
    Object.keys(obj).forEach(function (key) {
      if (typeof obj[key] !== "function") return;
      if (key.indexOf("__") === 0 || key.indexOf("runAll") === 0) return; /* 测试自身注入的函数不计入 */
      group.total[key] = true;
      if (CTOR_NAMES[key]) group.constructors[key] = true;
      obj[key] = CTOR_NAMES[key] ? wrapCtor(groupName, key, obj[key]) : wrapFn(groupName, key, obj[key]);
    });
  }

  /** 包装类原型方法 */
  function instrumentPrototype(groupName, proto, ctorName) {
    if (!proto || !coverageGroups[groupName]) return;
    var group = coverageGroups[groupName];
    Object.getOwnPropertyNames(proto).forEach(function (key) {
      if (key === "constructor") return;
      var desc = Object.getOwnPropertyDescriptor(proto, key);
      if (!desc || typeof desc.value !== "function") return;
      var label = ctorName + ".prototype." + key;
      group.total[label] = true;
      proto[key] = wrapFn(groupName, label, desc.value);
    });
  }

  function installCoverage() {
    instrumentObject("底层模板 templates", PO.templates);
    instrumentObject("中层上下文 context", PO.context);
    instrumentPrototype("中层上下文 context", PO.context.ContextCapture.prototype, "ContextCapture");
    instrumentObject("上层增强 enhancer", PO.enhancer);
    instrumentObject("智能体专项 agent", PO.agent);
    instrumentObject("主编排器 optimizer", PO);
    instrumentPrototype("主编排器 optimizer", PO.PromptOptimizer.prototype, "PromptOptimizer");
  }

  function reportCoverage() {
    var groups = [];
    var gTotal = 0, gCovered = 0;
    Object.keys(coverageGroups).forEach(function (gname) {
      var g = coverageGroups[gname];
      var names = Object.keys(g.total);
      var missing = [];
      var covered = 0;
      names.forEach(function (n) {
        var isCtor = !!g.constructors[n];
        var hit = !!g.called[n];
        if (!hit && isCtor) {
          /* 构造函数：其任意原型方法被调用即视为已实例化覆盖 */
          var prefix = n + ".prototype.";
          hit = Object.keys(g.total).some(function (m) {
            return m.indexOf(prefix) === 0 && g.called[m];
          });
        }
        if (hit) covered++; else missing.push(n);
      });
      gTotal += names.length;
      gCovered += covered;
      groups.push({ name: gname, total: names.length, covered: covered, percent: names.length ? +(covered / names.length * 100).toFixed(1) : 100, missing: missing });
    });
    return { groups: groups, total: gTotal, covered: gCovered, percent: gTotal ? +(gCovered / gTotal * 100).toFixed(1) : 0 };
  }

  PO.TestFW = {
    expect: expect,
    describe: describe,
    it: it,
    beforeEach: beforeEach,
    run: run,
    reset: reset,
    installCoverage: installCoverage,
    reportCoverage: reportCoverage,
    deepEqual: deepEqual,
  };
})(typeof window !== "undefined" ? window : globalThis);
