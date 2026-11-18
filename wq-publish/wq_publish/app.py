"""Flask 路由：上传校验 → 预览增删改 → 确认整批替换。"""
from __future__ import annotations

import os
import secrets
import sqlite3

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .storage import LIVE_FILENAME, PublishConflictError, Storage
from .validation import format_concentration, validate_csv
from .qc import (
    MAX_DISTINCT_KEYS,
    MAX_PAIRS,
    QCValidationError,
    analyze_pairs,
    validate_pair_csv,
)

MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def create_app(data_dir: str | None = None, downstream_dir: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    data_dir = data_dir or os.environ.get("WQ_DATA_DIR") or os.path.join(base, "data")
    downstream_dir = (
        downstream_dir
        or os.environ.get("WQ_DOWNSTREAM_DIR")
        or os.path.join(base, "downstream")
    )

    secret_path = os.path.join(data_dir, "flask_secret")
    os.makedirs(data_dir, exist_ok=True)
    if os.path.exists(secret_path):
        with open(secret_path, "rb") as fh:
            app.secret_key = fh.read()
    else:
        key = secrets.token_bytes(32)
        with open(secret_path, "wb") as fh:
            fh.write(key)
        os.chmod(secret_path, 0o600)
        app.secret_key = key

    storage = Storage(data_dir, downstream_dir)
    app.storage = storage  # 供自测脚本直接访问

    @app.template_filter("conc")
    def _conc(value):
        return format_concentration(value)

    @app.template_filter("pct")
    def _pct(value):
        """相对偏差比率（0..1）→ 百分比文本；None 显示占位。"""
        if value is None:
            return "—"
        return f"{value * 100:.2f}%"

    @app.get("/")
    def index():
        reg = storage.current_registry()
        rows = storage.current_rows()
        events = storage.recent_events()
        return render_template(
            "index.html", reg=reg, rows=rows, events=events,
            live_filename=LIVE_FILENAME,
        )

    @app.post("/preview")
    def preview():
        upload = request.files.get("csv_file")
        batch_no = request.form.get("batch_no", "")
        if upload is None or not upload.filename:
            flash("请选择仪器导出的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = upload.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            flash("CSV 文件超过 16MB 上限", "error")
            return redirect(url_for("index"))

        rows, errors = validate_csv(raw, batch_no)
        if errors:
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("index"))

        reg = storage.current_registry()
        base_version = reg["current_version"] if reg else 0
        token = storage.create_session(batch_no.strip(), base_version, rows)
        diff = storage.preview_diff(rows)
        storage.log(
            "preview",
            f"会话 {token[:8]} 基于 v{base_version} 预览批次 {batch_no.strip()}："
            f"新增 {len(diff['inserts'])} / 修改 {len(diff['updates'])} / "
            f"撤回 {len(diff['deletes'])} / 不变 {len(diff['unchanged'])}",
        )
        return render_template(
            "preview.html",
            token=token,
            batch_no=batch_no.strip(),
            base_version=base_version,
            current_version=base_version,
            rows=rows,
            diff=diff,
            live_filename=LIVE_FILENAME,
        )

    @app.post("/publish/<token>")
    def publish(token):
        session = storage.get_session(token)
        if session is None:
            flash("修订会话不存在或已过期，请重新上传 CSV", "error")
            return redirect(url_for("index"))

        reg = storage.current_registry()
        current_version = reg["current_version"] if reg else 0
        if session["status"] != "pending" or session["base_version"] != current_version:
            # 会话在预览期间被别的发布超越：作废会话，展示基于旧版的差异，要求重新预览
            if session["status"] == "pending":
                storage.mark_obsolete(token, current_version)
                stale_reason = (
                    f"您基于 v{session['base_version']} 发起修订，"
                    f"但当前版本已变为 v{current_version}（另一页面已成功发布）"
                )
            else:
                reason_map = {
                    "published": "该修订已经发布，不能重复发布",
                    "obsolete": "该预览已被更新的发布超越而作废",
                    "cancelled": "该修订已被放弃（取消）",
                }
                stale_reason = reason_map.get(session["status"], "该修订会话已失效")
            diff = storage.preview_diff(session["rows"])
            storage.log(
                "conflict",
                f"会话 {token[:8]} 发布被拒（基线 v{session['base_version']}，"
                f"当前 v{current_version}，状态 {session['status']}）",
            )
            return render_template(
                "preview.html",
                token=token,
                batch_no=session["batch_no"],
                base_version=session["base_version"],
                current_version=current_version,
                rows=session["rows"],
                diff=diff,
                stale=True,
                stale_reason=stale_reason,
                live_filename=LIVE_FILENAME,
            ), 409

        try:
            new_version, row_count = storage.publish(token)
        except PublishConflictError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except sqlite3.OperationalError as exc:  # 极端并发下写锁等待超时
            app.logger.warning("publish lock contention: %s", exc)
            flash("当前有其他发布正在写入，请稍后重新预览再发布", "error")
            return redirect(url_for("index"))
        flash(f"发布成功：v{new_version} 已生效，共 {row_count} 行", "success")
        return redirect(url_for("index"))

    @app.post("/cancel/<token>")
    def cancel(token):
        if storage.cancel_session(token):
            flash("已放弃本次修订，当前发布版本未受影响", "info")
        return redirect(url_for("index"))

    @app.get("/published.csv")
    def published_csv():
        if not os.path.exists(storage.live_path):
            abort(404, description="尚无已发布的 CSV")
        return send_file(
            storage.live_path, mimetype="text/csv",
            download_name=LIVE_FILENAME, as_attachment=False,
        )

    # ---------------- 平行样品质控 ----------------
    @app.get("/qc")
    def qc_page():
        versions = storage.list_versions()
        return render_template("qc.html", versions=versions)

    @app.post("/qc/report")
    def qc_report():
        versions = storage.list_versions()
        version_text = request.form.get("version", "").strip()
        upload = request.files.get("pair_csv")

        def _back(messages):
            for msg in messages:
                flash(msg, "error")
            return render_template("qc.html", versions=versions), 400

        if not version_text or not version_text.lstrip("-").isdigit():
            return _back(["请选择一个已发布版本作为质控分析基线"])
        version = int(version_text)
        version_reg = storage.version_registry(version)
        if version_reg is None:
            return _back([f"所选版本 v{version} 不存在，请重新选择已发布版本"])

        if upload is None or not upload.filename:
            return _back(["请选择配对 CSV 文件"])
        raw = upload.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return _back(["CSV 文件超过 16MB 上限"])

        try:
            pairs = validate_pair_csv(raw)
        except QCValidationError as exc:
            # 无效上传：不生成报告，仅回表单页展示带行号的错误
            return _back(exc.messages)

        # 只读取所选版本的不可变快照；分析期间即使有新发布也不影响本报告
        snapshot_rows = storage.version_rows(version)
        report = analyze_pairs(pairs, snapshot_rows)
        storage.log(
            "qc",
            f"质控报告基于 v{version}：{len(pairs)} 对，"
            f"超限 {sum(1 for r in report['results'] if r['status'] == 'exceeded')} 对，"
            f"缺失 {len(report['mandatory_keys'])} 键，"
            f"最少核查 {len(report['check_keys'])} 个键",
        )
        return render_template(
            "qc_report.html",
            version_reg=version_reg,
            current_reg=storage.current_registry(),
            pairs=pairs,
            report=report,
            max_pairs=MAX_PAIRS,
            max_keys=MAX_DISTINCT_KEYS,
        )

    @app.errorhandler(413)
    def too_large(_):
        flash("上传文件超过 16MB 上限", "error")
        return redirect(url_for("index"))

    return app
