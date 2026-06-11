from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    explicit = os.environ.get("ENTERPRISE_CORE_ROOT", "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([Path.cwd(), Path(__file__).resolve().parents[2]])
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "enterprise_core").is_dir():
            return resolved
    return Path(__file__).resolve().parents[2]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise_core.db import connect
from enterprise_core.people import resolve_userid_by_name
from enterprise_core.repositories import EnterpriseRepository
from enterprise_core.resources import recommended_fields_for_table
from enterprise_core.tools import (
    confirm_smartsheet_creation,
    create_smartsheet_from_names,
    propose_smartsheet,
)
from enterprise_core.wecom_client import WeComClient, config_from_env, load_dotenv
from tools.registry import registry

ENTERPRISE_TOOLSET = "enterprise-core"


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=lambda obj: getattr(obj, "__dict__", str(obj)))


def _check_enterprise_core() -> bool:
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(str(env_path))
    required = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_APP_SECRET",
    ]
    return all(os.environ.get(key, "").strip() for key in required)


def _repo() -> EnterpriseRepository:
    load_dotenv(str(ROOT / ".env"))
    conn = connect()
    return EnterpriseRepository(conn)


def _wecom_client() -> WeComClient:
    load_dotenv(str(ROOT / ".env"))
    return WeComClient(config_from_env())


def _handle_enterprise_resolve_user(args: dict[str, Any], **kwargs: Any) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return _json_result({"error": "name is required"})
    repo = _repo()
    userid = resolve_userid_by_name(name, repo)
    return _json_result({"name": name, "userid": userid})


def _handle_enterprise_recommend_smartsheet_fields(args: dict[str, Any], **kwargs: Any) -> str:
    table_name = str(args.get("table_name") or "").strip()
    if not table_name:
        return _json_result({"error": "table_name is required"})
    field_names = recommended_fields_for_table(table_name)
    return _json_result(
        {
            "table_name": table_name,
            "field_names": field_names,
            "reply_text": f"我建议“{table_name}”先包含这些字段：{'、'.join(field_names)}。如果你确认，我就创建企业微信智能表格并登记权限。",
        }
    )


def _handle_enterprise_propose_smartsheet(args: dict[str, Any], **kwargs: Any) -> str:
    requester_userid = str(args.get("requester_userid") or "").strip()
    conversation_id = str(args.get("conversation_id") or kwargs.get("task_id") or "").strip()
    table_name = str(args.get("table_name") or "").strip()
    permission_names = args.get("permission_names") or []
    if not isinstance(permission_names, list):
        return _json_result({"error": "permission_names must be a list"})
    if not requester_userid or not conversation_id or not table_name:
        return _json_result({"error": "requester_userid, conversation_id and table_name are required"})
    result = propose_smartsheet(
        requester_userid=requester_userid,
        conversation_id=conversation_id,
        table_name=table_name,
        permission_names=[str(name) for name in permission_names],
        repo=_repo(),
    )
    return _json_result(
        {
            "status": result["status"],
            "proposal_id": result["proposal_id"],
            "reply_text": result["reply_text"],
            "field_names": result["field_names"],
            "permission_userids": result["permission_userids"],
        }
    )


def _handle_enterprise_create_smartsheet(args: dict[str, Any], **kwargs: Any) -> str:
    repo = _repo()
    client = _wecom_client()
    create_fields = bool(args.get("create_fields", False))
    proposal_id = str(args.get("proposal_id") or "").strip()
    if proposal_id:
        result = confirm_smartsheet_creation(
            proposal_id,
            repo=repo,
            wecom_client=client,
            send_to_userids=[str(userid) for userid in args.get("send_to_userids") or []],
            create_fields=create_fields,
        )
    else:
        permission_names = args.get("permission_names") or []
        send_to_names = args.get("send_to_names") or []
        if not isinstance(permission_names, list) or not isinstance(send_to_names, list):
            return _json_result({"error": "permission_names and send_to_names must be lists"})
        result = create_smartsheet_from_names(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            table_name=str(args.get("table_name") or "").strip(),
            permission_names=[str(name) for name in permission_names],
            send_to_names=[str(name) for name in send_to_names],
            repo=repo,
            wecom_client=client,
            create_fields=create_fields,
        )
    resource = result["resource"]
    return _json_result(
        {
            "status": result["status"],
            "resource_id": resource.id,
            "docid": resource.docid,
            "url": resource.url,
            "reply_text": result["reply_text"],
        }
    )


registry.register(
    name="enterprise_resolve_user",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_resolve_user",
        "description": "Resolve a Chinese employee name to a WeCom userid using the enterprise Postgres user registry. Use before granting permissions or sending enterprise messages.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "员工姓名，例如 章阳群 或 章恩佐。"}
            },
            "required": ["name"],
        },
    },
    handler=_handle_enterprise_resolve_user,
    check_fn=_check_enterprise_core,
    emoji="office",
)

registry.register(
    name="enterprise_recommend_smartsheet_fields",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_recommend_smartsheet_fields",
        "description": "Recommend Chinese fields for a WeCom smartsheet before creation. Use when the user asks to create a table but fields are not fully specified.",
        "parameters": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "要创建的表名，例如 销售跟踪表。"}
            },
            "required": ["table_name"],
        },
    },
    handler=_handle_enterprise_recommend_smartsheet_fields,
    check_fn=_check_enterprise_core,
    emoji="table",
)

registry.register(
    name="enterprise_propose_smartsheet",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_propose_smartsheet",
        "description": "Create a pending enterprise smartsheet proposal and return a proposal_id. Use when confirmation is needed before creating the real WeCom smartsheet.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "企业微信发起人 userid，来自 Conversation info 的 sender_id。"},
                "conversation_id": {"type": "string", "description": "当前企业微信/Hermes 会话 id。"},
                "table_name": {"type": "string", "description": "要创建的表名。"},
                "permission_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要读写权限的员工姓名列表。",
                },
            },
            "required": ["requester_userid", "conversation_id", "table_name", "permission_names"],
        },
    },
    handler=_handle_enterprise_propose_smartsheet,
    check_fn=_check_enterprise_core,
    emoji="proposal",
)

registry.register(
    name="enterprise_create_smartsheet",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_create_smartsheet",
        "description": "Create a WeCom smartsheet, persist docid/resource/permissions/audit to Postgres, and optionally send the link. Accept either proposal_id or direct table/permission/send names when the user has clearly asked to create now.",
        "parameters": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "enterprise_propose_smartsheet returned proposal_id。"},
                "requester_userid": {"type": "string", "description": "直接创建时必填，企业微信发起人 userid。"},
                "conversation_id": {"type": "string", "description": "直接创建时必填，当前会话 id。"},
                "table_name": {"type": "string", "description": "直接创建时必填，表名。"},
                "permission_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "直接创建时必填，需要读写权限的员工姓名列表。",
                },
                "send_to_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "直接创建时可填，创建后要发送链接的员工姓名列表。",
                },
                "send_to_userids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "使用 proposal_id 创建时可填，创建后要发送链接的企业微信 userid 列表。",
                },
                "create_fields": {
                    "type": "boolean",
                    "description": "是否调用智能表格字段 API 自动建字段。默认 false，因为字段类型枚举仍需单独验证。",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    handler=_handle_enterprise_create_smartsheet,
    check_fn=_check_enterprise_core,
    emoji="smartsheet",
)
