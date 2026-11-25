FROM golang:1.27.4-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o /out/acquisition-gateway ./cmd/acquisition-gateway
RUN mkdir -p /out/var/lib/mozaic/quota /out/var/lib/mozaic/evidence

FROM gcr.io/distroless/static-debian13:nonroot
COPY --chown=65532:65532 --from=build /out/acquisition-gateway /acquisition-gateway
COPY --chown=65532:65532 --from=build /out/var/lib/mozaic /var/lib/mozaic
USER nonroot:nonroot
EXPOSE 8090
ENTRYPOINT ["/acquisition-gateway"]
