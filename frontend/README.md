# AI Analytics Hub — frontend

Vue 3 + TypeScript + Vite + Tailwind CSS implementation of the supplied Figma Make design. The interface is in Russian and uses the backend HTTP API for all business records. Only the theme preference is stored in localStorage.

## Run

Run these commands inside `frontend/` with Node.js 22.12 or newer:

```sh
mkdir -p .cache/tmp
TMPDIR="$PWD/.cache/tmp" XDG_CACHE_HOME="$PWD/.cache" npm ci --ignore-scripts --workspaces=false
npm run dev
```

Open http://127.0.0.1:5173. Start the existing backend separately on port 8000. Vite forwards `/api` to `http://127.0.0.1:8000` by default; configure a different backend with:

```sh
API_PROXY_TARGET=http://127.0.0.1:18000 npm run dev
```

`VITE_API_BASE_URL` optionally overrides the browser API base (default `/api/v1`). It is a build-time setting. Prefer the same-origin proxy; a cross-origin API must allow the frontend origin in its backend CORS configuration.

If the backend is unavailable, the interface shows an error and retry controls. A degraded readiness response retains its dependency diagnostics in «Сбор и обработка». Source refresh displays document counts or the recorded collection error, including failures returned with HTTP 200. An empty backend produces empty states. It never falls back to sample news, sources, user profiles or generated summaries. Old demo localStorage records are ignored. Test fixtures are isolated in `src/test/` and are not bundled into the app.

## Screens

- **Мониторинг:** server search and filters, pagination, original links, grouped publications, review flags, bulk visibility changes and manual material creation.
- **Карточка материала:** title, summary, category, tags, priority and type editing; NPA status and dated events; archive/unarchive; entities and AI reasoning; notes, revision history, model-version restoration, hide/delete/restore.
- **НПА:** lifecycle registry, status filters including archive, current state, event creation/history and archive retrieval.
- **Дайджест:** filtered server snapshot in Markdown or JSON, optional analyst notes, seven-day preset, temporary export editing and download. The backend limits each digest to 200 items; the UI explains this limit.
- **Источники:** RSS, regulator sites, Telegram and saved search; URL detection and preview; creation, schedule/URL/type/fetch-address editing, pause/resume, refresh, health history, deletion and restoration.
- **Сбор и обработка:** real counts and readiness, paginated document queue, AI processing runs with status/history, automatic monitor start/stop and one-off collection. Background status refreshes every five seconds while this screen is visible.

Missing backend capabilities have visible placeholders. The [requirement and API mapping](docs/backend-integration.md) records what is implemented and what still depends on backend work.

## Verify

```sh
npm test                 # Component workflows, HTTP contracts, utilities and request races
npm run build            # Vue/TypeScript checking plus production build
npm run test:integration  # Real API client through Vite to an isolated backend
```

The integration suite requires the existing backend Python dependencies in `../.venv`; `INTEGRATION_PYTHON` can select another installed Python environment. It starts the unchanged backend and frontend proxy on temporary loopback ports. Its database and config copy live only in `frontend/.cache/` and are removed after the run. It does not read production data or use external services or LLM credentials. A temporary local RSS feed is used only by this test.

Component tests use Vitest, Vue Test Utils and JSDOM. They cover API failures, timeouts, request cancellation, stale-response protection, filters, editing, duplicate conflicts, source operations, visibility, exports and navigation. These are not real-browser visual or accessibility audits.

## Docker

The existing root Compose setup can be started from the repository root:

```sh
docker compose up --build -d
```

The frontend is available at http://localhost:8080 and the backend docs at http://localhost:8000/docs. Nginx forwards `/api/` to `api:8000`, preserving paths and queries. Check the connection through http://localhost:8080/api/v1/health/ready. Rebuild the frontend image after source or build-time environment changes. Docker runtime validation requires an available Docker daemon.

## Structure

- `src/api/`: typed backend requests and response contracts.
- `src/components/`: screens, forms, material card and shared controls.
- `src/composables/`: API-backed dashboard state and cancellable remote loading.
- `src/data/dashboard.ts`: display vocabulary only, with no business records.
- `src/test/`, `src/**/*.test.ts`: test-only fixtures and automated coverage.
- `scripts/test-integration.mjs`: isolated integration runner.
- `src/index.css`: design tokens, responsive layout and theme styles.

Fonts use Google Fonts with system fallbacks. All installation caches, dependencies, tests and build output remain inside `frontend/`.
