/* ===== 列表页状态保持（个股列表 / 板块列表 / 板块成员列表 共用） =====
 *
 * 场景：列表翻到第 N 页 → 点击某项进入详情 → 返回列表时，恢复离开前的
 *       页码、每页条数、筛选关键词、排序条件与滚动位置。
 *
 * 仅当导航语义为「返回」时才恢复，从首页菜单、搜索等其它入口进入列表时
 * 仍按默认第 1 页展示（见 isReturnNav）：
 *   1) 浏览器后退/前进 —— navigation.type === 'back_forward'；
 *      此时页面可能从 bfcache 恢复（脚本不重跑，状态天然正确），
 *      也可能被重新加载（无 bfcache），本模块负责后者。
 *   2) 详情页「返回列表」链接 —— 链接上带 ?restore=1 标记。
 *
 * 存储：sessionStorage（按标签页隔离，关闭标签即失效），键前缀 tradeai:listState:
 */
(function () {
  'use strict';

  var PREFIX = 'tradeai:listState:';

  function key(name) { return PREFIX + name; }

  function _getParam(k) {
    try { return new URLSearchParams(location.search).get(k); } catch (e) { return null; }
  }

  /** 是否属于「从详情页返回列表」的导航 */
  function isReturnNav() {
    try {
      var nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || null;
      if (nav && nav.type === 'back_forward') return true;   // 浏览器后退/前进
    } catch (e) { /* 老浏览器忽略 */ }
    return _getParam('restore') === '1';                      // 详情页返回链接
  }

  /** 保存列表状态快照（页面离开前调用） */
  function save(name, snapshot) {
    try { sessionStorage.setItem(key(name), JSON.stringify(snapshot)); } catch (e) { /* 隐私模式忽略 */ }
  }

  /** 读取待恢复的状态；非「返回」导航一律返回 null（保持默认第 1 页） */
  function load(name) {
    if (!isReturnNav()) return null;
    try {
      var raw = sessionStorage.getItem(key(name));
      if (!raw) return null;
      var obj = JSON.parse(raw);
      return (obj && typeof obj === 'object') ? obj : null;
    } catch (e) { return null; }
  }

  function clear(name) {
    try { sessionStorage.removeItem(key(name)); } catch (e) { /* 忽略 */ }
  }

  /** 恢复后清除 URL 上的 restore 标记，避免手动刷新时重复走恢复分支 */
  function cleanUrl() {
    if (_getParam('restore') !== '1') return;
    try { history.replaceState(null, '', location.pathname + (location.hash || '')); } catch (e) { /* 忽略 */ }
  }

  /** 恢复滚动位置：内容异步渲染，故延时重试；用户已自行滚动则不再打扰 */
  function restoreScroll(y) {
    if (typeof y !== 'number' || y <= 0) return;
    try { history.scrollRestoration = 'manual'; } catch (e) { /* 忽略 */ }
    [0, 80, 250].forEach(function (delay) {
      setTimeout(function () {
        if (window.scrollY > 0) return;      // 用户已滚动, 放弃强制定位
        window.scrollTo(0, y);
      }, delay);
    });
  }

  window.ListState = {
    save: save,
    load: load,
    clear: clear,
    isReturnNav: isReturnNav,
    cleanUrl: cleanUrl,
    restoreScroll: restoreScroll,
  };
})();
