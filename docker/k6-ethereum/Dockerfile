FROM grafana/xk6:latest AS builder
WORKDIR /xk6
RUN xk6 build \
    --with github.com/distribworks/xk6-ethereum@latest \
    --output /k6-custom

FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=builder /k6-custom /usr/local/bin/k6
ENTRYPOINT ["k6"]
