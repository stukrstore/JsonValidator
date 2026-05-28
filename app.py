import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

try:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    _AOAI_IMPORTS_OK = True
except Exception as _aoai_imp_err:  # pragma: no cover
    _AOAI_IMPORTS_OK = False
    _AOAI_IMPORT_ERROR = str(_aoai_imp_err)

load_dotenv()

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB = os.environ.get("MONGODB_DB", "jsonvalidator")
MONGODB_COLLECTION = os.environ.get("MONGODB_COLLECTION", "requests")

AOAI_ENDPOINT = os.environ.get(
    "AOAI_ENDPOINT", "https://mskr-aoai-eastus.openai.azure.com/"
)
AOAI_DEPLOYMENT = os.environ.get("AOAI_DEPLOYMENT", "gpt-5.3-codex")
AOAI_API_VERSION = os.environ.get("AOAI_API_VERSION", "2025-04-01-preview")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin1234")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")
app.permanent_session_lifetime = timedelta(hours=8)


@app.before_request
def _require_login():
    if session.get("auth") is True:
        return None
    # Allow the login page itself, its POST, and static assets through.
    if request.endpoint in {"login", "static"}:
        return None
    if request.path.startswith("/static/"):
        return None
    if request.path == "/login":
        return None
    # API calls get a JSON 401; pages get redirected to /login.
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="authentication required"), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session.clear()
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next") or request.form.get("next") or "/"
            if not nxt.startswith("/"):
                nxt = "/"
            return redirect(nxt)
        error = "Invalid password."
    return render_template(
        "login.html",
        error=error,
        next=request.args.get("next", "/"),
    )


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))

_client: MongoClient | None = None


def client() -> MongoClient:
    global _client
    if _client is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI is not set")
        _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
    return _client


def db():
    return client()[MONGODB_DB]


# ---- Validators (DocumentDB / Cosmos DB for MongoDB vCore $jsonSchema) ----

V1_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["request_id", "schema_version"],
        "properties": {
            "request_id": {"bsonType": "string", "minLength": 1,
                           "description": "Mandatory unique request id"},
            "schema_version": {"bsonType": "string", "pattern": "^v1$",
                               "description": "Schema version tag"},
            "request_status": {"bsonType": "string"},
            "event": {"bsonType": "string"},
            "action": {"bsonType": ["string", "null"]},
            "comments": {"bsonType": ["string", "null"]},
            "file_path": {"bsonType": ["string", "null"]},
            "channel": {"bsonType": ["string", "null"]},
            "request_received_date_time": {"bsonType": "date"},
        },
    }
}

V2_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["request_id", "channel", "schema_version"],
        "properties": {
            "request_id": {"bsonType": "string", "minLength": 1,
                           "description": "Mandatory unique request id"},
            "channel": {"bsonType": "string", "minLength": 1,
                        "description": "Mandatory channel (added in V2)"},
            "schema_version": {"bsonType": "string", "pattern": "^v2$",
                               "description": "Schema version tag"},
            "request_status": {"bsonType": "string"},
            "event": {"bsonType": "string"},
            "action": {"bsonType": ["string", "null"]},
            "comments": {"bsonType": ["string", "null"]},
            "file_path": {"bsonType": ["string", "null"]},
            "request_received_date_time": {"bsonType": "date"},
        },
    }
}

VALIDATORS = {"v1": V1_VALIDATOR, "v2": V2_VALIDATOR}

# Tracks the validator the user asked us to apply, so we can fall back to
# client-side enforcement when the cluster reports the validator command is
# not supported (Cosmos DB for MongoDB vCore currently returns CommandNotSupported
# for createCollection/collMod with `validator`).
_active_state = {
    "version": None,           # "v1" / "v2" / None
    "server_enforced": False,  # True iff cluster accepted the $jsonSchema
}


_BSON_TYPE_PYTHON = {
    "string": (str,),
    "int": (int,),
    "long": (int,),
    "double": (float, int),
    "decimal": (float, int),
    "number": (int, float),
    "bool": (bool,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "date": (datetime,),
    "null": (type(None),),
}


def _check_type(value, bson_type):
    types = bson_type if isinstance(bson_type, list) else [bson_type]
    for t in types:
        py = _BSON_TYPE_PYTHON.get(t)
        if py and isinstance(value, py):
            # Special-case: bool is a subclass of int — reject bool when only
            # numeric types are allowed.
            if isinstance(value, bool) and t in ("int", "long", "double",
                                                  "decimal", "number"):
                continue
            return True
    return False


def validate_with_schema(doc, validator):
    """Lightweight Python implementation of the subset of $jsonSchema used
    in this demo. Returns a list of error strings; empty list = valid."""
    errors: list[str] = []
    schema = validator.get("$jsonSchema", validator)
    if not isinstance(doc, dict):
        return ["root document must be an object"]

    for field in schema.get("required", []):
        if field not in doc:
            errors.append(f"required field missing: '{field}'")

    for field, rule in schema.get("properties", {}).items():
        if field not in doc:
            continue
        value = doc[field]
        bt = rule.get("bsonType")
        if bt is not None and not _check_type(value, bt):
            errors.append(f"field '{field}' must be of bsonType {bt}, "
                          f"got {type(value).__name__}")
            continue
        if "minLength" in rule and isinstance(value, str) \
                and len(value) < rule["minLength"]:
            errors.append(f"field '{field}' shorter than minLength="
                          f"{rule['minLength']}")
        if "pattern" in rule and isinstance(value, str) \
                and not re.search(rule["pattern"], value):
            errors.append(f"field '{field}' does not match pattern "
                          f"{rule['pattern']!r}")
    return errors


def _coerce_dates(obj):
    """Convert MongoDB extended-JSON-ish {'$date': '...'} into datetime.

    Walks dicts/lists recursively. Matches the sample document from the spec.
    """
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$date"} and isinstance(obj["$date"], str):
            raw = obj["$date"].replace("Z", "+00:00")
            return datetime.fromisoformat(raw)
        return {k: _coerce_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates(v) for v in obj]
    return obj


def _json_safe(obj):
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# ---- Routes ----

@app.route("/")
def index():
    return render_template(
        "index.html",
        db_name=MONGODB_DB,
        coll_name=MONGODB_COLLECTION,
        v1=json.dumps(V1_VALIDATOR, indent=2),
        v2=json.dumps(V2_VALIDATOR, indent=2),
    )


@app.route("/api/health")
def health():
    try:
        client().admin.command("ping")
        return jsonify(ok=True, db=MONGODB_DB, collection=MONGODB_COLLECTION)
    except PyMongoError as e:
        return jsonify(ok=False, error=str(e)), 503


@app.route("/api/status")
def status():
    """Return current collection validator (if any) and document count."""
    try:
        info = db().command({"listCollections": 1,
                             "filter": {"name": MONGODB_COLLECTION}})
        batch = info.get("cursor", {}).get("firstBatch", [])
        exists = bool(batch)
        opts = batch[0].get("options", {}) if exists else {}
        count = db()[MONGODB_COLLECTION].estimated_document_count() if exists else 0
        return jsonify(
            exists=exists,
            validator=opts.get("validator"),
            validationLevel=opts.get("validationLevel"),
            validationAction=opts.get("validationAction"),
            count=count,
            activeVersion=_active_state["version"],
            serverEnforced=_active_state["server_enforced"],
        )
    except PyMongoError as e:
        return jsonify(error=str(e)), 500


@app.route("/api/apply-validator", methods=["POST"])
def apply_validator():
    """Try to create collection (or collMod) with the chosen validator.

    If the cluster does not yet support server-side `$jsonSchema` (Cosmos DB
    for MongoDB vCore returns CommandNotSupported / code 115), we still
    remember the choice so /api/insert can enforce it client-side.
    """
    version = (request.json or {}).get("version", "v1").lower()
    level = (request.json or {}).get("validationLevel", "strict")
    action = (request.json or {}).get("validationAction", "error")
    if version not in VALIDATORS:
        return jsonify(error=f"Unknown version: {version}"), 400
    validator = VALIDATORS[version]
    server_error = None
    server_enforced = False
    mode = "client-side-only"
    try:
        existing = db().command(
            {"listCollections": 1, "filter": {"name": MONGODB_COLLECTION}}
        )
        exists = bool(existing.get("cursor", {}).get("firstBatch"))
        try:
            if not exists:
                db().create_collection(
                    MONGODB_COLLECTION,
                    validator=validator,
                    validationLevel=level,
                    validationAction=action,
                )
                mode = "createCollection"
            else:
                db().command({
                    "collMod": MONGODB_COLLECTION,
                    "validator": validator,
                    "validationLevel": level,
                    "validationAction": action,
                })
                mode = "collMod"
            server_enforced = True
        except OperationFailure as e:
            if getattr(e, "code", None) == 115:
                # Cluster does not yet support server-side $jsonSchema.
                # Ensure the collection exists anyway so inserts work, and
                # we'll enforce the validator client-side instead.
                if not exists:
                    db().create_collection(MONGODB_COLLECTION)
                server_error = (
                    "Cluster returned CommandNotSupported (code 115) for "
                    "server-side $jsonSchema. Falling back to client-side "
                    "enforcement inside this app."
                )
            else:
                raise
    except PyMongoError as e:
        return jsonify(ok=False, error=str(e)), 500

    _active_state["version"] = version
    _active_state["server_enforced"] = server_enforced
    return jsonify(
        ok=True,
        mode=mode,
        version=version,
        validationLevel=level,
        validationAction=action,
        serverEnforced=server_enforced,
        serverNote=server_error,
    )


@app.route("/api/drop", methods=["POST"])
def drop():
    try:
        db().drop_collection(MONGODB_COLLECTION)
        _active_state["version"] = None
        _active_state["server_enforced"] = False
        return jsonify(ok=True)
    except PyMongoError as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/insert", methods=["POST"])
def insert():
    raw = (request.json or {}).get("document")
    if raw is None:
        return jsonify(ok=False, error="Missing 'document' field"), 400
    try:
        if isinstance(raw, str):
            doc = json.loads(raw)
        else:
            doc = raw
    except json.JSONDecodeError as e:
        return jsonify(ok=False, error=f"Invalid JSON: {e}"), 400

    doc = _coerce_dates(doc)

    # Client-side validation when the cluster did not accept the validator.
    if _active_state["version"] and not _active_state["server_enforced"]:
        errs = validate_with_schema(doc, VALIDATORS[_active_state["version"]])
        if errs:
            return jsonify(
                ok=False,
                enforcedBy="client",
                version=_active_state["version"],
                error="Document failed $jsonSchema validation",
                details=errs,
            ), 400

    try:
        result = db()[MONGODB_COLLECTION].insert_one(doc)
        return jsonify(
            ok=True,
            insertedId=str(result.inserted_id),
            enforcedBy="server" if _active_state["server_enforced"] else
                       ("client" if _active_state["version"] else "none"),
        )
    except OperationFailure as e:
        details = e.details if hasattr(e, "details") else {}
        return jsonify(
            ok=False,
            enforcedBy="server",
            error=str(e),
            code=getattr(e, "code", None),
            details=_json_safe(details),
        ), 400
    except PyMongoError as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/recent")
def recent():
    try:
        cursor = db()[MONGODB_COLLECTION].find().sort("_id", -1).limit(10)
        docs = []
        for d in cursor:
            d["_id"] = str(d["_id"])
            docs.append(_json_safe(d))
        return jsonify(ok=True, docs=docs)
    except PyMongoError as e:
        return jsonify(ok=False, error=str(e)), 500


# ---- Chat (Azure OpenAI, gpt-5.3-codex, RBAC via Managed Identity) ----

_DOCS_PATH = os.path.join(os.path.dirname(__file__), "docs",
                          "jsonschema_reference.md")
try:
    with open(_DOCS_PATH, "r", encoding="utf-8") as _fh:
        _JSONSCHEMA_REFERENCE = _fh.read()
except OSError:
    _JSONSCHEMA_REFERENCE = ""

_CHAT_SYSTEM_PROMPT = f"""You are an assistant embedded in the JsonValidator
test page for Azure Cosmos DB for MongoDB (vCore) `$jsonSchema` validators.

Your job is to:
1. Answer the user's questions about the features shown on this page
   (applying validators V1/V2, validationLevel, validationAction, schema
   versioning workarounds, server-side vs client-side enforcement, etc.).
2. When asked, generate a `$jsonSchema` validator (a JSON object suitable
   for `db.createCollection({{validator: ...}})` or `collMod`).

Ground every answer in the DocumentDB documentation below. If a user asks
for an unsupported keyword (e.g. `oneOf`, `anyOf`, `enum`,
`additionalProperties`, `patternProperties`, `allOf`, `not`,
`dependencies`, `min/maxProperties`, `title`), point out that DocumentDB
does not support it and suggest the documented workaround.

Always return generated validators as a fenced ```json code block. Be
concise. Reply in the language the user uses (Korean or English).

----- DocumentDB $jsonSchema reference -----
{_JSONSCHEMA_REFERENCE}
----- end reference -----
"""


_aoai_client = None
_aoai_error = None


def aoai_client():
    """Lazily build an AzureOpenAI client using RBAC (no API key)."""
    global _aoai_client, _aoai_error
    if _aoai_client is not None or _aoai_error is not None:
        return _aoai_client
    if not _AOAI_IMPORTS_OK:
        _aoai_error = (
            f"openai/azure-identity not installed: {_AOAI_IMPORT_ERROR}"
        )
        return None
    try:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _aoai_client = AzureOpenAI(
            azure_endpoint=AOAI_ENDPOINT,
            api_version=AOAI_API_VERSION,
            azure_ad_token_provider=token_provider,
        )
        return _aoai_client
    except Exception as e:  # pragma: no cover
        _aoai_error = f"failed to init AzureOpenAI: {e}"
        return None


@app.route("/api/chat", methods=["POST"])
def chat():
    payload = request.json or {}
    history = payload.get("messages") or []
    if not isinstance(history, list) or not history:
        return jsonify(ok=False, error="messages must be a non-empty list"), 400

    cleaned = []
    for m in history[-12:]:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content[:4000]})
    if not cleaned or cleaned[-1]["role"] != "user":
        return jsonify(ok=False, error="last message must be from user"), 400

    cli = aoai_client()
    if cli is None:
        return jsonify(ok=False, error=_aoai_error or "AOAI client unavailable"), 500

    messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}] + cleaned
    want_stream = bool(payload.get("stream"))

    if want_stream:
        def event_stream():
            try:
                with cli.responses.stream(
                    model=AOAI_DEPLOYMENT,
                    input=messages,
                    max_output_tokens=1500,
                ) as stream:
                    for event in stream:
                        etype = getattr(event, "type", "")
                        if etype == "response.output_text.delta":
                            delta = getattr(event, "delta", "") or ""
                            if delta:
                                yield "data: " + json.dumps({"delta": delta}) + "\n\n"
                        elif etype in ("response.error", "error"):
                            err = getattr(event, "error", None) or str(event)
                            yield "data: " + json.dumps({"error": str(err)}) + "\n\n"
                yield "data: " + json.dumps({"done": True}) + "\n\n"
            except Exception as e:  # pragma: no cover
                yield "data: " + json.dumps({"error": f"chat failed: {e}"}) + "\n\n"

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        resp = cli.responses.create(
            model=AOAI_DEPLOYMENT,
            input=messages,
            max_output_tokens=1500,
        )
        reply = (getattr(resp, "output_text", "") or "").strip()
        if not reply:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    t = getattr(c, "text", None)
                    if t:
                        parts.append(t)
            reply = "\n".join(parts).strip() or "(empty reply)"
        return jsonify(ok=True, reply=reply, model=AOAI_DEPLOYMENT)
    except Exception as e:  # pragma: no cover
        return jsonify(ok=False, error=f"chat failed: {e}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
