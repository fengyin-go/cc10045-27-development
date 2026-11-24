import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ReconciliationView from '@/views/ReconciliationView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 放在 /:moduleKey 之前，避免被通用模块路由吞掉。
    { path: '/reconciliation', name: 'reconciliation', component: ReconciliationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
