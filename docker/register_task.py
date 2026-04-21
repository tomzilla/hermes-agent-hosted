#!/usr/bin/env python3
"""
Register/unregister this container's task IP in DynamoDB for Lambda routing.
Only runs when deployed in ECS (checks for ECS_CONTAINER_METADATA_URI env var).

Usage:
    register_task.py register   # Register (default)
    register_task.py unregister # Deregister (cleanup on shutdown)

Required env vars (when in ECS):
    TENANT_ID    - Slack team/tenant ID
    USER_ID      - Bot user ID (U prefix)
    AWS_REGION   - AWS region for DynamoDB
    DYNAMO_TABLE - DynamoDB table name (default: hermes-task-registry)
    TASK_IP      - Private IP of this task (auto-detected if not set)
    TASK_PORT    - Port the webhook listens on (default: 8645)
"""

import os
import sys
import socket
from datetime import datetime

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Install with: pip install boto3", file=sys.stderr)
    sys.exit(1)

TABLE_NAME = os.getenv("DYNAMO_TABLE", "hermes-task-registry")
REGION = os.getenv("AWS_REGION", "us-east-1")
TENANT_ID = os.getenv("TENANT_ID", "").strip()
USER_ID = os.getenv("USER_ID", "").strip()
TASK_IP = os.getenv("TASK_IP", "").strip()
TASK_PORT = os.getenv("TASK_PORT", "8645")


def is_ecs():
    """Detect if running inside an ECS task."""
    return bool(os.getenv("ECS_CONTAINER_METADATA_URI") or os.getenv("ECS_CONTAINER_METADATA_URI_V4"))


def get_task_ip():
    """Get the container's primary private IP."""
    hostname = socket.gethostname()
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
        return "0.0.0.0"


def get_dynamo():
    return boto3.resource("dynamodb", region_name=REGION)


def register():
    ip = TASK_IP or get_task_ip()
    dynamo = get_dynamo()
    table = dynamo.Table(TABLE_NAME)

    item = {
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "task_ip": ip,
        "task_port": int(TASK_PORT),
        "registered_at": datetime.utcnow().isoformat(),
        "last_heartbeat": datetime.utcnow().isoformat(),
        "status": "running",
    }

    table.put_item(Item=item)
    print(f"[register] Registered: {TENANT_ID}/{USER_ID} -> {ip}:{TASK_PORT}")
    return item


def unregister():
    dynamo = get_dynamo()
    table = dynamo.Table(TABLE_NAME)

    try:
        table.update_item(
            Key={"tenant_id": TENANT_ID, "user_id": USER_ID},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "stopping"},
        )
        print(f"[unregister] Marked stopping: {TENANT_ID}/{USER_ID}")
    except Exception as e:
        print(f"[unregister] Failed to mark stopping: {e}", file=sys.stderr)


if __name__ == "__main__":
    if not is_ecs():
        print("[register] Not running in ECS — skipping DynamoDB registration")
        sys.exit(0)

    if not TENANT_ID or not USER_ID:
        print("ERROR: TENANT_ID and USER_ID env vars required", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1] if len(sys.argv) > 1 else "register"

    if action == "register":
        register()
    elif action == "unregister":
        unregister()
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(1)
