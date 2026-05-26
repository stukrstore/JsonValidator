# JsonValidator

Web-based test page that demonstrates Azure **Cosmos DB for MongoDB (vCore)** / **DocumentDB**
`$jsonSchema` validator behavior, including required-field enforcement and **schema versioning**
(V1 → V2) via `collMod`.

Reference: [MS Learn — `$jsonSchema`](https://learn.microsoft.com/en-us/documentdb/query/operators/evaluation-query/$jsonschema)

## Architecture

```
Browser
   │  HTTPS
   ▼
Azure Container Apps  (jsonvalidator-app)
  └─ Container Apps Environment: jsonvalidator-cae
     └─ Subnet: AZ-KR-INFRA-VNET / JsonValidator-ACA-Subnet (10.1.34.0/23)
   │  MongoDB wire protocol (SRV, TLS, SCRAM-SHA-256)
   ▼
Azure Cosmos DB for MongoDB (vCore)  =  mskr-documentdb
  └─ Database:  jsonvalidator
     └─ Collection: requests  (with $jsonSchema validator)
```

- Region: **Korea Central**
- VNet: `AZ-KR-INFRA-VNET` (RG `az-managemet-rg`)
- ACA subnet: `JsonValidator-ACA-Subnet` (10.1.34.0/23, delegated `Microsoft.App/environments`)
  - Created by `deploy/deploy.sh` if missing. The existing `ACA-Subnet` / `ACA-Subnet2` are already
    consumed by other Container Apps environments, so this app gets its own subnet.
- Image registry: `mskraksclustercr.azurecr.io`

## What the test page does

1. **Apply validator** — creates the `requests` collection with a `$jsonSchema` validator at V1 or
   V2. Subsequent applies use `collMod` to evolve the schema.
2. **Insert a document** — sends a JSON document to MongoDB and shows whether the validator
   accepts it. The classic failure case (missing `channel` under V2) is wired up as a one-click
   sample.
3. **List recent** — shows the 10 most recent documents in the collection.

### Schema versions

| Field | V1 required | V2 required |
|---|---|---|
| `request_id` | ✅ | ✅ |
| `channel` | optional | ✅ |
| `schema_version` | must match `^v1$` | must match `^v2$` |

The `schema_version` field is pinned via `pattern`, so the validator implicitly rejects documents
that claim the wrong version. DocumentDB does **not** support `oneOf` / `anyOf` / `enum`, so
conditional "if v1 then X else Y" required-lists must be expressed by either (a) using `collMod`
to switch the validator outright, or (b) splitting into per-version collections. This app uses
approach (a).

## Repository layout

```
.
├── app.py                 # Flask app + MongoDB client + validator definitions
├── templates/index.html   # Test page UI (English)
├── static/                # CSS + JS
├── Dockerfile             # python:3.13-slim + gunicorn
├── requirements.txt
├── deploy/deploy.sh       # Idempotent ACA deploy script
├── .env.sample            # Copy to .env and fill in real values
├── .gitignore             # Excludes .env
└── README.md
```

## Run locally

```bash
# 1. Activate your venv (example: virtualenvwrapper)
workon JsonValidator

# 2. Install deps
pip install -r requirements.txt

# 3. Configure
cp .env.sample .env
# edit .env and set MONGODB_URI (URL-encode special chars in the password —
# e.g. `#` becomes `%23`)

# 4. Run
python app.py
# → http://localhost:8000
```

### URL-encoding the password

The password contains characters that must be percent-encoded in the SRV URI:

| Char | Encoded |
|---|---|
| `#` | `%23` |
| `@` | `%40` |
| `/` | `%2F` |
| `:` | `%3A` |
| `?` | `%3F` |

Example: `REDACTED_PASSWORD` → `REDACTED_PASSWORD`.

## Deploy to Azure Container Apps

Prereqs:
- `az login` with permissions to the target subscription
- `containerapp` extension (auto-installed on first use)
- A populated `.env` file at the repo root

```bash
chmod +x deploy/deploy.sh
./deploy/deploy.sh
```

The script will:
1. Create resource group `AZ-JSONVALIDATOR-RG` (Korea Central) if missing
2. Build the image with `az acr build` and push to `mskraksclustercr.azurecr.io/jsonvalidator:latest`
3. Create the ACA environment `jsonvalidator-cae` bound to `JsonValidator-ACA-Subnet` (creating
   the subnet itself if necessary)
4. Create or update container app `jsonvalidator-app` (external ingress on 8000)
5. Print the public FQDN

Secrets are pushed as ACA secrets (`mongodb-uri`, `flask-secret`) and referenced via `secretref:`.

## API

All endpoints return JSON.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/`                      | Test page |
| `GET`  | `/api/health`            | Ping the cluster |
| `GET`  | `/api/status`            | Current validator + document count |
| `POST` | `/api/apply-validator`   | Body: `{"version":"v1"\|"v2","validationLevel":"strict","validationAction":"error"}` |
| `POST` | `/api/drop`              | Drop the collection |
| `POST` | `/api/insert`            | Body: `{"document": {...}}` |
| `GET`  | `/api/recent`            | Last 10 docs |

## Sample documents

✅ Passes V2:

```json
{
  "request_id": "test1",
  "request_status": "COMPLETED",
  "event": "XYZ",
  "channel": "ABC",
  "schema_version": "v2",
  "request_received_date_time": { "$date": "2026-03-17T16:52:16.881Z" }
}
```

❌ Fails V2 (`channel` missing):

```json
{
  "request_id": "test2",
  "schema_version": "v2"
}
```

## Prerequisite: enable `SchemaValidation` on the Mongo cluster

Server-side `$jsonSchema` on **Cosmos DB for MongoDB (vCore)** is a **preview feature** and must
be enabled on the cluster before `createCollection` / `collMod` will accept the `validator`
option. Without it the cluster returns `CommandNotSupported (code 115)` and the app falls back
to client-side enforcement.

Enable it once per cluster (replace IDs as appropriate):

```bash
SUB_ID=$(az account show --query id -o tsv)
RG="AZ-NOSQL-RG"
CLUSTER="mskr-documentdb"

# Inspect existing preview features (PATCH replaces the whole array, so re-include any others)
az resource show \
  --ids "/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.DocumentDB/mongoClusters/$CLUSTER" \
  --api-version 2024-10-01-preview \
  --query "properties.previewFeatures"

# Enable SchemaValidation
az resource patch \
  --ids "/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.DocumentDB/mongoClusters/$CLUSTER" \
  --api-version 2024-10-01-preview \
  --properties '{"previewFeatures": ["SchemaValidation"]}'
```

Notes:
- `GeoReplicas` can only be set at cluster **creation** — it cannot be added afterwards.
- The patch takes roughly 1–2 minutes to settle on the cluster.

## Notes / limitations

- **Cosmos DB for NoSQL** does **not** support `$jsonSchema`.
- **Cosmos DB for MongoDB (vCore)** — `$jsonSchema` on `createCollection` / `collMod` requires the
  `SchemaValidation` preview feature on the cluster (see section above). If it is not enabled the
  cluster returns `CommandNotSupported (code 115)` and **this app automatically falls back to
  validating documents in Python** using the same `$jsonSchema` definition, so the demo still
  works end-to-end. The UI status bar shows `mode=server-enforced` or
  `mode=client-enforced (fallback)` so you can tell which path is active.
- The page is intentionally minimal — it is a **test harness**, not a production schema
  registry. Do not expose it on the public internet without adding auth (e.g. ACA `auth` /
  Easy Auth, or front it with APIM).
- The container is configured with public ingress for demo convenience. For production deployments
  inside the VNet, set `--internal-only true` when creating the ACA environment and access the
  app via private endpoint / on-prem connectivity.
