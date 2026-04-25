#!/usr/bin/env python3
"""
Register/unregister this container's task IP via Lambda.
Only runs when deployed in ECS (checks for ECS_CONTAINER_METADATA_URI env var).

Usage:
    register_task.py register   # Register (default)
    register_task.py unregister # Deregister (cleanup on shutdown)

Required env vars (when in ECS):
    TENANT_ID    - Slack team/tenant ID
    USER_ID      - Bot user ID (U prefix)
    AWS_REGION   - AWS region for Lambda invocation
    TASK_IP      - Private IP of this task (auto-detected if not set)
    TASK_PORT    - Port the webhook listens on (default: 8645)
"""

import os
import sys
import socket
import json
import base64

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Install with: pip install boto3", file=sys.stderr)
    sys.exit(1)

REGION = os.getenv("AWS_REGION", "us-east-2")
TENANT_ID = os.getenv("TENANT_ID", "").strip()
USER_ID = os.getenv("USER_ID", "").strip()
ORG_ID = os.getenv("ORG_ID", "").strip()
TASK_IP = os.getenv("TASK_IP", "").strip()
TASK_PORT = os.getenv("TASK_PORT", "8645")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
LAMBDA_FUNCTION = os.getenv("LAMBDA_REGISTER_FUNCTION", "hermes-register")
WEBHOOK_PATH = "/webhooks/slack"


def is_ecs():
    """Detect if running inside an ECS task."""
    return bool(os.getenv("ECS_CONTAINER_METADATA_URI") or os.getenv("ECS_CONTAINER_METADATA_URI_V4"))


def get_task_ip():
    """Get the container's primary private IP.
    For ECS EC2 bridge mode: tries EC2 metadata first (gives EC2 host IP),
    then falls back to Docker hostname resolution.
    """
    # Try EC2 instance metadata first - this gives the EC2 host's private IP
    # which is reachable from Lambda in the VPC
    try:
        import urllib.request
        req = urllib.request.Request("http://169.254.169.254/latest/meta-data/local-ipv4")
        with urllib.request.urlopen(req, timeout=2) as resp:
            ec2_ip = resp.read().decode("utf-8")
            if ec2_ip and not ec2_ip.startswith("127."):
                print(f"[register] Using EC2 instance IP from metadata: {ec2_ip}")
                return ec2_ip
    except Exception as e:
        print(f"[register] EC2 metadata lookup failed ({e}), falling back to hostname resolution")

    # Fall back to Docker bridge IP
    hostname = socket.gethostname()
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
        return "0.0.0.0"


def get_task_metadata():
    """Get task ARN and other task metadata from ECS metadata endpoint.
    The ECS agent provides container-level metadata at a link-local address.
    """
    # Try ECS container metadata endpoint (v4)
    ecs_meta_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4") or os.getenv("ECS_CONTAINER_METADATA_URI")
    if not ecs_meta_uri:
        return {}

    try:
        import urllib.request
        req = urllib.request.Request(f"{ecs_meta_uri}/task")
        with urllib.request.urlopen(req, timeout=5) as resp:
            task_meta = json.loads(resp.read().decode("utf-8"))
            # Extract task ARN from the metadata
            task_arn = task_meta.get("TaskARN") or task_meta.get("taskArn")
            cluster = task_meta.get("Cluster")
            print(f"[register] ECS task metadata: ARN={task_arn}, Cluster={cluster}")
            return {"task_arn": task_arn, "cluster": cluster}
    except Exception as e:
        print(f"[register] ECS metadata lookup failed ({e})")
    return {}


def get_lambda_client():
    return boto3.client("lambda", region_name=REGION)


def register():
    ip = TASK_IP or get_task_ip()
    task_meta = get_task_metadata()
    task_arn = task_meta.get("task_arn")

    webhook_url = f"http://{ip}:{TASK_PORT}{WEBHOOK_PATH}"

    payload = {
        "action": "register",
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "channel_id": CHANNEL_ID,
        "hermes_webhook_url": webhook_url,
        "task_ip": ip,
        "task_port": int(TASK_PORT),
        "task_arn": task_arn,
    }

    try:
        lambda_client = get_lambda_client()
        response = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="RequestResponse",  # sync to get secret
            Payload=json.dumps(payload),
        )
        print(f"[register] Lambda invoked: {TENANT_ID}/{USER_ID} -> {webhook_url} (status: {response.get('StatusCode')})")

        # Parse response payload to get secret
        response_payload = response.get('Payload')
        if response_payload:
            import botocore.response
            response_text = response_payload.read().decode('utf-8')
            result = json.loads(response_text)
            body = json.loads(result.get('body', '{}'))
            secret = body.get('secret')
            if secret:
                secret_path = "/opt/data/hermes_webhook_secret.txt"
                try:
                    with open(secret_path, 'w') as f:
                        f.write(secret)
                    print(f"[register] Secret written to {secret_path}")
                except Exception as e:
                    print(f"[register] Failed to write secret: {e}")

        return {"task_ip": ip, "task_port": int(TASK_PORT), "hermes_webhook_url": webhook_url}
    except Exception as e:
        print(f"[register] Lambda invoke failed (non-fatal): {e}")
        return {"task_ip": ip, "task_port": int(TASK_PORT)}


def unregister():
    payload = {
        "action": "unregister",
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }

    try:
        lambda_client = get_lambda_client()
        response = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )
        print(f"[unregister] Lambda invoked: {TENANT_ID}/{USER_ID} (status: {response.get('StatusCode')})")
    except Exception as e:
        print(f"[unregister] Lambda invoke failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    if not is_ecs():
        print("[register] Not running in ECS — skipping registration")
        sys.exit(0)

    if not TENANT_ID or not USER_ID:
        print("[register] TENANT_ID or USER_ID not set — skipping registration")
        sys.exit(0)

    action = sys.argv[1] if len(sys.argv) > 1 else "register"

    if action == "register":
        register()
    elif action == "unregister":
        unregister()
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(1)
