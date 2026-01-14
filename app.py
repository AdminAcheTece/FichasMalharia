import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
import requests
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
    file_key = db.Column(db.String(255), nullable=True)  # ex: "fichas/arquivo.pdf"

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


class PedidoAccessToken(db.Model):
    """
    ✅ Token único para a página "Minha compra"
    """
    __tablename__ = "pedido_access_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(140), unique=True, nullable=False, index=True)

    pedido_id = db.Column(db.Integer, db.ForeignKey("pedidos.id"), nullable=False, index=True)

    expira_em = db.Column(db.DateTime, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

# -----------------------------
# Helpers
# -----------------------------
FICHAS_MOCK = [
    {
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
    categorias = [
        c[0] for c in db.session.query(Ficha.categoria)
        .filter(Ficha.ativa.is_(True))
        .distinct().order_by(Ficha.categoria).all()
        if c[0]
    ]
    tipos = [
        t[0] for t in db.session.query(Ficha.tipo_malha)
        .filter(Ficha.ativa.is_(True))
        .distinct().order_by(Ficha.tipo_malha).all()
        if t[0]
    ]
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
        "preco_centavos": f.preco_centavos,
    }

def format_brl_from_centavos(v: int) -> str:
    return f"R$ {float(Decimal(v)/100):.2f}".replace(".", ",")

def mask_email(email: str) -> str:
    try:
        user, dom = email.split("@", 1)
        if len(user) <= 2:
            user_mask = user[0] + "*"
        else:
            user_mask = user[:2] + "*" * max(3, len(user)-2)
        return f"{user_mask}@{dom}"
    except Exception:
        return email

def get_base_url() -> str:
    env = (os.getenv("BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    try:
        return request.host_url.rstrip("/")
    except Exception:
        return "http://localhost:5000"

# -----------------------------
# Carrinho (session) - NORMALIZADO
# -----------------------------
def cart_get():
    raw = session.get("cart", [])
    ids = []
    for x in raw:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue

    seen = set()
    normalized = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            normalized.append(i)

    session["cart"] = normalized
    session.modified = True
    return normalized

def cart_set(items):
    session["cart"] = [int(x) for x in items]
    session.modified = True

def cart_add(ficha_id: int):
    items = cart_get()
    ficha_id = int(ficha_id)
    if ficha_id not in items:
        items.append(ficha_id)
    cart_set(items)

def cart_remove(ficha_id: int):
    items = cart_get()
    ficha_id = int(ficha_id)
    items = [i for i in items if i != ficha_id]
    cart_set(items)

def cart_clear():
    cart_set([])

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

def s3_key_exists(bucket: str, key: str) -> bool:
    try:
        s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False

def presigned_download_url(file_key: str, expires_seconds: int = 600) -> str:
    client = s3_client()
    bucket = os.getenv("S3_BUCKET")
    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": file_key},
        ExpiresIn=expires_seconds,
    )

def resolve_preview_key(file_key: str) -> str | None:
    """
    Procura um arquivo de preview por convenção:
    - fichas/abc.pdf -> fichas/abc_preview.pdf
    - fichas/abc.pdf -> fichas/abc-preview.pdf
    - fichas/abc.pdf -> previews/abc.pdf
    """
    if not file_key or not file_key.lower().endswith(".pdf"):
        return None

    base = file_key[:-4]
    candidates = [
        f"{base}_preview.pdf",
        f"{base}-preview.pdf",
        f"previews/{file_key.split('/')[-1]}",
    ]

    bucket = os.getenv("S3_BUCKET")
    for k in candidates:
        if s3_key_exists(bucket, k):
            return k
    return None

# -----------------------------
# Email (SMTP)
# -----------------------------
def send_email(to_email: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    host = os.getenv("MAIL_HOST")
    port = int(os.getenv("MAIL_PORT", "587"))
    user = os.getenv("MAIL_USER")
    password = os.getenv("MAIL_PASS")
    mail_from = os.getenv("MAIL_FROM")

    if not all([host, port, user, password, mail_from]):
        app.logger.warning("E-mail não enviado: variáveis MAIL_* incompletas.")
        return False

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

    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(mail_from, [to_email], msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f"Erro ao enviar e-mail: {e}")
        return False

# -----------------------------
# Mercado Pago
# -----------------------------
def mp_enabled():
    return bool(os.getenv("MP_ACCESS_TOKEN"))

def mp_create_preference(pedido: Pedido, itens: list[PedidoItem]):
    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    items_payload = []
    for it in itens:
        unit_price = float(Decimal(it.preco_centavos_snapshot) / 100)
        items_payload.append({
            "title": it.titulo_snapshot,
            "quantity": 1,
            "unit_price": unit_price,
            "currency_id": "BRL",
        })

    base_url = get_base_url()
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
# Bootstrap: seed 1x por worker
# -----------------------------
@app.before_request
def _bootstrap():
    if not getattr(app, "_db_ready", False):
        ensure_db()
        app._db_ready = True

# -----------------------------
# Contexto global
# -----------------------------
@app.context_processor
def inject_globals():
    support_email = os.getenv("SUPPORT_EMAIL", "suporte@fichasdemalharia.com.br")
    support_whatsapp = os.getenv("SUPPORT_WHATSAPP", "")
    site_last_update = os.getenv("SITE_LAST_UPDATE", "2026-01-13")

    try:
        cart_count = len(cart_get())
    except Exception:
        cart_count = 0

    return {
        "support_email": support_email,
        "support_whatsapp": support_whatsapp,
        "site_last_update": site_last_update,
        "cart_count": cart_count,
        "current_path": getattr(request, "path", "/"),
    }

# -----------------------------
# Tokens (Pedido + Download)
# -----------------------------
def get_or_create_pedido_access_token(pedido_id: int) -> PedidoAccessToken:
    now = datetime.utcnow()

    existing = (
        PedidoAccessToken.query
        .filter_by(pedido_id=pedido_id)
        .order_by(PedidoAccessToken.expira_em.desc())
        .first()
    )
    if existing and existing.expira_em > now:
        return existing

    days = int(os.getenv("PURCHASE_PAGE_DAYS", "90"))
    expira = now + timedelta(days=days)

    t = PedidoAccessToken(
        token=secrets.token_urlsafe(32),
        pedido_id=pedido_id,
        expira_em=expira,
    )
    db.session.add(t)
    db.session.commit()
    return t

def get_or_create_download_token(pedido_id: int, ficha_id: int) -> DownloadToken:
    now = datetime.utcnow()
    max_downloads = int(os.getenv("DOWNLOAD_MAX", "5"))
    dias = int(os.getenv("DOWNLOAD_TOKEN_DAYS", "30"))
    expira = now + timedelta(days=dias)

    existing = (
        DownloadToken.query
        .filter_by(pedido_id=pedido_id, ficha_id=ficha_id)
        .order_by(DownloadToken.expira_em.desc())
        .first()
    )
    if existing and existing.expira_em > now and existing.downloads < existing.max_downloads:
        return existing

    token_str = secrets.token_urlsafe(32)
    t = DownloadToken(
        token=token_str,
        pedido_id=pedido_id,
        ficha_id=ficha_id,
        expira_em=expira,
        downloads=0,
        max_downloads=max_downloads,
    )
    db.session.add(t)
    db.session.commit()
    return t

# -----------------------------
# Rotas principais
# -----------------------------
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
        cart_ids=cart_ids,
        results_count=len(resultados),
    )

# ✅ HOME removida: agora "/" usa a página BUSCA (mesmo código / mesma renderização)
@app.get("/")
def home():
    return busca()

# -----------------------------
# Detalhes da ficha (produto)
# -----------------------------
@app.get("/ficha/<int:ficha_id>")
def ficha_detalhe(ficha_id):
    f = Ficha.query.get_or_404(ficha_id)
    if not f.ativa:
        abort(404)

    ficha = ficha_to_dict(f)
    cart_ids = cart_get()

    preview_url = None
    if storage_enabled() and f.file_key:
        preview_key = resolve_preview_key(f.file_key)
        if preview_key:
            preview_url = presigned_download_url(preview_key, expires_seconds=300)

    return render_template(
        "ficha.html",
        ficha=ficha,
        in_cart=(ficha_id in cart_ids),
        preview_url=preview_url,
    )

# -----------------------------
# Carrinho
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
# Checkout
# -----------------------------
@app.post("/checkout")
def checkout():
    if not mp_enabled():
        return "Mercado Pago não configurado (MP_ACCESS_TOKEN).", 500

    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Informe um e-mail válido para receber os links de download.", "error")
        return redirect(url_for("cart_view"))

    ids = cart_get()
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
    db.session.flush()

    itens: list[PedidoItem] = []
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

# -----------------------------
# Retornos Mercado Pago
# -----------------------------
@app.get("/pedido/<int:pedido_id>/sucesso")
def pedido_sucesso(pedido_id):
    return render_template("pedido_status.html", status="success", pedido_id=pedido_id)

@app.get("/pedido/<int:pedido_id>/falha")
def pedido_falha(pedido_id):
    return render_template("pedido_status.html", status="failure", pedido_id=pedido_id)

@app.get("/pedido/<int:pedido_id>/pendente")
def pedido_pendente(pedido_id):
    return render_template("pedido_status.html", status="pending", pedido_id=pedido_id)

# -----------------------------
# Webhook Mercado Pago
# -----------------------------
@app.post("/mp/webhook")
def mp_webhook():
    payload = request.get_json(silent=True) or {}

    payment_id = request.args.get("data.id") or request.args.get("id")
    if not payment_id:
        data = payload.get("data") or {}
        payment_id = data.get("id") or payload.get("id")

    if not payment_id:
        return ("ok", 200)

    try:
        pay = mp_fetch_payment(str(payment_id))
    except Exception as e:
        app.logger.error(f"Erro ao consultar pagamento {payment_id}: {e}")
        return ("ok", 200)

    status = pay.get("status")
    external_reference = pay.get("external_reference")

    if not external_reference:
        return ("ok", 200)

    pedido = Pedido.query.get(int(external_reference))
    if not pedido:
        return ("ok", 200)

    if pedido.status == "paid":
        return ("ok", 200)

    if status == "approved":
        pedido.status = "paid"
        pedido.mp_payment_id = str(payment_id)
        db.session.commit()

        try:
            gerar_links_e_enviar_email(pedido.id)
        except Exception as e:
            app.logger.error(f"Erro ao montar links/enviar e-mail pedido {pedido.id}: {e}")

    return ("ok", 200)

def gerar_links_e_enviar_email(pedido_id: int):
    pedido = Pedido.query.get_or_404(pedido_id)
    if pedido.status != "paid":
        return

    itens = PedidoItem.query.filter_by(pedido_id=pedido.id).all()
    if not itens:
        return

    base_url = get_base_url()

    ptoken = get_or_create_pedido_access_token(pedido.id)
    minha_compra_url = f"{base_url}/minha-compra/{ptoken.token}"

    links = []
    for it in itens:
        ficha = Ficha.query.get(it.ficha_id)
        if not ficha or not ficha.file_key:
            continue
        dt = get_or_create_download_token(pedido.id, ficha.id)
        links.append((ficha.titulo, f"{base_url}/download/{dt.token}"))

    subject = "Compra aprovada — acesso às suas fichas"

    text_lines = [
        "Pagamento aprovado ✅",
        "",
        "Acesse sua página de compra (recomendado):",
        minha_compra_url,
        "",
        "Se preferir, links diretos de download:",
        ""
    ]
    for titulo, link in links:
        text_lines.append(f"- {titulo}: {link}")
    text_lines.append("")
    text_lines.append(f"Suporte: {os.getenv('SUPPORT_EMAIL', 'suporte@fichasdemalharia.com.br')}")

    text_body = "\n".join(text_lines)

    html_items = "".join([
        f"<li><strong>{titulo}</strong><br><a href='{link}'>{link}</a></li>"
        for titulo, link in links
    ])

    html_body = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.5;">
      <h2>Pagamento aprovado ✅</h2>
      <p><strong>Acesse sua página de compra (recomendado):</strong></p>
      <p><a href="{minha_compra_url}">{minha_compra_url}</a></p>

      <p style="margin-top:18px;"><strong>Links diretos de download:</strong></p>
      <ul>{html_items}</ul>

      <p style="margin-top:18px;">Se precisar de ajuda, responda este e-mail.</p>
    </div>
    """

    send_email(pedido.email, subject, text_body, html_body)

# -----------------------------
# Minha compra (token único)
# -----------------------------
@app.get("/minha-compra/<token>")
def minha_compra(token):
    pt = PedidoAccessToken.query.filter_by(token=token).first()
    if not pt:
        abort(404)

    if datetime.utcnow() > pt.expira_em:
        abort(410)

    pedido = Pedido.query.get_or_404(pt.pedido_id)
    if pedido.status != "paid":
        abort(403)

    itens = PedidoItem.query.filter_by(pedido_id=pedido.id).all()
    base_url = get_base_url()

    items_view = []
    total_cent = 0

    for it in itens:
        ficha = Ficha.query.get(it.ficha_id)
        titulo = it.titulo_snapshot
        preco_cent = it.preco_centavos_snapshot
        total_cent += preco_cent

        download_link = None
        if ficha and ficha.file_key:
            dt = get_or_create_download_token(pedido.id, ficha.id)
            download_link = f"{base_url}/download/{dt.token}"

        details_link = f"/ficha/{it.ficha_id}" if ficha else None

        items_view.append({
            "titulo": titulo,
            "preco": format_brl_from_centavos(preco_cent),
            "download_link": download_link,
            "details_link": details_link,
        })

    return render_template(
        "minha_compra.html",
        pedido_id=pedido.id,
        pedido_data=pedido.criado_em.strftime("%d/%m/%Y %H:%M"),
        email_mask=mask_email(pedido.email),
        expira_em=pt.expira_em.strftime("%d/%m/%Y"),
        itens=items_view,
        total=format_brl_from_centavos(total_cent),
    )

# -----------------------------
# Download seguro
# -----------------------------
@app.get("/download/<token>")
def download(token):
    t = DownloadToken.query.filter_by(token=token).first()
    if not t:
        abort(404)

    if datetime.utcnow() > t.expira_em:
        abort(410)

    if t.downloads >= t.max_downloads:
        abort(429)

    pedido = Pedido.query.get(t.pedido_id)
    if not pedido or pedido.status != "paid":
        abort(403)

    ficha = Ficha.query.get(t.ficha_id)
    if not ficha or not ficha.ativa or not ficha.file_key:
        abort(404)

    t.downloads += 1
    db.session.commit()

    if not storage_enabled():
        return "Storage não configurado (S3_*).", 500

    url = presigned_download_url(ficha.file_key, expires_seconds=600)
    return redirect(url)

# -----------------------------
# Institucional
# -----------------------------
@app.get("/termos")
def termos():
    return render_template("termos.html")

@app.get("/privacidade")
def privacidade():
    return render_template("privacidade.html")

@app.get("/reembolso")
def reembolso():
    return render_template("reembolso.html")

@app.route("/contato", methods=["GET", "POST"])
def contato():
    msg_ok = False

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        assunto = (request.form.get("assunto") or "").strip()
        mensagem = (request.form.get("mensagem") or "").strip()

        to_email = os.getenv("SUPPORT_EMAIL", os.getenv("MAIL_FROM", "suporte@fichasdemalharia.com.br"))
        subject = f"[Contato site] {assunto}"
        text = f"De: {email}\nAssunto: {assunto}\n\nMensagem:\n{mensagem}\n"
        msg_ok = send_email(to_email, subject, text, None)

    return render_template("contato.html", msg_ok=msg_ok)

# -----------------------------
# Erros amigáveis
# -----------------------------
@app.errorhandler(403)
def err_403(_):
    return render_template("error.html", code=403, title="Acesso não autorizado", message="Você não tem permissão para acessar esta página."), 403

@app.errorhandler(404)
def err_404(_):
    return render_template("error.html", code=404, title="Página não encontrada", message="O endereço informado não existe ou foi removido."), 404

@app.errorhandler(410)
def err_410(_):
    return render_template("error.html", code=410, title="Link expirado", message="Este link expirou. Se precisar, solicite suporte e enviaremos um novo acesso."), 410

@app.errorhandler(429)
def err_429(_):
    return render_template("error.html", code=429, title="Limite de downloads atingido", message="Você atingiu o limite de downloads para este link. Fale com o suporte para revalidar o acesso."), 429

# -----------------------------
# Saúde
# -----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
