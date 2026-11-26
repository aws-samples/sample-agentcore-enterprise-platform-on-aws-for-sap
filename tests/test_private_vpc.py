# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""security.vpc: a chosen CIDR, a NAT per AZ, or the customer's own VPC.

The networking/* SSM contract must read the same in every mode, because the layers
and use cases place their runtimes and Lambdas from it alone.
"""

import json

import pytest
import yaml
from pydantic import ValidationError

from infra_utils.platform_config import PlatformConfig
from tests.app_synth import synth_app

VPC = "vpc-0abc1234def56789a"
SUBNETS = ["subnet-0aaa1111bbbb2222c", "subnet-0ddd3333eeee4444f"]


def _config(vpc: dict | None) -> str:
    security = {"networking": True} | ({"vpc": vpc} if vpc is not None else {})
    return yaml.safe_dump(
        {
            "project": "net",
            "environment": "vt",
            "region": "us-east-1",
            "security": security,
        }
    )


def _networking(vpc: dict | None) -> dict:
    result, out = synth_app(_config(vpc))
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads((out / "net-vt-networking.template.json").read_text())


def _of(template: dict, kind: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


@pytest.mark.parametrize(
    ("vpc", "message"),
    [
        ({"vpc_id": VPC}, "come together"),
        (
            {"vpc_id": VPC, "private_subnet_ids": SUBNETS[:1], "cidr": "10.1.0.0/16"},
            "at least two",
        ),
        (
            {"vpc_id": VPC, "private_subnet_ids": SUBNETS},
            "give the imported VPC's CIDR",
        ),
        (
            {
                "vpc_id": VPC,
                "private_subnet_ids": SUBNETS,
                "cidr": "10.1.0.0/16",
                "egress": "nat_per_az",
            },
            "egress applies only",
        ),
        ({"endpoints": "none"}, "needs an imported vpc_id"),
        ({"cidr": "10.1.0.0/24"}, "/16 to /22"),
        ({"cidr": "10.1.0.1/16"}, "host bits"),
        (
            {"private_subnet_ids": ["subnet-x", "subnet-x"], "vpc_id": VPC},
            "not subnet ids",
        ),
    ],
)
def test_a_half_or_wrong_vpc_block_is_refused(vpc, message):
    with pytest.raises(ValidationError, match=message):
        PlatformConfig.model_validate(yaml.safe_load(_config(vpc)))


def test_the_vpc_block_needs_networking():
    with pytest.raises(ValidationError, match="needs security.networking"):
        PlatformConfig.model_validate({"security": {"vpc": {"cidr": "10.1.0.0/16"}}})


def test_absent_and_default_blocks_build_todays_vpc():
    default = _networking(None)
    assert default == _networking({})
    (vpc,) = _of(default, "AWS::EC2::VPC")
    assert vpc["Properties"]["CidrBlock"] == "10.0.0.0/16"
    assert len(_of(default, "AWS::EC2::NatGateway")) == 1


def test_cidr_and_nat_per_az_shape_the_created_vpc():
    template = _networking({"cidr": "10.42.0.0/20", "egress": "nat_per_az"})
    (vpc,) = _of(template, "AWS::EC2::VPC")
    assert vpc["Properties"]["CidrBlock"] == "10.42.0.0/20"
    assert len(_of(template, "AWS::EC2::NatGateway")) == 2


def test_an_imported_vpc_publishes_the_same_contract_with_its_own_ids():
    result, out = synth_app(
        _config({"vpc_id": VPC, "private_subnet_ids": SUBNETS, "cidr": "10.1.0.0/16"})
    )
    assert result.returncode == 0, result.stderr[-2000:]
    template = json.loads((out / "net-vt-networking.template.json").read_text())
    for kind in (
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::NatGateway",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::RouteTable",
    ):
        assert not _of(template, kind), kind
    endpoints = _of(template, "AWS::EC2::VPCEndpoint")
    assert endpoints and all(
        e["Properties"]["VpcEndpointType"] == "Interface" for e in endpoints
    )
    assert all(e["Properties"]["SubnetIds"] == SUBNETS for e in endpoints)
    (sg,) = [
        g
        for g in _of(template, "AWS::EC2::SecurityGroup")
        if "runtime" in json.dumps(g["Properties"].get("GroupName", ""))
    ]
    assert sg["Properties"]["VpcId"] == VPC
    params = {
        p["Properties"]["Name"]: p["Properties"]["Value"]
        for p in _of(template, "AWS::SSM::Parameter")
    }
    assert params["/net/vt/networking/vpc-id"] == VPC
    assert params["/net/vt/networking/private-subnet-ids"] == ",".join(SUBNETS)

    runtime = json.loads(
        (out / "net-vt-runtime-orchestrator.template.json").read_text()
    )
    (cfn_runtime,) = _of(runtime, "AWS::BedrockAgentCore::Runtime")
    assert (
        cfn_runtime["Properties"]["NetworkConfiguration"]["NetworkModeConfig"][
            "Subnets"
        ]
        == SUBNETS
    )


def test_an_imported_vpc_can_leave_endpoints_to_the_network_team():
    template = _networking(
        {"vpc_id": VPC, "private_subnet_ids": SUBNETS, "endpoints": "none"}
    )
    assert not _of(template, "AWS::EC2::VPCEndpoint")


def _check_network():
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import check_network
    finally:
        sys.path.remove(str(scripts))
    return check_network


class _Ec2:
    def __init__(self, routes, gateway_endpoints=("com.amazonaws.us-east-1.s3",)):
        self.routes, self.gateway_endpoints = routes, gateway_endpoints

    def describe_route_tables(self, **_):
        return {
            "RouteTables": [
                {
                    "Associations": [{"Main": True}],
                    "Routes": [
                        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1"}
                    ],
                },
                {"Associations": [{"SubnetId": SUBNETS[1]}], "Routes": self.routes},
            ]
        }

    def describe_vpc_endpoints(self, **_):
        return {"VpcEndpoints": [{"ServiceName": s} for s in self.gateway_endpoints]}


def _subnets(*azs, vpc=VPC):
    return [
        {"SubnetId": s, "VpcId": vpc, "AvailabilityZoneId": az}
        for s, az in zip(SUBNETS, azs)
    ]


def test_check_network_refuses_a_public_foreign_or_single_az_subnet(capsys):
    check = _check_network().check_subnets_are_private
    nat = [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-2"}]
    check(_Ec2(nat), VPC, _subnets("use1-az1", "use1-az2"))
    assert "PASS" in capsys.readouterr().out
    check(_Ec2(nat, gateway_endpoints=()), VPC, _subnets("use1-az1", "use1-az2"))
    assert "WARN: no S3 gateway endpoint" in capsys.readouterr().out
    for ec2, subnets in [
        (
            _Ec2([{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}]),
            _subnets("use1-az1", "use1-az2"),
        ),
        (_Ec2(nat), _subnets("use1-az1", "use1-az1")),
        (_Ec2(nat), _subnets("use1-az1", "use1-az2", vpc="vpc-0fffffffffffffff0")),
    ]:
        with pytest.raises(SystemExit):
            check(ec2, VPC, subnets)
