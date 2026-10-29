/* ===== TradeAI 首页交互脚本 ===== */

(function () {
  'use strict';

  // ===== 移动端导航菜单切换 =====
  const navToggle = document.getElementById('navToggle');
  const navMenu = document.getElementById('navMenu');

  if (navToggle && navMenu) {
    navToggle.addEventListener('click', () => {
      const isOpen = navToggle.classList.toggle('active');
      navMenu.classList.toggle('active');
      navToggle.setAttribute('aria-expanded', String(isOpen));
      navToggle.setAttribute('aria-label', isOpen ? '关闭菜单' : '打开菜单');
    });

    // 点击菜单链接后自动收起
    navMenu.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', () => {
        navToggle.classList.remove('active');
        navMenu.classList.remove('active');
        navToggle.setAttribute('aria-expanded', 'false');
        navToggle.setAttribute('aria-label', '打开菜单');
      });
    });
  }

  // ===== 滚动时导航栏阴影增强 =====
  const navbar = document.getElementById('navbar');
  function onScroll() {
    if (window.scrollY > 10) {
      navbar.style.boxShadow = '0 4px 20px rgba(0, 0, 0, 0.06)';
    } else {
      navbar.style.boxShadow = 'none';
    }
  }
  window.addEventListener('scroll', onScroll);
  onScroll();

  // ===== 功能卡片 =====
  // 四张卡在 index.html 中已是语义化链接（<a class="feature-card" href="…">），
  // 故此处**不再绑定任何点击跳转** —— 若加 JS 跳转，Ctrl/⌘+点击会在新标签打开的
  // 同时把当前页也跳走（双重导航）。焦点态样式见 css/style.css 的
  // `.feature-card:focus-visible`。
})();
