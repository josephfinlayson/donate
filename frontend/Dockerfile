FROM node:22-alpine

WORKDIR /app

ARG NEXT_PUBLIC_BACKEND_URL=http://localhost:8080
ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL

COPY package.json ./
RUN npm install

COPY . .

CMD ["npm", "run", "dev"]
