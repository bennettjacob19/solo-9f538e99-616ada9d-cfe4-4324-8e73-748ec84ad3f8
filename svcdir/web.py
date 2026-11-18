"""Flask application: HTML directory editor plus the ``/resolve`` API."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .errors import DirectoryError, InputError
from .store import DirectoryStore, validate_lease_seconds


def _store_from_env() -> DirectoryStore:
    db_path = os.environ.get("SVCDIR_DB", str(Path("data") / "directory.sqlite3"))
    lease = float(os.environ.get("SVCDIR_RESTORE_LEASE_SECONDS", "30"))
    return DirectoryStore(db_path, restore_lease_seconds=lease)


def create_app(store: DirectoryStore | None = None, *, config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SVCDIR_TESTING=False,
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    store = store or _store_from_env()
    app.extensions["svcdir"] = store
    store.initialize()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        return response

    # -- error handling ---------------------------------------------------

    @app.errorhandler(DirectoryError)
    def handle_directory_error(exc: DirectoryError):
        if _wants_json():
            return jsonify({"ok": False, "error": exc.to_dict()}), exc.status_code
        return (
            render_template(
                "error.html",
                title="操作未完成",
                status=exc.status_code,
                error=exc.to_dict(),
            ),
            exc.status_code,
        )

    @app.errorhandler(404)
    def handle_404(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "not_found", "message": "接口不存在"}}),
                404,
            )
        return render_template("error.html", title="未找到", status=404,
                               error={"code": "not_found", "message": "页面或接口不存在"}), 404

    @app.errorhandler(405)
    def handle_405(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "method_not_allowed", "message": "方法不允许"}}),
                405,
            )
        return render_template("error.html", title="方法不允许", status=405,
                               error={"code": "method_not_allowed", "message": "请求方法不被允许"}), 405

    @app.errorhandler(500)
    def handle_500(exc):
        original = getattr(exc, "original_exception", None) or exc
        app.logger.error("unhandled error: %s", original, exc_info=original)
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "internal_error", "message": "服务器内部错误"}}),
                500,
            )
        return render_template("error.html", title="服务器错误", status=500,
                               error={"code": "internal_error", "message": "服务器内部错误"}), 500

    def _wants_json() -> bool:
        if request.path.startswith("/api/") or request.path == "/resolve":
            return True
        return request.accept_mimetypes["application/json"] > request.accept_mimetypes["text/html"]

    # -- API --------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        store.ping()
        return jsonify({"ok": True})

    @app.get("/resolve")
    def resolve():
        name = request.args.get("name")
        token = request.args.get("token") or None
        with store.read_view() as view:
            result = view.resolve(name, token=token)
        body = {
            "ok": True,
            "version": result["version"],
            "restore_in_progress": result["restore_in_progress"],
        }
        if result.get("lease") is not None:
            body["lease"] = result["lease"]
        if "address" in result:
            body["name"] = result["name"]
            body["address"] = result["address"]
            body["kind"] = result["kind"]
            if result["kind"] == "temporary":
                body["lease_expires_at"] = result["lease_expires_at"]
                body["lease_expires_in"] = result["lease_expires_in"]
        else:
            body["count"] = result["count"]
            body["services"] = result["services"]
            body["entries"] = result["entries"]
        return jsonify(body)

    @app.get("/api/state")
    def api_state():
        token = request.args.get("token")
        with store.read_view() as view:
            state = view.snapshot(token=token)
        return jsonify({"ok": True, **state})

    # -- temporary leases -------------------------------------------------

    @app.post("/api/temporary")
    def api_register_temporary():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        lease_seconds = payload.get("lease_seconds", payload.get("ttl"))
        result = store.register_temporary(
            payload.get("service"), payload.get("address"), lease_seconds
        )
        return jsonify({"ok": True, **result}), 201

    @app.post("/api/temporary/<path:service>/renew")
    def api_renew_temporary(service: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        token = payload.get("token", request.headers.get("X-Lease-Token"))
        result = store.renew_temporary(
            service,
            token,
            payload.get("lease_seconds", payload.get("ttl")),
        )
        return jsonify({"ok": True, "renewed": True, **result})

    @app.delete("/api/temporary/<path:service>")
    def api_revoke_temporary(service: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        token = payload.get("token", request.headers.get("X-Lease-Token"))
        return jsonify({"ok": True, "deleted": True, **store.revoke_temporary(service, token)})

    @app.get("/api/backups")
    def api_backups():
        return jsonify({"ok": True, "backups": store.list_backups()})

    @app.post("/api/services")
    def api_upsert():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.upsert(
            payload.get("service"), payload.get("address"), payload.get("note", "")
        )
        return jsonify({"ok": True, **result}), 200

    @app.delete("/api/services/<path:service>")
    def api_delete(service: str):
        return jsonify({"ok": True, **store.delete(service)})

    @app.post("/api/backups")
    def api_create_backup():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.create_backup(payload.get("name"))
        return jsonify({"ok": True, **result}), 201

    @app.get("/api/backups/<path:name>/validate")
    def api_validate_backup(name: str):
        return jsonify({"ok": True, **store.validate_backup(name)})

    @app.post("/api/backups/<path:name>/restore")
    def api_restore_backup(name: str):
        result = store.restore_backup(name)
        return jsonify({"ok": True, **result})

    # -- HTML -------------------------------------------------------------

    def _redirect_back(**kwargs):
        return redirect(url_for("index", **kwargs), code=303)

    @app.get("/")
    def index():
        token = request.args.get("token")
        snapshot = store.snapshot(token=token)
        probe = (request.args.get("probe") or "").strip()
        probe_result = None
        probe_error = None
        if probe:
            try:
                probe_result = store.resolve(probe)
            except DirectoryError as exc:
                probe_error = exc.message
        return render_template(
            "index.html",
            state=snapshot,
            message=request.args.get("message"),
            error=request.args.get("error"),
            probe=probe,
            probe_result=probe_result,
            probe_error=probe_error,
        )

    @app.post("/services")
    def form_upsert():
        try:
            result = store.upsert(
                request.form.get("service"), request.form.get("address"), request.form.get("note", "")
            )
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已保存 {result['service']} -> {result['address']}（目录版本 {result['version']}）"
        )

    @app.post("/services/<path:service>/delete")
    def form_delete(service: str):
        try:
            result = store.delete(service)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(message=f"已删除 {result['service']}（目录版本 {result['version']}）")

    @app.post("/temporary")
    def form_register_temporary():
        token = ""
        try:
            result = store.register_temporary(
                request.form.get("service"),
                request.form.get("address"),
                validate_lease_seconds(request.form.get("lease_seconds")),
            )
            token = result["token"]
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        # The token is surfaced once, then dropped from the URL/history.
        return _redirect_back(
            message=f"已登记临时服务 {result['service']} -> {result['address']}，"
                    f"租约到期 {result['lease_expires_at']}。"
                    f"操作凭据（只显示这一次，请立即保存）：{token}"
        )

    def _form_lease_target(service: str) -> str:
        """Read the service name from the hidden field, falling back to path.

        One form posts to two actions (renew/revoke), so the real name travels
        in the body rather than in an action-specific URL.
        """
        candidate = (request.form.get("service") or "").strip()
        if candidate:
            return candidate
        if service == "_":
            raise InputError("缺少服务名")
        return service

    @app.post("/temporary/<path:service>/renew")
    def form_renew_temporary(service: str):
        try:
            target = _form_lease_target(service)
            raw_ttl = request.form.get("lease_seconds")
            result = store.renew_temporary(
                target,
                request.form.get("token"),
                validate_lease_seconds(raw_ttl) if raw_ttl else None,
            )
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已续租 {result['service']}，新到期时间 {result['lease_expires_at']}"
        )

    @app.post("/temporary/<path:service>/revoke")
    def form_revoke_temporary(service: str):
        try:
            target = _form_lease_target(service)
            result = store.revoke_temporary(target, request.form.get("token"))
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已撤销临时服务 {result['service']}，该名称现已可重新登记"
        )

    @app.post("/backups")
    def form_create_backup():
        try:
            result = store.create_backup(request.form.get("name"))
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已创建备份 {result['name']}（{result['entry_count']} 条，版本 {result['version']}）"
        )

    @app.post("/backups/<path:name>/restore")
    def form_restore_backup(name: str):
        try:
            result = store.restore_backup(name)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已从备份 {result['backup']} 恢复：{result['entry_count']} 条，"
                    f"新目录版本 {result['new_version']}"
        )

    return app
