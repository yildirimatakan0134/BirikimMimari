from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel
import yfinance as yf
from typing import Optional
import uvicorn
from haber_botu import haberleri_getir


class Hisse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    kod: str = Field(index=True)
    adet: float
    maliyet: float


sqlite_url = "sqlite:///portfoy.db"
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


class HisseForm(BaseModel):
    kod: str
    adet: float
    maliyet: float


@app.get("/", response_class=HTMLResponse)
async def ana_sayfa(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/portfoy")
async def portfoy_getir():
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse)).all()
        try:
            dolar_kuru = yf.Ticker("USDTRY=X").fast_info['last_price']
        except:
            dolar_kuru = 33.0

        portfoy_detay = {}
        toplam_bakiye_usd = 0.0
        for h in hisseler:
            try:
                ticker = yf.Ticker(h.kod)
                guncel_fiyat = ticker.fast_info['last_price']
            except:
                guncel_fiyat = 0.0

            varlik_degeri = guncel_fiyat * h.adet
            toplam_bakiye_usd += varlik_degeri
            kar_zarar_usd = (guncel_fiyat - h.maliyet) * h.adet
            kar_zarar_yuzde = ((guncel_fiyat - h.maliyet) / h.maliyet) * 100 if h.maliyet > 0 else 0

            portfoy_detay[h.kod] = {
                "adet": h.adet,
                "maliyet": round(h.maliyet, 2),
                "fiyat": round(guncel_fiyat, 2),
                "toplam_usd": round(varlik_degeri, 2),
                "kar_zarar_usd": round(kar_zarar_usd, 2),
                "kar_zarar_yuzde": round(kar_zarar_yuzde, 2)
            }
        return {
            "detay": portfoy_detay,
            "toplam_usd": round(toplam_bakiye_usd, 2),
            "toplam_try": round(toplam_bakiye_usd * dolar_kuru, 2),
            "dolar_kuru": round(dolar_kuru, 2)
        }


@app.post("/api/portfoy/ekle")
async def portfoy_ekle(form: HisseForm):
    with Session(engine) as session:
        hisse_kod = form.kod.upper()
        statement = select(Hisse).where(Hisse.kod == hisse_kod)
        mevcut = session.exec(statement).first()
        if mevcut:
            yeni_toplam = mevcut.adet + form.adet
            mevcut.maliyet = ((mevcut.adet * mevcut.maliyet) + (form.adet * form.maliyet)) / yeni_toplam
            mevcut.adet = yeni_toplam
            session.add(mevcut)
        else:
            session.add(Hisse(kod=hisse_kod, adet=form.adet, maliyet=form.maliyet))
        session.commit()
    return {"mesaj": "Tamam"}


@app.delete("/api/portfoy/sil/{hisse_kodu}")
async def portfoy_sil(hisse_kodu: str):
    with Session(engine) as session:
        hisse = session.exec(select(Hisse).where(Hisse.kod == hisse_kodu.upper())).first()
        if hisse:
            session.delete(hisse)
            session.commit()
    return {"mesaj": "Silindi"}


@app.get("/api/haberler")
async def portfoy_haberleri():
    tum_haberler = {}
    with Session(engine) as session:
        hisseler = session.exec(select(Hisse)).all()
        for h in hisseler:
            tum_haberler[h.kod] = haberleri_getir(h.kod)
    return tum_haberler


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)