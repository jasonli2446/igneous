# The development server, for docker-compose.yml, which mounts web/src over /app/src so edits reload. The image
# also runs on its own with the source it was built from.
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 3000
# `next dev` compiles in the container as pages are requested, so it inlines NEXT_PUBLIC_API_URL from the
# container's environment (default http://localhost:8000, where the browser reaches the API).
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0"]
