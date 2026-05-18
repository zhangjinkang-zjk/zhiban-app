<template>
  <header class="floating-nav">
    <nav class="nav-links">
      <router-link to="/chat">AI 对话</router-link>
      <router-link to="/study-import">资料导入</router-link>
      <router-link to="/resources">学习资源</router-link>
      <router-link to="/spath">学习路径</router-link>
      <router-link to="/situation">学习情况</router-link>
    </nav>

  </header>
</template>

<script setup>
</script>

<style scoped>
.floating-nav {
  position: fixed;
  top: 22px;
  left: 50%;
  z-index: 20;
  width: min(1120px, calc(100% - 40px));
  min-height: 62px;
  padding: 10px 18px;
  transform: translateX(-50%);
  border: 1px solid rgba(201, 220, 233, 0.5);
  border-radius: 999px;
  background: rgba(250, 250, 250, 0.42);
  backdrop-filter: blur(24px) saturate(155%);
  -webkit-backdrop-filter: blur(24px) saturate(155%);
  display: flex;
  justify-content: center;
  align-items: center;
  box-shadow:
    0 20px 46px rgba(22, 63, 143, 0.1),
    inset 0 1px 0 rgba(250, 250, 250, 0.64);
}

.nav-links a {
  text-decoration: none;
}

.nav-links {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-width: 0;
}

.nav-links a {
  min-height: 38px;
  padding: 0 13px;
  border-radius: 999px;
  border: 1px solid rgba(201, 220, 233, 0.46);
  background: rgba(250, 250, 250, 0.28);
  color: #163f8f;
  font-size: 14px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  backdrop-filter: blur(12px) saturate(135%);
  -webkit-backdrop-filter: blur(12px) saturate(135%);
  transition:
    background 0.22s ease,
    border-color 0.22s ease,
    transform 0.22s ease,
    box-shadow 0.22s ease;
}

.nav-links a:hover,
.nav-links a.router-link-active {
  background: rgba(201, 220, 233, 0.58);
  border-color: rgba(95, 143, 195, 0.45);
  transform: translateY(-2px);
  box-shadow:
    0 8px 18px rgba(22, 63, 143, 0.12),
    inset 0 1px 0 rgba(250, 250, 250, 0.55);
}

.nav-links a:active {
  transform: translateY(0) scale(0.98);
  box-shadow: none;
}

@media (max-width: 980px) {
  .floating-nav {
    width: min(760px, calc(100% - 40px));
  }

  .nav-links {
    grid-column: 1 / -1;
    justify-content: flex-start;
    overflow-x: auto;
  }
}

@media (max-width: 640px) {
  .floating-nav {
    top: 12px;
    width: calc(100% - 24px);
  }

}
</style>
