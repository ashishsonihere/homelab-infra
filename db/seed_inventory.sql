-- Homelab inventory SEED — generated from LIVE discovery 2026-06-15 (ssh proxmox/devcore/pihole).
-- Load AFTER inventory_schema.sql:  \c homelab_inventory  then run this file.
-- Re-runnable: truncates the inventory tables first.

TRUNCATE services, ports, containers, networks, hosts RESTART IDENTITY CASCADE;

-- Hosts -------------------------------------------------------------------
INSERT INTO hosts (name, kind, vmid, ip, os, cpu, ram_gb, role, status, ssh_user, notes) VALUES
 ('proxmox','proxmox-host', NULL, '192.168.1.41', 'Proxmox VE 9.1.4 (kernel 6.17.4)', 'i3-8100 4c/4t', 32, 'hypervisor', 'running', 'root', 'HP ProDesk 600 G5 SFF. data-hdd storage is disabled (failing HDD).'),
 ('devcore','vm', 201, '192.168.1.44', 'Debian 12', '2 vCPU', 18, 'docker-host', 'running', 'root', '105G disk on local-lvm, ~70G free. 20 containers.'),
 ('pihole','lxc', 101, '192.168.1.50', 'Debian (LXC)', '-', 3, 'dns', 'running', 'root', 'Pi-hole + Unbound. ~3GB RAM (raised from 1GB). FTL on :53.');

-- Networks ----------------------------------------------------------------
INSERT INTO networks (scope, name, cidr, purpose) VALUES
 ('physical','LAN','192.168.1.0/24','Home LAN'),
 ('docker','proxy', NULL, 'Traefik ingress / edge'),
 ('docker','devcore_net', NULL, 'App + data services'),
 ('docker','observability_net', NULL, 'Metrics/logs/traces');

-- Containers (on devcore) — stack = docker compose project --------------
WITH h AS (SELECT id FROM hosts WHERE name='devcore')
INSERT INTO containers (host_id, name, stack, image, status)
SELECT h.id, c.name, c.stack, c.image, 'running'
FROM h, (VALUES
 ('traefik','traefik','traefik:v3.6.8'),
 ('portainer','portainer','portainer/portainer-ce:lts'),
 ('devcore-cloudflared','cloudflared','cloudflare/cloudflared:latest'),
 ('devcore-minio','minio','quay.io/minio/minio:RELEASE.2025-04-22'),
 ('devcore-n8n','n8n','n8nio/n8n:latest'),
 ('devcore-uptime-kuma','uptime-kuma','louislam/uptime-kuma:nightly2'),
 ('devcore-postgres','postgres','pgvector/pgvector:0.8.1-pg16-trixie'),
 ('devcore-redis','postgres','redis:8.2.4-alpine'),
 ('devcore-mathesar','postgres','mathesar/mathesar:latest'),
 ('devcore-postgres-backup','postgres','prodrigestivill/postgres-backup-local'),
 ('devcore-pgadmin','admin','dpage/pgadmin4:snapshot'),
 ('devcore-redisinsight','admin','redis/redisinsight'),
 ('devcore-prometheus','observability','prom/prometheus:main'),
 ('devcore-grafana','observability','grafana/grafana'),
 ('devcore-loki','observability','grafana/loki:latest'),
 ('devcore-tempo','observability','grafana/tempo:latest'),
 ('devcore-promtail','observability','grafana/promtail:latest'),
 ('devcore-alertmanager','observability','prom/alertmanager:latest'),
 ('devcore-node-exporter','observability','prom/node-exporter:master'),
 ('devcore-cadvisor','observability','ghcr.io/google/cadvisor')
) AS c(name, stack, image);

-- Services routed via Traefik (.lan) — from edge-gateway/dynamic + labels
WITH c AS (SELECT name, id FROM containers)
INSERT INTO services (container_id, name, domain, upstream_url, entrypoint, auth, tls)
SELECT c.id, s.name, s.domain, s.upstream, 'web', 'none', false
FROM c JOIN (VALUES
 ('portainer','portainer','portainer.lan','http://192.168.1.44:9000'),
 ('devcore-mathesar','mathesar','mathesar.lan','http://192.168.1.44:8000'),
 ('devcore-n8n','n8n','n8n.lan','http://192.168.1.44:5678')
) AS s(cname, name, domain, upstream) ON s.cname = c.name;

-- Notable host-published ports (0.0.0.0 on devcore .44) -------------------
WITH c AS (SELECT name, id FROM containers)
INSERT INTO ports (owner_kind, owner_id, host_port, container_port, protocol, exposed_via, purpose)
SELECT 'container', c.id, p.hp, p.cp, 'tcp', 'direct', p.purpose
FROM c JOIN (VALUES
 ('traefik',80,80,'HTTP ingress'),
 ('traefik',443,443,'HTTPS ingress'),
 ('devcore-alertmanager',9093,9093,'Alertmanager'),
 ('devcore-cadvisor',8080,8080,'cAdvisor'),
 ('devcore-loki',3100,3100,'Loki'),
 ('devcore-node-exporter',9100,9100,'node-exporter'),
 ('devcore-tempo',3200,3200,'Tempo')
) AS p(cname, hp, cp, purpose) ON p.cname = c.name;

-- Note: postgres:5432, redis:6379 are INTERNAL ONLY (not host-published) — good.
