# Vision Pro V3 Command Center

React + TypeScript + Lightweight Charts 5.0.7. This read-only frontend consumes
authenticated backend projections. Build with Node 24: `npm ci && npm test && npm run build`.
Run the FastAPI server from the repository root; see `docs/PHASE11_5_COMMAND_CENTER.md`.
Vite's development proxy is for frontend development only; authenticated integrated
validation uses the built frontend served on the backend's configured loopback origin.
No third-party chart application code, execution controls or fabricated live metrics.
