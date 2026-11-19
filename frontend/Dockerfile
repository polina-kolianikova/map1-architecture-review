FROM node:22-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
ARG VITE_API_BASE_URL=/api
ARG VITE_APP_LOCALE=en
ARG VITE_APP_AVAILABLE_LOCALES=en
ARG VITE_ENABLE_MAP_STATS=0
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_APP_LOCALE=$VITE_APP_LOCALE
ENV VITE_APP_AVAILABLE_LOCALES=$VITE_APP_AVAILABLE_LOCALES
ENV VITE_ENABLE_MAP_STATS=$VITE_ENABLE_MAP_STATS
RUN npm run build
RUN npm prune --omit=dev

FROM node:22-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752

WORKDIR /app
RUN chown node:node /app

COPY --from=build --chown=node:node /app/package*.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist
COPY --from=build --chown=node:node /app/server.mjs ./server.mjs

USER node

ENV SHOWCASE_BIND_HOST=0.0.0.0

EXPOSE 5173

CMD ["node", "server.mjs"]
