from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel
import yfinance as yf
from typing import Optional, Dict
import uvicorn
import os
import jwt
import requests
from datetime import datetime, timedelta
from passlib.context import CryptContext
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from haber_botu import haberleri_getir

# --- GÜVENLİK AYARLARI ---
SECRET_KEY = 
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")


# --- VERİTABANI MODELLERİ (SQLModel) ---
class Kullanici(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    kullanici_adi: str = Field(unique=True, index=True)
    sifre_hash: str


class Hisse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="kullanici.id")
    kod: str
    adet: float
    maliyet: float


class Bildirim(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="kullanici.id")
    baslik: str
    mesaj: str
    tarih: datetime = Field(default_factory=datetime.utcnow)
    okundu: bool = Field(default=False)


class FiyatAlarmi(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="kullanici.id")
    kod: str
    hedef_fiyat: float
    durum: str = Field(default="beklemede")


# --- VERİ GİRİŞ ŞABLONLARI (Pydantic) ---
class AuthForm(BaseModel):
    kullanici_adi: str
    sifre: str


class HisseForm(BaseModel):
    kod: str
    adet: float
    maliyet: float


class AlarmForm(BaseModel):
    kod: str
    fiyat: float


# --- VERİTABANI BAĞLANTISI ---
database_url = os.getenv("DATABASE_URL", "sqlite:///portfoy.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(database_url, connect_args={"check_same_thread": False} if "sqlite" in database_url else {})


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


app = FastAPI(title="Birikim Mimarı Pro Terminal")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# --- YARDIMCI FONKSİYONLAR ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password[:72], hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password[:72])


def create_access_token(data: dict):
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + timedelta(days=7)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None: raise HTTPException(status_code=401)
        return int(user_id)
    except:
        raise HTTPException(status_code=401)


# --- SAYFA VE AUTH ---
@app.get("/", response_class=HTMLResponse)
async def ana_sayfa(request: Request):
    # Jinja2 hatası düzeltildi
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/api/register")
def register(form: AuthForm):
    with Session(engine) as session:
        kullanici_lower = form.kullanici_adi.lower()
        if session.exec(select(Kullanici).where(Kullanici.kullanici_adi == kullanici_lower)).first():
            raise HTTPException(status_code=400, detail="Kullanıcı adı zaten kullanımda.")
        session.add(Kullanici(kullanici_adi=kullanici_lower, sifre_hash=get_password_hash(form.sifre)))
        session.commit()
        return {"mesaj": "Başarıyla kayıt olundu."}


@app.post("/api/login")
def login(form: AuthForm):
    with Session(engine) as session:
        user = session.exec(select(Kullanici).where(Kullanici.kullanici_adi == form.kullanici_adi.lower())).first()
        if not user or not verify_password(form.sifre, user.sifre_hash):
            raise HTTPException(status_code=400, detail="Hatalı kullanıcı adı veya şifre.")
        return {
            "access_token": create_access_token({"sub": str(user.id)}),
            "token_type": "bearer",
            "kullanici_adi": user.kullanici_adi
        }


# --- PORTFÖY ---
@app.get("/api/portfoy")
async def portfoy_getir(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        try:
            dolar_kuru = yf.Ticker("USDTRY=X").fast_info['last_price']
        except:
            dolar_kuru = 33.50

        detaylar = {}
        sektorler = {}
        toplam_usd = 0.0

        for h in hisseler:
            t = yf.Ticker(h.kod)
            try:
                fiyat = t.fast_info['last_price']
                info = t.info
                sektor = info.get('sector', 'Diğer')
            except:
                fiyat = 0.0
                sektor = 'Bilinmiyor'

            deger = fiyat * h.adet
            toplam_usd += deger
            sektorler[sektor] = sektorler.get(sektor, 0) + deger

            kz_usd = (fiyat - h.maliyet) * h.adet
            kz_yuzde = ((fiyat - h.maliyet) / h.maliyet * 100) if h.maliyet > 0 else 0

            detaylar[h.kod] = {
                "adet": h.adet, "maliyet": round(h.maliyet, 2), "fiyat": round(fiyat, 2),
                "toplam_usd": round(deger, 2), "kar_zarar_usd": round(kz_usd, 2), "kar_zarar_yuzde": round(kz_yuzde, 2),
                "sektor": sektor, "izleme_modu": h.adet == 0
            }

        return {
            "detay": detaylar, "sektorler": sektorler,
            "toplam_usd": round(toplam_usd, 2), "toplam_try": round(toplam_usd * dolar_kuru, 2),
            "dolar_kuru": round(dolar_kuru, 2)
        }


@app.post("/api/portfoy/ekle")
async def portfoy_ekle(form: HisseForm, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        kod = form.kod.upper()
        mevcut = session.exec(select(Hisse).where(Hisse.kod == kod, Hisse.user_id == user_id)).first()

        if mevcut:
            if form.adet > 0:
                yeni_adet = mevcut.adet + form.adet
                mevcut.maliyet = ((mevcut.adet * mevcut.maliyet) + (form.adet * form.maliyet)) / yeni_adet
                mevcut.adet = yeni_adet
            else:
                mevcut.adet = 0
            session.add(mevcut)
        else:
            session.add(Hisse(user_id=user_id, kod=kod, adet=form.adet, maliyet=form.maliyet))

        session.add(Bildirim(user_id=user_id, baslik="Portföy Güncellendi", mesaj=f"{kod} başarıyla listeye eklendi."))
        session.commit()
    return {"mesaj": "Başarılı"}


@app.delete("/api/portfoy/sil/{kod}")
async def portfoy_sil(kod: str, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisse = session.exec(select(Hisse).where(Hisse.kod == kod.upper(), Hisse.user_id == user_id)).first()
        if hisse:
            session.delete(hisse)
            session.add(Bildirim(user_id=user_id, baslik="Varlık Silindi", mesaj=f"{kod} portföyünüzden kaldırıldı."))
            session.commit()
    return {"mesaj": "Silindi"}


# --- BİLDİRİM VE ALARM ---
@app.get("/api/bildirimler")
async def bildirimleri_getir(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        return session.exec(select(Bildirim).where(Bildirim.user_id == user_id).order_by(Bildirim.tarih.desc())).all()


@app.post("/api/bildirimler/oku")
async def bildirimleri_okundu_yap(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        bildirimler = session.exec(select(Bildirim).where(Bildirim.user_id == user_id, Bildirim.okundu == False)).all()
        for b in bildirimler: b.okundu = True
        session.commit()
    return {"mesaj": "Tamamı okundu"}


@app.post("/api/alarm/kur")
async def alarm_kur(form: AlarmForm, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        session.add(FiyatAlarmi(user_id=user_id, kod=form.kod.upper(), hedef_fiyat=form.fiyat))
        session.commit()
    return {"mesaj": "Alarm kuruldu"}


# --- YAPAY ZEKA ---
@app.get("/api/haber-analiz")
async def haber_analiz_et(url: str, user_id: int = Depends(get_current_user)):
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        metin = " ".join([p.text for p in soup.find_all('p')[:3]])
        cevirmen = GoogleTranslator(source='auto', target='tr')
        return {"ozet": cevirmen.translate(metin), "url": url}
    except:
        return {"hata": "Haber metni çözümlenemedi."}


@app.get("/api/haberler")
async def portfoy_haberleri(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        return {h.kod: haberleri_getir(h.kod) for h in hisseler}


@app.get("/api/analiz/{kod}")
async def yapay_zeka_analizi(kod: str, user_id: int = Depends(get_current_user)):
    try:
        t = yf.Ticker(kod)
        hist = t.history(period="1y")
        if hist.empty: return {"hata": "Veri yok"}

        fiyat = hist['Close'].iloc[-1]
        sma50 = hist['Close'].tail(50).mean()
        sma200 = hist['Close'].tail(200).mean()

        delta = hist['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1]))

        skor = 5
        if fiyat > sma50: skor += 1
        if sma50 > sma200: skor += 2
        if rsi < 30: skor += 2
        if rsi > 70: skor -= 1

        yorum = f"**{kod}** şu an {round(fiyat, 2)} seviyesinde. Teknik skor 10 üzerinden **{min(skor, 10)}**. "
        yorum += f"SMA50 değeri {round(sma50, 2)}. RSI({round(rsi, 1)}) seviyesi "
        yorum += "piyasanın denge noktasında olduğunu gösteriyor." if 30 < rsi < 70 else "piyasanın aşırı uçlarda olduğunu gösteriyor."

        return {
            "kod": kod, "fiyat": round(fiyat, 2), "skor": min(skor, 10),
            "trend": "Yükseliş" if fiyat > sma50 else "Düşüş",
            "yorum": yorum, "sektor": t.info.get('sector', 'Bilinmiyor')
        }
    except Exception as e:
        return {"hata": str(e)}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
