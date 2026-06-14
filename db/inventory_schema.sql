-- Homelab Inventory / Node-Context schema
-- A structured, queryable "memory" of every node, container, network, port, and service.
-- Goal: n8n / Claude / any agent can connect and read the homelab's state in a structured format.
-- Run:  CREATE DATABASE homelab_inventory;  \c homelab_inventory  then run this file.
-- Populate from live discovery (pvesh / pct list / qm list / docker ps) — see Runbooks/Server-Discovery.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Physical/virtual hosts: Proxmox host, VMs, LXCs.
CREATE TABLE hosts (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        text UNIQUE NOT NULL,          -- 'proxmox', 'devcore', 'pihole'
    kind        text NOT NULL,                 -- 'proxmox-host' | 'vm' | 'lxc'
    vmid        int,                           -- Proxmox VMID/CTID (NULL for the host)
    ip          inet,
    mac         macaddr,
    os          text,
    cpu         text,
    ram_gb      numeric,
    role        text,                          -- 'hypervisor' | 'docker-host' | 'dns' ...
    status      text DEFAULT 'unknown',        -- 'running' | 'stopped' | 'unknown'
    ssh_user    text,
    notes       text,
    updated_at  timestamptz DEFAULT now()
);

-- Docker containers running on a host (usually the devcore VM).
CREATE TABLE containers (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    host_id     uuid REFERENCES hosts(id) ON DELETE CASCADE,
    name        text NOT NULL,                 -- 'devcore-postgres', 'traefik' ...
    stack       text,                          -- compose project: 'data','observability','edge'
    image       text,
    ip          inet,                          -- docker network IP (172.x)
    status      text,                          -- 'running','healthy','exited'
    role        text,
    notes       text,
    updated_at  timestamptz DEFAULT now(),
    UNIQUE (host_id, name)
);

-- Networks, physical and docker.
CREATE TABLE networks (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope       text NOT NULL,                 -- 'physical' | 'docker'
    name        text NOT NULL,                 -- 'LAN', 'data', 'observability', 'edge'
    cidr        cidr,
    purpose     text,
    UNIQUE (scope, name)
);

-- Ports: who listens where and how it's reached.
CREATE TABLE ports (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_kind   text NOT NULL,                -- 'host' | 'container'
    owner_id     uuid NOT NULL,                -- hosts.id or containers.id
    host_port    int,
    container_port int,
    protocol     text DEFAULT 'tcp',
    exposed_via  text,                         -- 'traefik' | 'direct' | 'cloudflare-tunnel' | 'internal'
    purpose      text
);

-- Services: the app + how it's routed (Traefik domain, upstream, auth).
CREATE TABLE services (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    container_id uuid REFERENCES containers(id) ON DELETE CASCADE,
    name         text NOT NULL,
    domain       text,                          -- 'portainer.lan'
    upstream_url text,                          -- 'http://192.168.1.44:9000'
    entrypoint   text,                          -- 'web' | 'websecure'
    auth         text,                          -- 'none' | 'basic' | 'authelia'
    tls          boolean DEFAULT false,
    notes        text,
    updated_at   timestamptz DEFAULT now()
);

-- Dependencies between anything (service -> db, container -> network ...).
CREATE TABLE dependencies (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    from_kind   text NOT NULL,
    from_id     uuid NOT NULL,
    to_kind     text NOT NULL,
    to_id       uuid NOT NULL,
    type        text                           -- 'connects_to','depends_on','routes_to'
);

-- Secret REFERENCES only — never values. Points at the SOPS-encrypted location.
CREATE TABLE secret_refs (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_kind  text,
    owner_id    uuid,
    key_name    text NOT NULL,                 -- 'POSTGRES_PASSWORD'
    sops_path   text NOT NULL,                 -- 'stacks/data/secrets.env'
    notes       text
);

-- Append-only change/state log (mirrors STATE_TRACKING discipline).
CREATE TABLE state_log (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    ts          timestamptz DEFAULT now(),
    actor       text,                          -- 'claude' | 'ashish'
    target      text,
    change      text NOT NULL
);

-- Convenience view: full service map for agents to read.
CREATE VIEW v_service_map AS
SELECT s.domain, s.upstream_url, s.auth, s.tls,
       c.name AS container, c.stack, c.image,
       h.name AS host, h.ip AS host_ip
FROM services s
JOIN containers c ON c.id = s.container_id
JOIN hosts h ON h.id = c.host_id
ORDER BY s.domain;
