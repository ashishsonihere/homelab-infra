# homelab-infra (GitOps)

Declarative config for the Proxmox homelab. Edit here → push → pull on the server → deploy.

## Structure

```
homelab-infra/
├── .sops.yaml                  # secrets encryption rules (age recipient)
├── .gitignore                  # blocks plaintext .env / age keys
├── .github/workflows/validate.yml   # CI: yamllint + compose config + hadolint
├── scripts/deploy.sh           # run ON THE SERVER: pulls + decrypts + deploys a stack
└── stacks/
    ├── data/                   # postgres(pgvector) + redis + admin UIs  (EXISTING)
    ├── market-research/        # NEW: ecommerce community scraper → KB / research
    └── career-ops/             # job-search tool (runs in Claude Code)
```

Each stack folder has: `docker-compose.yml`, `.env.example` (template, committed), and
`secrets.env` (SOPS-encrypted, committed). The real plaintext `secrets.env` is decrypted only
on the server at deploy time and is git-ignored.

## One-time setup

### On laptop + server
```bash
# install tools (server is Debian/Proxmox):
apt install -y age            # or: brew install age   (laptop)
# install sops from https://github.com/getsops/sops/releases

# generate ONE age keypair (do this on the laptop):
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt        # prints public key age1...
```
1. Put the printed **public key** into `.sops.yaml` (replace the placeholder).
2. Copy `keys.txt` to the **server** at `~/.config/sops/age/keys.txt` (`chmod 600`). This is the
   only secret that lives outside git — guard it (and back it up offline).

### Git init + push
```bash
cd C:\Users\ashis\homelab\homelab-infra
git init -b main
git add .
git commit -m "chore: initial GitOps scaffold"
# create a PRIVATE repo on GitHub, then:
git remote add origin git@github.com:<you>/homelab-infra.git
git push -u origin main
```

## Daily flow (manual pull — our chosen start)

```bash
# 1. On laptop: edit a stack, encrypt any new secret, commit, push
sops --encrypt --in-place stacks/market-research/secrets.env   # if you changed secrets
git commit -am "feat(market-research): add crawler service" && git push

# 2. On server (ssh devcore): pull + deploy
cd ~/homelab-infra && git pull
bash scripts/deploy.sh market-research
```

CI runs on every push and blocks merges if a compose file or Dockerfile is invalid.
Graduate to a self-hosted GitHub Actions runner later to auto-deploy on push.
