import { createRouter, createWebHashHistory } from 'vue-router'

import HomeView from '../pages/HomeView.vue'
import ResourceView from '../pages/ResourceView.vue'
import ChatView from '../pages/ChatView.vue'
import StudyPath from '../pages/StudyPath.vue'
import StudySituation from '../pages/StudySituation.vue'
import StudyImportView from '../pages/StudyImportView.vue'
import MyProfile from '../pages/MyAccount/MyProfile.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView
    },
    {
      path: '/resources',
      name: 'resources',
      component: ResourceView
    },
    {
      path: '/chat',
      name: 'chat',
      component: ChatView
    },
    {
      path: '/spath',
      name: 'spath',
      component: StudyPath
    },
    {
      path: '/situation',
      name: 'situation',
      component: StudySituation
    },
    {
      path: '/study-import',
      name: 'studyImport',
      component: StudyImportView
    },
    {
      path: '/profile',
      name: 'profile',
      component: MyProfile
    }
  ]
})

export default router
