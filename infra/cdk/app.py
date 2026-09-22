#!/usr/bin/env python3
"""CDK app for the Lou serverless deployment (louisville-open-data-i0q).

One stack, LouStack, in account 012146975534 / us-east-1, deployed as the
scoped `lou-deploy` role (infra/iam/README.md). Everything it creates is in
the `lou-` namespace and tagged Project=lou / ManagedBy=cdk — that is what the
deploy role's policies and the permissions boundary allow, and what the cost
budget filters on.

The `lou0` qualifier matches the scoped bootstrap (CDKToolkit-Lou): without it
CDK would look for the default-named bootstrap resources and fail.
"""
import os

import aws_cdk as cdk

from lou_stack import LouStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT", "012146975534"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)
LouStack(
    app, "LouStack",
    env=env,
    synthesizer=cdk.DefaultStackSynthesizer(qualifier="lou0"),
    description="Lou: Louisville open-data expenditure bot — Lambda + Function URL + CloudFront + DynamoDB",
)
cdk.Tags.of(app).add("Project", "lou")
cdk.Tags.of(app).add("ManagedBy", "cdk")
app.synth()
