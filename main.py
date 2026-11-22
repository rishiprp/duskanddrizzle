import os, secrets, csv, io, hmac, hashlib, time
import psycopg2, psycopg2.extras
from zoneinfo import ZoneInfo
from datetime import datetime, date
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET = os.environ.get("SECRET_KEY", "change-me").encode()
IST = ZoneInfo("Asia/Kolkata")
app = FastAPI(title="Dusk & Drizzle")

class Conn:
    def __init__(s): s.c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
    def execute(s, sql, p=()):
        cur = s.c.cursor(); cur.execute(sql.replace("?", "%s"), p); return cur
    def commit(s): s.c.commit()
    def rollback(s): s.c.rollback()
    def close(s): s.c.close()
def db(): return Conn()

def init():
    c = db()
    for q in """CREATE TABLE IF NOT EXISTS products(id SERIAL PRIMARY KEY, name TEXT UNIQUE, category TEXT DEFAULT 'Drinks', price DOUBLE PRECISION, cost DOUBLE PRECISION DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, order_no TEXT UNIQUE, idem TEXT UNIQUE, created_at TEXT, subtotal DOUBLE PRECISION, discount DOUBLE PRECISION, total DOUBLE PRECISION, payment TEXT, customer TEXT, note TEXT, status TEXT DEFAULT 'completed');
    CREATE TABLE IF NOT EXISTS order_items(id SERIAL PRIMARY KEY, order_id INTEGER REFERENCES orders(id), product_id INTEGER, name TEXT, qty INTEGER, price DOUBLE PRECISION, cost DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS expenses(id SERIAL PRIMARY KEY, name TEXT, category TEXT, amount DOUBLE PRECISION, date TEXT, method TEXT, notes TEXT)""".split(";"):
        c.execute(q)
    c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS image TEXT")
    c.execute("DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status='cancelled')")
    c.execute("DELETE FROM orders WHERE status='cancelled'")
    if not c.execute("SELECT 1 FROM products").fetchone():
        for n in ["Chocolate Strawberry","Chocolate Pineapple","Cold Coco","Cold Coffee","Strawberry Iced Latte","Brownie Strawberry Bowl"]:
            c.execute("INSERT INTO products(name,price,cost) VALUES(?,?,?)",(n,0,0))
    c.commit(); c.close()
init()

def make_token():
    exp = str(int(time.time()) + 30*86400)
    return exp + "." + hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()

def auth(authorization: str = Header(None)):
    try:
        exp, sig = (authorization or "").replace("Bearer ", "").split(".")
        ok = hmac.compare_digest(sig, hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()) and int(exp) > time.time()
    except Exception: ok = False
    if not ok: raise HTTPException(401, "Not authorised")

class Login(BaseModel): username: str; password: str
class Product(BaseModel): name: str; category: str = "Drinks"; price: float; cost: float = 0; active: bool = True; image: Optional[str] = None
class Item(BaseModel): product_id: int; qty: int
class Order(BaseModel):
    idem: str; items: list[Item]; discount: float = 0; payment: str = "Cash"
    customer: Optional[str] = None; note: Optional[str] = None
class Expense(BaseModel):
    name: str; category: str; amount: float; date: str; method: str = "Cash"; notes: Optional[str] = None

@app.post("/api/login")
def login(l: Login):
    if l.username == ADMIN_USER and secrets.compare_digest(l.password, ADMIN_PASS):
        return {"token": make_token()}
    raise HTTPException(401, "Wrong credentials")

@app.get("/api/products", dependencies=[Depends(auth)])
def products():
    c = db(); r = [dict(x) for x in c.execute("SELECT * FROM products ORDER BY id")]; c.close(); return r

@app.post("/api/products", dependencies=[Depends(auth)])
def add_product(p: Product):
    c = db()
    try: c.execute("INSERT INTO products(name,category,price,cost,active,image) VALUES(?,?,?,?,?,?)",(p.name,p.category,p.price,p.cost,int(p.active),p.image)); c.commit()
    except psycopg2.IntegrityError: c.rollback(); raise HTTPException(400, "Product already exists")
    finally: c.close()
    return {"ok": True}

@app.put("/api/products/{pid}", dependencies=[Depends(auth)])
def edit_product(pid: int, p: Product):
    c = db(); c.execute("UPDATE products SET name=?,category=?,price=?,cost=?,active=?,image=COALESCE(?,image) WHERE id=?",(p.name,p.category,p.price,p.cost,int(p.active),p.image,pid)); c.commit(); c.close(); return {"ok": True}

@app.delete("/api/products/{pid}", dependencies=[Depends(auth)])
def del_product(pid: int):
    c = db(); c.execute("UPDATE products SET active=0 WHERE id=?",(pid,)); c.commit(); c.close(); return {"ok": True}  # soft delete keeps order history intact

@app.post("/api/orders", dependencies=[Depends(auth)])
def new_order(o: Order):
    if not o.items: raise HTTPException(400, "Add at least one item")
    c = db()
    try:
        dup = c.execute("SELECT id FROM orders WHERE idem=?",(o.idem,)).fetchone()
        if dup: return {"order_no": c.execute("SELECT COUNT(*) FROM orders WHERE id<=?",(dup[0],)).fetchone()[0], "duplicate": True}
        lines, sub = [], 0
        for i in o.items:
            p = c.execute("SELECT * FROM products WHERE id=? AND active=1",(i.product_id,)).fetchone()
            if not p or i.qty < 1: raise HTTPException(400, "Invalid item")
            lines.append((p, i.qty)); sub += round(p["price"]*i.qty, 2)
        if o.discount < 0 or o.discount > sub: raise HTTPException(400, "Invalid discount")
        now = datetime.now(IST).replace(tzinfo=None); day = now.strftime("%Y%m%d")
        n = c.execute("SELECT COUNT(*) FROM orders WHERE order_no LIKE ?",(f"DD-{day}-%",)).fetchone()[0] + 1
        no = f"DD-{day}-{secrets.token_hex(4)}"
        cur = c.execute("INSERT INTO orders(order_no,idem,created_at,subtotal,discount,total,payment,customer,note) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (no,o.idem,now.isoformat(timespec="seconds"),sub,o.discount,round(sub-o.discount,2),o.payment,o.customer,o.note)).fetchone()[0]
        for p,q in lines:
            c.execute("INSERT INTO order_items(order_id,product_id,name,qty,price,cost) VALUES(?,?,?,?,?,?)",(cur,p["id"],p["name"],q,p["price"],p["cost"]))
        c.commit(); return {"order_no": c.execute("SELECT COUNT(*) FROM orders WHERE id<=?",(cur,)).fetchone()[0], "total": round(sub-o.discount,2)}
    except Exception:
        c.rollback(); raise
    finally: c.close()

@app.get("/api/orders", dependencies=[Depends(auth)])
def orders(q: str = "", day: str = ""):
    c = db(); items = {}
    for i in c.execute("SELECT order_id,name,qty,price FROM order_items ORDER BY id"):
        items.setdefault(i["order_id"], []).append({"name": i["name"], "qty": i["qty"], "price": i["price"]})
    out = []
    for n, r in enumerate(c.execute("SELECT * FROM orders ORDER BY id").fetchall(), 1):
        if q.strip() and str(n) != q.strip(): continue
        if day and not r["created_at"].startswith(day): continue
        d = dict(r); d["no"] = n; d["items"] = items.get(r["id"], []); out.append(d)
    c.close(); return out[::-1]

@app.post("/api/orders/{oid}/cancel", dependencies=[Depends(auth)])
def cancel(oid: int):
    c = db(); c.execute("DELETE FROM order_items WHERE order_id=?",(oid,)); c.execute("DELETE FROM orders WHERE id=?",(oid,)); c.commit(); c.close(); return {"ok": True}

@app.get("/api/expenses", dependencies=[Depends(auth)])
def expenses():
    c = db(); r = [dict(x) for x in c.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")]; c.close(); return r

@app.post("/api/expenses", dependencies=[Depends(auth)])
def add_expense(e: Expense):
    c = db(); c.execute("INSERT INTO expenses(name,category,amount,date,method,notes) VALUES(?,?,?,?,?,?)",(e.name,e.category,e.amount,e.date,e.method,e.notes)); c.commit(); c.close(); return {"ok": True}

@app.delete("/api/expenses/{eid}", dependencies=[Depends(auth)])
def del_expense(eid: int):
    c = db(); c.execute("DELETE FROM expenses WHERE id=?",(eid,)); c.commit(); c.close(); return {"ok": True}

def agg(c, s, e):
    o = c.execute("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM orders WHERE status='completed' AND date(created_at) BETWEEN ? AND ?",(s,e)).fetchone()
    it = c.execute("SELECT COALESCE(SUM(qty),0) q FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.status='completed' AND date(o.created_at) BETWEEN ? AND ?",(s,e)).fetchone()
    ex = c.execute("SELECT COALESCE(SUM(amount),0) a FROM expenses WHERE date BETWEEN ? AND ?",(s,e)).fetchone()
    return o["t"], o["n"], it["q"], ex["a"]

@app.get("/api/summary", dependencies=[Depends(auth)])
def summary(start: str, end: str):
    c = db(); rev, n, items, exp = agg(c, start, end)
    days = {}
    for r in c.execute("SELECT date(created_at) d, SUM(total) t FROM orders WHERE status='completed' AND date(created_at) BETWEEN ? AND ? GROUP BY d",(start,end)): days.setdefault(str(r["d"]),[0,0])[0] = r["t"]
    for r in c.execute("SELECT expenses.date d, SUM(amount) a FROM expenses WHERE date BETWEEN ? AND ? GROUP BY d",(start,end)): days.setdefault(str(r["d"]),[0,0])[1] = r["a"]
    prods = [dict(r) for r in c.execute("""SELECT i.name, SUM(i.qty) qty, SUM(i.qty*i.price) revenue, SUM(i.qty*(i.price-i.cost)) profit FROM order_items i JOIN orders o ON o.id=i.order_id
        WHERE o.status='completed' AND date(o.created_at) BETWEEN ? AND ? GROUP BY i.name ORDER BY qty DESC""",(start,end))]
    allrev, alln, _, allexp = agg(c, "1900-01-01", "2999-12-31"); c.close()
    return {"sales": rev, "orders": n, "items": items, "expenses": exp, "profit": round(rev-exp,2),
        "aov": round(rev/n,2) if n else 0, "best": prods[0]["name"] if prods else "-",
        "profit_pct": round((rev-exp)/rev*100,1) if rev else 0, "expense_pct": round(exp/rev*100,1) if rev else 0,
        "all_revenue": allrev, "all_expenses": allexp, "all_profit": round(allrev-allexp,2),
        "series": [{"d": k, "sales": v[0], "expenses": v[1], "profit": round(v[0]-v[1],2)} for k,v in sorted(days.items())], "products": prods}

@app.get("/api/export/orders.csv", dependencies=[Depends(auth)])
def export():
    c = db(); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Order","Date","Subtotal","Discount","Total","Payment","Customer","Status"])
    for r in c.execute("SELECT ROW_NUMBER() OVER (ORDER BY id),created_at,subtotal,discount,total,payment,customer,status FROM orders ORDER BY id"): w.writerow(list(r))
    c.close(); buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv")

@app.get("/")
def index(): return FileResponse("static/index.html")
@app.get("/sw.js")
def sw(): return FileResponse("static/sw.js", media_type="application/javascript")
app.mount("/static", StaticFiles(directory="static"), name="static")
