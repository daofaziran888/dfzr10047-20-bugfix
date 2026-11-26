import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
  params: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action, params }),
  })
  return await response.json()
}

export async function getEntry(key: string, id: number): Promise<EntryRow> {
  const response = await request(`/api/${key}/${id}`)
  if (!response.ok) throw new Error('业务详情读取失败')
  return await response.json()
}

export type DeliverAttempt = {
  尝试编号?: string
  版本?: string
  尝试类型?: string
  回执类型?: string
  报告缺失行数?: number | null
  报告送达行数?: number | null
  目标确认行数?: number | null
  目标确认凭据?: string
  回执时间?: string
  核对状态?: string
  核对说明?: string
  是否迟到?: boolean
  [key: string]: unknown
}

export type DeliverDetail = EntryRow & {
  是否矛盾?: boolean
  矛盾说明?: string
  尝试回执?: DeliverAttempt[]
  确认范围?: Record<string, unknown>
}

export async function getDeliverDetail(no: string): Promise<DeliverDetail> {
  const response = await request(`/api/result_deliver/no/${encodeURIComponent(no)}/detail`)
  if (!response.ok) throw new Error('投递详情读取失败')
  return await response.json()
}

export async function getReparseWorkbench(no: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/reparse_task/no/${encodeURIComponent(no)}/workbench`)
  if (!response.ok) throw new Error('重解析工作台读取失败')
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
