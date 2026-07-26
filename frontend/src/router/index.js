import { createRouter, createWebHashHistory } from 'vue-router'

import HomeView from '../pages/HomeView.vue'
import ResourceView from '../pages/ResourceView.vue'
import ResourceCenterView from '../pages/ResourceCenterView.vue'
import ChatView from '../pages/ChatView.vue'
import StudyPath from '../pages/StudyPath.vue'
import StudySituation from '../pages/StudySituation.vue'
import StudyImportView from '../pages/StudyImportView.vue'
import MyStudyView from '../pages/MyStudyView.vue'
import MyProfile from '../pages/MyAccount/MyProfile.vue'
import QuizRunnerView from '../pages/QuizRunnerView.vue'
import PresentationPlayerView from '../pages/PresentationPlayerView.vue'
import NotificationCenterView from '../pages/NotificationCenterView.vue'
import AdminDashboard from '../pages/AdminDashboard.vue'
import VideoComponentLab from '../pages/VideoComponentLab.vue'
import { isCurrentUserAdmin } from '../utils/auth'

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
      component: ResourceCenterView
    },
    {
      path: '/chat',
      name: 'chat',
      component: ChatView
    },
    {
      path: '/presentation-player',
      name: 'presentationPlayer',
      component: PresentationPlayerView
    },
    {
      path: '/video-components',
      name: 'videoComponents',
      component: VideoComponentLab
    },
    {
      path: '/question-bank',
      name: 'questionBank',
      redirect: {
        path: '/learning-resources',
        query: { category: 'quiz' }
      }
    },
    {
      path: '/question-bank/:quizId',
      name: 'quizRunner',
      component: QuizRunnerView
    },
    {
      path: '/learning-resources',
      name: 'learningResources',
      component: ResourceView
    },
    {
      path: '/learning-path',
      name: 'learningPath',
      component: StudyPath
    },
    {
      path: '/learning-situation',
      name: 'learningSituation',
      component: StudySituation
    },
    {
      path: '/mine',
      component: MyStudyView,
      redirect: '/learning-resources',
      children: [
        {
          path: 'resources',
          redirect: '/learning-resources'
        },
        {
          path: 'situation',
          redirect: '/learning-situation'
        },
        {
          path: 'path',
          redirect: '/learning-path'
        }
      ]
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
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: NotificationCenterView
    },
    {
      path: '/admin',
      name: 'admin',
      component: AdminDashboard,
      meta: { requiresAdmin: true }
    }
  ]
})

router.beforeEach(to => {
  if (to.meta?.requiresAdmin && !isCurrentUserAdmin()) {
    return '/profile'
  }
  return true
})

export default router
