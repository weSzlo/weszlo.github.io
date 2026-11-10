#!/usr/bin/env python3
"""
Sahibinden Fırsat Tarayıcı

config.yaml'daki aramaları tarar, ilanları yerel SQLite veritabanında biriktirir,
her ilanı benzer ilanların medyan fiyatıyla karşılaştırır ve piyasanın altında
kalan YENİ ilanları Telegram ve/veya e-posta ile gönderir.

Kullanım:
    python tarayici.py                # tara + bildir
    python tarayici.py --bildirme     # tara, bildirim gönderme (rapor dosyası yine yazılır)
    python tarayici.py --test-bildirim
"""
from __future__ import annotations

import argparse
import html
import os
import random
import re
import smtplib
import sqlite3
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import yaml

KLASOR = Path(__file__).resolve().parent
DB_YOLU = KLASOR / "ilanlar.db"
PROFIL_KLASORU = KLASOR / ".tarayici-profili"
RAPOR_KLASORU = KLASOR / "raporlar"
SAYFA_BOYUTU = 50


# ------------------------------------------------------------------ yardımcılar
def env_yukle() -> None:
    env = KLASOR / ".env"
    if not env.exists():
        return
    for satir in env.read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if not satir or satir.startswith("#") or "=" not in satir:
            continue
        k, v = satir.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sadelestir(s: str) -> str:
    """Türkçe karakterleri ve büyük/küçük harfi normalize eder (karşılaştırma için)."""
    s = (s or "").replace("İ", "i").replace("I", "ı").lower()
    tablo = str.maketrans("çğıöşü", "cgiosu")
    s = s.translate(tablo)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()


def sayi(s: str | None) -> float | None:
    if not s:
        return None
    rakam = re.sub(r"[^\d]", "", s.split(",")[0])
    return float(rakam) if rakam else None


def url_sayfa(url: str, offset: int) -> str:
    parca = urlsplit(url)
    q = dict(parse_qsl(parca.query, keep_blank_values=True))
    q["pagingSize"] = str(SAYFA_BOYUTU)
    q.setdefault("sorting", "date_desc")
    if offset:
        q["pagingOffset"] = str(offset)
    else:
        q.pop("pagingOffset", None)
    return urlunsplit((parca.scheme, parca.netloc, parca.path, urlencode(q), ""))


def tl(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f} TL".replace(",", ".")


# ------------------------------------------------------------------ veri modeli
@dataclass
class Ilan:
    id: str
    arama: str
    tip: str
    baslik: str
    url: str
    fiyat: float | None
    konum: str
    nitelik: dict = field(default_factory=dict)

    # türetilmiş alanlar
    @property
    def ilce(self) -> str:
        # Konum hücresi genelde "Osmangazi\nAltınova Mh." ya da "Bursa Nilüfer" biçimindedir
        return self.konum.split("\n")[0].strip() if self.konum else ""

    @property
    def mahalle(self) -> str:
        parcalar = [p.strip() for p in self.konum.split("\n") if p.strip()]
        return parcalar[1] if len(parcalar) > 1 else ""

    def nit(self, *anahtarlar: str) -> str:
        for k in self.nitelik:
            ks = sadelestir(k)
            if any(a in ks for a in anahtarlar):
                return self.nitelik[k]
        return ""

    @property
    def m2(self) -> float | None:
        v = self.nit("m2 (brut)", "m² (brut)", "m2", "m²", "metrekare")
        return sayi(v)

    @property
    def yil(self) -> str:
        return self.nit("yil", "model yili")

    @property
    def km(self) -> float | None:
        return sayi(self.nit("km", "kilometre"))

    @property
    def oda(self) -> str:
        return self.nit("oda")

    @property
    def model_anahtari(self) -> str:
        parcalar = [self.nit("marka"), self.nit("seri"), self.nit("model")]
        anahtar = " ".join(p for p in parcalar if p)
        return sadelestir(anahtar) or sadelestir(" ".join(self.baslik.split()[:3]))


# ------------------------------------------------------------------ veritabanı
def db_ac() -> sqlite3.Connection:
    con = sqlite3.connect(DB_YOLU)
    con.execute(
        """CREATE TABLE IF NOT EXISTS ilan (
            id TEXT PRIMARY KEY, arama TEXT, tip TEXT, baslik TEXT, url TEXT,
            fiyat REAL, konum TEXT, nitelik TEXT,
            ilk_gorulme TEXT, son_gorulme TEXT, bildirildi INTEGER DEFAULT 0,
            bildirilen_fiyat REAL)"""
    )
    return con


def db_kaydet(con: sqlite3.Connection, ilanlar: list[Ilan]) -> set[str]:
    """İlanları kaydeder; bu taramada ilk kez görülenlerin id'lerini döndürür."""
    import json

    simdi = datetime.now().isoformat(timespec="seconds")
    yeni = set()
    for i in ilanlar:
        var = con.execute("SELECT 1 FROM ilan WHERE id=?", (i.id,)).fetchone()
        if var:
            con.execute(
                "UPDATE ilan SET fiyat=?, son_gorulme=?, nitelik=?, konum=?, baslik=? WHERE id=?",
                (i.fiyat, simdi, json.dumps(i.nitelik, ensure_ascii=False), i.konum, i.baslik, i.id),
            )
        else:
            yeni.add(i.id)
            con.execute(
                "INSERT INTO ilan (id, arama, tip, baslik, url, fiyat, konum, nitelik, ilk_gorulme, son_gorulme)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (i.id, i.arama, i.tip, i.baslik, i.url, i.fiyat, i.konum,
                 json.dumps(i.nitelik, ensure_ascii=False), simdi, simdi),
            )
    con.commit()
    return yeni


def db_gecmis(con: sqlite3.Connection, arama: str, gun: int) -> list[Ilan]:
    import json

    sinir = (datetime.now() - timedelta(days=gun)).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT id, arama, tip, baslik, url, fiyat, konum, nitelik FROM ilan"
        " WHERE arama=? AND son_gorulme>=? AND fiyat IS NOT NULL",
        (arama, sinir),
    ).fetchall()
    return [Ilan(r[0], r[1], r[2], r[3], r[4], r[5], r[6] or "", json.loads(r[7] or "{}")) for r in rows]


# ------------------------------------------------------------------ tarama
SAYFA_JS = r"""
() => {
  const tablo = document.querySelector('#searchResultsTable');
  if (!tablo) return null;
  const basliklar = [...tablo.querySelectorAll('thead tr td, thead tr th')]
      .map(td => td.innerText.trim());
  const sonuc = [];
  for (const tr of tablo.querySelectorAll('tbody tr.searchResultsItem')) {
    const id = tr.getAttribute('data-id');
    if (!id || tr.classList.contains('nativeAd')) continue;
    const a = tr.querySelector('a.classifiedTitle') || tr.querySelector('a[href*="/ilan/"]');
    const hucreler = [...tr.querySelectorAll(':scope > td')];
    const nitelik = {};
    hucreler.forEach((td, i) => {
      if (td.classList.contains('searchResultsAttributeValue')) {
        const ad = basliklar[i] || ('alan' + i);
        nitelik[ad] = td.innerText.trim();
      }
    });
    const fiyat = tr.querySelector('.searchResultsPriceValue');
    const konum = tr.querySelector('.searchResultsLocationValue');
    sonuc.push({
      id, baslik: a ? a.innerText.trim() : '',
      url: a ? a.href : ('https://www.sahibinden.com/ilan/' + id),
      fiyat: fiyat ? fiyat.innerText.trim() : '',
      konum: konum ? konum.innerText.trim() : '',
      nitelik,
    });
  }
  return sonuc;
}
"""


def dogrulama_var_mi(page) -> bool:
    try:
        baslik = (page.title() or "").lower()
        govde = page.inner_text("body", timeout=3000)[:3000].lower()
    except Exception:
        return False
    isaretler = ["just a moment", "olağan dışı", "olagan disi", "robot olmadığınızı",
                 "güvenlik doğrulaması", "verify you are human", "checking your browser"]
    return any(x in baslik or x in govde for x in isaretler)


def sayfa_bekle(page, ayar: dict) -> None:
    if not dogrulama_var_mi(page):
        return
    sure = int(ayar["tarama"].get("dogrulama_bekleme", 120))
    print(f"  ! Doğrulama sayfası çıktı. Açılan tarayıcıda doğrulamayı tamamlayın ({sure} sn)...")
    bitis = time.time() + sure
    while time.time() < bitis:
        time.sleep(3)
        if not dogrulama_var_mi(page):
            print("  ✓ Doğrulama geçildi.")
            return
    raise RuntimeError("Doğrulama sayfası geçilemedi.")


def tara(ayar: dict) -> dict[str, list[Ilan]]:
    from playwright.sync_api import sync_playwright

    t = ayar["tarama"]
    sonuc: dict[str, list[Ilan]] = {}
    with sync_playwright() as p:
        secenekler = dict(
            user_data_dir=str(PROFIL_KLASORU),
            headless=not t.get("gorunur_tarayici", True),
            locale="tr-TR",
            viewport={"width": 1366, "height": 900},
        )
        try:
            ctx = p.chromium.launch_persistent_context(channel="chrome", **secenekler)
        except Exception:
            ctx = p.chromium.launch_persistent_context(**secenekler)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for arama in ayar["aramalar"]:
            ad, tip = arama["ad"], arama["tip"]
            ilceler = {sadelestir(x) for x in arama.get("ilceler", [])}
            print(f"\n▶ {ad}")
            liste: list[Ilan] = []
            for s in range(int(arama.get("max_sayfa", 5))):
                url = url_sayfa(arama["url"], s * SAYFA_BOYUTU)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    sayfa_bekle(page, ayar)
                    page.wait_for_selector("#searchResultsTable", timeout=20000)
                    veriler = page.evaluate(SAYFA_JS) or []
                except Exception as e:
                    print(f"  sayfa {s + 1}: hata/sonuç yok ({e.__class__.__name__}) - arama sonlandırıldı")
                    break
                if not veriler:
                    break
                for v in veriler:
                    i = Ilan(v["id"], ad, tip, v["baslik"], v["url"], sayi(v["fiyat"]),
                             v["konum"], v["nitelik"])
                    if ilceler and not any(x in sadelestir(i.konum) for x in ilceler):
                        continue
                    liste.append(i)
                print(f"  sayfa {s + 1}: {len(veriler)} ilan (toplam uygun: {len(liste)})")
                if len(veriler) < SAYFA_BOYUTU:
                    break
                time.sleep(random.uniform(t["bekleme_min"], t["bekleme_max"]))
            sonuc[ad] = liste
            time.sleep(random.uniform(t["bekleme_min"], t["bekleme_max"]))
        ctx.close()
    return sonuc


# ------------------------------------------------------------------ analiz
@dataclass
class Firsat:
    ilan: Ilan
    deger: float          # ilanın kıyaslanan değeri (fiyat veya m² fiyatı)
    medyan: float
    grup: str
    grup_n: int
    birim: str
    supheli: bool

    @property
    def indirim(self) -> float:
        return 1 - self.deger / self.medyan


def gruplar_olustur(ilan: Ilan) -> list[tuple[str, str]]:
    """İlan için en dar gruptan en genişe doğru karşılaştırma grupları (anahtar, açıklama)."""
    if ilan.tip == "arac":
        m, y = ilan.model_anahtari, ilan.yil
        out = []
        if m and y:
            out.append((f"{m}|{y}", f"{m} {y}"))
            try:
                yy = int(y)
                out.append((f"{m}|{yy // 2 * 2}-{yy // 2 * 2 + 1}", f"{m} {yy // 2 * 2}-{yy // 2 * 2 + 1}"))
            except ValueError:
                pass
        return out
    ilce = sadelestir(ilan.ilce)
    mah = sadelestir(ilan.mahalle)
    oda = sadelestir(ilan.oda)
    out = []
    if mah and oda:
        out.append((f"{ilce}|{mah}|{oda}", f"{ilan.ilce} / {ilan.mahalle} / {ilan.oda}"))
    if mah:
        out.append((f"{ilce}|{mah}", f"{ilan.ilce} / {ilan.mahalle}"))
    if oda:
        out.append((f"{ilce}|{oda}", f"{ilan.ilce} / {ilan.oda}"))
    if ilce:
        out.append((ilce, ilan.ilce))
    return out


def deger_ve_birim(ilan: Ilan) -> tuple[float | None, str]:
    if ilan.fiyat is None:
        return None, ""
    if ilan.tip in ("emlak", "devren") and ilan.m2 and ilan.m2 >= 10:
        return ilan.fiyat / ilan.m2, "TL/m²"
    if ilan.tip == "emlak":
        return None, ""  # m² yoksa emlakta sağlıklı kıyas yapılamaz
    return ilan.fiyat, "TL"


def grup_anahtarlari(ilan: Ilan) -> list[str]:
    _, birim = deger_ve_birim(ilan)
    return [f"{birim}#{k}" for k, _ in gruplar_olustur(ilan)]


def analiz(gecmis: list[Ilan], adaylar: list[Ilan], ayar: dict) -> list[Firsat]:
    f = ayar["firsat"]
    min_n = int(f["min_grup_ilan"])
    havuz: dict[str, list[tuple[float, Ilan]]] = {}
    for i in gecmis:
        d, _ = deger_ve_birim(i)
        if d is None or d <= 0:
            continue
        for k in grup_anahtarlari(i):
            havuz.setdefault(k, []).append((d, i))

    firsatlar = []
    for i in adaylar:
        d, birim = deger_ve_birim(i)
        if d is None or d <= 0:
            continue
        for (k, aciklama), tam in zip(gruplar_olustur(i), grup_anahtarlari(i)):
            uyeler = [(v, x) for v, x in havuz.get(tam, []) if x.id != i.id]
            if len(uyeler) < min_n:
                continue
            # aykırı değerleri (hatalı/ sembolik fiyat) kırp
            degerler = sorted(v for v, _ in uyeler)
            kes = max(1, len(degerler) // 10) if len(degerler) >= 10 else 0
            kirpik = degerler[kes:len(degerler) - kes] if kes else degerler
            med = statistics.median(kirpik)
            if i.tip == "arac" and i.km:
                kmler = [x.km for _, x in uyeler if x.km]
                if kmler and i.km > statistics.median(kmler) * float(f["arac_max_km_kat"]):
                    break  # yüksek KM ucuzluğu açıklıyor
            if d <= med * float(f["esik_oran"]):
                firsatlar.append(Firsat(i, d, med, aciklama, len(uyeler), birim,
                                        d <= med * float(f["supheli_oran"])))
            break  # yeterli büyüklükteki ilk (en dar) grupla karar ver
    firsatlar.sort(key=lambda x: (x.supheli, -x.indirim))
    return firsatlar


# ------------------------------------------------------------------ bildirim
def firsat_metni(fr: Firsat) -> str:
    i = fr.ilan
    detay = []
    if i.tip == "arac":
        detay = [x for x in (i.yil, f"{i.km:,.0f} km".replace(",", ".") if i.km else "") if x]
    else:
        detay = [x for x in (i.oda, f"{i.m2:.0f} m²" if i.m2 else "") if x]
    satirlar = [
        f"<b>{html.escape(i.baslik)}</b>",
        f"💰 {tl(i.fiyat)}  ·  📍 {html.escape(i.konum.replace(chr(10), ' / '))}",
    ]
    if detay:
        satirlar.append("ℹ️ " + html.escape(" · ".join(detay)))
    deger = f"{fr.deger:,.0f}".replace(",", ".")
    med = f"{fr.medyan:,.0f}".replace(",", ".")
    satirlar.append(
        f"📉 Piyasanın <b>%{fr.indirim * 100:.0f} altında</b> ({deger} vs medyan {med} {fr.birim}; "
        f"grup: {html.escape(fr.grup)}, {fr.grup_n} ilan)"
    )
    if fr.supheli:
        satirlar.append("⚠️ Aşırı ucuz: hatalı fiyat veya kapora dolandırıcılığı olabilir. Kapora göndermeyin!")
    satirlar.append(i.url)
    return "\n".join(satirlar)


def telegram_gonder(mesajlar: list[str]) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    parca = ""
    paketler = []
    for m in mesajlar:
        if len(parca) + len(m) + 2 > 3800:
            paketler.append(parca)
            parca = ""
        parca += m + "\n\n"
    if parca:
        paketler.append(parca)
    for p in paketler:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": p, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if not r.ok:
            print("Telegram hatası:", r.text[:300])
            return False
        time.sleep(1)
    return True


def eposta_gonder(konu: str, govde_html: str) -> bool:
    host, kul, sifre, alici = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_KULLANICI", "SMTP_SIFRE", "EPOSTA_ALICI"))
    if not all((host, kul, sifre, alici)):
        return False
    msg = MIMEText(govde_html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = konu, kul, alici
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")), timeout=30) as s:
        s.login(kul, sifre)
        s.sendmail(kul, [alici], msg.as_string())
    return True


def bildir(bolumler: dict[str, list[Firsat]], gonder: bool) -> None:
    tarih = datetime.now().strftime("%d.%m.%Y")
    toplam = sum(len(v) for v in bolumler.values())
    mesajlar = [f"🔎 <b>Sahibinden fırsat raporu - {tarih}</b>\nToplam {toplam} yeni fırsat"]
    for ad, frs in bolumler.items():
        if frs:
            mesajlar.append(f"━━━━━━━━━━\n<b>{html.escape(ad)}</b> ({len(frs)})")
            mesajlar.extend(firsat_metni(fr) for fr in frs)

    RAPOR_KLASORU.mkdir(exist_ok=True)
    rapor = RAPOR_KLASORU / f"rapor-{datetime.now():%Y-%m-%d}.html"
    govde = "<br><br>".join(m.replace("\n", "<br>") for m in mesajlar)
    govde = re.sub(r"(https://www\.sahibinden\.com/\S+)", r'<a href="\1">\1</a>', govde)
    rapor.write_text(f"<meta charset='utf-8'><body style='font-family:sans-serif'>{govde}</body>",
                     encoding="utf-8")
    print(f"\nRapor yazıldı: {rapor}")

    if not gonder:
        return
    if toplam == 0:
        mesajlar = [mesajlar[0] + "\nBugün kriterlere uyan yeni ilan bulunamadı."]
    tg = telegram_gonder(mesajlar)
    ep = eposta_gonder(f"Sahibinden fırsatları {tarih} ({toplam})", govde)
    if not (tg or ep):
        print("Bildirim kanalı ayarlı değil (.env dosyasına Telegram veya e-posta bilgisi girin).")
    else:
        print("Bildirim gönderildi:", ", ".join(x for x, ok in (("Telegram", tg), ("E-posta", ep)) if ok))


# ------------------------------------------------------------------ ana akış
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bildirme", action="store_true", help="bildirim gönderme")
    ap.add_argument("--test-bildirim", action="store_true", help="sadece test mesajı gönder")
    args = ap.parse_args()

    env_yukle()
    if args.test_bildirim:
        ok = telegram_gonder(["✅ Sahibinden tarayıcı test mesajı"])
        ok = eposta_gonder("Sahibinden tarayıcı test", "✅ Test mesajı") or ok
        print("Gönderildi" if ok else "Hiçbir kanal ayarlı değil / gönderilemedi")
        return

    ayar = yaml.safe_load((KLASOR / "config.yaml").read_text(encoding="utf-8"))
    con = db_ac()
    taranan = tara(ayar)

    bolumler: dict[str, list[Firsat]] = {}
    for ad, ilanlar in taranan.items():
        yeni = db_kaydet(con, ilanlar)
        gecmis = db_gecmis(con, ad, int(ayar["firsat"]["gecmis_gun"]))
        # daha önce bildirilmemiş ilanlar (ya da bildirildikten sonra fiyatı %5+ düşenler)
        bildirilmis = {r[0]: r[1] for r in con.execute(
            "SELECT id, bildirilen_fiyat FROM ilan WHERE arama=? AND bildirildi=1", (ad,))}
        adaylar = [i for i in ilanlar
                   if i.id in yeni or i.id not in bildirilmis
                   or (i.fiyat and bildirilmis[i.id] and i.fiyat < bildirilmis[i.id] * 0.95)]
        frs = analiz(gecmis, adaylar, ayar)
        print(f"{ad}: {len(ilanlar)} ilan, {len(yeni)} yeni, karşılaştırma havuzu {len(gecmis)}, "
              f"{len(frs)} fırsat")
        bolumler[ad] = frs
        if not args.bildirme:
            for fr in frs:
                con.execute("UPDATE ilan SET bildirildi=1, bildirilen_fiyat=? WHERE id=?",
                            (fr.ilan.fiyat, fr.ilan.id))
    con.commit()
    bildir(bolumler, gonder=not args.bildirme)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
