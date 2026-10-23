# KVKK — metin şablonları ve veri envanteri (işletmeciler için)

> Bu sayfa hukuki danışmanlık değildir. Bir Entegratör Status kurulumunu işleten taraf
> (kurum ya da kişi) 6698 sayılı KVKK kapsamında **veri sorumlusudur**. Aşağıdaki
> envanter yazılımın ne işlediğini ve ne zaman sildiğini koddan çıkarılmış haliyle
> verir; metinleri kendi durumuna göre bir hukukçuyla birlikte uyarla.

Kurulumdaki aydınlatma metni (`/aydinlatma`) bir taslaktır; metni
`src/sentinel/web/i18n/tr.yaml` içindeki `privacy` bölümünde değiştir.

## Veri envanteri

| Veri | Kimin | Neden | Ne zaman silinir |
|---|---|---|---|
| Ad, e-posta, parolanın tek yönlü özeti (Argon2id) | Kullanıcılar | Hesap ve oturum | Hesap silinince; doğrulanmamış hesaplar 48 saat sonra |
| Oturum kayıtları | Kullanıcılar | Güvenlik | Süresi dolduktan 1 gün sonra |
| Kısaltılmış IP adresi (IPv4 /24, IPv6 /48) | Kullanıcılar | Denetim kaydı, kötüye kullanımı fark etmek | Kurum denetim kaydı kurumla birlikte; platform kayıtları 1 yıl |
| TOTP sırrı (şifreli) | 2FA açan kullanıcılar | İki aşamalı giriş | Kapatınca ya da hesap silinince |
| Davet edilen e-posta adresi | Davet edilenler | Kuruma davet | Süresi dolan, kabul ya da iptal edilen davetler 30 gün sonra |
| Uyarı alıcısı e-posta adresi / Telegram sohbet kimliği | Alıcılar | Uyarı göndermek | Onaylanmayanlar 48 saat, abonelikten çıkanlar 30 gün sonra |
| Durum sayfası abonesi e-posta adresi | Kurumların müşterileri | Olay bildirimleri | Onaylanmayanlar 48 saat, abonelikten çıkanlar 30 gün sonra |
| Uyarı ve gönderim kayıtları | — | Gönderim takibi | 180 gün |
| Düzeltme talebi (konu, metin, isteğe bağlı e-posta) | Talep gönderenler | Ölçüm düzeltmeleri | Karardan 1 yıl sonra |
| Kurumların kimlik bilgileri (API anahtarı vb., şifreli) | Kurumlar | Yetkili kontroller | Kurum silince ya da kurumla birlikte |
| Kapatılan kurumun tüm verisi | Kurumlar | — | Kapatmadan 30 gün sonra |

Ölçüm sonuçları kişisel veri değildir (servislerin erişilebilirliği): ham gözlemler 30
gün, turlar 120 gün, saatlik özetler 400 gün tutulur. Yanıt içerikleri saklanmaz.

Çerezler yalnızca zorunlu olanlardır: oturum (`__Host-es_session`), giriş öncesi form
güvenliği (`__Host-es_pre`, 24 saat), davet (`__Host-es_invite`, 1 saat). İzleme,
reklam ya da analitik çerezi, üçüncü taraf betik yoktur. Herkese açık durum sayfası,
Atom akışı ve rozet hiç çerez koymaz; yalnızca abonelik formu form güvenliği çerezini
kullanır.

Aktarım: e-postalar senin SMTP sağlayıcın üzerinden, Telegram uyarıları (açtıysan)
Telegram üzerinden, webhook uyarıları kurumların belirlediği adreslere gider. Sunucu ya
da e-posta sağlayıcın yurt dışındaysa KVKK md. 9'u değerlendir.

## Aydınlatma metni şablonu

```text
AYDINLATMA METNİ

Veri sorumlusu: <VERİ_SORUMLUSU_UNVANI>, <ADRES>, <İLETİŞİM_E-POSTASI>.

İşlenen kişisel veriler: ad, e-posta adresi, parolanın tek yönlü özeti, oturum ve
güvenlik kayıtları (IP adresinin kısaltılmış hali), iki aşamalı giriş kullanılıyorsa
şifrelenmiş TOTP sırrı; uyarı alıcısı ya da durum sayfası abonesi olarak bildirilen
e-posta adresi veya Telegram sohbet kimliği; düzeltme talebiyle gönderilen bilgiler.

Amaçlar: hesabın oluşturulması ve güvenliği, hizmetin sunulması (bağımlılık izleme,
uyarılar, durum sayfaları), talep ve şikâyetlerin yanıtlanması, yasal yükümlülüklerin
yerine getirilmesi.

Hukuki sebepler (KVKK md. 5/2): sözleşmenin kurulması ve ifası (c), veri sorumlusunun
hukuki yükümlülüğü (ç), ilgili kişinin temel hak ve özgürlüklerine zarar vermemek
kaydıyla veri sorumlusunun meşru menfaati (f) — hizmet güvenliği. Uyarı ve durum sayfası
abonelikleri açık onayla (çift onaylı e-posta) başlar ve her mesajdaki bağlantıyla tek
tıkla sona erer.

Aktarım: e-posta gönderimi için <SMTP_SAĞLAYICISI>; barındırma için <BARINDIRMA_SAĞLAYICISI>.
<YURT_DIŞI_AKTARIM_VARSA_DAYANAĞI>.

Saklama süreleri: doğrulanmamış hesaplar 48 saat; onaylanmamış uyarı alıcıları ve
aboneler 48 saat; abonelikten çıkanlar 30 gün; uyarı geçmişi 180 gün; sonuçlanan
düzeltme talepleri 1 yıl; kapatılan kurumların verisi 30 gün sonra silinir. Hesabını
sildiğinde kişisel verilerin silinir.

Haklarınız (KVKK md. 11): verilerinizin işlenip işlenmediğini öğrenme, bilgi talep etme,
düzeltilmesini ya da silinmesini isteme ve diğer haklarınız için <BAŞVURU_ADRESİ>
adresine başvurabilirsiniz. Hesap ayarlarınızdan verilerinizi dışa aktarabilir ve
hesabınızı silebilirsiniz.
```

## Durum sayfası aboneliği için kısa metin

Abonelik formunun altında zaten şu bilgi gösterilir: her e-postada tek tıkla abonelikten
çıkma bağlantısı vardır, onaylanmayan adresler 48 saat sonra silinir ve aydınlatma
metnine bağlantı verilir. Aboneler kurumların müşterileri olduğundan, durum sayfasını
yayınlayan kurumla aranızdaki rol dağılımını (veri sorumlusu / veri işleyen) yazılı hale
getir.

> 🇹🇷 **VERBİS:** Kayıt yükümlülüğü çalışan sayısı ve yıllık bilanço gibi ölçütlere
> bağlıdır; kendi durumunu Kurul'un güncel kararlarına göre kontrol et.
