FROM golang:1.24-alpine AS builder

WORKDIR /src

COPY go.mod .
COPY go.sum .
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/openhost-catalog ./cmd/openhost-catalog

FROM alpine:3.21
WORKDIR /app

COPY --from=builder /out/openhost-catalog /usr/local/bin/openhost-catalog
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/openhost-catalog"]
