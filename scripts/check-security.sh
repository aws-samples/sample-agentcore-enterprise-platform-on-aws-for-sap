#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# The security gate CI runs, as one command: ASH over the source, ASH over the
# CloudFormation templates a production deployment synthesizes, cdk-nag over the
# same templates, and gitleaks over the full history. Each fails on any finding
# .ash.yaml / .cdk-nag.yaml / .gitleaks.toml does not suppress, which is the whole
# gate; CI's security jobs call this script.
#
#   scripts/check-security.sh                     all four
#   scripts/check-security.sh source|templates|cdk-nag|gitleaks
#
# Needs ash (the version .gitlab-ci.yml pins), grype, syft and cfn_nag_scan on
# PATH, gitleaks, and cdk-nag in $PY. ASH reports a missing grype or syft as
# MISSING and a missing cfn-nag as PASSED, both of which pass silently, so the ASH
# scans refuse to start without them. cfn-nag needs Ruby 3.x: its kwalify
# dependency fails on Ruby 4. cdk-nag is in $PY
# (ASH runs cdk-nag too, but 3.2.2 drops its results and reports PASSED, so
# scripts/check_cdk_nag.py runs it directly). No AWS credentials.
#
# The IaC scanners read CloudFormation, not CDK Python, so the source scan finds
# no infrastructure. The template scan synthesizes every production-mode contract
# fixture (each fixtures/contract/*.yaml under tests/, a use case or a layer whose
# deployment.mode is production; or TEMPLATE_CONFIGS), keeps only the
# *.template.json files, and scans those. By mode, not file name, so the
# platform-only fixture is scanned when every use case is removed.
# Template suppressions in .ash.yaml match by stack-name suffix, so they hold for
# any project/environment.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python3}"
[ -x "$PY" ] || PY=python3

# Fails unless every binary scanner ASH would otherwise skip without failing runs.
require_scanners() {
    local missing=() tool
    for tool in grype syft cfn_nag_scan; do
        "$tool" --version >/dev/null 2>&1 || missing+=("$tool")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    echo "missing or broken: ${missing[*]} (ASH would skip them and still pass; cfn-nag needs Ruby 3.x)" >&2
    return 1
}

WORK=""
trap '[ -z "$WORK" ] || rm -rf "$WORK"' EXIT

# The ASH config to scan with: .ash.yaml, plus the suppressions of each use case's and
# layer's own .ash.yaml (for its files and stack templates, so deleting the folder takes
# them with it) and of internal/.ash.yaml when a checkout has it (paths never published),
# the way internal/.gitleaks.toml extends .gitleaks.toml.
ash_config() {
    local merged="$REPO/.ash/config-merged.yaml"
    mkdir -p "$REPO/.ash"
    "$PY" -B - "$REPO" "$merged" <<'PYEOF' || return 1
import sys
from pathlib import Path

import yaml

repo, out = Path(sys.argv[1]), sys.argv[2]
config = yaml.safe_load((repo / ".ash.yaml").read_text())
extra = sorted(repo.glob("use-cases/*/.ash.yaml")) + sorted(repo.glob("layers/*/.ash.yaml"))
extra += [p for p in [repo / "internal" / ".ash.yaml"] if p.is_file()]
for path in extra:
    more = yaml.safe_load(path.read_text())["global_settings"]["suppressions"]
    config["global_settings"]["suppressions"].extend(more)
with open(out, "w") as f:
    yaml.safe_dump(config, f, sort_keys=False)
PYEOF
    echo "$merged"
}

source_scan() {
    require_scanners || return 1
    local config
    config=$(ash_config) || return 1
    ash --mode local --config "$config"
}

# Synthesizes every production fixture into $WORK/templates/<n>/, once per run:
# the template and cdk-nag scans read the same synth.
synth_templates() {
    [ -z "$WORK" ] || return 0
    # shellcheck disable=SC2206  # a space-separated list of paths, globbed
    local configs=(${TEMPLATE_CONFIGS:-})
    if [ -z "${TEMPLATE_CONFIGS:-}" ]; then
        # shellcheck disable=SC2207  # one fixture path per line, none with spaces
        configs=($("$PY" -B - {tests,use-cases/*,layers/*}/fixtures/contract/*.yaml <<'PY'
import sys
from pathlib import Path

import yaml

for f in sys.argv[1:]:
    if Path(f).is_file():
        doc = yaml.safe_load(Path(f).read_text()) or {}
        if (doc.get("deployment") or {}).get("mode") == "production":
            print(f)
PY
        ))
        [ "${#configs[@]}" -gt 0 ] || { echo "no production-mode contract fixture" >&2; return 1; }
    fi
    WORK="$(mktemp -d)"
    local work="$WORK" i=0 config
    for config in "${configs[@]}"; do
        i=$((i + 1))
        echo "synth: $config"
        # Bundling off: the templates are what is scanned, and the source scan
        # reads the Lambda and image sources at their real paths. The shell's own
        # AWS settings are dropped so the synth is the same on every machine, and
        # AZ pinning, an AWS lookup, is skipped.
        env -u AWS_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
            PLATFORM_CONFIG="$config" CDK_OUTDIR="$work/synth-$i" \
            CDK_CONTEXT_JSON='{"aws:cdk:bundling-stacks":[]}' \
            CDK_DEFAULT_ACCOUNT=111111111111 CDK_DEFAULT_REGION=us-east-1 \
            PLATFORM_ALLOW_PLACEHOLDERS=1 AGENTCORE_SKIP_AZ_PINNING=1 \
            "$PY" -B app.py >"$work/synth-$i.log" 2>&1 ||
            { tail -40 "$work/synth-$i.log"; WORK=""; rm -rf "$work"; return 1; }
        mkdir -p "$work/templates/$i"
        # Only what CloudFormation deploys: tree.json, manifest.json and the
        # *.assets.json / *.metadata.json bookkeeping never leave the machine.
        cp "$work/synth-$i"/*.template.json "$work/templates/$i/"
    done
}

templates_scan() {
    require_scanners || return 1
    synth_templates || return 1
    local config
    config=$(ash_config) || return 1
    ash --mode local --config "$config" --source-dir "$WORK/templates" \
        --output-dir .ash/templates
}

cdk_nag_scan() {
    synth_templates || return 1
    mkdir -p .ash/cdk-nag
    "$PY" -B scripts/check_cdk_nag.py \
        --report .ash/cdk-nag/findings.json "$WORK/templates"
}

gitleaks_scan() {
    # internal/.gitleaks.toml extends the root config with the fork's allowlists.
    local config=.gitleaks.toml
    [ -f internal/.gitleaks.toml ] && config=internal/.gitleaks.toml
    gitleaks git . --config "$config" --redact --no-banner
}

case "${1:-all}" in
    source) source_scan ;;
    templates) templates_scan ;;
    cdk-nag) cdk_nag_scan ;;
    gitleaks) gitleaks_scan ;;
    all)
        rc=0
        source_scan || rc=1
        templates_scan || rc=1
        cdk_nag_scan || rc=1
        gitleaks_scan || rc=1
        exit "$rc"
        ;;
    *)
        echo "usage: $0 [source|templates|cdk-nag|gitleaks]" >&2
        exit 2
        ;;
esac
