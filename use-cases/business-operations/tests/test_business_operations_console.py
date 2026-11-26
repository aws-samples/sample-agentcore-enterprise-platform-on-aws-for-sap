# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations console: hosting, CSP, and the cross-stack wiring.

The console stack deploys first and publishes its URL; the main stack reads it
for the console client's callback URL and the CORS origin. The runtime stack,
deployed last, writes aws-exports.json into the console bucket, naming the
run-event channel it creates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "use-cases" / "business-operations"))

from business_operations import api, console

PREFIX = "agentcore-workshop-dev"
CONSOLE = f"{PREFIX}-uc-business-operations-console"
MAIN = f"{PREFIX}-uc-business-operations"
RUNTIME = f"{PREFIX}-uc-business-operations-runtime"
URL_PARAM = "/agentcore-workshop/dev/use-cases/business-operations/console-url"


@pytest.fixture(scope="module")
def out() -> Path:
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return out


def _template(out: Path, stack: str) -> dict:
    return json.loads((out / f"{stack}.template.json").read_text())


def _of(template: dict, kind: str) -> list[dict]:
    return [
        r["Properties"] for r in template["Resources"].values() if r["Type"] == kind
    ]


def _param_ref(template: dict, ssm_name: str) -> dict:
    (logical,) = [
        k for k, p in template["Parameters"].items() if p.get("Default") == ssm_name
    ]
    return {"Ref": logical}


def test_csp_names_each_service_by_region_and_no_third_party():
    policy = console.csp("eu-west-1", "https://pool.auth.eu-west-1.amazoncognito.com")
    directives = dict(d.split(" ", 1) for d in policy.split("; ") if " " in d)
    assert directives["script-src"] == "'self'"
    assert directives["font-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    connect = directives["connect-src"].split()
    assert "https://pool.auth.eu-west-1.amazoncognito.com" in connect
    assert "https://*.execute-api.eu-west-1.amazonaws.com" in connect
    # No account-wide AWS wildcard and no font CDN.
    assert not any(s.endswith("://*.amazonaws.com") for s in connect)
    assert "googleapis" not in policy and "gstatic" not in policy


def test_exports_omit_ticketing_when_it_is_off():
    base = {
        "issuer_url": "i",
        "client_id": "c",
        "url": "u",
        "region": "r",
        "api_url": "a",
        "admins_group": "g",
    }
    off = console.exports(**base, ticketing=False)
    assert off["ticketingEnabled"] is False
    on = console.exports(**base, ticketing=True)
    # /tickets is on the console API: there is no second URL to publish.
    assert on["ticketingEnabled"] is True and "ticketsApiUrl" not in on
    assert off["redirect_uri"] == off["post_logout_redirect_uri"] == "u"
    assert "runEventsRealtimeDomain" not in off


def test_exports_name_the_run_event_channel():
    """Without both domains the console opens no channel: no run, not even the
    briefing's narration, renders until its trace lands on the case."""
    base = {
        "issuer_url": "i",
        "client_id": "c",
        "url": "u",
        "region": "r",
        "api_url": "a",
        "admins_group": "g",
        "ticketing": False,
    }
    on = console.exports(**base, run_events=("h.appsync-api", "r.appsync-realtime-api"))
    assert on["runEventsHttpDomain"] == "h.appsync-api"
    assert on["runEventsRealtimeDomain"] == "r.appsync-realtime-api"


def test_exports_link_the_dashboard_and_agentcore_observability():
    """The console has no observability page of its own; the rail links these two."""
    base = {
        "issuer_url": "i",
        "client_id": "c",
        "url": "u",
        "region": "eu-west-1",
        "api_url": "a",
        "admins_group": "g",
        "ticketing": False,
    }
    assert "dashboardUrl" not in console.exports(**base)
    on = console.exports(**base, dashboard_url="https://d")
    assert on["dashboardUrl"] == "https://d"
    assert on["agentObservabilityUrl"].startswith(
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai"
    )


def test_console_stack_serves_the_spa_privately_with_the_csp(out):
    t = _template(out, CONSOLE)
    (headers,) = _of(t, "AWS::CloudFront::ResponseHeadersPolicy")
    security = headers["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert security["ContentSecurityPolicy"]["Override"] is True
    assert security["FrameOptions"]["FrameOption"] == "DENY"
    (dist,) = _of(t, "AWS::CloudFront::Distribution")
    config = dist["DistributionConfig"]
    # Client routes are rewritten by the function, so a missing chunk stays an error.
    assert "CustomErrorResponses" not in config
    assert config["DefaultCacheBehavior"]["FunctionAssociations"]
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    for bucket in _of(t, "AWS::S3::Bucket"):
        assert "VersioningConfiguration" not in bucket
        assert bucket["PublicAccessBlockConfiguration"]["RestrictPublicBuckets"] is True
    site, index = _of(t, "Custom::CDKBucketDeployment")
    assert site["Prune"] is True
    assert site["Exclude"] == [console.CONFIG_FILE, console.INDEX_FILE]
    # A browser revalidates index.html, so it never asks for a pruned chunk.
    assert index["Prune"] is False
    assert index["Include"] == [console.INDEX_FILE]
    assert index["SystemMetadata"]["cache-control"] == "no-cache"
    names = {p["Name"] for p in _of(t, "AWS::SSM::Parameter")}
    assert URL_PARAM in names


def test_main_stack_takes_the_console_url_and_writes_the_config(out):
    t = _template(out, MAIN)
    url = _param_ref(t, URL_PARAM)
    (client,) = [
        c for c in _of(t, "AWS::Cognito::UserPoolClient") if "CallbackURLs" in c
    ]
    assert url in client["CallbackURLs"] and url in client["LogoutURLs"]
    assert api.AUTHORING_REDIRECT_URI in client["CallbackURLs"]
    # Groups ride in these tokens, so their lifetime bounds how stale a role can be.
    assert client["AccessTokenValidity"] == client["IdTokenValidity"] == 5
    assert client["TokenValidityUnits"]["AccessToken"] == "minutes"
    assert client["TokenValidityUnits"]["IdToken"] == "minutes"
    cors = {
        json.dumps(f["Environment"]["Variables"]["CORS_ALLOWED_ORIGINS"])
        for f in _of(t, "AWS::Lambda::Function")
        if "CORS_ALLOWED_ORIGINS" in f.get("Environment", {}).get("Variables", {})
    }
    assert cors == {json.dumps(url)}
    assert not _of(t, "Custom::CDKBucketDeployment")
    manifest = json.loads((out / "manifest.json").read_text())
    assert CONSOLE in manifest["artifacts"][MAIN]["dependencies"]


def test_the_console_client_has_a_managed_login_branding(out):
    # The shared domain serves managed login, which shows no page to a client
    # without a branding style.
    t = _template(out, MAIN)
    hosted = {
        logical
        for logical, r in t["Resources"].items()
        if r["Type"] == "AWS::Cognito::UserPoolClient"
        and "CallbackURLs" in r["Properties"]
    }
    brandings = _of(t, "AWS::Cognito::ManagedLoginBranding")
    assert len(hosted) == 1
    assert {b["ClientId"]["Ref"] for b in brandings} == hosted
    assert all(b["UseCognitoProvidedValues"] is True for b in brandings)


def test_runtime_stack_writes_the_config_with_its_channel(out):
    t = _template(out, RUNTIME)
    (config,) = _of(t, "Custom::CDKBucketDeployment")
    assert config["Prune"] is False
    assert config["SystemMetadata"]["cache-control"] == "no-cache"
    (events,) = [
        k for k, r in t["Resources"].items() if r["Type"] == "AWS::AppSync::Api"
    ]
    # The domains are attributes of the Events API this stack creates.
    markers = json.dumps(config["SourceMarkers"])
    assert (
        f'"{events}", "Dns.Realtime"' in markers
        and f'"{events}", "Dns.Http"' in markers
    )


def test_the_log_bucket_is_emptied_only_after_its_writers_are_deleted(out):
    """A delete removes dependents first, so the hosting bucket and the distribution,
    which write the access logs, must depend on the log bucket's emptying resource."""
    resources = _template(out, CONSOLE)["Resources"]
    (empty,) = [
        k
        for k, r in resources.items()
        if r["Type"] == "Custom::S3AutoDeleteObjects"
        and "HostingBucketAccessLogs" in json.dumps(r["Properties"]["BucketName"])
    ]
    writers = [
        r
        for k, r in resources.items()
        if r["Type"] == "AWS::CloudFront::Distribution"
        or (
            r["Type"] == "AWS::S3::Bucket"
            and k.startswith("HostingBucket")
            and "AccessLogs" not in k
        )
    ]
    assert len(writers) == 2
    for r in writers:
        assert empty in r.get("DependsOn", [])


def test_exports_carry_every_console_feature_flag():
    base = {
        "issuer_url": "i",
        "client_id": "c",
        "url": "u",
        "region": "r",
        "api_url": "a",
        "admins_group": "g",
        "ticketing": False,
    }
    assert console.exports(**base)["features"] == {}
    on = console.exports(**base, features={"test_data": True})
    assert on["features"] == {"test_data": True}


def test_console_features_default_off_and_refuse_what_is_not_a_flag():
    from business_operations import config as uc_config

    assert uc_config.load({}).console.features.model_dump() == {}
    for bad in (
        # Release review is a production checklist item, not a console view.
        {"console": {"features": {"release_review": True}}},
        {"console": {"features": {"no_such_flag": True}}},
        {"console": {"theme": "dark"}},
    ):
        with pytest.raises(ValueError, match="use_cases.business-operations"):
            uc_config.load(bad)


def test_the_published_aws_exports_carries_the_configured_flags():
    import yaml

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "contract"
        / "business-operations.yaml"
    )
    settings = yaml.safe_load(fixture.read_text())
    settings["use_cases"]["business-operations"]["console"] = {"features": {}}
    result, out = synth_app(yaml.safe_dump(settings))
    assert result.returncode == 0, result.stderr[-2000:]
    published = [
        json.loads(p.read_text()) for p in out.glob("asset.*/aws-exports.json")
    ]
    # `test_data` mirrors `test_data.enabled`, off in this fixture.
    assert published and all(p["features"] == {"test_data": False} for p in published)


def test_a_promotion_only_stack_says_so_only_after_sign_in():
    import yaml

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "contract"
        / "business-operations.yaml"
    )
    settings = yaml.safe_load(fixture.read_text())
    settings["use_cases"]["business-operations"]["promotion"] = {"role": "quality"}
    result, out = synth_app(yaml.safe_dump(settings))
    assert result.returncode == 0, result.stderr[-2000:]
    published = [
        json.loads(p.read_text()) for p in out.glob("asset.*/aws-exports.json")
    ]
    # Anyone can fetch the file, so it says nothing of where the stack sits
    # (`GET /deployment/settings` does, behind sign-in).
    assert published and all("promotion_only" not in p["features"] for p in published)
    assert all("promotion" not in json.dumps(p) for p in published)
    # The brand is always written, so the console carries no default of its own.
    assert all(
        p["brand"]
        == {"name": "Business Operations", "subtitle": "SAP exception handling"}
        for p in published
    )


def test_the_brand_defaults_to_the_consoles_own_words_and_is_published():
    from business_operations import config as uc_config

    assert uc_config.load({}).console.brand.model_dump(exclude_none=True) == {
        "name": "Business Operations",
        "subtitle": "SAP exception handling",
    }
    base = {
        "issuer_url": "https://issuer",
        "client_id": "c",
        "url": "https://console",
        "region": "us-east-1",
        "api_url": "https://api",
        "ticketing": False,
        "admins_group": "admins",
    }
    out = console.exports(**base, brand={"name": "AP Desk", "subtitle": "Invoices"})
    assert out["brand"] == {"name": "AP Desk", "subtitle": "Invoices"}
    marked = {"name": "AP Desk", "subtitle": "Invoices", "monogram": "AP"}
    assert console.exports(**base, brand=marked)["brand"] == marked


@pytest.mark.parametrize(
    "brand",
    [
        {"name": ""},
        {"name": "   "},
        {"name": " Leading"},
        {"name": "Trailing "},
        {"name": "x" * 41},
        {"subtitle": "y" * 81},
        {"name": "<b>Bold</b>"},
        {"subtitle": "two\nlines"},
        {"subtitle": "tab\there"},
        {"name": 7},
        {"logo": "x.png"},
    ],
)
def test_a_brand_that_is_not_one_line_of_plain_text_fails(brand):
    from business_operations import config as uc_config

    with pytest.raises(ValueError, match="use_cases.business-operations"):
        uc_config.load({"console": {"brand": brand}})


@pytest.mark.parametrize("monogram", ["", "ABCD", "A B", "A&", "<b>", "\n", "🙂", 7])
def test_a_monogram_that_is_not_1_to_3_letters_or_digits_fails(monogram):
    from business_operations import config as uc_config

    with pytest.raises(ValueError, match="use_cases.business-operations"):
        uc_config.load({"console": {"brand": {"monogram": monogram}}})


@pytest.mark.parametrize("monogram", ["B", "AP", "x9", "Ö", "経理"])
def test_a_monogram_is_written_only_when_set(monogram):
    from business_operations import config as uc_config

    brand = uc_config.load({"console": {"brand": {"monogram": monogram}}}).console.brand
    assert brand.model_dump(exclude_none=True)["monogram"] == monogram
    assert "monogram" not in uc_config.load({}).console.brand.model_dump(
        exclude_none=True
    )


@pytest.mark.parametrize("name", ["AP & AR Desk", "Opérations", "Ops (EMEA) — North"])
def test_plain_text_with_punctuation_is_a_brand(name):
    from business_operations import config as uc_config

    assert (
        uc_config.load({"console": {"brand": {"name": name}}}).console.brand.name
        == name
    )
