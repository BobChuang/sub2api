"""Read the three explicitly identified gateway keys from the local deployment.

This helper never calls an HTTP endpoint. Docker/psql output stays private, and
the default metadata check does not select full keys from PostgreSQL at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse


CHANNELS = ("cline", "opencode", "commandcode")
EXPECTED_MASKS = {
    "cline": ("sk-fcf", "cafd"),
    "opencode": ("sk-d01", "6c97"),
    "commandcode": ("sk-c58", "bbeb"),
}
_DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _fail(stage: str) -> None:
    # Never interpolate subprocess output, inspect data, environment or DB rows.
    raise ValueError(f"系统 Key 读取失败：{stage}")


def _run(stage: str, args: list[str], deadline: float, sql: str | None = None) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail(stage + "，超时")
    try:
        result = subprocess.run(
            [*_DOCKER, *args], input=sql, text=True, capture_output=True,
            timeout=min(15, remaining), check=False,
        )
    except subprocess.TimeoutExpired:
        _fail(stage + "，超时")
    except (OSError, UnicodeError):
        _fail(stage + "，无法执行 Docker")
    if result.returncode:
        _fail(stage + f"，退出码 {result.returncode}")
    return result.stdout


def _inspect(name: str, stage: str, deadline: float) -> dict:
    raw = _run(stage, ["inspect", "--type", "container", name], deadline)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _fail(stage + "，返回格式错误")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        _fail(stage + "，容器不唯一")
    result = data[0]
    state = result.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        _fail(stage + "，容器未运行")
    actual_name = result.get("Name")
    if not isinstance(actual_name, str) or actual_name.lstrip("/") != name:
        _fail(stage + "，容器名称不匹配")
    return result


def _environment(container: dict) -> dict[str, str]:
    entries = (container.get("Config") or {}).get("Env") or []
    if not isinstance(entries, list):
        _fail("容器环境格式错误")
    values = {}
    for entry in entries:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        if name in values and values[name] != value:
            _fail("容器环境变量重复")
        values[name] = value
    return values


def _local_port(base_url: str) -> int:
    if not isinstance(base_url, str):
        _fail("Base URL 格式错误")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        host, port = parsed.hostname, parsed.port
    except (ValueError, TypeError):
        _fail("Base URL 格式错误")
    if (parsed.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        _fail("系统 Key 只允许用于本机 loopback 地址")
    if port is not None and not 1 <= port <= 65535:
        _fail("Base URL 端口无效")
    return port if port is not None else (443 if parsed.scheme == "https" else 80)


def _verify_service_port(service: dict, port: int) -> None:
    ports = (service.get("NetworkSettings") or {}).get("Ports") or {}
    if not isinstance(ports, dict):
        _fail("服务端口信息无效")
    for target, bindings in ports.items():
        if not target.endswith("/tcp") or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if (isinstance(binding, dict) and str(binding.get("HostPort")) == str(port)
                    and binding.get("HostIp", "") in ("", "0.0.0.0", "127.0.0.1", "::", "::1")):
                return
    _fail("Base URL 端口与服务容器公布的本机端口不匹配")


def _verify_database(service: dict, database: dict, database_name: str) -> tuple[str, str, int]:
    env = _environment(service)
    db_env = _environment(database)
    host = env.get("DATABASE_HOST", "")
    user = env.get("DATABASE_USER", "")
    dbname = env.get("DATABASE_DBNAME", "")
    if not host or not _IDENTIFIER.fullmatch(user) or not _IDENTIFIER.fullmatch(dbname):
        _fail("服务缺少明确的数据库主机、用户或数据库名")
    # Reject connection strings/options instead of letting psql reinterpret them.
    try:
        port = int(env.get("DATABASE_PORT", "5432"))
    except (ValueError, TypeError):
        _fail("数据库端口无效")
    if not 1 <= port <= 65535:
        _fail("数据库端口无效")
    if db_env.get("POSTGRES_USER") and db_env["POSTGRES_USER"] != user:
        _fail("服务与 PostgreSQL 容器的数据库用户不匹配")
    if db_env.get("POSTGRES_DB") and db_env["POSTGRES_DB"] != dbname:
        _fail("服务与 PostgreSQL 容器的数据库名不匹配")
    service_networks = (service.get("NetworkSettings") or {}).get("Networks") or {}
    database_networks = (database.get("NetworkSettings") or {}).get("Networks") or {}
    if not isinstance(service_networks, dict) or not isinstance(database_networks, dict):
        _fail("容器网络信息无效")
    shared = set(service_networks) & set(database_networks)
    for network in shared:
        endpoint = database_networks[network]
        if not isinstance(endpoint, dict):
            continue
        aliases = {database_name}
        for field in ("Aliases", "DNSNames"):
            items = endpoint.get(field) or []
            if isinstance(items, list):
                aliases.update(item for item in items if isinstance(item, str))
        aliases.update(value for field in ("IPAddress", "GlobalIPv6Address")
                       if isinstance((value := endpoint.get(field)), str) and value)
        if host.lower() in {alias.lower() for alias in aliases}:
            return user, dbname, port
    _fail("数据库主机未指向共享 Docker 网络中的指定 PostgreSQL 容器")


def _query(include_secrets: bool) -> str:
    secret_column = ", k.key AS key" if include_secrets else ""
    return """SELECT COALESCE(json_agg(row_to_json(selected) ORDER BY selected.name, selected.api_key_id), '[]'::json)
FROM (
    SELECT k.id AS api_key_id, k.name, k.user_id, k.group_id,
           g.name AS group_name, g.platform, k.status,
           u.status AS user_status, g.status AS group_status,
           (u.id IS NULL OR u.deleted_at IS NOT NULL) AS user_deleted,
           (g.id IS NULL OR g.deleted_at IS NOT NULL) AS group_deleted,
           (k.expires_at IS NOT NULL AND k.expires_at < NOW()) AS expired,
           (k.quota > 0 AND k.quota_used >= k.quota) AS quota_exhausted,
           LEFT(k.key, 6) AS key_prefix, RIGHT(k.key, 4) AS key_suffix""" + secret_column + """
    FROM api_keys AS k
    LEFT JOIN users AS u ON u.id = k.user_id
    LEFT JOIN groups AS g ON g.id = k.group_id
    WHERE k.deleted_at IS NULL
      AND LOWER(k.name) IN ('cline', 'opencode', 'commandcode')
) AS selected;
"""


def _validate_rows(rows, include_secrets: bool) -> tuple[dict, dict]:
    if not isinstance(rows, list):
        _fail("查询结果格式错误")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            _fail("Key 身份记录格式错误")
        channel = row["name"].lower()
        if channel not in CHANNELS:
            _fail("查询返回了未指定的 Key")
        if channel in found:
            _fail("同名 Key 不唯一，无法确定截图中的身份")
        found[channel] = row
    if set(found) != set(CHANNELS):
        _fail("没有找到完整的三个指定 Key")
    identities, keys = {}, {}
    owners = set()
    for channel in CHANNELS:
        row = found[channel]
        for field in ("api_key_id", "user_id", "group_id"):
            if type(row.get(field)) is not int or row[field] <= 0:
                _fail("Key 或用户、分组关联无效")
        prefix, suffix = EXPECTED_MASKS[channel]
        if row.get("key_prefix") != prefix or row.get("key_suffix") != suffix:
            _fail("Key 前后缀与截图不匹配")
        if any(row.get(field) != "active" for field in ("status", "user_status", "group_status")):
            _fail("Key、用户或分组未启用")
        for field in ("user_deleted", "group_deleted", "expired", "quota_exhausted"):
            if row.get(field) is not False:
                _fail("Key 关联已删除、已过期或额度耗尽")
        if not isinstance(row.get("group_name"), str) or not row["group_name"]:
            _fail("Key 分组名称无效")
        if not isinstance(row.get("platform"), str) or not row["platform"]:
            _fail("Key 分组平台无效")
        owners.add(row["user_id"])
        if include_secrets:
            key = row.get("key")
            if (not isinstance(key, str) or not 11 <= len(key) <= 128
                    or not key.startswith(prefix) or not key.endswith(suffix)
                    or any(char.isspace() or ord(char) < 32 for char in key)):
                _fail("完整 Key 格式或身份不匹配")
            keys[channel] = key
        identities[channel] = {
            field: row[field] for field in ("api_key_id", "name", "user_id", "group_id",
                                             "group_name", "platform", "status", "user_status", "group_status")
        }
        identities[channel]["masked_key"] = prefix + "..." + suffix
    if len(owners) != 1:
        _fail("三个 Key 不属于同一用户")
    if include_secrets and len(set(keys.values())) != len(CHANNELS):
        _fail("三个 Key 必须互不相同")
    return keys, identities


def load_system_keys(base_url: str, service_container: str = "sub2api",
                     database_container: str = "sub2api-postgres",
                     include_secrets: bool = False) -> tuple[dict, dict]:
    """Return selected in-memory keys (opt-in) and always-safe identity metadata.

    Only the local Docker socket and the service's configured local database are
    used. All subprocesses share a 15-second deadline. PostgreSQL is read-only.
    """
    if type(include_secrets) is not bool:
        _fail("include_secrets 必须是布尔值")
    port = _local_port(base_url)
    if (not isinstance(service_container, str) or not _CONTAINER.fullmatch(service_container)
            or not isinstance(database_container, str) or not _CONTAINER.fullmatch(database_container)
            or service_container == database_container):
        _fail("容器名称无效")
    deadline = time.monotonic() + 15
    service = _inspect(service_container, "检查服务容器", deadline)
    _verify_service_port(service, port)
    database = _inspect(database_container, "检查 PostgreSQL 容器", deadline)
    user, dbname, dbport = _verify_database(service, database, database_container)
    result = _run("只读查询指定 Key", [
        "exec", "-i", "--user", "postgres", "--env",
        "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000",
        database_container, "psql", "-h", "/var/run/postgresql", "-p", str(dbport),
        "--no-password", "-U", user, "-d", dbname, "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
    ], deadline, _query(include_secrets))
    try:
        rows = json.loads(result)
    except (ValueError, TypeError):
        _fail("查询结果不是 JSON")
    keys, identities = _validate_rows(rows, include_secrets)
    metadata = {"source": "local_database", "service_container": service_container,
                "database_container": database_container, "identities": identities}
    return keys, metadata
