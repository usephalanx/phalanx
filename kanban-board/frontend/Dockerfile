# ---------------------------------------------------------------------------
# Development stage — vite dev server with HMR
# ---------------------------------------------------------------------------
FROM node:20-alpine AS dev

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

COPY frontend/ .

EXPOSE 5173

CMD ["npx", "vite", "--host", "0.0.0.0"]

# ---------------------------------------------------------------------------
# Production build stage — static assets served by nginx
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

COPY frontend/ .
RUN npm run build

FROM nginx:alpine AS prod

COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
