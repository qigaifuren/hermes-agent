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
from enterprise_core.dispatch import dispatch_task
from enterprise_core.writeback import write_to_my_resource
from enterprise_core.people import resolve_userid_by_name
from enterprise_core.repositories import EnterpriseRepository
from enterprise_core.resources import recommended_fields_for_table
from enterprise_core.tools import (
    add_smartsheet_records,
    confirm_smartsheet_creation,
    create_wecom_doc,
    create_wecom_smartpage,
    create_smartsheet_from_names,
    delete_smartsheet_fields,
    delete_smartsheet_records,
    doc_batch_update,
    doc_insert_image,
    doc_insert_table,
    doc_update_text_property,
    online_sheet_add_sheet,
    online_sheet_delete_sheet,
    online_sheet_get_range,
    online_sheet_get_schema,
    online_sheet_update_range,
    propose_smartsheet,
    smartsheet_get_schema,
    update_doc_content,
    update_smartsheet_fields,
    update_smartsheet_records,
    upload_doc_image,
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
    required = ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    if not all(os.environ.get(key, "").strip() for key in required):
        return False
    corp_id = os.environ.get("WECOM_CORP_ID") or os.environ.get("CorpID")
    agent_id = os.environ.get("WECOM_AGENT_ID") or os.environ.get("AgentId")
    app_secret = os.environ.get("WECOM_APP_SECRET") or os.environ.get("Secret")
    return all(str(value or "").strip() for value in (corp_id, agent_id, app_secret))


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
    field_names = args.get("field_names") or []
    if not isinstance(field_names, list):
        return _json_result({"error": "field_names must be a list"})
    if not requester_userid or not conversation_id or not table_name:
        return _json_result({"error": "requester_userid, conversation_id and table_name are required"})
    result = propose_smartsheet(
        requester_userid=requester_userid,
        conversation_id=conversation_id,
        table_name=table_name,
        permission_names=[str(name) for name in permission_names],
        repo=_repo(),
        field_names=[str(name) for name in field_names] or None,
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
    raw_field_names = args.get("field_names") or []
    if not isinstance(raw_field_names, list):
        return _json_result({"error": "field_names must be a list"})
    field_names = [str(name) for name in raw_field_names] or None
    # 给出真实列名即视为需要建字段，无需用户再显式 create_fields=true。
    create_fields = bool(args.get("create_fields", False)) or bool(field_names)
    proposal_id = str(args.get("proposal_id") or "").strip()
    if proposal_id:
        result = confirm_smartsheet_creation(
            proposal_id,
            repo=repo,
            wecom_client=client,
            send_to_userids=[str(userid) for userid in args.get("send_to_userids") or []],
            create_fields=create_fields,
            field_names=field_names,
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
            field_names=field_names,
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


def _resource_json(result: dict[str, Any]) -> str:
    resource = result["resource"]
    return _json_result(
        {
            "status": result["status"],
            "resource_id": resource.id,
            "resource_type": resource.resource_type,
            "docid": resource.docid,
            "url": resource.url,
            "reply_text": result["reply_text"],
        }
    )


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [str(item) for item in value]


def _dict_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} items must be objects")
    return [dict(item) for item in value]


def _matrix(value: Any, field_name: str) -> list[list[Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    rows: list[list[Any]] = []
    for item in value:
        if not isinstance(item, list):
            raise ValueError(f"{field_name} items must be arrays")
        rows.append(list(item))
    return rows


def _handle_enterprise_create_doc(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = create_wecom_doc(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            title=str(args.get("title") or "").strip(),
            content=str(args.get("content") or ""),
            send_to_names=_string_list(args.get("send_to_names") or [], "send_to_names"),
            repo=_repo(),
            wecom_client=_wecom_client(),
            parentid=str(args.get("parentid") or "").strip(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _resource_json(result)


def _handle_enterprise_dispatch_task(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = dispatch_task(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            task_type=str(args.get("task_type") or "").strip(),
            name=str(args.get("name") or "").strip(),
            target_team=str(args.get("target_team") or "").strip(),
            field_names=_string_list(args.get("field_names") or [], "field_names"),
            content=str(args.get("content") or ""),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    return _resource_json(result)


def _handle_enterprise_write_to_my_resource(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = args.get("records") or []
        if not isinstance(records, list):
            return _json_result({"error": "records must be a list"})
        result = write_to_my_resource(
            userid=str(args.get("requester_userid") or "").strip(),
            name_hint=str(args.get("name_hint") or "").strip(),
            records=[dict(r) for r in records],
            content=str(args.get("content") or ""),
            repo=_repo(),
            wecom_client=_wecom_client(),
            identity_field=(str(args["identity_field"]).strip() if args.get("identity_field") else None),
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    return _json_result(result)


def _handle_enterprise_update_doc_content(args: dict[str, Any], **kwargs: Any) -> str:
    result = update_doc_content(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        url=str(args.get("url") or "").strip(),
        content=str(args.get("content") or ""),
        content_type=int(args.get("content_type") or 1),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_doc_batch_update(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        requests = _dict_list(args.get("requests") or [], "requests")
        result = doc_batch_update(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            docid=str(args.get("docid") or "").strip(),
            url=str(args.get("url") or "").strip(),
            requests=requests,
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _json_result(result)


def _handle_enterprise_upload_doc_image(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = upload_doc_image(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            filename=str(args.get("filename") or "").strip(),
            file_base64=str(args.get("file_base64") or ""),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _json_result(result)


def _handle_enterprise_doc_insert_image(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = doc_insert_image(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            docid=str(args.get("docid") or "").strip(),
            url=str(args.get("url") or "").strip(),
            image_id=str(args.get("image_id") or "").strip(),
            index=int(args.get("index") or 1),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _json_result(result)


def _handle_enterprise_doc_insert_table(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = doc_insert_table(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            docid=str(args.get("docid") or "").strip(),
            url=str(args.get("url") or "").strip(),
            rows=int(args.get("rows") or 1),
            columns=int(args.get("columns") or 1),
            index=int(args.get("index") or 1),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _json_result(result)


def _handle_enterprise_doc_update_text_property(args: dict[str, Any], **kwargs: Any) -> str:
    text_property = args.get("text_property") or {}
    if not isinstance(text_property, dict):
        return _json_result({"error": "text_property must be an object"})
    result = doc_update_text_property(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        url=str(args.get("url") or "").strip(),
        start_index=int(args.get("start_index") or 1),
        end_index=int(args.get("end_index") or 1),
        text_property=dict(text_property),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_doc_get_content(args: dict[str, Any], **kwargs: Any) -> str:
    client = _wecom_client()
    return _json_result(client.get_doc_content(docid=str(args.get("docid") or "").strip(), url=str(args.get("url") or "").strip()))


def _handle_enterprise_create_smartpage(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = create_wecom_smartpage(
            requester_userid=str(args.get("requester_userid") or "").strip(),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            title=str(args.get("title") or "").strip(),
            pages=_dict_list(args.get("pages") or [], "pages"),
            send_to_names=_string_list(args.get("send_to_names") or [], "send_to_names"),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    return _resource_json(result)


def _handle_enterprise_smartsheet_get_schema(args: dict[str, Any], **kwargs: Any) -> str:
    result = smartsheet_get_schema(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_get_schema(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_get_schema(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_get_range(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_get_range(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        range_a1=str(args.get("range") or args.get("range_a1") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_update_range(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        values = _matrix(args.get("values") or [], "values")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = online_sheet_update_range(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        range_a1=str(args.get("range") or args.get("range_a1") or "").strip(),
        values=values,
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_add_sheet(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_add_sheet(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        title=str(args.get("title") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_delete_sheet(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_delete_sheet(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_add_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = _dict_list(args.get("records") or [], "records")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = add_smartsheet_records(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        records=records,
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_update_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = _dict_list(args.get("records") or [], "records")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = update_smartsheet_records(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        records=records,
        key_type=str(args.get("key_type") or "CELL_VALUE_KEY_TYPE_FIELD_TITLE"),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_delete_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        record_ids = _string_list(args.get("record_ids") or [], "record_ids")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = delete_smartsheet_records(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        record_ids=record_ids,
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_update_fields(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        fields = _dict_list(args.get("fields") or [], "fields")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = update_smartsheet_fields(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        fields=fields,
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_delete_fields(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        field_ids = _string_list(args.get("field_ids") or [], "field_ids")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = delete_smartsheet_fields(
        requester_userid=str(args.get("requester_userid") or "").strip(),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        field_ids=field_ids,
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


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
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户数据的真实列名（如 订单ID、日期、客户...）。给出时用它替代按表名推荐的字段；为空则回退启发式推荐。",
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
        "description": "Create a real WeCom smartsheet, persist docid/resource/permissions/audit to Postgres, and optionally send the link. Use this immediately when the user asks to create/generate a table or random test smartsheet; do not only promise that you will create it. Accept either proposal_id or direct table/permission/send names when the user has clearly asked to create now. 当用户贴了带表头的数据（如订单表）时，必须从数据中抽取真实列名传 field_names，建表后再调用 enterprise_smartsheet_get_schema 取 sheet_id、enterprise_smartsheet_add_records 按列名分批写入所有数据行；不要只建空表。",
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
                    "description": "直接创建时可填，需要读写权限的员工姓名列表；没有指定人员时使用空数组。",
                },
                "send_to_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "直接创建时可填，创建后要发送链接的员工姓名列表；没有指定人员时使用空数组，并在回复中返回链接。",
                },
                "send_to_userids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "使用 proposal_id 创建时可填，创建后要发送链接的企业微信 userid 列表。",
                },
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户数据的真实列名（如 订单ID、日期、客户...）。给出时会自动建这些列（FIELD_TYPE_TEXT）并清理 WeCom 默认列；用户贴了带表头的数据时必须传。",
                },
                "create_fields": {
                    "type": "boolean",
                    "description": "是否自动建字段（字段类型统一用 FIELD_TYPE_TEXT）。一般不用显式设；传了 field_names 即视为 true。",
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

registry.register(
    name="enterprise_create_doc",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_create_doc",
        "description": "Create a real WeCom ordinary document, optionally write Markdown content, persist the resource, and send a card. Use when the user asks to create a document, report, meeting note, or Markdown-style rich text document.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "企业微信发起人 userid。"},
                "conversation_id": {"type": "string", "description": "当前企业微信/Hermes 会话 id。"},
                "title": {"type": "string", "description": "文档标题。"},
                "content": {"type": "string", "description": "Markdown 内容。没有内容时可传空字符串。"},
                "send_to_names": {"type": "array", "items": {"type": "string"}, "description": "创建后要发送卡片的员工姓名列表。"},
                "parentid": {"type": "string", "description": "可选父目录 id。"},
            },
            "required": ["requester_userid", "conversation_id", "title", "content"],
        },
    },
    handler=_handle_enterprise_create_doc,
    check_fn=_check_enterprise_core,
    emoji="doc",
)

registry.register(
    name="enterprise_dispatch_task",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_dispatch_task",
        "description": (
            "管理者（gm/supervisor/boss）分发任务：创建带列的智能表或带内容的文档，"
            "自动授权给目标团队全部成员并推送卡片。task_type='smartsheet' 时用 field_names 传列名；"
            "task_type='doc' 时用 content 传 Markdown 内容。target_team 可传 '周婉倪团队' 或 '周婉倪' 或 userid。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "发起分发的管理者 userid"},
                "conversation_id": {"type": "string"},
                "task_type": {"type": "string", "enum": ["smartsheet", "doc"]},
                "name": {"type": "string", "description": "表名或文档标题"},
                "target_team": {"type": "string", "description": "目标团队，如 '周婉倪团队'"},
                "field_names": {"type": "array", "items": {"type": "string"}, "description": "智能表列名（task_type=smartsheet 时）"},
                "content": {"type": "string", "description": "文档 Markdown 内容（task_type=doc 时）"},
            },
            "required": ["requester_userid", "task_type", "name", "target_team"],
        },
    },
    handler=_handle_enterprise_dispatch_task,
    check_fn=_check_enterprise_core,
    emoji="dispatch",
)

registry.register(
    name="enterprise_write_to_my_resource",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_write_to_my_resource",
        "description": (
            "员工把数据写入自己有权限的智能表，或把内容追加到有权限的文档。"
            "只能写有 write 权限的资源；name_hint 用来按表名/文档名定位。"
            "智能表用 records=[{\"values\": {\"列名\": \"值\"}}]（列必须已存在，否则报错）；"
            "文档用 content 传要追加的文本。匹配到多个资源会返回 candidates 让你向用户确认。"
            "可选 identity_field：指定姓名列名，会自动把发起员工姓名填进去。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "发起写入的员工 userid"},
                "name_hint": {"type": "string", "description": "目标表名/文档名关键词"},
                "records": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "智能表记录，每条形如 {\"values\": {\"列名\": \"值\"}}",
                },
                "content": {"type": "string", "description": "文档要追加的文本（写文档时）"},
                "identity_field": {"type": "string", "description": "可选：自动填员工姓名的列名"},
            },
            "required": ["requester_userid", "name_hint"],
        },
    },
    handler=_handle_enterprise_write_to_my_resource,
    check_fn=_check_enterprise_core,
    emoji="write",
)

registry.register(
    name="enterprise_update_doc_content",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_update_doc_content",
        "description": "Overwrite or update a WeCom ordinary document using Markdown content. Use only when the user clearly asks to edit a specific existing document.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "企业微信发起人 userid。"},
                "docid": {"type": "string", "description": "文档真实 docid；与 url 二选一。"},
                "url": {"type": "string", "description": "文档 URL；与 docid 二选一。"},
                "content": {"type": "string", "description": "Markdown 内容。"},
                "content_type": {"type": "integer", "description": "内容类型，1 表示 Markdown。", "default": 1},
            },
            "required": ["requester_userid", "content"],
        },
    },
    handler=_handle_enterprise_update_doc_content,
    check_fn=_check_enterprise_core,
    emoji="edit",
)

registry.register(
    name="enterprise_doc_get_content",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_doc_get_content",
        "description": "Read a WeCom ordinary document by docid or URL before summarizing, editing, or locating insertion positions.",
        "parameters": {
            "type": "object",
            "properties": {
                "docid": {"type": "string", "description": "文档真实 docid；与 url 二选一。"},
                "url": {"type": "string", "description": "文档 URL；与 docid 二选一。"},
            },
            "required": [],
        },
    },
    handler=_handle_enterprise_doc_get_content,
    check_fn=_check_enterprise_core,
    emoji="read",
)

registry.register(
    name="enterprise_doc_batch_update",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_doc_batch_update",
        "description": "Batch edit a WeCom ordinary document using official document/batch_update requests such as insert_text, replace_text, delete_content, insert_image, insert_table, insert_paragraph, insert_page_break, and update_text_property. Use for rich document layout after reading the document when needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "url": {"type": "string"},
                "requests": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["requester_userid", "requests"],
        },
    },
    handler=_handle_enterprise_doc_batch_update,
    check_fn=_check_enterprise_core,
    emoji="edit",
)

registry.register(
    name="enterprise_upload_doc_image",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_upload_doc_image",
        "description": "Upload an image for WeCom document insertion. Use before enterprise_doc_insert_image when the user asks to insert or beautify a document with a picture.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "filename": {"type": "string"},
                "file_base64": {"type": "string", "description": "图片文件 base64 内容。"},
            },
            "required": ["requester_userid", "filename", "file_base64"],
        },
    },
    handler=_handle_enterprise_upload_doc_image,
    check_fn=_check_enterprise_core,
    emoji="image",
)

registry.register(
    name="enterprise_doc_insert_image",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_doc_insert_image",
        "description": "Insert an uploaded image into a WeCom ordinary document using image_id and document index. Upload first with enterprise_upload_doc_image when only a local image is available.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "url": {"type": "string"},
                "image_id": {"type": "string"},
                "index": {"type": "integer"},
            },
            "required": ["requester_userid", "image_id", "index"],
        },
    },
    handler=_handle_enterprise_doc_insert_image,
    check_fn=_check_enterprise_core,
    emoji="image",
)

registry.register(
    name="enterprise_doc_insert_table",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_doc_insert_table",
        "description": "Insert a table block into a WeCom ordinary document at a specific document index.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "url": {"type": "string"},
                "rows": {"type": "integer"},
                "columns": {"type": "integer"},
                "index": {"type": "integer"},
            },
            "required": ["requester_userid", "rows", "columns", "index"],
        },
    },
    handler=_handle_enterprise_doc_insert_table,
    check_fn=_check_enterprise_core,
    emoji="table",
)

registry.register(
    name="enterprise_doc_update_text_property",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_doc_update_text_property",
        "description": "Update text style in a WeCom ordinary document, such as bold, italic, underline, font size, or color, over a known index range.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "url": {"type": "string"},
                "start_index": {"type": "integer"},
                "end_index": {"type": "integer"},
                "text_property": {"type": "object"},
            },
            "required": ["requester_userid", "start_index", "end_index", "text_property"],
        },
    },
    handler=_handle_enterprise_doc_update_text_property,
    check_fn=_check_enterprise_core,
    emoji="style",
)

registry.register(
    name="enterprise_create_smartpage",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_create_smartpage",
        "description": "Create a real WeCom smart document/smartpage with one or more pages, persist the resource, and send a card. Use for knowledge bases, multi-page plans, and smart documents.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "企业微信发起人 userid。"},
                "conversation_id": {"type": "string", "description": "当前企业微信/Hermes 会话 id。"},
                "title": {"type": "string", "description": "智能文档标题。"},
                "pages": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "页面列表：page_title 必填，page_content 可选，content_type 0=纯文本 1=Markdown。",
                },
                "send_to_names": {"type": "array", "items": {"type": "string"}, "description": "创建后要发送卡片的员工姓名列表。"},
            },
            "required": ["requester_userid", "conversation_id", "title", "pages"],
        },
    },
    handler=_handle_enterprise_create_smartpage,
    check_fn=_check_enterprise_core,
    emoji="smartpage",
)

registry.register(
    name="enterprise_smartsheet_get_schema",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_smartsheet_get_schema",
        "description": "Load WeCom smartsheet sheets and fields before editing records. Use this before add/update/delete record operations when field titles or sheet_id are uncertain.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
            },
            "required": ["requester_userid", "docid", "sheet_id"],
        },
    },
    handler=_handle_enterprise_smartsheet_get_schema,
    check_fn=_check_enterprise_core,
    emoji="schema",
)

registry.register(
    name="enterprise_online_sheet_get_schema",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_online_sheet_get_schema",
        "description": "Load ordinary WeCom online spreadsheet /sheet/ properties. Use for existing /sheet/ links before reading or editing ranges; do not use for /smartsheet/ smart tables.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
            },
            "required": ["requester_userid", "docid"],
        },
    },
    handler=_handle_enterprise_online_sheet_get_schema,
    check_fn=_check_enterprise_core,
    emoji="sheet",
)

registry.register(
    name="enterprise_online_sheet_get_range",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_online_sheet_get_range",
        "description": "Read a range from an existing ordinary WeCom online spreadsheet /sheet/. Use after enterprise_online_sheet_get_schema identifies sheet_id and range.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
                "range": {"type": "string", "description": "A1 range, e.g. A1:D20."},
            },
            "required": ["requester_userid", "docid", "sheet_id", "range"],
        },
    },
    handler=_handle_enterprise_online_sheet_get_range,
    check_fn=_check_enterprise_core,
    emoji="read",
)

registry.register(
    name="enterprise_online_sheet_update_range",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_online_sheet_update_range",
        "description": "Update cells in an existing ordinary WeCom online spreadsheet /sheet/. Use only for explicit edits to known ranges; this is not the smart table /smartsheet/ API.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array", "items": {"type": "array"}},
            },
            "required": ["requester_userid", "docid", "sheet_id", "range", "values"],
        },
    },
    handler=_handle_enterprise_online_sheet_update_range,
    check_fn=_check_enterprise_core,
    emoji="edit",
)

registry.register(
    name="enterprise_online_sheet_add_sheet",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_online_sheet_add_sheet",
        "description": "Add a child sheet tab to an existing ordinary WeCom online spreadsheet /sheet/. This does not create a new /sheet/ document.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["requester_userid", "docid", "title"],
        },
    },
    handler=_handle_enterprise_online_sheet_add_sheet,
    check_fn=_check_enterprise_core,
    emoji="sheet",
)

registry.register(
    name="enterprise_online_sheet_delete_sheet",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_online_sheet_delete_sheet",
        "description": "Delete a child sheet tab from an existing ordinary WeCom online spreadsheet /sheet/. Use only after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
            },
            "required": ["requester_userid", "docid", "sheet_id"],
        },
    },
    handler=_handle_enterprise_online_sheet_delete_sheet,
    check_fn=_check_enterprise_core,
    emoji="delete",
)

for _name, _description, _handler, _records_status in [
    (
        "enterprise_smartsheet_add_records",
        'Add rows to a WeCom smartsheet. Each record MUST have a "values" wrapper: [{"values": {"列名1": "值1", "列名2": "值2"}}]. Do NOT send flat dicts like [{"列名": "值"}] — those create empty rows. Field keys must be field titles, not field IDs.',
        _handle_enterprise_smartsheet_add_records,
        "records",
    ),
    (
        "enterprise_smartsheet_update_records",
        "Update rows in a WeCom smartsheet. Records must include record_id and values, and values should use field titles as keys.",
        _handle_enterprise_smartsheet_update_records,
        "records",
    ),
]:
    registry.register(
        name=_name,
        toolset=ENTERPRISE_TOOLSET,
        schema={
            "name": _name,
            "description": _description,
            "parameters": {
                "type": "object",
                "properties": {
                    "requester_userid": {"type": "string"},
                    "docid": {"type": "string"},
                    "sheet_id": {"type": "string"},
                    _records_status: {"type": "array", "items": {"type": "object"}},
                    "key_type": {"type": "string", "description": "更新记录时可选，默认 CELL_VALUE_KEY_TYPE_FIELD_TITLE。"},
                },
                "required": ["requester_userid", "docid", "sheet_id", _records_status],
            },
        },
        handler=_handler,
        check_fn=_check_enterprise_core,
        emoji="rows",
    )

registry.register(
    name="enterprise_smartsheet_delete_records",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_smartsheet_delete_records",
        "description": "Delete rows from a WeCom smartsheet. Use only for explicitly requested deletion of known record IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
                "record_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["requester_userid", "docid", "sheet_id", "record_ids"],
        },
    },
    handler=_handle_enterprise_smartsheet_delete_records,
    check_fn=_check_enterprise_core,
    emoji="delete",
)

registry.register(
    name="enterprise_smartsheet_update_fields",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_smartsheet_update_fields",
        "description": "Rename or update WeCom smartsheet fields. Use after reading field IDs with enterprise_smartsheet_get_schema.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["requester_userid", "docid", "sheet_id", "fields"],
        },
    },
    handler=_handle_enterprise_smartsheet_update_fields,
    check_fn=_check_enterprise_core,
    emoji="columns",
)

registry.register(
    name="enterprise_smartsheet_delete_fields",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_smartsheet_delete_fields",
        "description": "Delete WeCom smartsheet fields. Use only for explicitly requested deletion of known field IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string"},
                "sheet_id": {"type": "string"},
                "field_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["requester_userid", "docid", "sheet_id", "field_ids"],
        },
    },
    handler=_handle_enterprise_smartsheet_delete_fields,
    check_fn=_check_enterprise_core,
    emoji="delete",
)
