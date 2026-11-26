# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations under the platform's identity.mode direct (Entra issues every token).

Units: the API's token authorizer, Cedar's per-mode principal condition, the identity
module's refusals and principal map, entitlement's role and `azp` reads, the run checks' and
the invoker's shared caller key, and the person-token hand-off from POST /runs through
the invoker. Then what app.py synthesizes from the direct-mode contract fixture.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

import bo_sap_helper
import jwt
import pytest
from app_synth import synth_app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_business_operations_api import UC, USERS, _event

from infra_utils.use_case_context import UseCaseContext
from sap_kit import cedar_policies, run_grant

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(UC))

from business_operations import cedar, identity, policies
from business_operations import config as uc_config

TENANT = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KEYS_URL = "https://login.microsoftonline.com/keys"
PLATFORM_APP, CONSOLE_APP = "platform-app", "console-app"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── The API's token authorizer ──


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk(key, kid="k1") -> dict:
    return {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": kid,
    }


def _sign(key, claims: dict, *, kid="k1", alg="RS256") -> str:
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def authorizer(monkeypatch, rsa_key):
    monkeypatch.setenv("DISCOVERY_URL", f"{ISSUER}/.well-known/openid-configuration")
    monkeypatch.setenv("AUDIENCE", PLATFORM_APP)
    monkeypatch.setenv("ALLOWED_CLIENTS", CONSOLE_APP)
    mod = _load("bo_api_authorizer", UC / "api_authorizer" / "index.py")
    jwks = {"keys": [_jwk(rsa_key)]}
    docs = {
        f"{ISSUER}/.well-known/openid-configuration": {
            "issuer": ISSUER,
            "jwks_uri": KEYS_URL,
        },
        KEYS_URL: jwks,
    }
    hits: list[str] = []

    def fake_open(
        _self, req, data=None, timeout=None
    ):  # every fetch, urlopen or PyJWKClient
        url = req if isinstance(req, str) else req.full_url
        hits.append(url)
        return io.BytesIO(json.dumps(docs[url]).encode())

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    mod.docs, mod.hits = docs, hits
    return mod


def _claims(**over):
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": PLATFORM_APP,
        "azp": CONSOLE_APP,
        "sub": "person-sub",
        "oid": "person-oid",
        "exp": now + 600,
        "nbf": now - 10,
        "roles": [USERS, "business-operations-ap-clerk"],
        "scp": "business-operations",
        **over,
    }


def test_a_console_access_token_is_allowed_with_its_claims_as_context(
    authorizer, rsa_key
):
    out = authorizer.handler(
        {
            "authorizationToken": f"Bearer {_sign(rsa_key, _claims())}",
            "methodArn": "arn:aws:execute-api:us-east-1:1:api/prod/GET/cases",
        },
        None,
    )
    (statement,) = out["policyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Resource"] == "arn:aws:execute-api:us-east-1:1:api/prod/*"
    assert out["principalId"] == "person-sub"
    assert json.loads(out["context"]["roles"]) == [
        USERS,
        "business-operations-ap-clerk",
    ]
    assert out["context"]["azp"] == CONSOLE_APP


def test_the_v1_issuer_form_of_the_same_tenant_is_accepted(authorizer, rsa_key):
    v1 = _claims(iss=f"https://sts.windows.net/{TENANT}/", azp=None, appid=CONSOLE_APP)
    assert authorizer.verify(_sign(rsa_key, v1))["sub"] == "person-sub"


@pytest.mark.parametrize(
    "claims,sign",
    [
        pytest.param({"aud": CONSOLE_APP}, {}, id="an-id-token"),
        pytest.param({"aud": "another-api"}, {}, id="another-audience"),
        pytest.param({"azp": "another-app"}, {}, id="another-client"),
        pytest.param(
            {"iss": "https://login.microsoftonline.com/x/v2.0"}, {}, id="issuer"
        ),
        pytest.param({"exp": int(time.time()) - 120}, {}, id="expired"),
        pytest.param({"nbf": int(time.time()) + 600}, {}, id="not-yet"),
        pytest.param({"sub": ""}, {}, id="no-subject"),
        pytest.param({}, {"kid": "unknown"}, id="unknown-kid"),
    ],
)
def test_anything_else_is_refused(authorizer, rsa_key, claims, sign):
    with pytest.raises(authorizer.Refused):
        authorizer.verify(_sign(rsa_key, _claims(**claims), **sign))


def test_a_tampered_or_resigned_token_is_refused(authorizer, rsa_key):
    head, _body, sig = _sign(rsa_key, _claims()).split(".")
    forged = _b64(json.dumps(_claims(roles=["business-operations-admins"])).encode())
    with pytest.raises(authorizer.Refused, match="signature"):
        authorizer.verify(f"{head}.{forged}.{sig}")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(authorizer.Refused, match="signature"):
        authorizer.verify(_sign(other, _claims()))


def _unsigned(header: dict, claims: dict) -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."


@pytest.mark.parametrize(
    "header",
    [
        pytest.param({"alg": "none", "kid": "k1"}, id="none"),
        pytest.param({"alg": "RS512", "kid": "k1"}, id="RS512"),
        pytest.param({"alg": "PS256", "kid": "k1"}, id="PS256"),
        pytest.param({"alg": "RS256"}, id="no-kid"),
        pytest.param({"alg": "RS256", "kid": 1}, id="kid-not-a-string"),
    ],
)
def test_only_rs256_with_a_kid_is_accepted(authorizer, header):
    with pytest.raises(authorizer.Refused):
        authorizer.verify(_unsigned(header, _claims()))


def test_an_hmac_token_keyed_with_the_public_key_is_refused(authorizer, rsa_key):
    """Algorithm confusion: alg is pinned before any key is used."""
    pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    head = _b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    body = _b64(json.dumps(_claims()).encode())
    sig = _b64(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(authorizer.Refused, match="only RS256"):
        authorizer.verify(f"{head}.{body}.{sig}")


@pytest.mark.parametrize(
    "token", ["", "x", "x.y", "x.y.z", "a.b.c.d"], ids=["empty", "1", "2", "3", "4"]
)
def test_a_malformed_token_is_refused(authorizer, token):
    with pytest.raises(authorizer.Refused):
        authorizer.verify(token)


@pytest.mark.parametrize("claim", ["exp", "iss", "aud"])
def test_a_token_missing_a_required_claim_is_refused(authorizer, rsa_key, claim):
    claims = _claims()
    del claims[claim]
    with pytest.raises(authorizer.Refused):
        authorizer.verify(_sign(rsa_key, claims))


def test_a_list_audience_is_refused_even_when_it_names_ours(authorizer, rsa_key):
    with pytest.raises(authorizer.Refused, match="audience"):
        authorizer.verify(_sign(rsa_key, _claims(aud=[PLATFORM_APP, "other"])))


def test_the_skew_is_a_minute_either_way(authorizer, rsa_key):
    now = int(time.time())
    assert authorizer.verify(_sign(rsa_key, _claims(exp=now - 30, nbf=now + 30)))
    with pytest.raises(authorizer.Refused, match="expired"):
        authorizer.verify(_sign(rsa_key, _claims(exp=now - 90)))
    with pytest.raises(authorizer.Refused, match="not yet valid"):
        authorizer.verify(_sign(rsa_key, _claims(nbf=now + 90)))


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_a_key_shorter_than_2048_bits_is_refused(monkeypatch, authorizer):
    short = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    authorizer.docs[KEYS_URL]["keys"] = [_jwk(short)]
    with pytest.raises(authorizer.Refused, match="too short"):
        authorizer.verify(_sign(short, _claims()))


def test_an_empty_audience_setting_refuses_everything(monkeypatch, authorizer, rsa_key):
    monkeypatch.setattr(authorizer, "AUDIENCE", "")
    with pytest.raises(authorizer.Refused, match="audience"):
        authorizer.verify(_sign(rsa_key, _claims(aud="")))


def test_a_rotated_key_is_found_and_forged_kids_refetch_once_a_minute(
    authorizer, rsa_key
):
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert authorizer.verify(_sign(rsa_key, _claims()))  # caches k1 only
    authorizer.docs[KEYS_URL]["keys"].append(
        _jwk(rotated, "k2")
    )  # the tenant publishes k2
    authorizer._jwks()._last_successful_fetch -= authorizer.REFETCH_EVERY + 1
    fetched = authorizer.hits.count(KEYS_URL)
    assert authorizer.verify(_sign(rotated, _claims(), kid="k2"))
    assert authorizer.hits.count(KEYS_URL) == fetched + 1
    for kid in ("forged-1", "forged-2", "forged-3"):
        with pytest.raises(authorizer.Refused, match="unknown signing key"):
            authorizer.verify(_sign(rsa_key, _claims(), kid=kid))
    assert authorizer.hits.count(KEYS_URL) == fetched + 1


def test_a_withdrawn_key_stops_verifying_once_the_keys_age_out(
    monkeypatch, authorizer, rsa_key
):
    token = _sign(rsa_key, _claims())
    assert authorizer.verify(token)  # k1 cached
    authorizer.docs[KEYS_URL]["keys"] = []
    assert authorizer.verify(token)  # still within the TTL
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + authorizer.KEYS_TTL + 1)
    with pytest.raises(authorizer.Refused, match="unknown signing key"):
        authorizer.verify(token)


def test_an_unreadable_key_source_is_a_401_not_a_500(monkeypatch, rsa_key):
    monkeypatch.setenv("DISCOVERY_URL", f"{ISSUER}/.well-known/openid-configuration")
    monkeypatch.setenv("AUDIENCE", PLATFORM_APP)
    monkeypatch.setenv("ALLOWED_CLIENTS", CONSOLE_APP)
    mod = _load("bo_api_authorizer_down", UC / "api_authorizer" / "index.py")

    def down(url, timeout=None):
        raise mod.urllib.error.URLError("no route")

    monkeypatch.setattr(mod.urllib.request, "urlopen", down)
    with pytest.raises(Exception, match="^Unauthorized$"):
        mod.handler(
            {
                "authorizationToken": f"Bearer {_sign(rsa_key, _claims())}",
                "methodArn": "a/b/c",
            },
            None,
        )


def test_a_refusal_is_a_401_not_a_policy(authorizer):
    with pytest.raises(Exception, match="^Unauthorized$"):
        authorizer.handler(
            {"authorizationToken": "Bearer x.y.z", "methodArn": "a/b/c"}, None
        )


# ── Cedar per mode ──

PRINCIPALS = {"read": ["reader", "writer"], "write": ["writer"], None: [CONSOLE_APP]}


def test_brokered_conditions_are_unchanged():
    assert (
        cedar.for_mode(policies.STATEMENTS[0].text, principals=None)
        == policies.STATEMENTS[0].text
    )
    assert 'getTag("scope")' in cedar.scoped_permit(["a"], "read", "arn:g")
    assert cedar.scoped_permit(["a"], None, "arn:g").endswith('"arn:g"\n);')


def test_direct_conditions_key_on_the_calling_app():
    text = cedar.scoped_permit(["t___read"], "read", "arn:g", principals=PRINCIPALS)
    assert 'principal.getTag("azp") == "reader" ||' in text
    assert 'principal.getTag("azp") == "writer")' in text
    assert "scope" not in text
    obo = cedar.scoped_permit(["t___odata_read"], None, "arn:g", principals=PRINCIPALS)
    assert f'principal.getTag("azp") == "{CONSOLE_APP}"' in obo


def test_every_direct_statement_drops_the_scope_tag():
    for s in policies.STATEMENTS:
        text = cedar.for_mode(s.text, principals=PRINCIPALS)
        assert 'getTag("scope")' not in text, s.id
        if s.text.startswith("permit"):
            assert 'getTag("azp")' in text, s.id
    mail = next(s for s in policies.STATEMENTS if s.id == "notifications")
    assert '"reader"' not in cedar.for_mode(mail.text, principals=PRINCIPALS)


@pytest.mark.parametrize("apps", [[], ['x" || true || "']])
def test_a_scope_no_app_holds_or_a_bad_id_is_refused(apps):
    with pytest.raises(ValueError):
        cedar_policies.principal_condition(cedar.SCOPE_PREFIX, "read", {"read": apps})


# ── The identity module ──


def _ctx(mode="direct", clients=True):
    from infra_utils.platform_config import PlatformConfig

    ident = {"mode": mode}
    if mode == "direct":
        ident |= {
            "idp": "entra_id",
            "tenant_id": TENANT,
            "client_id": PLATFORM_APP,
            "client_secret_name": "s",
        }
        if clients:
            app = {"client_secret_name": "s"}
            ident["use_case_clients"] = {
                "business-operations": {
                    "reader": {"client_id": "reader", **app},
                    "writer": {"client_id": "writer", **app},
                }
            }
    platform = PlatformConfig.model_validate(
        {
            "deployment": {"platform_account": "111122223333"},
            "identity": ident,
            "layers": {"sap": {"access": {"business-operations": {}}}},
            "use_cases": {"business-operations": {}},
        }
    )
    return UseCaseContext("p", "dev", platform)


ENTRA = {
    "console_client_id": CONSOLE_APP,
    "api_scope": f"api://{PLATFORM_APP}/business-operations",
    "delegating_client_id": "delegating",
    "delegating_secret_name": "d",
}
OBO = {
    "system_role": "sandbox",
    "flow": "obo",
    "base_url": "https://sap.example.com/",
    "secret_name": "x",
    "scopes": ["api://sap/.default"],
    "gateway_scopes": ["api://mcp/.default"],
}


def test_direct_mode_needs_its_apps():
    with pytest.raises(ValueError, match="console_client_id"):
        identity.require(_ctx(), uc_config.load({}))
    identity.require(_ctx(), uc_config.load({"entra": ENTRA}))


def test_brokered_mode_refuses_an_entra_block():
    with pytest.raises(ValueError, match="direct"):
        identity.require(_ctx("brokered"), uc_config.load({"entra": ENTRA}))
    assert identity.cedar_principals(_ctx("brokered"), uc_config.load({})) is None


def test_the_console_holds_scopes_only_when_its_token_is_forwarded():
    plain = uc_config.load({"entra": ENTRA})
    assert identity.cedar_principals(_ctx(), plain) == {
        "read": ["reader", "writer"],
        "write": ["writer"],
        None: [CONSOLE_APP],
    }
    obo = bo_sap_helper.config({"entra": ENTRA, "sap": {"systems": {"p": OBO}}})
    assert identity.forwards_person_token(_ctx(), obo)
    assert identity.cedar_principals(_ctx(), obo) == {
        "read": ["reader", "writer", CONSOLE_APP],
        "write": ["writer", CONSOLE_APP],
        None: [CONSOLE_APP],
    }


# ── Entitlement: app roles and `azp` ──


def _entra_event(roles, azp=CONSOLE_APP, resource="/cases"):
    event = _event("GET", resource, groups=())
    claims = {"aud": PLATFORM_APP, "azp": azp, "sub": "s", "roles": json.dumps(roles)}
    event["requestContext"]["authorizer"] = claims  # a Lambda authorizer's context
    return event


@pytest.fixture
def entitlement(monkeypatch):
    import entitlement

    monkeypatch.setenv("API_ALLOWED_CLIENTS", CONSOLE_APP)
    monkeypatch.setenv("USERS_GROUP", USERS)
    monkeypatch.setenv("ADMINS_GROUP", "business-operations-admins")
    monkeypatch.setenv("GROUP_CLAIMS", "roles")
    return entitlement


def test_direct_mode_reads_roles_and_the_console_app(entitlement):
    assert entitlement.admit(_entra_event([USERS])) is None
    assert entitlement.admit(_entra_event([])) == entitlement.NOT_A_USER
    assert entitlement.admit(_entra_event([USERS], azp="other")) == (
        entitlement.FORBIDDEN_CLIENT
    )


def test_with_roles_configured_a_groups_claim_grants_nothing(entitlement):
    event = _entra_event([])
    event["requestContext"]["authorizer"]["groups"] = json.dumps([USERS])
    assert entitlement.admit(event) == entitlement.NOT_A_USER


# ── The run checks and the invoker name the same caller ──


def _jwt(claims: dict) -> str:
    return f"h.{_b64(json.dumps(claims).encode())}.s"


@pytest.mark.parametrize(
    "claims,key",
    [
        ({"client_id": "cognito-client"}, "cognito-client"),
        ({"azp": "writer", "oid": "sp-oid"}, "writer"),
        ({"azp": CONSOLE_APP, "oid": "person-oid", "scp": "x"}, "user:person-oid"),
    ],
)
def test_the_caller_key_follows_the_token(claims, key):
    assert run_grant.caller_key(claims) == key


@pytest.fixture
def invoker(monkeypatch):
    for k, v in {
        "RUNTIME_ARN_PARAMETER": "/p/arn",
        "CASES_TABLE": "cases",
        "AWS_REGION": "us-east-1",
        "AGENT_CLIENT_ID": "writer",
        "RUN_GRANTS_TABLE": "grants",
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        mod = _load("bo_invoker_direct", UC / "invoker/index.py")
    mod.ssm.get_parameter.return_value = {"Parameter": {"Value": "arn:aws:x:runtime/r"}}
    monkeypatch.setattr(mod, "get_delegating_token", lambda: "delegating-token")
    return mod


def test_a_forwarded_token_binds_the_grant_and_rides_as_a_header(invoker, monkeypatch):
    token = _jwt({"azp": CONSOLE_APP, "oid": "person-oid", "scp": "x", "sub": "s"})
    assert invoker.person_key(token) == run_grant.caller_key(
        {"azp": CONSOLE_APP, "oid": "person-oid", "scp": "x"}
    )
    sent = {}

    class Stream:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout=None):
        sent["headers"] = dict(request.header_items())
        sent["body"] = request.data
        return Stream()

    monkeypatch.setattr(invoker.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        invoker, "_drain_stream", lambda resp, deadline: (1, "RUN_FINISHED")
    )
    invoker._handle_direct(
        {
            "case_id": "",
            "prompt": "p",
            "run_id": "r1",
            "subject": "s",
            "user_token": token,
        },
        None,
    )
    grant = invoker.dynamodb.Table.return_value.put_item.call_args.kwargs["Item"]
    assert grant["client_id"] == "user:person-oid"
    headers = {k.lower(): v for k, v in sent["headers"].items()}
    assert headers[invoker.USER_TOKEN_HEADER.lower()] == token
    assert headers["authorization"] == "Bearer delegating-token"
    assert token.encode() not in sent["body"]  # never in the agent's payload
    # The run id the browser gets back is not enough to use the grant: its secret
    # rides only this header, and the grant holds only its hash.
    secret = headers[invoker.RUN_GRANT_HEADER.lower()]
    assert len(secret) >= 32 and secret.encode() not in sent["body"]
    assert grant["secret_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(grant)


def test_without_a_token_the_grant_names_the_agent_client(invoker):
    assert invoker._grant_item({}, "r", attended=True, now=0)["client_id"] == "writer"


# ── POST /runs forwards the caller's bearer only when told to ──


@pytest.fixture
def runs(monkeypatch):
    def load(forward: bool):
        for k, v in {
            "API_ALLOWED_CLIENTS": CONSOLE_APP,
            "USERS_GROUP": USERS,
            "GROUP_CLAIMS": "roles",
            "CASES_TABLE": "cases",
            "INVOKER_FUNCTION_NAME": "invoker",
            "ENTITLEMENT_MAP": json.dumps({"ap": {}}),
            "AWS_REGION": "us-east-1",
        }.items():
            monkeypatch.setenv(k, v)
        if forward:
            monkeypatch.setenv("FORWARD_USER_TOKEN", "true")
        else:
            monkeypatch.delenv("FORWARD_USER_TOKEN", raising=False)
        with mock.patch("boto3.client"), mock.patch("boto3.resource"):
            mod = _load(f"bo_runs_direct_{forward}", UC / "api" / "runs" / "index.py")
        mod.lambda_client = mock.MagicMock()
        return mod

    return load


@pytest.mark.parametrize("forward", [False, True])
def test_runs_hands_the_invoker_the_bearer_only_when_forwarding(runs, forward):
    mod = runs(forward)
    event = _entra_event([USERS], resource="/runs")
    event.update(
        httpMethod="POST",
        body=json.dumps({"prompt": "hello", "session_id": "s" * 33}),
        headers={"Authorization": "Bearer the-person-token"},
    )
    response = mod.handler(event, None)
    assert response["statusCode"] == 202, response["body"]
    payload = json.loads(mod.lambda_client.invoke.call_args.kwargs["Payload"])
    assert payload.get("user_token") == ("the-person-token" if forward else None)


# ── What app.py synthesizes in direct mode ──

PREFIX = "agentcore-workshop-dev"
MAIN = f"{PREFIX}-uc-business-operations"


@pytest.fixture(scope="module")
def out() -> Path:
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations-direct.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return out


def _resources(out: Path, stack: str) -> dict:
    return json.loads((out / f"{stack}.template.json").read_text())["Resources"]


def _of(resources: dict, kind: str) -> list[dict]:
    return [r["Properties"] for r in resources.values() if r["Type"] == kind]


def test_direct_mode_creates_nothing_on_a_pool(out):
    for stack in (MAIN, f"{MAIN}-runtime", f"{MAIN}-sap", f"{MAIN}-knowledge"):
        kinds = {r["Type"] for r in _resources(out, stack).values()}
        assert not {k for k in kinds if k.startswith("AWS::Cognito::")}, stack


def test_the_api_takes_the_entra_token_authorizer(out):
    resources = _resources(out, MAIN)
    authorizers = _of(resources, "AWS::ApiGateway::Authorizer")
    assert authorizers and {a["Type"] for a in authorizers} == {"TOKEN"}
    (fn,) = [
        f
        for f in _of(resources, "AWS::Lambda::Function")
        if "AUDIENCE" in (f.get("Environment") or {}).get("Variables", {})
    ]
    env = fn["Environment"]["Variables"]
    assert env["AUDIENCE"] == "REPLACE_WITH_ENTRA_APP_CLIENT_ID"
    assert env["ALLOWED_CLIENTS"] == "REPLACE_WITH_CONSOLE_APP_ID"
    methods = _of(resources, "AWS::ApiGateway::Method")
    assert {m["AuthorizationType"] for m in methods} <= {"CUSTOM", "NONE"}


def test_the_run_route_forwards_and_mints_at_entra(out):
    (runs,) = [
        f["Environment"]["Variables"]
        for f in _of(_resources(out, MAIN), "AWS::Lambda::Function")
        if "FORWARD_USER_TOKEN" in (f.get("Environment") or {}).get("Variables", {})
    ]
    assert runs["DELEGATING_TOKEN_URL"].startswith("https://login.microsoftonline.com/")
    assert runs["DELEGATING_SCOPE"] == "REPLACE_WITH_DELEGATING_APP_ID/.default"
    assert runs["GROUP_CLAIMS"] == "roles"


def test_cedar_keys_on_azp_in_every_stack(out):
    texts = json.dumps(
        [
            _of(_resources(out, s), "AWS::BedrockAgentCore::Policy")
            for s in (MAIN, f"{MAIN}-sap", f"{MAIN}-knowledge")
        ]
    )
    assert 'getTag(\\"scope\\")' not in texts
    assert 'getTag(\\"azp\\") == \\"REPLACE_WITH_WRITER_APP_ID\\"' in texts
    assert 'getTag(\\"azp\\") == \\"REPLACE_WITH_CONSOLE_APP_ID\\"' in texts


def test_the_runtime_admits_the_delegating_app_and_the_person_token(out):
    resources = _resources(out, f"{MAIN}-runtime")
    (runtime,) = _of(resources, "AWS::BedrockAgentCore::Runtime")
    jwt = runtime["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["AllowedAudience"] == ["REPLACE_WITH_DELEGATING_APP_ID"]
    assert "AllowedClients" not in jwt
    allow = runtime["RequestHeaderConfiguration"]["RequestHeaderAllowlist"]
    assert identity.USER_TOKEN_HEADER in allow
    env = runtime["EnvironmentVariables"]
    assert env["RUNTIME_DELEGATING_ONLY"] == "true"
    assert env["RUNTIME_USER_TOKEN_AUDIENCE"] == "REPLACE_WITH_ENTRA_APP_CLIENT_ID"
    assert env["GATEWAY_SCOPES"] == "REPLACE_WITH_ENTRA_APP_CLIENT_ID/.default"
    (api,) = _of(resources, "AWS::AppSync::Api")
    modes = {p["AuthType"] for p in api["EventConfig"]["AuthProviders"]}
    assert modes == {"AWS_IAM", "OPENID_CONNECT"}
