ARG XK6_IMAGE_TAG=1.3.7
FROM grafana/xk6:${XK6_IMAGE_TAG} AS builder

ARG K6_BUILD_VERSION=v0.51.1-0.20240610082146-1f01a9bc2365
ARG XK6_ETHEREUM_VERSION=v1.0.4

# Keep these pins aligned: xk6-ethereum v1.0.4 builds successfully
# with this exact k6 revision, while newer k6 major versions do not.
WORKDIR /xk6
RUN xk6 build ${K6_BUILD_VERSION} \
    --with github.com/distribworks/xk6-ethereum@${XK6_ETHEREUM_VERSION} \
    --output /xk6/k6-custom

FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=builder /xk6/k6-custom /usr/local/bin/k6
ENTRYPOINT ["k6"]
