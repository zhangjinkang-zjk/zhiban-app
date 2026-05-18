<template>
  <div class="page">
    <div class="app-shell">
      <slot name="nav"></slot>

      <main class="main">
        <header class="topbar">
          <div class="search-box">
            <span>搜索课程、计划、错题...</span>
          </div>

          <div class="user-box" @click="openLogin">
            <div class="avatar"></div>
            <div>
              <strong>{{ username }}</strong>
              <p>{{ major }}</p>
            </div>
          </div>
        </header>

        <LoginView
          :visible="showLogin"
          @close="showLogin = false"
          @login="handleLogin"
        />

        <slot></slot>
      </main>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { getUserProfile } from '../api/apis'
import LoginView from './LoginView.vue'

defineOptions({
  name: 'background'
})

const username = ref('未登录账户')
const major = ref('请点击登录')
const showLogin = ref(false)

const resetUser = () => {
  username.value = '未登录账户'
  major.value = '请点击登录'
}

const loadUser = async () => {
  if (!localStorage.getItem('token')) {
    resetUser()
    return
  }

  try {
    const result = await getUserProfile()
    const user = result?.data || result?.user || result || {}

    username.value = user.username || '已登录账户'
    major.value = user.major || '暂未填写专业'
  } catch (error) {
    resetUser()
  }
}

const openLogin = () => {
  if (localStorage.getItem('token')) return

  showLogin.value = true
}

const handleLogin = async () => {
  showLogin.value = false
  await loadUser()
}

onMounted(loadUser)
</script>

<style scoped>
* {
  box-sizing: border-box;
}

.page {
  min-height: 100vh;
  background: #e9edf2;
  padding: 12px;
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
  color: #1f2937;
}

.app-shell {
  width: calc(100vw - 24px);
  min-height: calc(100vh - 12px);
  margin: 0 auto;
  background: #eff3f4;
  border-radius: 20px;
  display: grid;
  grid-template-columns: 170px 1fr;
  overflow: hidden;
  box-shadow: 0 24px 70px rgba(31, 41, 55, 0.12);
}

.main {
  padding: 26px 30px 30px;
}

.topbar {
  height: 64px;
  display: grid;
  grid-template-columns: 1fr 210px;
  gap: 22px;
  align-items: center;
  margin-bottom: 24px;
}

.search-box {
  height: 52px;
  background: #ffffff;
  border-radius: 14px;
  display: flex;
  align-items: center;
  padding: 0 22px;
  color: #9ca3af;
  box-shadow: 0 10px 30px rgba(31, 41, 55, 0.04);
}

.user-box {
  height: 52px;
  background: #eff3f4;
  border-radius: 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 14px;
  cursor: pointer;
  transition: background 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
}

.user-box:hover {
  background: #d7e4ef;
  box-shadow: 0 8px 18px rgba(24, 63, 143, 0.08);
  transform: translateY(-1px);
}

.user-box:active {
  transform: translateY(0);
}

.avatar {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background: linear-gradient(135deg, #d9c7b8, #f3e7dd);
}

.user-box strong {
  display: block;
  font-size: 14px;
}

.user-box p {
  margin: 2px 0 0;
  font-size: 12px;
  color: #8b95a5;
}
</style>
