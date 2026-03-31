import os, time, uuid
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_file
import psycopg2, psycopg2.extras, bcrypt, jwt, requests as http

app = Flask(__name__, static_folder="static")
SECRET_KEY = os.environ.get("SECRET_KEY", "modely-dev-secret")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
FAL_KEY = os.environ.get("FAL_KEY", "")


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

    def elapsed():
        return f"{time.time()-t0:.1f}s"

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
