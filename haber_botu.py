import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET


def haberleri_getir(hisse_kodu):
    arama_terimi = urllib.parse.quote(f"{hisse_kodu} stock")
    url = f"https://news.google.com/rss/search?q={arama_terimi}&hl=en-US&gl=US&ceid=US:en"

    haber_listesi = []  # Haberleri bu listenin içinde toplayacağız

    try:
        istek = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        yanit = urllib.request.urlopen(istek)
        xml_verisi = yanit.read()

        root = ET.fromstring(xml_verisi)
        haberler = root.findall('./channel/item')

        for haber in haberler[:3]:
            baslik = haber.find('title').text
            link = haber.find('link').text
            tarih = haber.find('pubDate').text

            temiz_baslik = baslik.split(" - ")[0]
            kaynak = baslik.split(" - ")[-1] if " - " in baslik else "Bilinmeyen Kaynak"

            # Her bir haberi sözlük (dictionary) olarak listeye ekliyoruz
            haber_listesi.append({
                "baslik": temiz_baslik,
                "kaynak": kaynak,
                "tarih": tarih,
                "link": link
            })

    except Exception as e:
        print(f"{hisse_kodu} için haberler çekilirken hata: {e}")

    return haber_listesi  # Toplanan haberleri siteye gönderiyoruz