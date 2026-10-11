FROM nginx:1.28.3-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236
COPY --chmod=0444 hermes/broker/nginx.conf.template /opt/roo/nginx.conf.template
COPY --chmod=0555 hermes/broker/entrypoint.sh /opt/roo/entrypoint.sh
USER 10000:10000
ENTRYPOINT ["/opt/roo/entrypoint.sh"]
