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
from haber_botu import haberleri_getir
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

# --- GÜVENLİK VE ŞİFRELEME ---
SECRET_KEY = os.getenv("SECRET_KEY", "birikim_mimari_ozel_anahtar_99")
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
    user_id: int = Field(foreign_key="kullanici.id")
    kod: str
    adet: float
    maliyet: float


# --- VERİTABANI BAĞLANTISI (RENDER + LOKAL UYUMLU) ---
database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
else:
    database_url = "sqlite:///portfoy.db"

engine_args = {"check_same_thread": False} if "sqlite" in database_url else {}
engine = create_engine(database_url, connect_args=engine_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


# --- UYGULAMA KURULUMU ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# --- YARDIMCI GÜVENLİK FONKSİYONLARI ---
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
        if user_id is None:
            raise HTTPException(status_code=401)
        return int(user_id)
    except:
        raise HTTPException(status_code=401)


# --- API ENDPOINT'LERİ (KİMLİK DOĞRULAMA) ---
class AuthForm(BaseModel):
    kullanici_adi: str
    sifre: str


@app.post("/api/register")
def register(form: AuthForm):
    with Session(engine) as session:
        kullanici_lower = form.kullanici_adi.lower()
        if session.exec(select(Kullanici).where(Kullanici.kullanici_adi == kullanici_lower)).first():
            raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış.")
        session.add(Kullanici(kullanici_adi=kullanici_lower, sifre_hash=get_password_hash(form.sifre)))
        session.commit()
        return {"mesaj": "Başarılı"}


@app.post("/api/login")
def login(form: AuthForm):
    with Session(engine) as session:
        user = session.exec(select(Kullanici).where(Kullanici.kullanici_adi == form.kullanici_adi.lower())).first()
        if not user or not verify_password(form.sifre, user.sifre_hash):
            raise HTTPException(status_code=400, detail="Hatalı giriş.")
        return {"access_token": create_access_token({"sub": str(user.id)}), "token_type": "bearer",
                "kullanici_adi": user.kullanici_adi}


@app.get("/", response_class=HTMLResponse)
async def ana_sayfa(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# --- API ENDPOINT'LERİ (PORTFÖY YÖNETİMİ) ---
class HisseForm(BaseModel):
    kod: str
    adet: float
    maliyet: float


@app.get("/api/portfoy")
async def portfoy_getir(user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        try:
            dolar_kuru = yf.Ticker("USDTRY=X").fast_info['last_price']
        except:
            dolar_kuru = 32.50

        portfoy_detay = {}
        toplam_usd = 0.0

        for h in hisseler:
            try:
                fiyat = yf.Ticker(h.kod).fast_info['last_price']
            except:
                fiyat = 0.0

            deger = fiyat * h.adet
            toplam_usd += deger
            kz_usd = (fiyat - h.maliyet) * h.adet
            kz_yuzde = ((fiyat - h.maliyet) / h.maliyet * 100) if h.maliyet > 0 else 0

            portfoy_detay[h.kod] = {
                "adet": h.adet,
                "maliyet": round(h.maliyet, 2),
                "fiyat": round(fiyat, 2),
                "toplam_usd": round(deger, 2),
                "kar_zarar_usd": round(kz_usd, 2),
                "kar_zarar_yuzde": round(kz_yuzde, 2),
                "izleme_modu": h.adet == 0  # Adet 0 ise Watchlist (İzleme) modudur
            }

        return {"detay": portfoy_detay, "toplam_usd": round(toplam_usd, 2),
                "toplam_try": round(toplam_usd * dolar_kuru, 2), "dolar_kuru": round(dolar_kuru, 2)}


@app.post("/api/portfoy/ekle")
async def portfoy_ekle(form: HisseForm, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        kod = form.kod.upper()
        mevcut = session.exec(select(Hisse).where(Hisse.kod == kod, Hisse.user_id == user_id)).first()

        if mevcut:
            yeni_adet = mevcut.adet + form.adet
            if yeni_adet > 0:
                if mevcut.adet == 0:
                    mevcut.maliyet = form.maliyet
                elif form.adet > 0:
                    mevcut.maliyet = ((mevcut.adet * mevcut.maliyet) + (form.adet * form.maliyet)) / yeni_adet
            mevcut.adet = yeni_adet
            session.add(mevcut)
        else:
            session.add(Hisse(user_id=user_id, kod=kod, adet=form.adet, maliyet=form.maliyet))
        session.commit()
    return {"mesaj": "Başarılı"}


@app.delete("/api/portfoy/sil/{kod}")
async def portfoy_sil(kod: str, user_id: int = Depends(get_current_user)):
    with Session(engine) as session:
        hisse = session.exec(select(Hisse).where(Hisse.kod == kod.upper(), Hisse.user_id == user_id)).first()
        if hisse:
            session.delete(hisse)
            session.commit()
    return {"mesaj": "Silindi"}


# --- PİYASA RADARI (HABERLER) ---
@app.get("/api/haberler")
async def portfoy_haberleri(user_id: int = Depends(get_current_user)):
    haber_sonuc = {}
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse).where(Hisse.user_id == user_id)).all()
        for h in hisseler:
            haber_sonuc[h.kod] = haberleri_getir(h.kod)
    return haber_sonuc


# --- YAPAY ZEKA: HABER ÇEVİRİ VE ÖZET MOTORU ---
@app.get("/api/haber-analiz")
async def haber_analiz(url: str, user_id: int = Depends(get_current_user)):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')

        paragraflar = soup.find_all('p')
        metin = " ".join([p.text.strip() for p in paragraflar[:3] if len(p.text.strip()) > 30])

        if not metin:
            return {"hata": "Bu haber sitesi otomatik okumaya kapalı. Haberi orijinal sitesinden okumanız gerekiyor."}

        cevirmen = GoogleTranslator(source='auto', target='tr')
        turkce_ozet = cevirmen.translate(metin)

        return {"ozet": turkce_ozet, "orijinal_url": url}
    except Exception as e:
        return {"hata": "Haber metni çekilirken bir sorun oluştu."}


# --- YAPAY ZEKA: HİSSE ANALİZ MOTORU ---
@app.get("/api/analiz/{kod}")
async def yapay_zeka_analizi(kod: str, user_id: int = Depends(get_current_user)):
    try:
        hisse = yf.Ticker(kod)
        hist = hisse.history(period="6mo")
        if hist.empty:
            return {"hata": "Bu hisse için yeterli grafik verisi bulunamadı."}

        son_fiyat = hist['Close'].iloc[-1]
        sma20 = hist['Close'].tail(20).mean()
        sma50 = hist['Close'].tail(50).mean()
        en_yuksek = hist['High'].max()
        en_dusuk = hist['Low'].min()

        info = hisse.info
        fks = info.get('trailingPE', None)
        fk_yorum = ""
        if fks:
            if fks < 10:
                fk_yorum = "F/K oranı düşük, şirket değer yatırımı açısından ucuz kalmış olabilir."
            elif fks > 25:
                fk_yorum = "F/K oranı yüksek, fiyatlama biraz primli görünüyor."
            else:
                fk_yorum = "F/K oranı sektör ortalamalarında, dengeli bir fiyatlama var."

        trend = "Pozitif (Boğa)" if son_fiyat > sma50 else "Negatif (Ayı)"
        momentum = "Güçlü" if son_fiyat > sma20 else "Zayıf"
        direnc = en_yuksek * 0.98
        destek = en_dusuk * 1.02

        yorum = f"{kod} şu anda ${round(son_fiyat, 2)} seviyesinde işlem görüyor. Orta vadeli (50 Günlük) trend **{trend}**, kısa vadeli momentum ise **{momentum}** durumunda. "
        if son_fiyat > direnc:
            yorum += "Hisse son 6 ayın zirvelerine (direnç bölgesine) çok yakın, kâr satışı baskısı görülebilir."
        elif son_fiyat < destek:
            yorum += "Hisse son ayların dibine (destek bölgesine) yaklaşmış, teknik bir tepki alımı gelebilir."
        else:
            yorum += f"Aşağıda ana destek ${round(destek, 2)}, yukarıda ise direnç hedefi ${round(direnc, 2)} seviyeleridir."

        return {
            "kod": kod,
            "fiyat": round(son_fiyat, 2),
            "trend": trend,
            "momentum": momentum,
            "yorum": yorum,
            "fk": round(fks, 2) if fks else "Bilinmiyor",
            "fk_yorum": fk_yorum
        }
    except Exception as e:
        return {"hata": f"Analiz motoru bir sorunla karşılaştı: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)