from __future__ import annotations

import hashlib
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
from enterprise_core.dispatch import dispatch_task, grant_resource_permission
from enterprise_core.groups import ensure_appchat
from enterprise_core.sharing import push_existing_resource_to_group, rename_resource
from enterprise_core.writeback import write_to_my_resource
from enterprise_core.people import resolve_members_with_candidates, resolve_userid_by_name
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
    smartsheet_get_records,
    smartsheet_get_schema,
    update_doc_content,
    update_smartsheet_fields,
    update_smartsheet_records,
    upload_doc_image,
)
from enterprise_core.models import ScheduledJob
from enterprise_core.wecom_client import WeComClient, config_from_env, load_dotenv
from tools.registry import registry

ENTERPRISE_TOOLSET = "enterprise-core"


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=lambda obj: getattr(obj, "__dict__", str(obj)))


def _trusted_requester(args: dict[str, Any]) -> str:
    """绑定认证身份：优先用网关经可信头 X-Hermes-Actor-Id 注入的会话身份 HERMES_SESSION_USER_ID，
    忽略 LLM 传的 requester_userid（防止 agent 把自己冒充成 muzhi/他人来越权）。无可信身份时回退入参。"""
    actor = ""
    try:
        from gateway.session_context import get_session_env

        actor = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    except Exception:
        actor = ""
    return actor or str(args.get("requester_userid") or "").strip()


def _session_id(args: dict[str, Any], **kwargs: Any) -> str:
    """本会话稳定标识（跨轮一致），用作"最近资源"记忆的键。

    优先用网关注入的 HERMES_SESSION_ID（与 _trusted_requester 同源、task-local、跨轮稳定），
    回退 conversation_id / task_id。决不依赖 LLM 传的 conversation_id（运行时并未注入）。
    """
    sid = ""
    try:
        from gateway.session_context import get_session_env

        sid = (get_session_env("HERMES_SESSION_ID", "") or "").strip()
    except Exception:
        sid = ""
    return sid or str(args.get("conversation_id") or kwargs.get("task_id") or "").strip()


def _remember(session_id: str, requester: str, result: dict[str, Any], action: str = "touched") -> None:
    """创建/发送类工具成功后，把资源记进会话"最近资源"记忆（best-effort）。"""
    if not session_id or not isinstance(result, dict):
        return
    try:
        from enterprise_core.session_memory import record_resource_touch, record_touch_for_resource

        res = result.get("resource")
        if res is not None and getattr(res, "id", ""):
            record_touch_for_resource(_repo(), session_id, requester, res, action=action)
            return
        resource_id = str(result.get("resource_id") or "").strip()
        if not resource_id:
            return
        record_resource_touch(
            _repo(),
            session_id,
            requester,
            resource_id=resource_id,
            docid=str(result.get("docid") or "").strip(),
            url=str(result.get("url") or "").strip(),
            action=action,
        )
    except Exception:
        pass


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
    requester_userid = _trusted_requester(args)
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
            requester_userid=_trusted_requester(args),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            table_name=str(args.get("table_name") or "").strip(),
            permission_names=[str(name) for name in permission_names],
            send_to_names=[str(name) for name in send_to_names],
            repo=repo,
            wecom_client=client,
            create_fields=create_fields,
            field_names=field_names,
            force_create=bool(args.get("force_create", False)),
        )
    resource = result["resource"]
    _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="created")
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
    payload = {
        "status": result["status"],
        "resource_id": resource.id,
        "resource_type": resource.resource_type,
        "docid": resource.docid,
        "url": resource.url,
        "reply_text": result["reply_text"],
    }
    if "group_push" in result:
        # 群推结果（group_key / "skipped:..." / "failed:..."）如实透出，不静默吞掉
        payload["group_push"] = result["group_push"]
    return _json_result(payload)


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
            requester_userid=_trusted_requester(args),
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
    _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="created")
    return _resource_json(result)


def _handle_enterprise_dispatch_task(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        result = dispatch_task(
            requester_userid=_trusted_requester(args),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            task_type=str(args.get("task_type") or "").strip(),
            name=str(args.get("name") or "").strip(),
            target_team=str(args.get("target_team") or "").strip(),
            field_names=_string_list(args.get("field_names") or [], "field_names"),
            content=str(args.get("content") or ""),
            repo=_repo(),
            wecom_client=_wecom_client(),
            push_to_group=bool(args.get("push_to_group", False)),
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="dispatched")
    return _resource_json(result)


def _handle_enterprise_write_to_my_resource(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = args.get("records") or []
        if not isinstance(records, list):
            return _json_result({"error": "records must be a list"})
        result = write_to_my_resource(
            userid=_trusted_requester(args),
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


def _weekly_report_job_id(requester_userid: str, resource_scope: dict[str, Any]) -> str:
    """确定性 job_id：同一管理者 + 同一范围始终得到同一 id，
    这样不带 job_id 的重复配置会 upsert 更新同一个 job，而非创建重复报告。"""
    seed = f"{requester_userid}|{resource_scope}"
    return "weekly_report_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _handle_enterprise_schedule_report(args: dict[str, Any], **kwargs: Any) -> str:
    requester = _trusted_requester(args)
    if not requester:
        return _json_result({"error": "requester_userid is required"})
    schedule = str(args.get("schedule") or "0 14 * * 5").strip()
    scope = args.get("resource_scope") or {}
    recipients = args.get("recipients") or []
    if not isinstance(scope, dict) or not isinstance(recipients, list):
        return _json_result({"error": "resource_scope must be object and recipients must be a list"})
    enabled = bool(args.get("enabled", True))
    to_group = str(args.get("to_group") or "").strip()
    recipient_policy: dict[str, Any] = {"to": [str(r) for r in recipients]}
    if to_group:
        recipient_policy["to_group"] = to_group
    repo = _repo()
    job_id = str(args.get("job_id") or "").strip() or _weekly_report_job_id(requester, scope)
    job = ScheduledJob(
        id=job_id,
        created_by_userid=requester,
        job_type="weekly_report",
        schedule=schedule,
        recipient_policy=recipient_policy,
        resource_scope=dict(scope),
        output_formats=["doc", "card"],
        enabled=enabled,
    )
    repo.create_scheduled_job(job)
    return _json_result({
        "status": "scheduled" if enabled else "disabled",
        "job_id": job_id,
        "schedule": schedule,
        "resource_scope": scope,
        "recipients": recipients,
        "to_group": to_group,
    })


def _handle_enterprise_update_doc_content(args: dict[str, Any], **kwargs: Any) -> str:
    result = update_doc_content(
        requester_userid=_trusted_requester(args),
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
            requester_userid=_trusted_requester(args),
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
            requester_userid=_trusted_requester(args),
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
            requester_userid=_trusted_requester(args),
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
            requester_userid=_trusted_requester(args),
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
        requester_userid=_trusted_requester(args),
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
            requester_userid=_trusted_requester(args),
            conversation_id=str(args.get("conversation_id") or kwargs.get("task_id") or "").strip(),
            title=str(args.get("title") or "").strip(),
            pages=_dict_list(args.get("pages") or [], "pages"),
            send_to_names=_string_list(args.get("send_to_names") or [], "send_to_names"),
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="created")
    return _resource_json(result)


def _handle_enterprise_smartsheet_get_schema(args: dict[str, Any], **kwargs: Any) -> str:
    result = smartsheet_get_schema(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_get_records(args: dict[str, Any], **kwargs: Any) -> str:
    result = smartsheet_get_records(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
        limit=int(args.get("limit") or 100),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_get_schema(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_get_schema(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_get_range(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_get_range(
        requester_userid=_trusted_requester(args),
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
        requester_userid=_trusted_requester(args),
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
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        title=str(args.get("title") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
    )
    return _json_result(result)


def _handle_enterprise_online_sheet_delete_sheet(args: dict[str, Any], **kwargs: Any) -> str:
    result = online_sheet_delete_sheet(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_add_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = _dict_list(args.get("records") or [], "records")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = add_smartsheet_records(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        records=records,
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_update_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        records = _dict_list(args.get("records") or [], "records")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = update_smartsheet_records(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        records=records,
        key_type=str(args.get("key_type") or "CELL_VALUE_KEY_TYPE_FIELD_TITLE"),
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_delete_records(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        record_ids = _string_list(args.get("record_ids") or [], "record_ids")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = delete_smartsheet_records(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        record_ids=record_ids,
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_update_fields(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        fields = _dict_list(args.get("fields") or [], "fields")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = update_smartsheet_fields(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        fields=fields,
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
    )
    return _json_result(result)


def _handle_enterprise_smartsheet_delete_fields(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        field_ids = _string_list(args.get("field_ids") or [], "field_ids")
    except ValueError as exc:
        return _json_result({"error": str(exc)})
    result = delete_smartsheet_fields(
        requester_userid=_trusted_requester(args),
        docid=str(args.get("docid") or "").strip(),
        sheet_id=str(args.get("sheet_id") or "").strip(),
        field_ids=field_ids,
        repo=_repo(),
        wecom_client=_wecom_client(),
        session_id=_session_id(args, **kwargs),
        name_hint=str(args.get("name_hint") or args.get("table_name") or "").strip(),
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
        "description": "Create a real WeCom smartsheet, persist docid/resource/permissions/audit to Postgres, and optionally send the link. Use this immediately when the user asks to create/generate a table or random test smartsheet; do not only promise that you will create it. Accept either proposal_id or direct table/permission/send names when the user has clearly asked to create now. 当用户贴了带表头的数据（如订单表）时，必须从数据中抽取真实列名传 field_names，建表后再调用 enterprise_smartsheet_get_schema 取 sheet_id、enterprise_smartsheet_add_records 按列名分批写入所有数据行；不要只建空表。注意：用户指向【已有】表（如「把它/这张表 发给/分享给 某人」）时不要用本工具新建，应改用 enterprise_grant_resource_permission；同名资源已存在时本工具会返回 already_exists 而不重复建表。",
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
                "force_create": {
                    "type": "boolean",
                    "description": "默认 false：同名资源已存在时返回 already_exists、不重复建表。仅当用户明确要求「再建一张同名表」时才设 true。",
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
                "push_to_group": {"type": "boolean", "description": "可选：同时把任务卡片推送到目标团队的企业群（appchat）"},
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
    name="enterprise_schedule_report",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_schedule_report",
        "description": (
            "管理者配置定期工作汇总报告（默认每周五下午）。报告会汇总范围内成员有权限的"
            "智能表/文档内容，生成报告文档并卡片推送给收件人。schedule 用 cron 表达式"
            "（默认 '0 14 * * 5' 周五14点）；resource_scope 用 {\"team\":\"周婉倪\"} 或 "
            "{\"userids\":[...]}；recipients 是收件人 userid 列表。重复传相同 job_id 可更新配置，"
            "enabled=false 可暂停。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "配置报告的管理者 userid"},
                "schedule": {"type": "string", "description": "cron 表达式，默认 '0 14 * * 5'（周五14点）"},
                "resource_scope": {"type": "object", "description": "范围，如 {\"team\":\"周婉倪\"} 或 {\"userids\":[\"u1\"]}"},
                "recipients": {"type": "array", "items": {"type": "string"}, "description": "收件人 userid 列表"},
                "enabled": {"type": "boolean", "description": "是否启用，默认 true；false 暂停"},
                "job_id": {"type": "string", "description": "可选：传已有 job_id 更新配置"},
                "to_group": {"type": "string", "description": "可选：把报告也推送到该企业群（appchat）的 group_key，如 'team:周婉倪'（先用 enterprise_create_group 建群）"},
            },
            "required": ["requester_userid", "resource_scope", "recipients"],
        },
    },
    handler=_handle_enterprise_schedule_report,
    check_fn=_check_enterprise_core,
    emoji="schedule",
)


def _handle_enterprise_grant_resource_permission(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        members = args.get("member_names") or []
        if not isinstance(members, list):
            return _json_result({"error": "member_names must be a list"})
        member_names = [str(m) for m in members]
        note = str(args.get("note") or "").strip()
        repo = _repo()
        # 接收人姓名解析不到时，不猜、不新建——返回候选让 agent 请用户确认。
        resolution = resolve_members_with_candidates(member_names, repo)
        if resolution["unresolved"]:
            hints = []
            for item in resolution["unresolved"]:
                if item["candidates"]:
                    cand = "，".join(f"{c['name']}({c['userid']})" for c in item["candidates"])
                    hints.append(f"「{item['name']}」没找到，你是指：{cand}？")
                else:
                    hints.append(f"「{item['name']}」没找到，通讯录里也没有相近的人。")
            return _json_result({
                "status": "need_recipient_confirmation",
                "unresolved": resolution["unresolved"],
                "reply_text": (
                    "；".join(hints)
                    + "。请确认接收人，或先同步通讯录后再试——我没有新建表，也没有发给你。"
                ),
            })
        result = grant_resource_permission(
            requester_userid=_trusted_requester(args),
            resource_name=str(args.get("resource_name") or "").strip(),
            member_names=member_names,
            repo=repo,
            wecom_client=_wecom_client(),
            note=note,
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    if result.get("status") == "need_resource_confirmation":
        # 重名表：把候选透出去让 agent 请用户指明，不悄悄选一张
        return _json_result(result)
    resource = result["resource"]
    _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="shared")
    return _json_result({
        "status": result["status"],
        "resource_id": resource.id,
        "member_userids": result["member_userids"],
        "sent_userids": result.get("sent_userids", []),
        "reply_text": result["reply_text"],
    })


def _handle_enterprise_create_group(args: dict[str, Any], **kwargs: Any) -> str:
    """建/复用一个企业群（appchat）并把成员拉进去，供之后主动/定时推送任务、报告、通知。"""
    try:
        name = str(args.get("name") or "").strip()
        if not name:
            return _json_result({"error": "name（群名）必填"})
        group_key = str(args.get("group_key") or "").strip() or f"group:{name}"
        members = args.get("members") or []
        if not isinstance(members, list):
            return _json_result({"error": "members must be a list"})
        member_names = [str(m) for m in members]
        repo = _repo()
        # 成员姓名解析不到时，不猜、不建群——返回候选让 agent 请用户确认。
        resolution = resolve_members_with_candidates(member_names, repo)
        if resolution["unresolved"]:
            hints = []
            for item in resolution["unresolved"]:
                if item["candidates"]:
                    cand = "，".join(f"{c['name']}({c['userid']})" for c in item["candidates"])
                    hints.append(f"「{item['name']}」没找到，你是指：{cand}？")
                else:
                    hints.append(f"「{item['name']}」没找到，通讯录里也没有相近的人。")
            return _json_result({
                "status": "need_member_confirmation",
                "unresolved": resolution["unresolved"],
                "reply_text": "；".join(hints) + "。请确认群成员，或先同步通讯录后再试——我没有建群。",
            })
        owner = _trusted_requester(args)
        chat = ensure_appchat(group_key, name, owner, resolution["resolved"], repo, _wecom_client())
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    return _json_result({
        "status": "group_ready",
        "group_key": chat.group_key,
        "chatid": chat.chatid,
        "member_userids": chat.member_userids,
        "reply_text": (
            f"企业群「{name}」已就绪（{len(chat.member_userids)} 人），"
            f"之后用 enterprise_dispatch_task(push_to_group=true) 或 "
            f"enterprise_schedule_report(to_group='{chat.group_key}') 即可往这个群推任务/报告。"
        ),
    })


def _handle_enterprise_rename_resource(args: dict[str, Any], **kwargs: Any) -> str:
    """重命名【已有】表/文档：真调 rename_doc 改名并同步登记表。"""
    try:
        resource_ref = str(args.get("resource_name") or "").strip()
        new_name = str(args.get("new_name") or "").strip()
        if not resource_ref:
            return _json_result({"error": "resource_name（要改名的表/文档名或链接）必填"})
        if not new_name:
            return _json_result({"error": "new_name（新名称）必填"})
        result = rename_resource(
            requester_userid=_trusted_requester(args),
            resource_ref=resource_ref,
            new_name=new_name,
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    if result.get("status") == "need_resource_confirmation":
        # 重名：透出候选让 agent 请用户指明，不悄悄选一张、不新建
        return _json_result(result)
    out: dict[str, Any] = {"status": result["status"], "reply_text": result.get("reply_text", "")}
    res = result.get("resource")
    if res is not None:
        out["resource_id"] = getattr(res, "id", "")
    for key in ("old_name", "new_name", "wecom_error"):
        if key in result:
            out[key] = result[key]
    return _json_result(out)


def _handle_enterprise_push_resource_to_group(args: dict[str, Any], **kwargs: Any) -> str:
    """把【已有】表/文档发到企业群(appchat)；群不存在时诚实提议建群，绝不新建表/用 wecom-cli。"""
    try:
        resource_ref = str(args.get("resource_name") or "").strip()
        group_ref = str(args.get("group_key") or args.get("group_name") or "").strip()
        if not resource_ref:
            return _json_result({"error": "resource_name（要发送的已有表/文档名或链接）必填"})
        if not group_ref:
            return _json_result({"error": "group_name 或 group_key（目标企业群）必填"})
        result = push_existing_resource_to_group(
            requester_userid=_trusted_requester(args),
            resource_ref=resource_ref,
            group_ref=group_ref,
            repo=_repo(),
            wecom_client=_wecom_client(),
        )
    except (ValueError, PermissionError) as exc:
        return _json_result({"error": str(exc)})
    if result.get("status") == "need_resource_confirmation":
        return _json_result(result)
    out: dict[str, Any] = {"status": result["status"], "reply_text": result.get("reply_text", "")}
    res = result.get("resource")
    if res is not None:
        out["resource_id"] = getattr(res, "id", "")
        _remember(_session_id(args, **kwargs), _trusted_requester(args), result, action="sent_to_group")
    for key in ("group_key", "group_name"):
        if key in result:
            out[key] = result[key]
    return _json_result(out)


registry.register(
    name="enterprise_grant_resource_permission",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_grant_resource_permission",
        "description": (
            "把【已存在】的智能表/文档分享给某人（= 授权 read+write 并给对方发卡通知）。"
            "凡是指向已有资源的「把它/这张表/这个文档 发给 / 发送 / 转发 / 分享给 某人」"
            "「让某人也能看 / 也能改这张已有表」「把刚建的表给某人」"
            "「把这张表授权给XX」「给某文档加某人读写权限」——都用本工具，"
            "【不要新建】、【不要发给自己】。"
            "resource_name 按名称定位已有资源；member_names 是接收人姓名或 userid 列表。"
            "若用户还带了说明/留言（如「这是测试，可更改」），放进 note，会随卡片发给接收人。"
            "区分：要【新建】表/文档并分发给团队才用 enterprise_dispatch_task。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "发起分享/授权的人 userid（须是资源负责人或管理者）"},
                "resource_name": {"type": "string", "description": "目标【已有】表/文档名称"},
                "member_names": {"type": "array", "items": {"type": "string"}, "description": "接收人姓名或 userid 列表"},
                "note": {"type": "string", "description": "可选：发给接收人的附言/说明（如「这是测试，可更改」），随卡片送达。"},
            },
            "required": ["requester_userid", "resource_name", "member_names"],
        },
    },
    handler=_handle_enterprise_grant_resource_permission,
    check_fn=_check_enterprise_core,
    emoji="grant",
)

registry.register(
    name="enterprise_create_group",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_create_group",
        "description": (
            "建一个企业群（应用群聊 appchat）并把成员拉进去，供之后主动/定时推送任务、报告、通知。"
            "自建应用无法加入用户自建群，只能往本应用建的群发——所以「把消息发到某个群」要先用本工具建群。"
            "owner 默认是发起的管理者；members 是成员姓名或 userid 列表（含 owner 至少 2 人）；"
            "group_key 可选，用来稳定复用同一个群（不传则按群名派生）。返回 group_key，之后"
            "enterprise_dispatch_task(push_to_group=true) 或 enterprise_schedule_report(to_group=...) 即可往这个群推。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "建群的管理者 userid（会作为群 owner）"},
                "name": {"type": "string", "description": "群名"},
                "members": {"type": "array", "items": {"type": "string"}, "description": "成员姓名或 userid 列表"},
                "group_key": {"type": "string", "description": "可选：稳定复用键，如 'team:周婉倪'；不传则按群名派生"},
            },
            "required": ["requester_userid", "name", "members"],
        },
    },
    handler=_handle_enterprise_create_group,
    check_fn=_check_enterprise_core,
    emoji="group",
)

registry.register(
    name="enterprise_rename_resource",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_rename_resource",
        "description": (
            "重命名【已有】的智能表/文档（真调企业微信 rename_doc 接口改名，并同步登记表）。"
            "凡「把这张表/这个文档改名叫…」「重命名为…」「表名改成…」都用本工具——"
            "【不要】回复\"系统没有重命名工具/无法通过 API 改名\"，企业微信支持按真实 docid 改名。"
            "resource_name 可传表/文档名，或直接传链接（含 s3_… 短 id，工具会自动反查真实 docid）；"
            "new_name 是新名称。重名定位不到唯一资源会返回候选让你向用户确认，绝不新建。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "发起改名的人 userid（须是资源负责人或管理者）"},
                "resource_name": {"type": "string", "description": "要改名的【已有】表/文档名称，或其链接"},
                "new_name": {"type": "string", "description": "新名称"},
            },
            "required": ["requester_userid", "resource_name", "new_name"],
        },
    },
    handler=_handle_enterprise_rename_resource,
    check_fn=_check_enterprise_core,
    emoji="rename",
)

registry.register(
    name="enterprise_push_resource_to_group",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_push_resource_to_group",
        "description": (
            "把【已存在】的智能表/文档发到【企业群】(应用群 appchat)。凡「把这张表/这个文档发到…群里」"
            "「推到 XX 群」指向已有资源的发群请求都用本工具，【不要新建表】、【绝不用 wecom-cli】、"
            "【绝不让用户手动复制链接】。resource_name 传已有表/文档名或链接；group_name 传群名（或 group_key）。"
            "若该企业群还不存在，工具会返回诚实说明（status=need_group）——自建应用发不进用户自己拉的微信群，"
            "需先用 enterprise_create_group 建企业群并拉成员，再发；把这句话转达用户、别编\"权限限制\"。"
            "区分：【新建】表/文档并发给【团队】用 enterprise_dispatch_task；发给【个人】用 enterprise_grant_resource_permission。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string", "description": "发起分享到群的人 userid（须是资源负责人或管理者）"},
                "resource_name": {"type": "string", "description": "要发送的【已有】表/文档名称，或其链接"},
                "group_name": {"type": "string", "description": "目标企业群名（也可改用 group_key 指定）"},
                "group_key": {"type": "string", "description": "可选：企业群稳定键，如 'group:muzhi工作群' / 'team:周婉倪'"},
            },
            "required": ["requester_userid", "resource_name"],
        },
    },
    handler=_handle_enterprise_push_resource_to_group,
    check_fn=_check_enterprise_core,
    emoji="group",
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
        "description": (
            "读取 WeCom 智能表的子表与字段（编辑记录前用它确认 sheet_id / 列名）。"
            "返回的 fields 即列名；WeCom 不回字段定义时会从已有数据【只读】推断（标 fields_inferred）——"
            "绝不要为探测列名去写测试行。引用「那个表/这张表/刚才的表」时可【不传 docid】、改传 name_hint"
            "（或留空），工具会按本会话最近操作过的表命中；docid 用真实 API docid（不是 URL 里的 s3_ 标识）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string", "description": "真实 API docid；引用『那个表』时可留空，改用 name_hint"},
                "sheet_id": {"type": "string", "description": "可留空：留空时取该表第一个子表"},
                "name_hint": {"type": "string", "description": "表名关键词；docid 留空时按本会话最近资源命中"},
            },
            "required": ["requester_userid"],
        },
    },
    handler=_handle_enterprise_smartsheet_get_schema,
    check_fn=_check_enterprise_core,
    emoji="schema",
)

registry.register(
    name="enterprise_smartsheet_get_records",
    toolset=ENTERPRISE_TOOLSET,
    schema={
        "name": "enterprise_smartsheet_get_records",
        "description": (
            "读取 WeCom 智能表已有记录。每行返回 {record_id, values:{字段:文本}}——record_id 用于删除/更新"
            "具体行（配合 enterprise_smartsheet_delete_records / update_records）。自动翻页跳过 WeCom 排在最前的空默认行。"
            "用户要「读取/查看/清空/删除/修改表里的内容」前都先用本工具读出 record_id——这是读写闭环里负责读取的工具。"
            "docid 用真实 API docid（不是 URL 里的 s3_ 标识）。返回里也带 fields（列名，空表会只读推断）。"
            "引用「那个表/这张表/刚才的表」时【不要自己猜 docid】，可留空 docid、改传 name_hint（或都留空），"
            "工具会按本会话最近操作过的表命中；多张同名会让你反问，绝不乱选、绝不新建。"
            "这是【只读】工具——读取/查看内容只用它，绝不能用写/删工具去探测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_userid": {"type": "string"},
                "docid": {"type": "string", "description": "真实 API docid；引用『那个表』时可留空，改用 name_hint"},
                "sheet_id": {"type": "string", "description": "可留空：留空时取该表第一个子表"},
                "name_hint": {"type": "string", "description": "表名关键词；docid 留空时按本会话最近资源命中"},
                "limit": {"type": "integer", "description": "最多读取多少行，默认 100"},
            },
            "required": ["requester_userid"],
        },
    },
    handler=_handle_enterprise_smartsheet_get_records,
    check_fn=_check_enterprise_core,
    emoji="read",
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


def _install_harness_guards() -> int:
    """给本 toolset 中有策略的工具的 handler 套上 observe 模式 guard（幂等）。

    在所有 registry.register(...) 之后调用：原地替换 entry.handler。
    """
    from enterprise_core.harness import guard, policy_for

    wrapped_count = 0
    for name in registry.get_tool_names_for_toolset(ENTERPRISE_TOOLSET):
        if policy_for(name) is None:
            continue
        entry = registry.get_entry(name)
        if entry is None or getattr(entry.handler, "__wrapped__", None) is not None:
            continue
        entry.handler = guard(name, entry.handler)
        wrapped_count += 1
    return wrapped_count


_install_harness_guards()
