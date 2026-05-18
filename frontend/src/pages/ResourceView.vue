<template>
  <background>
    <template #nav>
      <Navigation />
    </template>

    <button @click="showLogin = true" class="loginbuttom">打开登录弹窗</button>

    <LoginView
      :visible="showLogin"
      @close="showLogin = false"
      @login="handleLogin"
      @register="handleRegister"
    />
  </background>
</template>

<script setup>
import { ref } from 'vue'
import Navigation from '../components/NavView.vue'
import background from '../components/background.vue'
import LoginView from '../components/LoginView.vue'

const showLogin = ref(false)

const handleLogin = (data) => {
  const user = data?.data || data?.user || data

  if (user?.id) {
    localStorage.setItem('user_id', String(user.id))
  }

  if (user?.token) {
    localStorage.setItem('token', user.token)
  }

  if (data?.token) {
    localStorage.setItem('token', data.token)
  }

  if (user?.id) {
    localStorage.setItem('token', String(user.id))
  }

  console.log('登录成功：', data)
}

const handleRegister = (data) => {
  console.log('注册成功：', data)
}
</script>

<style scoped>
.loginbuttom{
  height: 50px;
  width: 150px;
  border-radius: 20px;
  background-color: #59a2eb;
  font-weight: 600;
  box-shadow: 0 2px 2px rgba(47, 79, 62, 0.16);
  transition: all 0.25s ease;

}
.loginbuttom:hover{
   background: #cdf464;
  transform: translateX(6px) scale(1.03);
  box-shadow: 0 4px 9px rgba(47, 79, 62, 0.16);
}
</style>
