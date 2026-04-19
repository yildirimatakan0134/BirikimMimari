from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel
import yfinance as yf
from typing import Optional
import uvicorn
import os
import jwt
from datetime import datetime, timedelta
from passlib.context import CryptContext
from haber_botu import haberleri_getir  # Senin haber botun

# --- GÜVENLİK VE ŞİFRELEME AYARLARI ---
SECRET_KEY = "super_gizli_anahtar_degistirilebilir"
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")


# --- VERİTABANI MODELLERİ ---
class Kullanici(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    kullanici_adi: str = Field(unique=True, index=True)
    sifre_hash: str


class Hisse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="kullanici.id")  # Hangi kullanıcıya ait?
    kod: str
    adet: float
    maliyet: float


# Render PostgreSQL URL'si varsa onu kullan, yoksa bilgisayarda (lokalde) SQLite kullan.
sqlite_url = "sqlite:///portfoy.db"
DATABASE_URL = os.environ.get("DATABASE_URL", sqlite_url)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# --- YARDIMCI GÜVENLİK FONKSİYONLARI (72 KARAKTER LİMİTİ EKLENDİ) ---
def verify_password(plain_password, hashed_password):
    # Gelen şifreyi 72 karakterle sınırla ki sistem çökmesin
    return pwd_context.verify(plain_password[:72], hashed_password)


def get_password_hash(password):
    # Kaydedilecek şifreyi 72 karakterle sınırla
    return pwd_context.hash(password[:72])


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=7)  # Oturum 7 gün açık kalır
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Geçersiz kimlik")
        return int(user_id)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Giriş süresi doldu veya geçersiz")


# --- API ENDPOINT'LERİ (YOLLARI) ---

class AuthForm(BaseModel):
    kullanici_adi: str
    sifre: str


@app.post("/api/register")
def register(form: AuthForm):
    with Session(engine) as session:
        mevcut = session.exec(select(Kullanici).where(Kullanici.kullanici_adi == form.kullanici_adi)).first()
        if mevcut:
            raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış.")
        yeni_kullanici = Kullanici(kullanici_adi=form.kullanici_adi, sifre_hash=get_password_hash(form.sifre))
        session.add(yeni_kullanici)
        session.commit()
        return {"mesaj": "Kayıt başarılı! Şimdi giriş yapabilirsiniz."}


@app.post("/api/login")
def login(form: AuthForm):
    with Session(engine) as session:
        user = session.exec(select(Kullanici).where(Kullanici.kullanici_adi == form.kullanici_adi)).first()
        if not user or not verify_password(form.sifre, user.sifre_hash):
            raise HTTPException(status_code=400, detail="Hatalı kullanıcı adı veya şifre.")

        access_token = create_access_token(data={"sub": str(user.id)})
        return {"access_token": access_token, "token_type": "bearer", "kullanici_adi": user.kullanici_adi}


@app.get("/", response_class=HTMLResponse)
async def ana_sayfa(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


class HisseForm(BaseModel):
    kod: str
    adet: float
    maliyet: float


@app.get("/api/portfoy")
async def portfoy_getir(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        # SADECE GİRİŞ YAPAN KULLANICININ HİSSELERİNİ GETİR
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        try:
            dolar_kuru = yf.Ticker("USDTRY=X").fast_info['last_price']
        except:
            dolar_kuru = 33.0

        portfoy_detay = {}
        toplam_bakiye_usd = 0.0
        for h in hisseler:
            try:
                guncel_fiyat = yf.Ticker(h.kod).fast_info['last_price']
            except:
                guncel_fiyat = 0.0

            varlik_degeri = guncel_fiyat * h.adet
            toplam_bakiye_usd += varlik_degeri
            kar_zarar_usd = (guncel_fiyat - h.maliyet) * h.adet
            kar_zarar_yuzde = ((guncel_fiyat - h.maliyet) / h.maliyet) * 100 if h.maliyet > 0 else 0

            portfoy_detay[h.kod] = {
                "adet": h.adet, "maliyet": round(h.maliyet, 2), "fiyat": round(guncel_fiyat, 2),
                "toplam_usd": round(varlik_degeri, 2), "kar_zarar_usd": round(kar_zarar_usd, 2),
                "kar_zarar_yuzde": round(kar_zarar_yuzde, 2)
            }
        return {"detay": portfoy_detay, "toplam_usd": round(toplam_bakiye_usd, 2),
                "toplam_try": round(toplam_bakiye_usd * dolar_kuru, 2), "dolar_kuru": round(dolar_kuru, 2)}


@app.post("/api/portfoy/ekle")
async def portfoy_ekle(form: HisseForm, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisse_kod = form.kod.upper()
        # SADECE KULLANICININ KENDİ HİSSESİNİ KONTROL ET
        statement = select(Hisse).where(Hisse.kod == hisse_kod).where(Hisse.user_id == user_id)
        mevcut = session.exec(statement).first()
        if mevcut:
            yeni_toplam = mevcut.adet + form.adet
            mevcut.maliyet = ((mevcut.adet * mevcut.maliyet) + (form.adet * form.maliyet)) / yeni_toplam
            mevcut.adet = yeni_toplam
            session.add(mevcut)
        else:
            session.add(Hisse(user_id=user_id, kod=hisse_kod, adet=form.adet, maliyet=form.maliyet))
        session.commit()
    return {"mesaj": "Eklendi"}


@app.delete("/api/portfoy/sil/{hisse_kodu}")
async def portfoy_sil(hisse_kodu: str, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisse = session.exec(
            select(Hisse).where(Hisse.kod == hisse_kodu.upper()).where(Hisse.user_id == user_id)).first()
        if hisse:
            session.delete(hisse)
            session.commit()
    return {"mesaj": "Silindi"}


@app.get("/api/haberler")
async def portfoy_haberleri(user_id: int = Depends(get_current_user)):
    tum_haberler = {}
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        for h in hisseler:
            tum_haberler[h.kod] = haberleri_getir(h.kod)
    return tum_haberler


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)