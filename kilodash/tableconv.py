"""Tables converter — the on-device web app that produces and manages the
decode-table store (TABLES.md). THE writer; every other party only reads.

One app, three tabs, reviewed from a big screen (same user model as
Node-RED / Signal K):

  PGN       upload/pick a vendor PDF → subprocess extraction → side-by-side
            review (source text vs. produced JSON) → human approves →
            validator runs → table + manifest land in the store atomically.
            Extraction is assistive, approval is human — a silently wrong
            bit-field offset is worse than no table; nothing is ever
            auto- or batch-approved.
  Installed inventory from the store — enable/disable, remove, view
            manifest, download (the flat SD-export table for decode-only
            consumers, plus the Light browse index per §5b), and ingest
            hand-copied files from the inbox (§6).
  DBC       stub ("coming") — same ingest→validate→store flow later into
            tables/dbc/.

Lifecycle: on-demand + idle timeout. The Tables tile starts the systemd
unit (setup/kilodash-tables.service); this process exits itself after
--idle-min minutes with no HTTP activity AND no in-flight job (a long PDF
parse counts as activity — Known gotchas), so navigating kilodash away
never kills a working session. Killing at any moment is safe: all store
writes are tmp-file + atomic rename (tables/store.py).

Untrusted input discipline: uploads are size-capped, extension + magic
checked; PDF parsing runs in a subprocess (kilodash/pdfextract.py) for
crash isolation; argv is list[str] throughout, no shell anywhere; client
filenames never become paths (uploads get generated ids, store names are
slug-validated).

Address selection is net.advertise_addr() (eth0 preferred — shared helper,
see its docstring for the dual-NIC routing caveat).
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from flask import (Flask, abort, redirect, render_template_string, request,
                   send_file, url_for)

from . import net

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tables import store, validate  # noqa: E402

VERSION = "1.0"
MAX_UPLOAD = 30 * 1024 * 1024        # vendor PDFs are big; 30 MB is generous
EXTRACT_TIMEOUT = 180                # seconds; a big parse still counts as
                                     # activity via the jobs counter
UPLOAD_TTL = 7 * 86400               # abandoned reviews get purged
IDLE_DEFAULT_MIN = 15


class Activity:
    """Idle-timeout bookkeeping: last HTTP touch + in-flight jobs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._jobs = 0

    def touch(self):
        with self._lock:
            self._last = time.monotonic()

    def job_start(self):
        with self._lock:
            self._jobs += 1

    def job_end(self):
        with self._lock:
            self._jobs -= 1
            self._last = time.monotonic()

    def idle_secs(self):
        with self._lock:
            if self._jobs > 0:
                return 0.0
            return time.monotonic() - self._last


# ------------------------------------------------------------------ helpers --
def _uid_ok(uid):
    return isinstance(uid, str) and len(uid) == 12 \
        and all(c in "0123456789abcdef" for c in uid)


def _upload_pdf(uid):
    return os.path.join(store.upload_dir(), uid + ".pdf")


def _upload_extract(uid):
    return os.path.join(store.upload_dir(), uid + ".extract.json")


def _pending_reviews():
    out = []
    try:
        names = sorted(os.listdir(store.upload_dir()))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".extract.json"):
            continue
        uid = fn[:-len(".extract.json")]
        try:
            with open(_upload_extract(uid)) as f:
                meta = json.load(f)
            out.append({"uid": uid, "source": meta.get("source_doc", "?"),
                        "error": meta.get("error")})
        except (OSError, ValueError):
            continue
    return out


def _purge_stale_uploads():
    now = time.time()
    try:
        names = os.listdir(store.upload_dir())
    except OSError:
        return
    for fn in names:
        p = os.path.join(store.upload_dir(), fn)
        try:
            if now - os.path.getmtime(p) > UPLOAD_TTL:
                os.remove(p)
        except OSError:
            continue


def _drop_upload(uid):
    for p in (_upload_pdf(uid), _upload_extract(uid)):
        try:
            os.remove(p)
        except OSError:
            pass


def _run_extraction(uid):
    """PDF → text+skeleton via the sandboxed worker. Never raises; failures
    land in the extract file so the review page can fall back to manual
    JSON entry."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [sys.executable, "-m", "kilodash.pdfextract", _upload_pdf(uid)]
    result = {"pages": [], "skeleton": {"PGNs": []}}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, cwd=repo,
                           timeout=EXTRACT_TIMEOUT)
        if p.returncode == 0:
            result.update(json.loads(p.stdout))
        else:
            result["error"] = (p.stderr.strip() or "extractor failed")[:300]
    except subprocess.TimeoutExpired:
        result["error"] = f"extraction exceeded {EXTRACT_TIMEOUT}s"
    except (OSError, ValueError) as e:
        result["error"] = str(e)[:300]
    return result


# ---------------------------------------------------------------- templates --
_BASE = """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Scottina · Tables</title><style>
 body{font:15px/1.45 system-ui,sans-serif;margin:0;background:#0b0f0c;color:#cde}
 a{color:#8f8}.wrap{max-width:1100px;margin:0 auto;padding:0 16px 40px}
 nav{background:#122016;padding:10px 16px;margin-bottom:18px}
 nav a{margin-right:18px;font-weight:700;text-decoration:none}
 nav a.on{color:#cfc;border-bottom:2px solid #4c4}
 h1{font-size:20px}h2{font-size:16px;margin-top:26px}
 .card{background:#122016;border-radius:10px;padding:14px 16px;margin:10px 0}
 .msg{background:#1d3323;border-left:4px solid #4c4;padding:8px 12px;margin:10px 0}
 .err{background:#33201d;border-left-color:#c54}
 table{border-collapse:collapse;width:100%}
 td,th{padding:6px 10px;text-align:left;border-bottom:1px solid #1e3325}
 .cols{display:flex;gap:16px;align-items:stretch}
 .cols>div{flex:1;min-width:0}
 pre,textarea{background:#0e1810;color:#cfc;border:1px solid #2a4433;
   border-radius:8px;padding:10px;width:100%;box-sizing:border-box;
   font:13px/1.4 ui-monospace,monospace;overflow:auto}
 pre{max-height:70vh;white-space:pre-wrap}
 textarea{min-height:70vh}
 input[type=text]{background:#0e1810;color:#cfc;border:1px solid #2a4433;
   border-radius:6px;padding:6px 10px;font:14px ui-monospace,monospace}
 button,.btn{background:#2c6a3f;color:#efe;border:0;border-radius:8px;
   padding:8px 14px;font-weight:700;cursor:pointer;text-decoration:none;
   display:inline-block}
 button.warn{background:#7a2f2a}button.ghost{background:#23392b}
 .dim{color:#7a9;font-size:13px}.badge{background:#23392b;border-radius:6px;
   padding:2px 8px;font-size:12px}
</style></head><body>
<nav><span style="color:#7a9;margin-right:20px">Scottina · Tables converter
 v{{version}}</span>
 <a href="{{url_for('pgn')}}" class="{{'on' if tab=='pgn' else ''}}">PGN</a>
 <a href="{{url_for('installed')}}"
    class="{{'on' if tab=='installed' else ''}}">Installed</a>
 <a href="{{url_for('dbc')}}" class="{{'on' if tab=='dbc' else ''}}">DBC</a>
</nav><div class="wrap">
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
{% if err %}<div class="msg err">{{err}}</div>{% endif %}
{% block body %}{% endblock %}
</div></body></html>"""

_PGN = """{% extends "base" %}{% block body %}
<h1>Vendor PDF → PGN table</h1>
<div class="card"><form method="post" action="{{url_for('upload')}}"
  enctype="multipart/form-data">
 <p>Upload a vendor PDF. Text is extracted <b>as an aid only</b> — you
 review source and JSON side by side and nothing lands in the store until
 you approve it (and it passes the validator).</p>
 <input type="file" name="pdf" accept=".pdf" required>
 <button type="submit">Upload &amp; extract</button>
</form></div>
{% if pending %}<h2>Pending reviews</h2>
{% for p in pending %}<div class="card">
 <a href="{{url_for('review', uid=p.uid)}}">{{p.source}}</a>
 {% if p.error %}<span class="badge">extraction failed — manual JSON</span>
 {% endif %}
 <form method="post" style="display:inline"
   action="{{url_for('discard', uid=p.uid)}}">
  <button class="ghost" type="submit">Discard</button></form>
</div>{% endfor %}{% endif %}
{% endblock %}"""

_REVIEW = """{% extends "base" %}{% block body %}
<h1>Review — {{source}}</h1>
{% if exerr %}<div class="msg err">Extraction: {{exerr}} — enter the table
JSON manually below.</div>{% endif %}
<p class="dim">Left: extracted source text (check every offset against it).
Right: the JSON that will be installed — Canboat subset per TABLES.md §2.
Approval is yours alone; the validator is the last gate.</p>
<form method="post" action="{{url_for('install')}}">
<input type="hidden" name="uid" value="{{uid}}">
<p>Store name: <input type="text" name="name" value="{{name}}"
  pattern="[a-z0-9_-]{1,64}"> <span class="dim">[a-z0-9_-], ≤64</span>
 <button type="submit">Approve → validate → install</button></p>
<div class="cols">
 <div><h2>Source text ({{pages|length}} pages)</h2>
  <pre>{% for pg in pages %}--- page {{loop.index}} ---
{{pg}}
{% endfor %}</pre></div>
 <div><h2>Table JSON</h2>
  <textarea name="json" spellcheck="false">{{json_text}}</textarea></div>
</div></form>
{% endblock %}"""

_INSTALLED = """{% extends "base" %}{% block body %}
<h1>Installed PGN tables</h1>
{% if not tables %}<p class="dim">Nothing installed yet — convert a PDF on
the PGN tab, or drop Canboat-style JSON in the inbox.</p>{% endif %}
{% if tables %}<div class="card"><table>
<tr><th>name</th><th>PGNs</th><th>state</th><th>source</th><th>converted</th>
<th></th></tr>
{% for t in tables %}<tr>
 <td><b>{{t.name}}</b></td><td>{{t.pgn_count}}</td>
 <td>{% if not t.verified %}<span class="badge">unverified</span>
     {% elif t.enabled %}enabled{% else %}disabled{% endif %}</td>
 <td class="dim">{{t.meta.source_doc if t.meta else '—'}}</td>
 <td class="dim">{{t.meta.converted if t.meta else '—'}}</td>
 <td>
  {% if t.verified %}<form method="post" style="display:inline"
    action="{{url_for('toggle', name=t.name)}}"><button class="ghost"
    type="submit">{{'Disable' if t.enabled else 'Enable'}}</button></form>
  {% endif %}
  <a class="btn ghost" href="{{url_for('download', name=t.name)}}">JSON</a>
  <a class="btn ghost" href="{{url_for('light_index', name=t.name)}}">index</a>
  <a class="btn ghost" href="{{url_for('manifest', name=t.name)}}">manifest</a>
  <form method="post" style="display:inline"
    action="{{url_for('remove', name=t.name)}}"
    onsubmit="return confirm('Remove {{t.name}}?')">
   <button class="warn" type="submit">Remove</button></form>
 </td></tr>{% endfor %}</table>
 <p class="dim">JSON = the flat SD-export table (TABLES.md §5), read by
 decode-only consumers. index = Scottina Light's browse-then-decode
 index (§5b); Light loads a per-PGN detail file only for what you pick.
 </p></div>{% endif %}
{% if inbox %}<h2>Inbox (hand-copied files in tables/)</h2>
<div class="card"><p class="dim">Files dropped by the Files screen's USB
import or by hand. Inert until ingested here — consumers never read the
inbox (TABLES.md §6).</p>
{% for fn in inbox %}<form method="post" style="margin:4px 0"
  action="{{url_for('ingest')}}">
 <input type="hidden" name="file" value="{{fn}}">
 <code>{{fn}}</code>
 <button type="submit">Validate &amp; ingest</button></form>
{% endfor %}</div>{% endif %}
{% endblock %}"""

_DBC = """{% extends "base" %}{% block body %}
<h1>DBC import — coming</h1>
<div class="card"><p>The DBC tab will take raw <code>.dbc</code> signal
databases through the same ingest → validate → store flow into
<code>tables/dbc/</code>, feeding the future DBC screen. Until then, park
<code>.dbc</code> files on a USB stick — the Files screen already carries
them to <code>tables/</code>.</p></div>
{% endblock %}"""


# ---------------------------------------------------------------------- app --
def create_app(activity=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    act = activity or Activity()
    app.extensions["activity"] = act
    store.ensure_dirs()
    _purge_stale_uploads()

    from jinja2 import DictLoader
    app.jinja_loader = DictLoader({
        "base": _BASE, "pgn": _PGN, "review": _REVIEW,
        "installed": _INSTALLED, "dbc": _DBC})

    def render(tpl, **kw):
        kw.setdefault("msg", request.args.get("msg"))
        kw.setdefault("err", request.args.get("err"))
        return render_template_string(
            '{% extends "' + tpl + '" %}', version=VERSION, **kw)

    @app.before_request
    def _touch():
        if request.path != "/status":    # the tile's poll isn't "activity"
            act.touch()

    @app.get("/")
    def root():
        return redirect(url_for("pgn"))

    @app.get("/status")
    def status():
        """Machine-readable service state for the Tables tile (idle clock,
        table count) — the tile mirrors, it never computes."""
        return {"version": VERSION,
                "idle_secs": round(act.idle_secs(), 1),
                "tables": len(store.list_tables()),
                "pending": len(_pending_reviews())}

    # ------------------------------------------------------------ PGN tab --
    @app.get("/pgn")
    def pgn():
        return render("pgn", tab="pgn", pending=_pending_reviews())

    @app.post("/pgn/upload")
    def upload():
        f = request.files.get("pdf")
        if f is None or not f.filename:
            return redirect(url_for("pgn", err="no file"))
        if not f.filename.lower().endswith(".pdf") \
                or f.stream.read(5) != b"%PDF-":
            return redirect(url_for("pgn", err="not a PDF (magic check)"))
        f.stream.seek(0)
        uid = uuid.uuid4().hex[:12]
        f.save(_upload_pdf(uid))
        act.job_start()
        try:
            result = _run_extraction(uid)
        finally:
            act.job_end()
        result["source_doc"] = os.path.basename(f.filename)[:120]
        tmp = _upload_extract(uid) + ".tmp"
        with open(tmp, "w") as out:
            json.dump(result, out)
        os.replace(tmp, _upload_extract(uid))
        return redirect(url_for("review", uid=uid))

    @app.get("/review/<uid>")
    def review(uid):
        if not _uid_ok(uid) or not os.path.exists(_upload_extract(uid)):
            abort(404)
        with open(_upload_extract(uid)) as f:
            ex = json.load(f)
        return render(
            "review", tab="pgn", uid=uid,
            source=ex.get("source_doc", "?"), exerr=ex.get("error"),
            pages=ex.get("pages", []),
            name=store.slugify(ex.get("source_doc", "table")),
            json_text=json.dumps(ex.get("skeleton", {"PGNs": []}), indent=2))

    @app.post("/review/<uid>/discard")
    def discard(uid):
        if _uid_ok(uid):
            _drop_upload(uid)
        return redirect(url_for("pgn", msg="review discarded"))

    @app.post("/install")
    def install():
        uid = request.form.get("uid", "")
        name = request.form.get("name", "").strip()
        raw = request.form.get("json", "")
        if not store.valid_name(name):
            return redirect(url_for("review", uid=uid,
                                    err="bad name: [a-z0-9_-], ≤64"))
        try:
            obj = json.loads(raw)
            _tables, warns = validate.validate(obj)
        except ValueError as e:          # json error or TableInvalid
            return redirect(url_for("review", uid=uid,
                                    err=f"rejected: {e}"))
        source = "manual"
        if _uid_ok(uid) and os.path.exists(_upload_extract(uid)):
            with open(_upload_extract(uid)) as f:
                source = json.load(f).get("source_doc", "manual")
        store.install(name, obj, source_doc=source,
                      converter_version=VERSION, pgn_count=len(_tables))
        if _uid_ok(uid):
            _drop_upload(uid)
        msg = f"installed {name} ({len(_tables)} PGNs)"
        if warns:
            msg += f" — {len(warns)} warning(s): " + "; ".join(warns[:3])
        return redirect(url_for("installed", msg=msg))

    # ------------------------------------------------------ Installed tab --
    @app.get("/installed")
    def installed():
        return render("installed", tab="installed",
                      tables=store.list_tables(), inbox=store.inbox_files())

    @app.post("/tables/<name>/toggle")
    def toggle(name):
        if not store.valid_name(name):
            abort(404)
        cur = next((t for t in store.list_tables() if t["name"] == name),
                   None)
        if cur is None:
            abort(404)
        new = store.set_enabled(name, not (cur["meta"] or {}).get("enabled"))
        return redirect(url_for(
            "installed",
            msg=f"{name} {'enabled' if new else 'disabled'}"))

    @app.post("/tables/<name>/remove")
    def remove(name):
        if not store.valid_name(name):
            abort(404)
        store.remove(name)
        return redirect(url_for("installed", msg=f"{name} removed"))

    @app.get("/tables/<name>/download")
    def download(name):
        if not store.valid_name(name) \
                or not os.path.exists(store.table_path(name)):
            abort(404)
        return send_file(store.table_path(name), as_attachment=True,
                         download_name=name + ".json")

    @app.get("/tables/<name>/manifest")
    def manifest(name):
        if not store.valid_name(name):
            abort(404)
        meta = store.read_meta(name)
        if meta is None:
            abort(404)
        return meta

    @app.get("/tables/<name>/index")
    def light_index(name):
        """Scottina Light's browse index for this table (TABLES.md §5b) —
        the same shared writer the USB push uses, so the two export paths
        emit byte-identical bytes."""
        if not store.valid_name(name) \
                or not os.path.exists(store.table_path(name)):
            abort(404)
        from tables import lightindex
        try:
            built = lightindex.artifacts_for_table(name)
        except validate.TableInvalid:
            abort(404)
        if built is None:
            abort(404)
        index_str, _details = built
        return app.response_class(
            index_str, mimetype="application/json",
            headers={"Content-Disposition": "attachment; "
                     f'filename="{lightindex.index_filename(name)}"'})

    @app.get("/tables/<name>/detail/<int:pgn>")
    def light_detail(name, pgn):
        """One per-PGN detail file (TABLES.md §5c): full §2 field defs."""
        if not store.valid_name(name):
            abort(404)
        from tables import lightindex
        try:
            built = lightindex.artifacts_for_table(name)
        except validate.TableInvalid:
            abort(404)
        if built is None:
            abort(404)
        _index, details = built
        fn = lightindex.detail_filename(pgn)
        if fn not in details:
            abort(404)
        return app.response_class(
            details[fn], mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    @app.post("/inbox/ingest")
    def ingest():
        fn = request.form.get("file", "")
        if fn not in store.inbox_files():     # allow-list, never a path
            abort(404)
        path = os.path.join(store.inbox_dir(), fn)
        try:
            _tables, warns = validate.validate_file(path)
        except validate.TableInvalid as e:
            return redirect(url_for("installed",
                                    err=f"{fn} rejected: {e}"))
        with open(path) as f:
            obj = json.load(f)
        name = store.slugify(fn)
        store.install(name, obj, source_doc=f"inbox:{fn}",
                      converter_version=VERSION, pgn_count=len(_tables))
        os.remove(path)                        # ingest = move (§6)
        msg = f"ingested {fn} → {name} ({len(_tables)} PGNs)"
        if warns:
            msg += f" — {len(warns)} warning(s)"
        return redirect(url_for("installed", msg=msg))

    # ------------------------------------------------------------ DBC tab --
    @app.get("/dbc")
    def dbc():
        return render("dbc", tab="dbc")

    return app


def _watchdog(act, idle_min):
    while True:
        time.sleep(15)
        if act.idle_secs() >= idle_min * 60:
            print(f"[tableconv] idle {idle_min} min — exiting", flush=True)
            # atomic store writes make an exit safe at any instant
            os._exit(0)


def main():
    ap = argparse.ArgumentParser(description="Scottina tables converter")
    ap.add_argument("--port", type=int, default=net.TABLECONV_PORT)
    ap.add_argument("--host", default="0.0.0.0")   # LAN-facing by design
    ap.add_argument("--idle-min", type=float, default=IDLE_DEFAULT_MIN)
    args = ap.parse_args()
    act = Activity()
    app = create_app(act)
    if args.idle_min > 0:
        threading.Thread(target=_watchdog, args=(act, args.idle_min),
                         daemon=True).start()
    print(f"[tableconv] http://{net.advertise_addr()}:{args.port}/ "
          f"(idle timeout {args.idle_min:g} min)", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
