# Cloud hosting guide

How to run EOU Bond Tracker as a hosted web application for a small team
(roughly 2–15 users). It covers the container image, managed PostgreSQL,
configuration, backups and access control, with a recommended path and the
alternatives.

---

## 1. Recommended architecture

```
 Users (browser, HTTPS)
        │
        ▼
 ┌──────────────────────┐     Identity-aware proxy / SSO (optional, recommended)
 │ Managed HTTPS +      │
 │ load balancer        │
 └─────────┬────────────┘
           ▼
 ┌──────────────────────┐     1 container (2 workers × 4 threads)
 │ App container        │     stateless; config from env / secret store
 │ gunicorn eoubond...  │
 └─────────┬────────────┘
           ▼  private network / SSL
 ┌──────────────────────┐     automated daily backups + point-in-time recovery
 │ Managed PostgreSQL   │
 └──────────────────────┘
```

The app is stateless: everything lives in PostgreSQL. One small container
(0.5 vCPU, 512 MB–1 GB RAM) and the smallest production tier of managed
PostgreSQL are enough for a small team.

**Host in an Indian region** (for example AWS `ap-south-1` Mumbai, Azure Central
India, Google Cloud `asia-south1` Mumbai) to keep customs records in India and
keep latency low.

### Platform options

| Option | App runs on | Database | Good for |
|---|---|---|---|
| **A. AWS (recommended)** | App Runner or ECS Fargate | RDS for PostgreSQL | Teams already on AWS; strongest backup/PITR and IAM |
| B. Azure | Azure Container Apps | Azure Database for PostgreSQL – Flexible Server | Microsoft 365 / Entra ID shops (easy SSO) |
| C. Google Cloud | Cloud Run | Cloud SQL for PostgreSQL | Simplest container hosting; Google Workspace SSO via IAP |
| D. PaaS (Render, Railway, etc.) | Web service from the Dockerfile | Their managed PostgreSQL | Fastest set-up; check region and backup retention first |

Every option uses the same image and the same three environment variables.

---

## 2. Containerisation

The repo ships a production [Dockerfile](../Dockerfile):

* `python:3.12-slim`, dependencies from `requirements.txt`;
* runs as a non-root user;
* on start: `python -m eoubond.cli upgrade` (applies Alembic migrations), then
  `gunicorn eoubond.wsgi:app` on `$PORT` (default 8000).

Build and test locally:

```bash
docker compose up --build
```

Push to your registry (AWS example):

```bash
aws ecr create-repository --repository-name eoubond --region ap-south-1
```
```bash
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.ap-south-1.amazonaws.com
```
```bash
docker build -t <account>.dkr.ecr.ap-south-1.amazonaws.com/eoubond:1.0.0 .
```
```bash
docker push <account>.dkr.ecr.ap-south-1.amazonaws.com/eoubond:1.0.0
```

Tag every release with a version (not only `latest`), so you can roll back to
the previous image.

> Running migrations at container start is fine with **one** app instance. If
> you scale to several instances, run `python -m eoubond.cli upgrade` as a
> separate one-off task before rolling out the new image, and remove it from the
> `CMD`.

---

## 3. Managed PostgreSQL

Recommended settings (RDS names; Azure / GCP have equivalents):

| Setting | Value |
|---|---|
| Engine | PostgreSQL 16 |
| Size | `db.t4g.micro` / `small` to start |
| Storage | 20 GB gp3, storage autoscaling on |
| Public access | **No** – reachable only from the app's VPC / private network |
| Encryption at rest | On (default KMS key is fine) |
| Force SSL | On (`rds.force_ssl=1`) and add `?sslmode=require` to the URL |
| Automated backups | On, **retention 35 days** (the maximum) |
| Deletion protection | On |
| Multi-AZ | Optional; worth it once the system is the book of record |

Create a dedicated database and login (not the master user) for the app:

```sql
CREATE DATABASE eoubond;
CREATE USER eoubond_app WITH PASSWORD '<long random password>';
GRANT ALL PRIVILEGES ON DATABASE eoubond TO eoubond_app;
\c eoubond
GRANT ALL ON SCHEMA public TO eoubond_app;
```

Connection string:

```
postgresql+psycopg://eoubond_app:<password>@<endpoint>:5432/eoubond?sslmode=require
```

(`postgres://` and `postgresql://` URLs are also accepted and converted.)

---

## 4. Environment configuration

| Variable | Required | Value |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL URL above |
| `SECRET_KEY` | yes | 48+ random characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SESSION_COOKIE_SECURE` | yes in cloud | `1` (cookies only over HTTPS) |
| `EOU_RULES_FILE` | no | path to an alternative `rules.toml` |
| `PORT` | no | set by most platforms; default 8000 |

Store `DATABASE_URL` and `SECRET_KEY` in the platform's secret store (AWS Secrets
Manager / SSM Parameter Store, Azure Key Vault, GCP Secret Manager) and inject
them as environment variables. Never bake them into the image or commit them.

**Business rules** (`config/rules.toml`) are part of the image, so a rate change
goes through a normal release and is recorded in version control. To change
rules without rebuilding, mount a file and point `EOU_RULES_FILE` at it.

---

## 5. Step-by-step: AWS App Runner (recommended path)

1. **Network** – use the default VPC or create one with two private subnets in
   `ap-south-1`.
2. **Database** – create RDS PostgreSQL 16 in the private subnets with the
   settings in §3. Security group: allow 5432 only from the App Runner VPC
   connector's security group.
3. **Secrets** – in Secrets Manager create `eoubond/DATABASE_URL` and
   `eoubond/SECRET_KEY`.
4. **Image** – build and push to ECR (§2).
5. **App Runner service**
   * Source: the ECR image; port 8000.
   * Instance: 0.5 vCPU / 1 GB; min 1, max 2 instances.
   * Environment: `SESSION_COOKIE_SECURE=1`; secrets from step 3.
   * Networking: outgoing traffic through a **VPC connector** in the private
     subnets (so it can reach RDS).
   * Health check: HTTP path `/login`.
6. **First admin user** – run once from a machine that can reach the database
   (bastion / Cloud Shell in the VPC, or an ECS one-off task with the same image):
   ```bash
   python -m eoubond.cli create-user admin --role admin
   ```
   Then sign in and create the other users under **Users**.
7. **Domain** – add a custom domain (e.g. `bond.yourcompany.in`) in App Runner;
   it issues the TLS certificate.
8. **Access control** – see §7 (at minimum, restrict by IP or put SSO in front).
9. **Alarms** – CloudWatch alarms on RDS free storage, CPU and on App Runner 5xx
   responses, sent to an email list.

Equivalent on **Azure**: Container Apps (ingress HTTPS, min replicas 1) +
PostgreSQL Flexible Server with private access + Key Vault references.
On **Google Cloud**: Cloud Run (min instances 1) + Cloud SQL with the Cloud SQL
connector or private IP + Secret Manager.

---

## 6. Backups and recovery

The bond ledger and audit trail are statutory records – plan for recovery
**before** go-live.

1. **Automated backups + point-in-time recovery** (managed DB): 35-day retention.
   Lets you restore to any minute, e.g. just before a wrong posting.
2. **Monthly logical export kept long-term**: customs records must be kept for
   years, longer than snapshot retention. Run monthly (scheduled task / cron
   container):
   ```bash
   pg_dump --format=custom --no-owner "$DATABASE_URL_PLAIN" > eoubond-$(date +%Y%m).dump
   ```
   (`DATABASE_URL_PLAIN` = the URL without the `+psycopg` part.) Upload to object
   storage (S3 / Blob / GCS) with **versioning and object lock**, a lifecycle rule
   to move it to archive storage after 90 days, and retention matching your
   record-keeping obligation (at least 8 years is a common choice; confirm yours).
3. **Monthly Excel archive** – download **Reports → All reports** after month
   close and file it with the month's papers. It is human-readable without the app.
4. **Test a restore every quarter**: restore the latest snapshot to a temporary
   instance, point a local copy of the app at it, check the bond balances against
   the last month's Excel archive, then delete it.

Recovery targets this gives a small team: data loss ≤ 5 minutes (PITR),
restore time about 1 hour.

---

## 7. Access control

Layer it; each layer is cheap.

1. **Application roles** (built in): `admin`, `operator`, `viewer`. Give
   `admin` to one or two people. Auditors and management get `viewer`.
   Disable (don't delete) users who leave – their names stay in the audit trail.
2. **Network restriction** – if the team works from the office / VPN, allow only
   those IPs (AWS WAF IP set on App Runner, Azure Container Apps IP restrictions,
   Cloud Armor on GCP).
3. **SSO in front of the app (recommended)** – put an identity-aware proxy in
   front so only company accounts with MFA can reach the login page:
   Google Cloud IAP, Azure Container Apps built-in authentication (Entra ID),
   or AWS ALB + Cognito / OIDC. App passwords then become a second factor.
4. **HTTPS only** with `SESSION_COOKIE_SECURE=1`. The app sets `HttpOnly` and
   `SameSite=Lax` cookies and checks a CSRF token on every form post.
5. **Database** – private network only, SSL enforced, app uses its own
   non-master login. Only one or two admins hold cloud console access; enable MFA
   for them.
6. **Review** – once a quarter, export **Reports → Audit trail** and review user
   list, role changes, bond edits and cancellations.

---

## 8. Releases and maintenance

* **Release**: build a new tagged image → (if more than one instance, run
  `upgrade` as a one-off task) → deploy → smoke-test login, dashboard and one
  report → done. Roll back by redeploying the previous tag; Alembic migrations are
  additive by convention so the previous image keeps working.
* Take a manual DB snapshot before any release that contains a migration.
* Monthly: apply base-image updates (`docker build --pull`), check dependency
  advisories, confirm last night's backup exists.
* Run the test suite (`python -m pytest`) in CI on every change; it uses SQLite
  and needs no database server. Add a CI job against a PostgreSQL service
  container before go-live.

---

## 9. Go-live checklist

- [ ] Rates and rules in `config/rules.toml` confirmed with the customs consultant
- [ ] Item master with item-specific BCD/SWS rates loaded
- [ ] BOMs for every model and version loaded with correct effective dates
- [ ] Opening stock and opening bond balances loaded and reconciled to the bond register
- [ ] `SECRET_KEY` generated and stored in the secret store; `SESSION_COOKIE_SECURE=1`
- [ ] Database private, SSL on, backups 35 days, deletion protection on
- [ ] Monthly `pg_dump` to versioned, locked object storage scheduled
- [ ] Restore tested once
- [ ] Users created with least-privilege roles; SSO or IP restriction in front
- [ ] Alarms on DB storage, CPU and app errors
