import os
from flask import Flask, render_template, request

app = Flask(__name__)

# ✅ V1: dados mock (depois vira banco)
FICHAS = [
    {
        "id": 1,
        "titulo": "Meia Malha 100% Algodão 170 g/m²",
        "tipo_malha": "Meia Malha",
        "composicao": "100% Algodão",
        "gramatura": 170,
        "largura": 1.60,
        "categoria": "Camisetas",
        "preco": 29.90,
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
    },
]

@app.get("/")
def home():
    categorias = sorted({f["categoria"] for f in FICHAS})
    tipos = sorted({f["tipo_malha"] for f in FICHAS})
    return render_template("home.html", categorias=categorias, tipos=tipos)

@app.get("/busca")
def busca():
    # filtros via querystring
    tipo = (request.args.get("tipo") or "").strip()
    categoria = (request.args.get("categoria") or "").strip()
    comp = (request.args.get("composicao") or "").strip()
    gmin = request.args.get("gramatura_min", type=int)
    gmax = request.args.get("gramatura_max", type=int)

    resultados = FICHAS[:]

    if tipo:
        resultados = [f for f in resultados if f["tipo_malha"] == tipo]
    if categoria:
        resultados = [f for f in resultados if f["categoria"] == categoria]
    if comp:
        resultados = [f for f in resultados if comp.lower() in f["composicao"].lower()]
    if gmin is not None:
        resultados = [f for f in resultados if f["gramatura"] >= gmin]
    if gmax is not None:
        resultados = [f for f in resultados if f["gramatura"] <= gmax]

    categorias = sorted({f["categoria"] for f in FICHAS})
    tipos = sorted({f["tipo_malha"] for f in FICHAS})

    return render_template(
        "busca.html",
        fichas=resultados,
        filtros={
            "tipo": tipo, "categoria": categoria, "composicao": comp,
            "gramatura_min": gmin, "gramatura_max": gmax
        },
        categorias=categorias,
        tipos=tipos
    )

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
