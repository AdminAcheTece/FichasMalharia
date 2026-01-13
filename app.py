import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

import requests
import boto3
from botocore.config import Config

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, abort, flash
)
from flask_sqlalchemy import SQLAlchemy

# -----------------------------
# App + Config
# -----------------------------
app = Flask(__name__)

# ✅ Necessário para carrinho por sessão
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# ✅ DB (Render Postgres)
# Render costuma fornecer DATABASE_URL; às vezes vem "postgres://" e o SQLAlchemy quer "postgresql://"
db_url = os.getenv("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------
# Models
# -----------------------------
class Ficha(db.Model):
    __tablename__ = "fichas"
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(160), nullable=False)
    tipo_malha = db.Column(db.String(80), index=True)
    composicao = db.Column(db.String(140), index=True)
    gramatura = db.Column(db.Integer, index=True)
    largura = db.Column(db.Numeric(6, 2))
    categoria = db.Column(db.String(80), index=True)

    preco_centavos = db.Column(db.Integer, nullable=False)

    # caminho do arquivo no bucket (ex: "fichas/meia_malha_alg_170.pdf")
    file_key = db.Column(db.String(255), nullable=True)

    ativa = db.Column(db.Boolean, default=True, index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class Pedido(db.Model):
    __tablename__ = "pedidos"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(160), nullable=False, index=True)

    status = db.Column(db.String(20), default="pending", index=True)  # pending|paid|cancelled
    total_centavos = db.Column(db.Integer, nullable=False)

    mp_preference_id = db.Column(db.String(120), index=True)
    mp_payment_id = db.Column(db.String(120), index=True)

    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class PedidoItem(db.Model):
    __tablename__ = "pedido_itens"
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey("pedidos.id"), nullable=False, index=True)
    ficha_id = db.Column(db.Integer, db.ForeignKey("fichas.id"), nullable=False, index=True)

    titulo_snapshot = db.Column(db.String(160), nullable=False)
    preco_centavos_snapshot = db.Column(db.Integer, nullable=False)


class DownloadToken(db.Model):
    __tablename__ = "download_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(140), unique=True, nullable=False, index=True)

    pedido_id = db.Column(db.Integer, db.ForeignKey("pedidos.id"), nullable=False, index=True)
    ficha_id = db.Column(db.Integer, db.ForeignKey("fichas.id"), nullable=False, index=True)

    expira_em = db.Column(db.DateTime, nullable=False)
    downloads = db.Column(db.Integer, default=0)
    max_downloads = db.Column(db.Integer, default=5)

# -----------------------------
# Helpers (DB seed + filters)
# -----------------------------
# ✅ Seus mocks (vamos usar para "seed" inicial do banco)
FICHAS_MOCK = [
    {
        "id": 1,
        "titulo": "Meia Malha 100% Algodão 170 g/m²",
        "tipo_malha": "Meia Malha",
        "composicao": "100% Algodão",
        "gramatura": 170,
        "largura": 1.60,
        "categoria": "Camisetas",
        "preco": 29.90,
        "file_key": "fichas/meia_malha_algodao_170.pdf",
    },
    {
        "id": 2,
        "titulo": "Piquet c/ Elastano 220 g/m²",
        "tipo_malha": "Piquet",
        "composicao": "96% Algodão 4% Elastano",
        "gramatura": 220,
        "largura": 1.80,
        "categoria": "Polos",
        "preco": 39.90,
        "file_key": "fichas/piquet_elastano_220.pdf",
    },
    {
        "id": 3,
        "titulo": "Moletom 3 cabos 300 g/m²",
        "tipo_malha": "Moletom",
        "composicao": "50% Algodão 50% Poliéster",
        "gramatura": 300,
        "largura": 1.90,
        "categoria": "Moletons",
        "preco": 49.90,
        "file_key": "fichas/moletom_3_cabos_300.pdf",
    },
]

def preco_to_centavos(preco_float: float) -> int:
    return int(Decimal(str(preco_float)) * 100)

def ensure_db():
    """Cria tabelas e (opcional) faz seed das fichas se estiver vazio."""
    db.create_all()
    if Ficha.query.count() == 0:
        for f in FICHAS_MOCK:
            ficha = Ficha(
                titulo=f["titulo"],
                tipo_malha=f["tipo_malha"],
                composicao=f["composicao"],
                gramatura=f["gramatura"],
                largura=Decimal(str(f["largura"])),
                categoria=f["categoria"],
                preco_centavos=preco_to_centavos(f["preco"]),
                file_key=f.get("file_key"),
                ativa=True,
            )
            db.session.add(ficha)
        db.session.commit()

def get_distinct_filters():
    categorias = [c[0] for c in db.session.query(Ficha.categoria).filter(Ficha.ativa.is_(True)).distinct().order_by(Ficha.categoria).all() if c[0]]
    tipos = [t[0] for t in db.session.query(Ficha.tipo_malha).filter(Ficha.ativa.is_(True)).distinct().order_by(Ficha.tipo_malha).all() if t[0]]
    return categorias, tipos

def ficha_to_dict(f: Ficha):
    return {
        "id": f.id,
        "titulo": f.titulo,
        "tipo_malha": f.tipo_malha,
        "composicao": f.composicao,
        "gramatura": f.gramatura,
        "largura": float(f.largura) if f.largura is not None else None,
        "categoria": f.categoria,
        "preco": float(Decimal(f.preco_centavos) / 100),
    }

# -----------------------------
# Cart (session)
# -----------------------------
def cart_get():
    return session.get("cart", [])

def cart_set(items):
    session["cart"] = items
    session.modified = True

def cart_add(ficha_id: int):
    items = cart_get()
    if ficha_id not in items:
        items.append(ficha_id)
    cart_set(items)

def cart_remove(ficha_id: int):
    items = cart_get()
    items = [i for i in items if i != ficha_id]
    cart_set(items)

def cart_clear():
    cart_set([])

def cart_total_centavos():
    ids = cart_get()
    if not ids:
        return 0
    fichas = Ficha.query.filter(Ficha.id.in_(ids), Ficha.ativa.is_(True)).all()
    return sum(f.preco_centavos for f in fichas)

# -----------------------------
# Storage (S3/R2) presigned URL
# -----------------------------
def storage_enabled():
    return all([
        os.getenv("S3_ENDPOINT"),
        os.getenv("S3_BUCKET"),
        os.getenv("S3_ACCESS_KEY"),
        os.getenv("S3_SECRET_KEY"),
    ])

def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        config=Config(signature_version="s3v4"),
        region_name=os.getenv("S3_REGION", "auto"),
    )

def presigned_download_url(file_key: str, expires_seconds: int = 600) -> str:
    client = s3_client()
    bucket = os.getenv("S3_BUCKET")
    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": file_key},
        ExpiresIn=expires_seconds,
    )

# -----------------------------
# Email (SMTP simples)
# -----------------------------
def send_email(to_email: str, subject: str, text_body: str, html_body: str | None = None):
    """
    Envia e-mail via SMTP usando variáveis:
    MAIL_HOST, MAIL_PORT, MAIL_USER, MAIL_PASS, MAIL_FROM
    """
    host = os.getenv("MAIL_HOST")
    port = int(os.getenv("MAIL_PORT", "587"))
    user = os.getenv("MAIL_USER")
    password = os.getenv("MAIL_PASS")
    mail_from = os.getenv("MAIL_FROM")

    if not all([host, port, user, password, mail_from]):
        app.logger.warning("E-mail não enviado: variáveis MAIL_* incompletas.")
        return

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import make_msgid, formatdate
    import smtplib

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["Reply-To"] = mail_from

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(mail_from, [to_email], msg.as_string())

# -----------------------------
# Mercado Pago (Checkout + Webhook)
# -----------------------------
def mp_enabled():
    return bool(os.getenv("MP_ACCESS_TOKEN"))

def mp_create_preference(pedido: Pedido, itens: list[PedidoItem]):
    """
    Cria preferência de pagamento via API REST do Mercado Pago.
    Retorna dict com 'id' e 'init_point' (link de pagamento).
    """
    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    items_payload = []
    for it in itens:
        # MP pede price unit em float (BRL)
        unit_price = float(Decimal(it.preco_centavos_snapshot) / 100)
        items_payload.append({
            "title": it.titulo_snapshot,
            "quantity": 1,
            "unit_price": unit_price,
            "currency_id": "BRL",
        })

    base_url = os.getenv("BASE_URL", "").rstrip("/")  # ex: https://fichasdemalharia.com.br
    if not base_url:
        # fallback: tenta montar do request, mas no Render pode variar; melhor setar BASE_URL
        base_url = "http://localhost:5000"

    notification_url = f"{base_url}/mp/webhook"

    payload = {
        "items": items_payload,
        "external_reference": str(pedido.id),
        "notification_url": notification_url,
        "payer": {"email": pedido.email},
        "back_urls": {
            "success": f"{base_url}/pedido/{pedido.id}/sucesso",
            "failure": f"{base_url}/pedido/{pedido.id}/falha",
            "pending": f"{base_url}/pedido/{pedido.id}/pendente",
        },
        "auto_return": "approved",
    }

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def mp_fetch_payment(payment_id: str):
    access_token = os.getenv("MP_ACCESS_TOKEN")
    r = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

# -----------------------------
# Routes (mantidas + novas)
# -----------------------------
@app.before_request
def _bootstrap():
    # Garante tabelas e seed das fichas (uma vez)
    ensure_db()

@app.get("/")
def home():
    categorias, tipos = get_distinct_filters()
    return render_template("home.html", categorias=categorias, tipos=tipos)

@app.get("/busca")
def busca():
    tipo = (request.args.get("tipo") or "").strip()
    categoria = (request.args.get("categoria") or "").strip()
    comp = (request.args.get("composicao") or "").strip()
    gmin = request.args.get("gramatura_min", type=int)
    gmax = request.args.get("gramatura_max", type=int)

    q = Ficha.query.filter(Ficha.ativa.is_(True))

    if tipo:
        q = q.filter(Ficha.tipo_malha == tipo)
    if categoria:
        q = q.filter(Ficha.categoria == categoria)
    if comp:
        q = q.filter(Ficha.composicao.ilike(f"%{comp}%"))
    if gmin is not None:
        q = q.filter(Ficha.gramatura >= gmin)
    if gmax is not None:
        q = q.filter(Ficha.gramatura <= gmax)

    resultados = [ficha_to_dict(f) for f in q.order_by(Ficha.id.desc()).all()]
    categorias, tipos = get_distinct_filters()

    # ✅ Carrinho para o template (para mostrar "No carrinho" e contador)
    cart_ids = cart_get()

    return render_template(
        "busca.html",
        fichas=resultados,
        filtros={
            "tipo": tipo,
            "categoria": categoria,
            "composicao": comp,
            "gramatura_min": gmin,
            "gramatura_max": gmax
        },
        categorias=categorias,
        tipos=tipos,
        cart_ids=cart_ids,          # ✅ necessário para checar "f.id in cart_ids"
        cart_count=len(cart_ids),   # ✅ badge/contador
    )

from decimal import Decimal
from flask import session, redirect, request, url_for, render_template, abort, flash

# -----------------------------
# Cart helpers (session)
# -----------------------------
def cart_get():
    """
    Retorna sempre uma lista de IDs INT (sem duplicar).
    Isso evita bug no template (f.id in cart_ids) e no SQL IN().
    """
    raw = session.get("cart", [])
    ids = []
    for x in raw:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    # remove duplicados mantendo ordem
    seen = set()
    normalized = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            normalized.append(i)
    session["cart"] = normalized  # normaliza e salva de volta
    session.modified = True
    return normalized

def cart_set(items):
    session["cart"] = [int(x) for x in items]
    session.modified = True

def cart_add(ficha_id: int):
    items = cart_get()
    if ficha_id not in items:
        items.append(int(ficha_id))
    cart_set(items)

def cart_remove(ficha_id: int):
    items = cart_get()
    items = [i for i in items if i != int(ficha_id)]
    cart_set(items)

def cart_clear():
    cart_set([])

def cart_total_centavos():
    ids = cart_get()
    if not ids:
        return 0
    fichas = Ficha.query.filter(Ficha.id.in_(ids), Ficha.ativa.is_(True)).all()
    return sum(f.preco_centavos for f in fichas)

@app.context_processor
def inject_cart_count():
    try:
        return {"cart_count": len(cart_get())}
    except Exception:
        return {"cart_count": 0}

# -----------------------------
# Rotas do Carrinho
# -----------------------------
@app.post("/cart/add/<int:ficha_id>")
def cart_add_route(ficha_id):
    ficha = Ficha.query.get_or_404(ficha_id)
    if not ficha.ativa:
        abort(404)
    cart_add(ficha_id)
    return redirect(request.referrer or url_for("busca"))

@app.post("/cart/remove/<int:ficha_id>")
def cart_remove_route(ficha_id):
    cart_remove(ficha_id)
    return redirect(request.referrer or url_for("cart_view"))

@app.get("/cart")
def cart_view():
    ids = cart_get()
    fichas = Ficha.query.filter(Ficha.id.in_(ids), Ficha.ativa.is_(True)).all() if ids else []
    fichas_dict = [ficha_to_dict(f) for f in fichas]
    total_cent = sum(f.preco_centavos for f in fichas)
    total = float(Decimal(total_cent) / 100)

    return render_template("cart.html", fichas=fichas_dict, total=total)

# -----------------------------
# Checkout (cria pedido + preferência MP)
# -----------------------------
@app.post("/checkout")
def checkout():
    if not mp_enabled():
        return "Mercado Pago não configurado (MP_ACCESS_TOKEN).", 500

    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Informe um e-mail válido para receber os links de download.", "error")
        return redirect(url_for("cart_view"))

    ids = cart_get()  # ✅ já vem normalizado em INT
    if not ids:
        flash("Seu carrinho está vazio.", "error")
        return redirect(url_for("busca"))

    fichas = Ficha.query.filter(Ficha.id.in_(ids), Ficha.ativa.is_(True)).all()
    if not fichas:
        flash("Não foi possível validar os itens do carrinho.", "error")
        return redirect(url_for("busca"))

    total_cent = sum(f.preco_centavos for f in fichas)

    pedido = Pedido(email=email, status="pending", total_centavos=total_cent)
    db.session.add(pedido)
    db.session.flush()  # obtém pedido.id

    itens = []
    for f in fichas:
        it = PedidoItem(
            pedido_id=pedido.id,
            ficha_id=f.id,
            titulo_snapshot=f.titulo,
            preco_centavos_snapshot=f.preco_centavos
        )
        db.session.add(it)
        itens.append(it)

    db.session.commit()

    pref = mp_create_preference(pedido, itens)
    pedido.mp_preference_id = pref.get("id")
    db.session.commit()

    cart_clear()

    init_point = pref.get("init_point")
    if not init_point:
        return "Não foi possível gerar link de pagamento.", 500

    return redirect(init_point)

# ---- Páginas de retorno do MP (opcionais, mas úteis) ----
@app.get("/pedido/<int:pedido_id>/sucesso")
def pedido_sucesso(pedido_id):
    return render_template("pedido_status.html", status="success", pedido_id=pedido_id)

@app.get("/pedido/<int:pedido_id>/falha")
def pedido_falha(pedido_id):
    return render_template("pedido_status.html", status="failure", pedido_id=pedido_id)

@app.get("/pedido/<int:pedido_id>/pendente")
def pedido_pendente(pedido_id):
    return render_template("pedido_status.html", status="pending", pedido_id=pedido_id)

# ---- Webhook Mercado Pago ----
@app.post("/mp/webhook")
def mp_webhook():
    # MP pode mandar parâmetros diferentes dependendo do tipo de notificação.
    # Vamos tentar cobrir os formatos mais comuns:
    payload = request.get_json(silent=True) or {}
    payment_id = None

    # Formato comum: ?type=payment&data.id=123
    payment_id = request.args.get("data.id") or request.args.get("id") or payment_id

    # Formato JSON: {"type":"payment","data":{"id":"123"}}
    if not payment_id:
        data = payload.get("data") or {}
        payment_id = data.get("id") or payload.get("id")

    if not payment_id:
        # sem id, não dá para processar
        return ("ok", 200)

    try:
        pay = mp_fetch_payment(str(payment_id))
    except Exception as e:
        app.logger.error(f"Erro ao consultar pagamento {payment_id}: {e}")
        return ("ok", 200)

    status = pay.get("status")  # approved, pending, rejected...
    external_reference = pay.get("external_reference")  # colocamos como pedido.id

    if not external_reference:
        return ("ok", 200)

    pedido = Pedido.query.get(int(external_reference))
    if not pedido:
        return ("ok", 200)

    # Idempotência: se já está pago, só devolve ok
    if pedido.status == "paid":
        return ("ok", 200)

    if status == "approved":
        pedido.status = "paid"
        pedido.mp_payment_id = str(payment_id)
        db.session.commit()

        # cria tokens + envia e-mail
        try:
            gerar_tokens_e_enviar_email(pedido.id)
        except Exception as e:
            app.logger.error(f"Erro ao gerar tokens/enviar e-mail pedido {pedido.id}: {e}")

    return ("ok", 200)

def gerar_tokens_e_enviar_email(pedido_id: int):
    pedido = Pedido.query.get_or_404(pedido_id)
    if pedido.status != "paid":
        return

    itens = PedidoItem.query.filter_by(pedido_id=pedido.id).all()
    if not itens:
        return

    # Expiração padrão: 30 dias
    dias = int(os.getenv("DOWNLOAD_TOKEN_DAYS", "30"))
    expira = datetime.utcnow() + timedelta(days=dias)
    max_downloads = int(os.getenv("DOWNLOAD_MAX", "5"))

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = "http://localhost:5000"

    links = []
    for it in itens:
        ficha = Ficha.query.get(it.ficha_id)
        if not ficha or not ficha.file_key:
            # Se não tiver arquivo, pula (ou você pode travar e alertar)
            continue

        token_str = secrets.token_urlsafe(32)
        t = DownloadToken(
            token=token_str,
            pedido_id=pedido.id,
            ficha_id=ficha.id,
            expira_em=expira,
            downloads=0,
            max_downloads=max_downloads,
        )
        db.session.add(t)
        links.append((ficha.titulo, f"{base_url}/download/{token_str}"))

    db.session.commit()

    if not links:
        return

    subject = "Compra aprovada — suas fichas técnicas"

    text_lines = [
        "Pagamento aprovado ✅",
        "",
        "Aqui estão seus links para baixar as fichas (os links expiram):",
        ""
    ]
    for titulo, link in links:
        text_lines.append(f"- {titulo}: {link}")
    text_lines.append("")
    text_lines.append("Se precisar de ajuda, responda este e-mail.")

    text_body = "\n".join(text_lines)

    # HTML simples (sem exagero para reduzir risco de spam)
    html_items = "".join([f"<li><strong>{titulo}</strong><br><a href='{link}'>{link}</a></li>" for titulo, link in links])
    html_body = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.5;">
      <h2>Pagamento aprovado ✅</h2>
      <p>Aqui estão seus links para baixar as fichas (os links expiram):</p>
      <ul>{html_items}</ul>
      <p>Se precisar de ajuda, responda este e-mail.</p>
    </div>
    """

    send_email(pedido.email, subject, text_body, html_body)

# ---- Download seguro ----
@app.get("/download/<token>")
def download(token):
    t = DownloadToken.query.filter_by(token=token).first()
    if not t:
        abort(404)

    if datetime.utcnow() > t.expira_em:
        abort(410)  # expirou

    if t.downloads >= t.max_downloads:
        abort(429)

    pedido = Pedido.query.get(t.pedido_id)
    if not pedido or pedido.status != "paid":
        abort(403)

    ficha = Ficha.query.get(t.ficha_id)
    if not ficha or not ficha.ativa or not ficha.file_key:
        abort(404)

    # incrementa contador
    t.downloads += 1
    db.session.commit()

    # Se storage não estiver configurado, falha claramente
    if not storage_enabled():
        return "Storage não configurado (S3_ENDPOINT/S3_BUCKET/S3_ACCESS_KEY/S3_SECRET_KEY).", 500

    url = presigned_download_url(ficha.file_key, expires_seconds=600)
    return redirect(url)

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
