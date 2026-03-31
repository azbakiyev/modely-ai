import os, time, uuid
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_file, Response
import psycopg2, psycopg2.extras, bcrypt, jwt, requests as http

app = Flask(__name__, static_folder="static")
SECRET_KEY = os.environ.get("SECRET_KEY", "modely-dev-secret")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
FAL_KEY = os.environ.get("FAL_KEY", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "modelyadmin2026")
APP_URL = os.environ.get("APP_URL", "https://modely-ai-production.up.railway.app")

WELCOME_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:40px">
<div style="max-width:520px;margin:0 auto;background:#161b22;border-radius:16px;padding:40px;border:1px solid #30363d">
  <div style="font-size:32px;font-weight:800;margin-bottom:8px">modely<span style="color:#8b5cf6">.ai</span></div>
  <h2 style="font-size:22px;margin:0 0 16px">Welcome! You're all set 🎉</h2>
  <p style="color:#8b949e;line-height:1.6">We've added <strong style="color:#22c55e">3 free credits</strong> to your account to get started.</p>
  <div style="background:#0d1117;border-radius:12px;padding:24px;margin:24px 0;text-align:center">
    <div style="font-size:56px;font-weight:800;color:#8b5cf6">3</div>
    <div style="color:#8b949e;font-size:14px;margin-top:4px">free 3D generations</div>
  </div>
  <p style="color:#8b949e;line-height:1.6;font-size:14px">Each generation creates a print-ready STL file from your prompt or photo. Takes about 10 minutes per model.</p>
  <p style="color:#8b949e;line-height:1.6;font-size:14px;margin-top:12px">You can preview your model in 3D directly in the browser and download the STL for any slicer (Bambu Studio, OrcaSlicer, Cura, etc).</p>
  <a href="{app_url}" style="display:block;background:linear-gradient(135deg,#8b5cf6,#3b82f6);color:#fff;text-decoration:none;padding:14px;border-radius:10px;text-align:center;font-weight:600;margin-top:28px;font-size:15px">Start Generating &#x2192;</a>
  <p style="color:#30363d;font-size:12px;text-align:center;margin-top:28px">modely.ai &mdash; AI 3D model generator for 3D printing</p>
</div>
</body></html>
"""


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        credits INTEGER DEFAULT 3,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS generations (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        prompt TEXT,
        status VARCHAR(50) DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.commit()
    conn.close()


def send_welcome_email(email):
    if not RESEND_KEY:
        return
    try:
        http.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
            json={
                "from": "modely.ai <onboarding@resend.dev>",
                "to": [email],
                "subject": "Welcome to modely.ai — 3 free 3D generations inside!",
                "html": WELCOME_HTML.format(app_url=APP_URL)
            },
            timeout=10
        )
    except Exception as e:
        print(f"Email error: {e}")


def require_auth(f):
    @wraps(f)
    def d(*a, **kw):
        tok = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not tok:
            return jsonify({"error": "Auth required"}), 401
        try:
            data = jwt.decode(tok, SECRET_KEY, algorithms=["HS256"])
            request.user_id = data["user_id"]
        except Exception:
            return jsonify({"error": "Invalid token"}), 401
        return f(*a, **kw)
    d.__name__ = f.__name__
    return d


@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.json or {}
    email = d.get("email", "").strip().lower()
    pw = d.get("password", "")
    if not email or not pw:
        return jsonify({"error": "Email and password required"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password min 6 characters"}), 400
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO users (email,password_hash) VALUES (%s,%s) RETURNING id,credits", (email, h))
        row = c.fetchone()
        conn.commit()
        conn.close()
        token = jwt.encode({"user_id": row["id"], "exp": datetime.utcnow() + timedelta(days=30)}, SECRET_KEY, algorithm="HS256")
        send_welcome_email(email)
        return jsonify({"token": token, "credits": row["credits"], "email": email})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Email already registered"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    email = d.get("email", "").strip().lower()
    pw = d.get("password", "")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id,password_hash,credits FROM users WHERE email=%s", (email,))
        row = c.fetchone()
        conn.close()
        if not row or not bcrypt.checkpw(pw.encode(), row["password_hash"].encode()):
            return jsonify({"error": "Invalid email or password"}), 401
        token = jwt.encode({"user_id": row["id"], "exp": datetime.utcnow() + timedelta(days=30)}, SECRET_KEY, algorithm="HS256")
        return jsonify({"token": token, "credits": row["credits"], "email": email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/me")
@require_auth
def me():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT email,credits FROM users WHERE id=%s", (request.user_id,))
    row = c.fetchone()
    conn.close()
    return jsonify({"email": row["email"], "credits": row["credits"]})


@app.route("/api/upload", methods=["POST"])
@require_auth
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"})
    f = request.files["file"]
    p = f"/tmp/up_{uuid.uuid4().hex[:8]}{Path(f.filename).suffix.lower()}"
    f.save(p)
    return jsonify({"saved_path": p})


@app.route("/api/run", methods=["POST"])
@require_auth
def run():
    import fal_client, trimesh, numpy as np
    d = request.json or {}
    action = d.get("action")
    params = d.get("params", {})
    t0 = time.time()
    def elapsed(): return f"{time.time()-t0:.1f}s"

    if action in {"generate_image", "hunyuan3d"}:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT credits FROM users WHERE id=%s", (request.user_id,))
        row = c.fetchone()
        conn.close()
        if not row or row["credits"] <= 0:
            return jsonify({"error": "no_credits"}), 402

    try:
        os.environ["FAL_KEY"] = FAL_KEY

        if action == "generate_image":
            res = fal_client.subscribe("fal-ai/nano-banana-2", arguments={
                "prompt": params["prompt"], "aspect_ratio": "1:1",
                "resolution": "1K", "output_format": "png", "num_images": 1})
            url = (res.get("images") or [{}])[0].get("url", "")
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE users SET credits=credits-1 WHERE id=%s", (request.user_id,))
            c.execute("INSERT INTO generations (user_id,prompt,status) VALUES (%s,%s,'image')",
                      (request.user_id, params.get("prompt", "")[:500]))
            conn.commit(); conn.close()
            return jsonify({"image_url": url, "time": elapsed()})

        elif action == "prepare_file":
            return jsonify({"image_url": fal_client.upload_file(params["file_path"]), "time": elapsed()})

        elif action == "denoise":
            res = fal_client.subscribe("fal-ai/nano-banana-2/edit", arguments={
                "prompt": "clean background: pure white, isolate main object, sharp edges, no shadows, centered",
                "image_urls": [params["image_url"]], "aspect_ratio": "1:1",
                "resolution": "1K", "num_images": 1})
            url = (res.get("images") or [{}])[0].get("url", "")
            return jsonify({"image_url": url, "time": elapsed()})

        elif action == "hunyuan3d":
            res = fal_client.subscribe("fal-ai/hunyuan3d-v3/image-to-3d", arguments={
                "input_image_url": params["image_url"],
                "generate_type": "Geometry", "face_count": 50000, "enable_pbr": False})
            glb_url = res.get("model_glb", {}).get("url", "")
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE users SET credits=credits-1 WHERE id=%s", (request.user_id,))
            c.execute("INSERT INTO generations (user_id,prompt,status) VALUES (%s,%s,'3d')",
                      (request.user_id, params.get("image_url", "")[:500]))
            conn.commit(); conn.close()
            return jsonify({"glb_url": glb_url, "time": elapsed()})

        elif action == "convert":
            data = None
            for _ in range(5):
                r = http.get(params["glb_url"], timeout=60)
                if r.status_code == 200:
                    data = r.content
                    break
                time.sleep(15)
            if not data:
                return jsonify({"error": "Failed to download GLB"})
            tmp = f"/tmp/m_{uuid.uuid4().hex[:8]}.glb"
            Path(tmp).write_bytes(data)
            mesh = trimesh.load(tmp)
            if isinstance(mesh, trimesh.Scene):
                parts = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
                mesh = trimesh.util.concatenate(parts) if parts else list(mesh.geometry.values())[0]
            out_stl = f"/tmp/m_{uuid.uuid4().hex[:8]}.stl"
            mesh.export(out_stl)
            return jsonify({"stl_path": out_stl, "time": elapsed()})

        elif action == "scale":
            mesh = trimesh.load(params["stl_path"])
            dims = mesh.bounds[1] - mesh.bounds[0]
            if dims.max() > 0:
                mesh.apply_scale(float(params.get("height_mm", 80)) / dims.max())
            out_stl = params["stl_path"].replace(".stl", "_s.stl")
            mesh.export(out_stl)
            return jsonify({"stl_path": out_stl, "time": elapsed()})

        elif action == "analyze":
            mesh = trimesh.load(params["stl_path"])
            dims = mesh.bounds[1] - mesh.bounds[0]
            return jsonify({"watertight": bool(mesh.is_watertight), "faces": len(mesh.faces),
                "dims_x": round(float(dims[0]), 1), "dims_y": round(float(dims[1]), 1),
                "dims_z": round(float(dims[2]), 1), "time": elapsed()})

        elif action == "printability":
            mesh = trimesh.load(params["stl_path"])
            angles = np.degrees(np.arccos(np.clip(np.dot(mesh.face_normals, [0, 1, 0]), -1, 1)))
            ov = float(np.mean(angles > 45) * 100)
            score = max(0, min(100, int(100 - min(40, ov * 0.8) - (5 if not mesh.is_watertight else 0))))
            return jsonify({"score": score, "overhang_pct": round(ov, 1),
                "watertight": bool(mesh.is_watertight), "time": elapsed(),
                "verdict": ("Excellent — ready to print" if score >= 85 else
                    "Good — minor adjustments" if score >= 65 else
                    "Moderate — review settings" if score >= 45 else
                    "Complex — significant prep needed")})

        return jsonify({"error": f"Unknown: {action}"})
    except Exception as e:
        return jsonify({"error": str(e), "time": elapsed()})


@app.route("/api/download", methods=["POST"])
@require_auth
def download():
    p = (request.json or {}).get("stl_path", "")
    if not os.path.exists(p):
        return jsonify({"error": "File not found"}), 404
    return send_file(p, mimetype="application/octet-stream",
                     as_attachment=True, download_name="model_3d.stl")


@app.route("/admin")
def admin_page():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW:
        return Response("Access denied. Add ?pw=YOUR_PASSWORD", 401)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM users")
        total_users = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at > NOW() - INTERVAL '24 hours'")
        new_today = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM generations")
        total_gens = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM generations WHERE created_at > NOW() - INTERVAL '24 hours'")
        gens_today = c.fetchone()["cnt"]
        c.execute("SELECT COALESCE(SUM(credits),0) as s FROM users")
        total_credits = c.fetchone()["s"]
        c.execute("""
            SELECT u.email, u.credits, u.created_at,
                COUNT(g.id) as gen_count
            FROM users u
            LEFT JOIN generations g ON g.user_id = u.id
            GROUP BY u.id, u.email, u.credits, u.created_at
            ORDER BY u.created_at DESC LIMIT 200""")
        users = c.fetchall()
        conn.close()
    except Exception as e:
        return f"DB Error: {e}", 500

    rows = "".join([
        f'<tr><td>{u["email"]}</td><td style="color:#8b5cf6;font-weight:600">{u["credits"]}</td>'
        f'<td>{u["gen_count"]}</td><td style="color:#8b949e">{str(u["created_at"])[:16]}</td></tr>'
        for u in users
    ])

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>modely.ai admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:28px;min-height:100vh}}
h1{{font-size:22px;font-weight:800;margin-bottom:24px}}
h1 em{{color:#8b5cf6;font-style:normal}}
.stats{{display:flex;gap:14px;margin-bottom:28px;flex-wrap:wrap}}
.stat{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:18px 24px;min-width:110px}}
.sn{{font-size:34px;font-weight:800;color:#8b5cf6;line-height:1}}
.sl{{font-size:11px;color:#8b949e;margin-top:5px;text-transform:uppercase;letter-spacing:.05em}}
table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:12px;overflow:hidden;border:1px solid #30363d}}
th{{background:#21262d;padding:11px 16px;text-align:left;font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:10px 16px;border-top:1px solid #21262d;font-size:13px}}
tr:hover td{{background:#1c2128}}
.badge{{background:rgba(139,92,246,.15);color:#8b5cf6;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:600}}
</style></head><body>
<h1>modely<em>.ai</em> &mdash; Admin</h1>
<div class="stats">
  <div class="stat"><div class="sn">{total_users}</div><div class="sl">Total Users</div></div>
  <div class="stat"><div class="sn">{new_today}</div><div class="sl">New Today</div></div>
  <div class="stat"><div class="sn">{total_gens}</div><div class="sl">Total Gens</div></div>
  <div class="stat"><div class="sn">{gens_today}</div><div class="sl">Gens Today</div></div>
  <div class="stat"><div class="sn">{total_credits}</div><div class="sl">Credits Left</div></div>
</div>
<table>
<thead><tr><th>Email</th><th>Credits</th><th>Generations</th><th>Joined</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


with app.app_context():
    if DATABASE_URL:
        try:
            init_db()
        except Exception as e:
            print(f"DB init warning: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
