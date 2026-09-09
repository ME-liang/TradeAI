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

  // ===== 功能卡片点击反馈（预留路由） =====
  document.querySelectorAll('.feature-card').forEach((card) => {
    card.style.cursor = 'pointer';
    card.addEventListener('click', () => {
      const id = card.getAttribute('id');
      if (id === 'stock') {
        console.log('进入个股分析模块');
        // window.location.href = '/stock.html';
      } else if (id === 'sector') {
        console.log('进入板块洞察模块');
        // window.location.href = '/sector.html';
      } else if (id === 'report') {
        console.log('进入智能报告模块');
        // window.location.href = '/report.html';
      }
    });
  });
})();
