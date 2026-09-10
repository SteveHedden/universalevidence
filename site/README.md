# Universal Evidence frontend

React and Vite interface for UE Search and Graph. It requires a working UE API; the browser bundle does not contain registry data or crosswalks.

## Development

Use Node.js 24 or newer. From this directory:

```bash
npm ci
npm run dev
```

Set `VITE_API_BASE_URL=http://localhost:8000` in `site/.env.local` to use a local API. With no base URL, the client uses same-origin paths; the development server proxies configured query, graph and taxonomy routes to port 8000. Never put secrets in `VITE_` variables: they are browser-visible.

## Tests and builds

```bash
npm run test:run
VITE_API_BASE_URL=/api npm run build
```

The build checks TypeScript, generates dependency license notices and writes static assets to `dist/`. `/api` requires a same-origin reverse proxy to FastAPI. For a different API origin, set the base URL at build time and configure that origin's CORS policy to accept your frontend.

`npm run preview` previews static assets locally; it is not a production backend and does not automatically provide an `/api` reverse proxy.

## Publishing static assets

Serve `dist/` through a static server or hosting provider, with a working API connection. Keep `third-party/` alongside the bundle so dependency notices remain available. Use SPA fallback for application routes and preserve any separate static pages.

Publish new hashed assets before switching `index.html`; retain previous assets for open browser sessions and rollback. The index should not be cached long-term. See the [deployment guide](../deploy/VPS_SETUP.md) for the server layout and verification requirements.
