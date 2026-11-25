FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -ldflags='-s -w' -o /out/artifact-custody ./cmd/artifact-custody \
 && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -ldflags='-s -w' -o /out/artifact-custody-migrate ./cmd/artifact-custody-migrate \
 && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -ldflags='-s -w' -o /out/artifact-custody-provision-objects ./cmd/artifact-custody-provision-objects \
 && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -ldflags='-s -w' -o /out/artifact-custody-reconcile ./cmd/artifact-custody-reconcile

FROM gcr.io/distroless/static-debian13:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5 AS runtime
COPY --from=build /out/artifact-custody /artifact-custody
USER 65532:65532
EXPOSE 8086
ENTRYPOINT ["/artifact-custody"]

FROM gcr.io/distroless/static-debian13:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5 AS migrator
COPY --from=build /out/artifact-custody-migrate /artifact-custody-migrate
COPY migrations /migrations
WORKDIR /
USER 65532:65532
ENTRYPOINT ["/artifact-custody-migrate"]

FROM gcr.io/distroless/static-debian13:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5 AS object-provisioner
COPY --from=build /out/artifact-custody-provision-objects /artifact-custody-provision-objects
USER 65532:65532
ENTRYPOINT ["/artifact-custody-provision-objects"]

FROM gcr.io/distroless/static-debian13:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5 AS reconciler
COPY --from=build /out/artifact-custody-reconcile /artifact-custody-reconcile
USER 65532:65532
ENTRYPOINT ["/artifact-custody-reconcile"]

FROM build AS integration-tests
ENTRYPOINT ["go", "test"]
