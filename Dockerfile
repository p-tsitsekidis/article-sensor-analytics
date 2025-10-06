FROM grafana/grafana:latest

ENV GF_INSTALL_PLUGINS="yesoreyeram-infinity-datasource"

COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards

EXPOSE 3000