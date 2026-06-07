# Catena Community admin shell (catena-admin) -- multi-stage Go build into a
# distroless static image. The binary is fully self-contained: templates,
# static assets, and translations are go:embed-ed, the SSH action runner uses
# pure-Go golang.org/x/crypto/ssh (no ssh binary), and license verification is
# offline ed25519. EE plugins are NOT baked in -- they are pulled at runtime
# into CATENA_PLUGINS_DIR only under an active Business license and exec'd as
# go-plugin children.
#
# Build context = the catena-ce repo root:
#   docker build -t catena-admin:dev .

FROM golang:1.26.4-bookworm AS build
WORKDIR /src

# Dependencies first for layer caching.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# CGO off -> a static binary that runs on distroless/static. -trimpath + -s -w
# for a smaller, reproducible artifact.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" \
    -o /out/catena-admin ./cmd/catena-admin

# Pre-create the runtime dirs so a no-volume run still works and the perms are
# right for the nonroot user:
#   /var/lib/catena/plugins  -- where the license-gated pull lands EE binaries
#   /tmp (1777)              -- go-plugin opens its handshake socket here
RUN mkdir -p /out/rootfs/var/lib/catena/plugins \
 && mkdir -p /out/rootfs/tmp && chmod 1777 /out/rootfs/tmp

FROM gcr.io/distroless/static-debian12:nonroot
# CA certs + tzdata ship in distroless/static; outbound TLS to the license
# endpoint works out of the box.
COPY --from=build /out/catena-admin /usr/local/bin/catena-admin
COPY --from=build --chown=nonroot:nonroot /out/rootfs/var/lib/catena /var/lib/catena
COPY --from=build /out/rootfs/tmp /tmp

ENV CATENA_ADMIN_ADDR=:8080 \
    CATENA_PLUGINS_DIR=/var/lib/catena/plugins
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/catena-admin"]
