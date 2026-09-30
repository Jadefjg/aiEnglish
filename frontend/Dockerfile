FROM node:22-alpine AS vendor
WORKDIR /build
COPY frontend/package.json ./package.json
RUN npm install --omit=dev

FROM nginx:1.27-alpine
COPY frontend/index.html /usr/share/nginx/html/index.html
COPY frontend/learn.html /usr/share/nginx/html/learn.html
COPY frontend/learn.js /usr/share/nginx/html/learn.js
COPY frontend/auth.js /usr/share/nginx/html/auth.js
COPY --from=vendor /build/node_modules/vue/dist/vue.global.prod.js /usr/share/nginx/html/vendor/vue.global.prod.js
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
