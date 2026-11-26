import { createRouter, createWebHistory } from 'vue-router'

import DeliveryDetailView from '@/views/DeliveryDetailView.vue'
import Overview from '@/views/Overview.vue'
import ReparseTaskDetailView from '@/views/ReparseTaskDetailView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 详情/工作台支持数字行 id 或业务编号，并兼容常见的 /detail/、/no/ 路径形态。
    { path: '/result_deliver/:key', name: 'result-deliver-detail', component: DeliveryDetailView, props: true },
    { path: '/result_deliver/detail/:key', name: 'result-deliver-detail-nested', component: DeliveryDetailView, props: true },
    { path: '/result_deliver/no/:key', name: 'result-deliver-by-no', component: DeliveryDetailView, props: true },
    { path: '/reparse_task/:key', name: 'reparse-task-detail', component: ReparseTaskDetailView, props: true },
    { path: '/reparse_task/workbench/:key', name: 'reparse-task-workbench', component: ReparseTaskDetailView, props: true },
    { path: '/reparse_task/detail/:key', name: 'reparse-task-detail-nested', component: ReparseTaskDetailView, props: true },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
