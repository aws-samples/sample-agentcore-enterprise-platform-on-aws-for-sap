# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The first console user, `admin_email`, made with the stack so Cognito mails a welcome.

`AdminCreateUser` sends the invitation: the temporary password, valid seven days. The
email is marked verified, because the pool recovers an account through a verified
email only. Every change to a `UserPoolUser`'s attributes needs a replacement, and its
username is custom, so CloudFormation refuses one: keep the attribute list as it is.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito

from business_operations.packs import PACK_ADMINS_GROUP


def create(stack: cdk.Stack, pool_id: str, email: str) -> cognito.CfnUserPoolUser:
    """The user, in every group this stack creates except pack-admins (structure)."""
    user = cognito.CfnUserPoolUser(
        stack,
        "AdminUser",
        user_pool_id=pool_id,
        username=email,
        user_attributes=[
            cognito.CfnUserPoolUser.AttributeTypeProperty(name="email", value=email),
            cognito.CfnUserPoolUser.AttributeTypeProperty(
                name="email_verified", value="true"
            ),
        ],
        desired_delivery_mediums=["EMAIL"],
    )
    groups = [
        g
        for g in stack.node.find_all()
        if isinstance(g, cognito.CfnUserPoolGroup) and g.group_name != PACK_ADMINS_GROUP
    ]
    for group in groups:
        attachment = cognito.CfnUserPoolUserToGroupAttachment(
            stack,
            f"AdminUserIn-{group.node.id}",
            user_pool_id=pool_id,
            username=email,
            group_name=group.group_name,
        )
        attachment.add_dependency(user)
        attachment.add_dependency(group)
    return user
