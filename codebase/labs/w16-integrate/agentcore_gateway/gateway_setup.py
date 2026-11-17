#!/usr/bin/env python3
"""AgentCore Gateway — the managed version of the door you built by hand.

In mcp_demo.py you wrote the governed door yourself: typed tools, a scope
check the LLM cannot talk past, an audit row per access, a scoped key per
analyst. This script stands up the SAME door as managed AWS resources, so you
can name what the managed service is charging you for:

    mcp_demo.py (hand-rolled)              AgentCore (managed)
    -------------------------------------  -----------------------------------
    the scoped mrdn_ key in an env var  →  Identity: API-key credential
                                           provider (a vault; the key never
                                           lives in agent code again)
    the MeridianMCPServer class         →  Gateway (an MCP endpoint AWS runs)
    each @tool wrapper you typed        →  Gateway target: the OpenAPI spec
                                           (openapi.json) — every operationId
                                           becomes an MCP tool
    "who may call the door?"            →  Gateway authorizer (AWS_IAM: the
                                           caller signs with SigV4)

The platform's RBAC/audit story does NOT move: scope is still enforced inside
/api/v1 on every request. Gateway only replaces the tool-wiring and key-
handling layers — the deterministic enforcement layer stays where it was.

The four rungs:

    python3 gateway_setup.py plan      # $0 floor — prints every AWS call it
                                       # WOULD make, real payloads, no creds
    python3 gateway_setup.py apply     # create provider + gateway + target
    python3 gateway_setup.py invoke    # MCP tools/list + get_dispute 29
    python3 gateway_setup.py destroy   # leave nothing running you don't own

`apply` needs: AWS creds with bedrock-agentcore + iam permissions, and
MERIDIAN_API_KEY (your scoped analyst key) in the env.

IK shared-account note: the account fails closed (explicit deny on
bedrock-agentcore:*) while its AWS Lab is STOPPED in the Lab Control Panel —
that state is captured in logs/gateway_denied_2026-07-22.txt. With the lab
STARTED the full ladder runs (verified live 2026-07-22, see
logs/gateway_apply_live_2026-07-22.txt). Run `plan` anywhere, creds or not.
"""
from __future__ import annotations

import json
import sys
import os
from pathlib import Path

HERE = Path(__file__).parent
REGION = os.environ.get("AWS_REGION", "us-east-1")
GATEWAY_NAME = "meridian-gateway"
PROVIDER_NAME = "meridian-scoped-key"
TARGET_NAME = "meridian-api"
ROLE_NAME = "MeridianAgentCoreGatewayRole"
STATE_FILE = HERE / "gateway_state.json"          # ids/ARNs apply created, for destroy
OPENAPI = (HERE / "openapi.json").read_text()

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
# What the gateway itself is allowed to do on our behalf: read the stored key,
# mint workload tokens. Least privilege — same lesson as the scoped mrdn_ key.
ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetResourceApiKey",
            "secretsmanager:GetSecretValue",
        ],
        "Resource": "*",
    }],
}


def _mask(key: str) -> str:
    return key[:9] + "…" if key else "<MERIDIAN_API_KEY not set>"


def calls(api_key: str, role_arn: str = "arn:aws:iam::<your-account>:role/" + ROLE_NAME):
    """The exact control-plane calls, in order. `plan` prints these; `apply` runs them."""
    return [
        ("bedrock-agentcore-control.create_api_key_credential_provider", {
            "name": PROVIDER_NAME,
            "apiKey": _mask(api_key),
        }),
        ("iam.create_role  (once)", {
            "RoleName": ROLE_NAME,
            "AssumeRolePolicyDocument": TRUST_POLICY,
            "_inline_policy": ROLE_POLICY,
        }),
        ("bedrock-agentcore-control.create_gateway", {
            "name": GATEWAY_NAME,
            "roleArn": role_arn,
            "protocolType": "MCP",
            "authorizerType": "AWS_IAM",
            "description": "Managed MCP door over the Meridian /api/v1 (W16 lab)",
        }),
        ("bedrock-agentcore-control.create_gateway_target", {
            "gatewayIdentifier": "<gateway id from create_gateway>",
            "name": TARGET_NAME,
            "targetConfiguration": {"mcp": {"openApiSchema": {
                "inlinePayload": f"<openapi.json — {len(OPENAPI)} bytes, 4 operations: "
                                 "get_dispute, get_dispute_evidence, search_policies, "
                                 "create_dispute_ticket>"}}},
            "credentialProviderConfigurations": [{
                "credentialProviderType": "API_KEY",
                "credentialProvider": {"apiKeyCredentialProvider": {
                    "providerArn": "<provider ARN from create_api_key_credential_provider>",
                    "credentialLocation": "HEADER",
                    "credentialParameterName": "Authorization",
                    "credentialPrefix": "Bearer ",
                }},
            }],
        }),
    ]


def plan() -> None:
    print("PLAN — the calls `apply` will make (nothing is created):\n")
    for i, (name, payload) in enumerate(calls(os.environ.get("MERIDIAN_API_KEY", "")), 1):
        print(f"  {i}. {name}")
        print("     " + json.dumps(payload, indent=2).replace("\n", "\n     "))
        print()
    print("Then: MCP endpoint = the gatewayUrl create_gateway returns;")
    print("`invoke` signs tools/list + tools/call with SigV4 (service bedrock-agentcore).")
    print("Every operationId in openapi.json is now a tool ANY MCP client can call —")
    print("your agent, Claude Code, an IDE — with the key injected by the vault,")
    print("and RBAC still enforced (and audited) inside /api/v1 itself.")


def _denied(e) -> None:
    msg = str(e)
    action = msg.split("perform: ")[-1].split(" on ")[0] if "perform: " in msg else "?"
    print(f"\n{msg}", file=sys.stderr)
    print(f"\n⛔ AccessDenied on {action}", file=sys.stderr)
    print("   On the IK shared account this means the AWS Lab is STOPPED — press "
          "Start in the Lab Control Panel (same denial captured verbatim in "
          "logs/gateway_denied_2026-07-22.txt).", file=sys.stderr)
    print("   On your own account: attach BedrockAgentCoreFullAccess (+ iam:PassRole "
          "on the gateway role).", file=sys.stderr)
    print("   Meanwhile: run `plan` — it works anywhere, creds or not.",
          file=sys.stderr)
    sys.exit(2)


def _clients():
    import boto3
    return (boto3.client("iam"),
            boto3.client("bedrock-agentcore-control", region_name=REGION))


def _wait_ready(ctl, gateway_id: str, timeout: int = 180) -> None:
    """Gateways are created async — target creation fails until status=READY."""
    import time
    for _ in range(timeout // 5):
        status = ctl.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status == "READY":
            return
        if status in ("FAILED", "DELETING"):
            sys.exit(f"gateway entered {status}")
        print(f"  … gateway {status}, waiting")
        time.sleep(5)
    sys.exit(f"gateway not READY after {timeout}s")


def apply() -> None:
    from botocore.exceptions import ClientError
    api_key = os.environ.get("MERIDIAN_API_KEY") or sys.exit("Set MERIDIAN_API_KEY")
    iam, ctl = _clients()
    # re-running apply resumes from gateway_state.json instead of duplicating
    state: dict = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    try:
        # 1. the key goes into the vault, out of your env files
        if "provider_arn" not in state:
            try:
                state["provider_arn"] = ctl.create_api_key_credential_provider(
                    name=PROVIDER_NAME, apiKey=api_key)["credentialProviderArn"]
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConflictException":
                    raise
                state["provider_arn"] = ctl.get_api_key_credential_provider(
                    name=PROVIDER_NAME)["credentialProviderArn"]
        provider_arn = state["provider_arn"]
        print(f"✓ credential provider {provider_arn}")

        # 2. execution role for the gateway (idempotent)
        try:
            role_arn = iam.create_role(
                RoleName=ROLE_NAME,
                AssumeRolePolicyDocument=json.dumps(TRUST_POLICY))["Role"]["Arn"]
            iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="gateway-least-priv",
                                PolicyDocument=json.dumps(ROLE_POLICY))
            print(f"✓ role {role_arn}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
            print(f"✓ role exists {role_arn}")

        # 3. the managed door
        if "gateway_id" not in state:
            gw = ctl.create_gateway(
                name=GATEWAY_NAME, roleArn=role_arn, protocolType="MCP",
                authorizerType="AWS_IAM",
                description="Managed MCP door over the Meridian /api/v1 (W16 lab)")
            state.update(gateway_id=gw["gatewayId"], gateway_url=gw.get("gatewayUrl"))
        print(f"✓ gateway {state['gateway_id']} → {state.get('gateway_url')}")
        _wait_ready(ctl, state["gateway_id"])

        # 4. the spec becomes the toolset
        tgt = ctl.create_gateway_target(
            gatewayIdentifier=state["gateway_id"], name=TARGET_NAME,
            targetConfiguration={"mcp": {"openApiSchema": {"inlinePayload": OPENAPI}}},
            credentialProviderConfigurations=[{
                "credentialProviderType": "API_KEY",
                "credentialProvider": {"apiKeyCredentialProvider": {
                    "providerArn": provider_arn,
                    "credentialLocation": "HEADER",
                    "credentialParameterName": "Authorization",
                    "credentialPrefix": "Bearer ",
                }}}])
        state["target_id"] = tgt["targetId"]
        print(f"✓ target {tgt['targetId']} (4 tools from openapi.json)")
        print("\nDone. Next: python3 gateway_setup.py invoke")
    except ClientError as e:
        if "AccessDenied" in str(e):
            _denied(e)
        raise
    finally:
        if state:
            STATE_FILE.write_text(json.dumps(state, indent=2))


def _sigv4_post(url: str, body: dict) -> dict:
    """One SigV4-signed MCP call — this is what 'authorizerType: AWS_IAM' means."""
    import urllib.request
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    creds = boto3.Session().get_credentials()
    req = AWSRequest(method="POST", url=url, data=json.dumps(body),
                     headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    body_bytes = req.body if isinstance(req.body, bytes) else req.body.encode()
    http_req = urllib.request.Request(url, data=body_bytes,
                                      headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(http_req, timeout=30) as r:
        return json.loads(r.read())


def invoke() -> None:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else sys.exit(
        "No gateway_state.json — run apply first")
    url = state["gateway_url"]
    tools = _sigv4_post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    print("tools/list →", names)
    out = _sigv4_post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": f"{TARGET_NAME}___get_dispute",
                                       "arguments": {"id": "29"}}})
    print("get_dispute 29 →", json.dumps(out.get("result", out))[:400])


def destroy() -> None:
    from botocore.exceptions import ClientError
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    iam, ctl = _clients()
    def _delete_target_and_wait():
        import time
        ctl.delete_gateway_target(
            gatewayIdentifier=state["gateway_id"], targetId=state["target_id"])
        # target deletion is async; gateway delete fails while any target remains
        for _ in range(24):
            if not ctl.list_gateway_targets(
                    gatewayIdentifier=state["gateway_id"]).get("items"):
                return
            time.sleep(5)

    for what, fn in [
        ("target", _delete_target_and_wait),
        ("gateway", lambda: ctl.delete_gateway(gatewayIdentifier=state["gateway_id"])),
        ("credential provider", lambda: ctl.delete_api_key_credential_provider(
            name=PROVIDER_NAME)),
        ("role policy", lambda: iam.delete_role_policy(
            RoleName=ROLE_NAME, PolicyName="gateway-least-priv")),
        ("role", lambda: iam.delete_role(RoleName=ROLE_NAME)),
    ]:
        try:
            fn()
            print(f"✓ deleted {what}")
        except (ClientError, KeyError) as e:
            print(f"  (skip {what}: {e})")
    STATE_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    {"plan": plan, "apply": apply, "invoke": invoke, "destroy": destroy}[
        sys.argv[1] if len(sys.argv) > 1 else "plan"]()
