# Sahibinden Fırsat Tarayıcı

Her gün sahibinden.com'daki aramaları tarar, **piyasa fiyatının altındaki yeni ilanları** bulup
Telegram ve/veya e-posta ile gönderir.

- **Araç:** Tüm Türkiye. Fiyat, aynı seri/model + model yılındaki ilanların medyanıyla kıyaslanır.
  Yüksek KM nedeniyle ucuz olan ilanlar elenir.
- **Emlak (satılık/kiralık daire):** Bursa Osmangazi, Yıldırım, Nilüfer. **m² fiyatı**, aynı
  mahalle + oda sayısı (yetersizse ilçe) medyanıyla kıyaslanır.
- **Devren satılık / kiralık:** Aynı üç ilçe. m² varsa m² fiyatı, yoksa fiyat kıyaslanır.
- Varsayılan eşik: medyanın **en az %15 altı**. %50'den fazla ucuzsa "dolandırıcılık riski" uyarısı eklenir.
- İlanlar `ilanlar.db` içinde birikir. Program her gün çalıştıkça karşılaştırma havuzu büyür ve
  sonuçlar iyileşir. **İlk 1-2 gün az sonuç çıkması normaldir.**
- Aynı ilan ikinci kez gönderilmez. Fiyatı %5'ten fazla düşerse tekrar gönderilir.

> ⚠️ sahibinden.com sunuculardan (GitHub, bulut) gelen otomatik erişimi engelliyor. Bu yüzden program
> **kendi bilgisayarınızda**, normal bir Chrome penceresiyle çalışır. Siteyi yormamak için sayfalar
> arasında birkaç saniye bekler. Sitenin kullanım koşulları otomatik veri toplamayı kısıtlar. Kişisel
> ve makul ölçüde kullanın.

## 1. Kurulum (bir kez)

1. [Python 3.10+](https://www.python.org/downloads/) kurun. Windows'ta kurarken **"Add Python to PATH"** kutusunu işaretleyin.
2. Google Chrome kurulu olsun.
3. Bu klasörde bir komut satırı açıp şunları çalıştırın:
   ```
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

## 2. Bildirim ayarı

`.env.example` dosyasını `.env` adıyla kopyalayın ve doldurun.

**Telegram (önerilen, ücretsiz):**
1. Telegram'da **@BotFather**'a `/newbot` yazın ve bot oluşturun. Verilen token'ı `TELEGRAM_BOT_TOKEN` alanına yazın.
2. Botunuza herhangi bir mesaj gönderin.
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini açın. `"chat":{"id":123...}` değerini `TELEGRAM_CHAT_ID` alanına yazın.
4. Test edin: `python tarayici.py --test-bildirim`

**E-posta (Gmail):** Google hesabınızda 2 adımlı doğrulamayı açın, "Uygulama şifreleri"nden bir şifre
oluşturun ve `SMTP_*` / `EPOSTA_ALICI` alanlarını doldurun.

## 3. Aramaları kontrol edin (önemli)

`config.yaml` hazır aramalarla gelir. sahibinden.com bazen kategori adreslerini değiştirir. Bu yüzden
en garantili yöntem şudur:

1. sahibinden.com'da istediğiniz filtreyi seçin. Örnek: *Emlak > Konut > Satılık > Daire > Bursa > Osmangazi, Yıldırım, Nilüfer*.
2. Adres çubuğundaki URL'yi kopyalayın.
3. `config.yaml` içinde ilgili aramanın `url:` alanına yapıştırın.

Araçlarda tüm ilanlar yerine belirli marka ya da modelleri izlemek isterseniz, o filtrelerle yeni bir
arama ekleyebilirsiniz. Eşikleri `firsat:` bölümünden değiştirebilirsiniz.

## 4. İlk çalıştırma

```
python tarayici.py
```
Bir Chrome penceresi açılır. Site "robot musunuz" doğrulaması gösterirse 2 dakika içinde elle çözün.
Oturum `.tarayici-profili` klasöründe saklanır, sonraki günlerde genelde tekrar sormaz.
Sonuçlar Telegram/e-postaya gider. Ayrıca `raporlar/rapor-TARİH.html` olarak kaydedilir.

## 5. Her gün otomatik çalıştırma

**Windows:** *Görev Zamanlayıcı* > *Temel Görev Oluştur* > Günlük, örn. 08:00 > *Program başlat* >
`calistir.bat` dosyasını seçin. "Başlangıç konumu" alanına bu klasörün yolunu yazın.
Görevin özelliklerinde **"Yalnızca kullanıcı oturum açtığında çalıştır"** seçili kalsın,
çünkü tarayıcı penceresi görünür açılır.

**macOS / Linux:** `crontab -e` ile şu satırı ekleyin:
```
0 8 * * * cd /KLASOR/YOLU/sahibinden-tarayici && /usr/bin/python3 tarayici.py >> tarama.log 2>&1
```

Bilgisayar o saatte kapalıysa tarama yapılmaz. Görev Zamanlayıcı'da *"Kaçırılan görevi en kısa sürede çalıştır"* seçeneğini açabilirsiniz.

## Sorun giderme
- **"sayfa 1: hata/sonuç yok"**: URL değişmiş olabilir. 3. adımdaki gibi URL'yi siteden yeniden kopyalayın.
- **Sürekli doğrulama çıkıyor**: `bekleme_min/max` değerlerini artırın, `max_sayfa` değerlerini düşürün.
- **Hiç fırsat çıkmıyor**: Birkaç gün veri birikmesini bekleyin ya da `esik_oran` değerini 0.90 yapın.
