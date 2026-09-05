# Frontend requirements and backend integration

The frontend implements the HTTP surface currently defined in `src/api/routes/` at repository level. Backend files are read for contracts and are not changed by this integration. All paths below are relative to `/api/v1`.

## Mapping to context/task.md

| Requirement | Frontend implementation | Backend dependency / limit |
| --- | --- | --- |
| 1.1 — RSS/media, regulator and Telegram collection | Source creation with automatic detection, preview, initial collection option, frequency, pause/resume and individual refresh | Existing collector and source adapters perform ingestion. Automatic monitoring can now be started/stopped from the processing screen; it must be restarted after the API process restarts. |
| 1.2 — AI summary and who/what/when/impact | Shows server summaries, entity roles, reasoning, confidence and incomplete/review flags; manual creation optionally requests AI processing | Batch processing is started through the API, with live status and run history. Missing LLM configuration is shown explicitly; the backend can produce degraded baseline cards. |
| 1.2 — categories and priority | Server tags/categories and high/medium/low importance; analyst overrides with reasons and history | The backend owns automatic classification; the frontend does not duplicate ranking logic. |
| 1.3 — feed and filters | Search, source/category/priority/date/type/status filters, sorting, server facets and cursor pagination | The backend searches text, tags and entities. Displayed dates use the browser timezone; date-only query boundaries use the backend timezone shown in status. |
| 1.3 — originals and deduplication | Safe original links, grouped publication count and all linked sources in the material card | Backend grouping decisions are preserved. |
| 1.4 — manage sources | Add/probe, edit name/frequency/content hint, pause, refresh, inspect history, delete, restore | Existing URL/type/fetch-address changes are supported. The server preserves documents and resets collection position when relocating a source. |
| 1.4 — edit material | Title, summary, category, priority, tags, type and NPA status saved by PATCH | Categories are tags. Editing a category preserves other custom/category tags. |
| 1.4 — hide/remove | Per-item and bulk feed/digest exclusion, soft deletion/restoration, source deletion with optional material hiding | Source deletion reports retained documents and affected tracked NPA. |
| 1.4 — manual material | Title or original URL, pasted raw text, date, type, NPA state, optional AI processing; explicit duplicate override | Queued responses direct users to the processing queue. |
| NPA as long-lived objects | Separate registry, status changes, linked documents, events, revisions and archive-status filter | Dated status changes, hearings and custom deadline events can be created. Recognized statuses advance an NPA only when no analyst override locks its status; event dates do not schedule reminders. |
| Weekly digest and address-specific selection | Seven-day preset, filters, current NPA state, notes opt-in, Markdown/JSON export, manual export editing | The export contains current matching records; automated weekly scheduling, saved snapshots and sending are unavailable. To review all tracked NPA regardless of publication date, choose NPA without a date restriction. |
| Analyst correction and feedback | Edit reasons, notes, visible model proposals and restoring a field to its model version | No invented model proposals. |
| MVP: ≥5 sources and ≥10 summaries | Live source and material counts and empty states | Requires actual backend ingestion and configured AI. The frontend does not seed sample records to claim these thresholds. |
| Quality/timing targets | Processing/review flags, live status, delayed source list | Summary accuracy, ingestion latency, throughput and relevance targets require backend and dataset evaluation; frontend tests do not establish them. |

## HTTP coverage

| Screen / action | Requests |
| --- | --- |
| App metadata and connection | `GET /filters`, `GET /status`, `GET /health/ready`, `GET /health` |
| Feed / NPA | `GET /items`, `GET /items/facets` |
| Processing queue | `GET /documents` with source/text/date/limit filters and cursor pagination |
| AI processing | `GET /processing`, `GET /processing/runs`, `POST /processing/runs`, `GET /processing/runs/{id}` |
| Automatic / one-off collection | `GET /collection`, `POST /collection/start`, `POST /collection/stop`, `POST /collection/runs` |
| Events and archive | `POST /items/{id}/events`, `POST /items/{id}/archive`, `POST /items/{id}/unarchive`; feed `archived=exclude/include/only` |
| Manual addition / card / edit | `POST /items`, `GET /items/{id}`, `PATCH /items/{id}` |
| Visibility / restoration | `POST /items/{id}/hide`, `/unhide`, `/restore`; `DELETE /items/{id}`; `POST /items/bulk` |
| Notes / audit / model version | `POST /items/{id}/notes`, `GET /items/{id}/revisions`, `POST /items/{id}/revert` |
| Digest | `POST /digest` |
| Source discovery and creation | `POST /sources/probe`, `POST /sources` |
| Source management | `GET /sources`, `GET /sources/{id}`, `PATCH /sources/{id}`, `DELETE /sources/{id}`, `POST /sources/{id}/restore` |
| Collection and history | `POST /sources/{id}/refresh`, `GET /sources/{id}/health` |

## Deliberate placeholders

The UI labels these capabilities as unavailable instead of simulating success:

- Persisting/scheduling/emailing digests or producing server PDF reports.
- Authentication and user profile details.
- Listing/managing company profiles: a processing run can accept a known profile ID, or use the active profile when omitted.

The processing queue loads 20 documents at a time using the server cursor, resetting pagination when source/date/search filters change. A digest similarly contains at most 200 materials; a limit notice appears when reached. Source health displays the latest 20 runs.

## Validation boundaries

Automated component tests use isolated HTTP fixtures to cover UI successes and failures. The separate live integration suite uses the actual TypeScript API client, Vite proxy and unchanged FastAPI server with an isolated SQLite database. It checks persisted material/source operations, filtering, revision history, notes, digests, visibility/restoration and local RSS collection. All temporary records are confined to the test database, which is deleted afterward.

External media/Telegram availability, paid search, LLM output quality, Docker networking and real-browser rendering require their corresponding services/runtime. They are not simulated as successful checks.

Latest local validation: **122 frontend tests passed**, **59 live integration checks passed**, and **production build with Vue/TypeScript checking passed**.

## Compatibility with the updated backend

The frontend supports the new processing, collection, event, archive and source-relocation routes while retaining the following service behavior:

- Readiness can return HTTP 503 with a valid `HealthResponse`. The interface preserves dependency diagnostics, distinguishes a degraded server from a disconnected server, and supports retry. Other HTTP 503 responses remain errors.
- Source refresh can return HTTP 200 with a failed `SourceRun`. The interface checks both error fields, displays successful document counts, and refreshes status after a failed poll as well as a successful one.
- Source PATCH requests contain only changed fields. Renaming a source does not resend `poll_interval`, which would reset `next_run_at`. Clearing `category_hint` sends the backend's empty-string value.
- Material PATCH requests omit unchanged fields and unchanged tag sets. A title edit therefore does not unnecessarily rewrite tag provenance or overwrite other fields. Saving an unchanged form makes no request.
- Manual duplicate override appears only for the `possible_duplicate` machine code, rather than every HTTP 409 response.

Live validation additionally covers malformed query HTTP 400, unknown request-field HTTP 422, name-only schedule preservation, content-hint clearing and failed RSS poll history. No file outside `frontend/` was changed for this update.

## Background work and limits

«Сбор и обработка» polls job status every five seconds while the page is visible, and stops polling after navigation. It refreshes queue totals when a processing run or collection cycle completes. Processing history shows the latest 20 runs; individual run details include final counters and errors. Accepted HTTP 202 responses are shown as requests accepted, not work completed. Busy/conflict errors retain their backend messages.

A one-off collection response does not contain a job ID. The interface therefore reports the request and the latest recorded collection cycle without claiming that an unchanged counter is its final result. Automatic collection and AI processing are separate operations. Collection force resets saved collection position; explicitly selecting a source takes precedence over the due-only option on the backend.

Source edits send only changed fields. When changing a URL or type, an unchanged fetch address is omitted so the backend can resolve the new address. An explicitly edited fetch address is sent. Duplicate-address errors leave the editor open with its draft intact.

Archiving is independent of hide/delete and NPA lifecycle status. Archived items can be retrieved with the feed archive filter and restored in their card. Digest generation always excludes archived records, even if a caller asks to include them.

The live suite additionally verifies event persistence/status advancement, archive filtering and digest exclusion, document cursor pages, source relocation with document retention, one-off collection, AI run completion without credentials, processing history, and monitor start/stop. All test records remain in temporary databases under `frontend/.cache/`.
