ARG BUILD_FROM=ghcr.io/hassio-addons/base-python:20.0.0
FROM ${BUILD_FROM}

RUN apk add --no-cache tshark

COPY rootfs /
RUN chmod a+x /usr/bin/imou-intercept-daemon \
  && chmod a+x /etc/services.d/imou-intercept/run \
  && chmod a+x /etc/services.d/imou-intercept/finish || true
