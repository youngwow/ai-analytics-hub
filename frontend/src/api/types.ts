export type Priority = 'high' | 'medium' | 'low'
export type ItemType = 'news' | 'npa'
export type Visibility = 'visible' | 'hidden_feed' | 'hidden_digest' | 'deleted'
export type SourceStatus = 'active' | 'paused' | 'error' | 'deleted'
export interface FeedItem {
  id: number; type: ItemType; npa_status: string | null; npa_key: string | null
  title: string | null; summary: string | null; priority: Priority; tags: string[]
  published_at: string | null; canonical_url: string | null; source_name: string | null; sources_count: number | null
  visibility: Visibility; hidden_reason: string; origin: string; confidence: number | null
  relevance_score: number | null; reasoning: string | null; snippet: string | null
  flags: { degraded: boolean; needs_review: boolean; date_estimated: boolean; edited: boolean }
}
export interface Item extends Omit<FeedItem, 'flags' | 'canonical_url' | 'source_name' | 'sources_count' | 'snippet'> {
  cluster_id: number; analyst_note: string; manual_overrides: string[]; is_archived: boolean
  degraded: boolean; needs_review: boolean; date_estimated: boolean; model_name: string
  prompt_version: number | null; profile_version: number | null; processed_at: string
}
export interface Revision { id: number | null; item_id: number; field: string; old_value: string | null; new_value: string | null; actor: string; source_of_change: string; edit_reason: string; created_at: string }
export interface Note { id: number | null; item_id: number; body: string; author: string; created_at: string }
export interface ItemCard {
  item: Item; canonical_url: string | null
  entities: { id: number | null; role: string; value: string; normalized_value: string; evidence_start: number | null; evidence_end: number | null }[]
  sources: { id: number; url: string; title: string | null; published_at: string | null; is_canonical: number; source_name: string | null }[]
  events: { id: number | null; item_id: number; status: string; occurred_at: string | null; source_url: string; note: string; created_by: string; created_at: string }[]
  revisions: Revision[]; notes: Note[]; tags: { item_id: number; tag: string; is_manual: boolean }[]
  model_proposals: Record<string, Revision | null>
}
export interface Source {
  id: number; name: string; url: string; kind: string; category: string; fetch_url: string
  status: SourceStatus; normalized_url: string; poll_interval: string; next_run_at: string | null
  category_hint: string | null; created_at: string; notes: string; deleted_at: string | null; created_by: string
}
export interface SourceRun { id: number | null; source_id: number; started_at: string; finished_at: string | null; http_status: number | null; items_found: number; items_new: number; error_code: string; error_message: string }
export interface SourceHealth { source: Source; documents: number; consecutive_failures: number; last_success_at: string | null; last_error: string | null; runs: SourceRun[] }
export interface Probe { resolved_type: string; feed_url: string; title: string; detection_method: string; suggested_poll_interval: string; already_exists: boolean; already_exists_source_id: number | null; preview: { title: string; url: string; published_at: string | null }[]; warnings: string[]; note: string }
export interface Health { status: 'ok' | 'degraded'; app: string; version: string; environment: string; checks: Record<string, string> }
export interface Status { last_collect_at: string | null; documents: number; items: number; unprocessed: number; sources: Record<string, number>; stale_sources: { id: number; name: string; overdue_minutes: number; consecutive_failures: number; last_error: string }[]; timezone: string }
export interface Filters { sources: Pick<Source, 'id' | 'name' | 'kind' | 'category' | 'status'>[]; tags: string[]; npa_statuses: string[]; priorities: string[]; types: string[]; orders: string[]; timezone: string }
export interface Facets { total: number; by_priority: Record<string, number>; by_type: Record<string, number>; by_source: { source_id: number; name: string; count: number }[]; top_tags: { tag: string; count: number }[]; took_ms: number }
export interface Feed { items: FeedItem[]; total: number; next_cursor: string | null; took_ms: number }
export interface Documents { documents: { id: number; title: string | null; url: string; source_id: number; source_name: string | null; published_at: string | null; chars: number | null }[]; total: number; took_ms: number }
export interface Digest { title: string; generated_at: string; items: number; format: 'markdown' | 'json'; body: string }
export interface FeedQuery { q?: string; type?: string; npa_status?: string; priority?: string[]; tag?: string[]; source_id?: number[]; from?: string; to?: string; order?: string; limit?: number; cursor?: string; include_hidden?: boolean }
export interface ItemUpdate { title?: string; summary?: string; type?: ItemType; npa_status?: string; priority?: Priority; tags?: string[]; edit_reason?: string }
export interface ItemCreate { title: string; url: string; raw_text: string; type: ItemType; npa_status?: string; published_at?: string; run_llm: boolean; force: boolean }
export interface SourceCreate { url: string; title: string; type: string; poll_interval: string; category_hint: string | null; backfill_limit: number; created_by: string }
export interface SourceUpdate { title?: string; poll_interval?: string; category_hint?: string; status?: SourceStatus }
export interface ManualResult { id: number | null; document_id: number; origin: string; processing_status: string }
