# Frontend Setup

## Install dependencies

```bash
cd kanban-board/frontend
npm install
```

## Development

```bash
# Start dev server (proxies /api to localhost:8000)
npm run dev
```

## Build

```bash
npm run build
```

## Test

```bash
npm test
```

## Environment Variables

Create a `.env` file in the frontend directory (optional):

```env
VITE_API_BASE_URL=/api
```

The default value `/api` works with the Vite dev server proxy that forwards requests to the backend at `http://localhost:8000`.
