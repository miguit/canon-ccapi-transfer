# canont – kuvat Canon-kamerasta langattomasti

Selainkäyttöliittymä ja komentorivityökalu, joilla kuvat siirretään Canon EOS M50 Mark II:sta
(ja muista CCAPI-kameroista) Linux-koneelle Wi-Fin yli.

- ruudukko kortin kuvista pikkukuvineen, kansioittain
- suodattimet JPEG / RAW / video ja "vain uudet"
- valinta klikkaamalla, vaihto+klikkaus valitsee välin, `N` valitsee kaikki siirtämättömät
- esikatselu (tuplaklikkaus tai välilyönti, nuolinäppäimet selaavat)
- siirto taustalla edistymispalkin kanssa, kuvauspäivän mukaisiin alikansioihin
- jo siirretyt merkitään eikä niitä siirretä uudelleen (nimi + koko)
- esikatselussa valotustiedot (suljinaika, aukko, ISO, valotuksen korjaus, polttoväli, tila),
  luetaan esikatselukuvan EXIF:stä (`canont/exif.py`), ei ylimääräisiä kamerapyyntöjä
- **Etsin** (E): kameran live-kuva, histogrammi, kuvausasetukset ja laukaisin (välilyönti).
  Live view on päällä kamerassa vain etsimen ollessa auki (sammuu 5 s sulkemisen jälkeen)
- **Live**-tila (L): jokainen uusi kuva avautuu heti isona, F tai tuplaklikkaus = koko näyttö.
  Palvelin kysyy kameran tapahtumia (`event/polling`) sekunnin välein vain, kun Live on päällä
- valittujen poisto kameran kortilta (Poista tai Delete-näppäin). Vahvistus varoittaa, jos
  tiedostoa ei ole siirretty koneelle eikä NAS:lle. Kamerassa suojattuja ei poisteta
- toimii ilman nettiä: fontit ja ikonit ovat paketissa, koska kameran tukiasemaan
  liitetty kone on offline

## 1. Kameran valmistelu (kerran)

Kamera käyttää Canonin **CCAPI**-rajapintaa (HTTP, portti 8080). Se pitää aktivoida kerran:

1. Rekisteröidy ilmaiseksi Canonin kehittäjäohjelmaan (Canon Developer Programme /
   Canon Developer Community) ja pyydä pääsy CCAPI:hin.
2. Lataa sieltä **CCAPI Activation Tool** (Windows tai macOS).
3. Kytke kamera USB:llä koneeseen ja aja aktivointi.

Aktivoinnin jälkeen kameran Wi-Fi-valikkoon tulee CCAPI-yhteys. Valitse tukiasematila
(kamera luo oman verkon) ja liitä Linux-kone siihen.

## 2. Asennus

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 3. Käyttö

```bash
.venv/bin/canont                 # käynnistää palvelimen ja avaa selaimen (http://localhost:8765)
.venv/bin/canont list            # listaa kortin tiedostot, ✓ = jo siirretty
.venv/bin/canont pull            # siirrä kaikki uudet
.venv/bin/canont pull --kind raw # vain RAW:t
.venv/bin/canont --camera 192.168.1.2 serve   # kun IP on tiedossa
.venv/bin/canont --dest ~/Kuvat/M50 serve     # paikallinen kohdekansio (tallentuu)
.venv/bin/canont --nas /run/user/$UID/gvfs/smb-share:server=…,share=homes/<käyttäjä>/Photos/Canon serve
.venv/bin/canont --target nas pull            # siirrä uudet NAS:lle
```

Kohteita on kaksi: **Kone** ja **NAS**. Työkalupalkin kytkin valitsee kohteen, ja kansiot
asetetaan Asetuksista. NAS-kansio on NAS:n SMB-liitos (esim. tiedostonhallinnasta avattu
gvfs-liitos). Synology Photos indeksoi `homes/<käyttäjä>/Photos`-kansioon tulevat kuvat itse.
"Jo siirretty" pidetään kummallekin kohteelle erikseen. Jos NAS-liitos puuttuu, siirto ei käynnisty.

Kamera löytyy automaattisesti tässä järjestyksessä: viimeksi käytetty IP, SSDP-haku,
oletusyhdyskäytävä (tukiasematilassa kamera on yleensä se), lähiverkon /24-skannaus.

Oletuskohde on `~/Kuvat/Canon/VVVV-KK-PP/` (XDG-kuvakansio). Asetukset ja siirtohistoria
ovat tiedostoissa `~/.local/share/canont/`, pikkukuvavälimuisti on `~/.cache/canont/`.

## Kehitys ilman kameraa

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python tools/mock_camera.py --port 18080 &
.venv/bin/canont --camera 127.0.0.1 --camera-port 18080 serve
.venv/bin/python -m pytest tests
```

Valekamera matkii CCAPI:n listausta (100 tiedostoa / sivu), pikkukuvia, latausta, poistoa,
tapahtumia, live view'tä ja laukaisinta (`curl -X POST localhost:18080/mock/shoot` = ota kuva).
Se vastaa myös 503 rinnakkaisiin pyyntöihin kuten oikea kamera.

## Rakenne

| Tiedosto | Tehtävä |
|---|---|
| `canont/ccapi.py` | CCAPI-asiakas: pyynnöt sarjoitettu lukolla, 503 yritetään uudelleen |
| `canont/discovery.py` | kameran etsintä (SSDP, yhdyskäytävä, skannaus) |
| `canont/library.py` | yhteys, kohteet (kone/NAS), siirtohistoria, välimuistit, siirto- ja poistojono, Live, etsin |
| `canont/exif.py` | valotustiedot esikatselukuvan EXIF:stä |
| `canont/web.py` | FastAPI JSON-rajapinta + staattiset tiedostot |
| `canont/static/` | käyttöliittymä (HTML/CSS/JS, ei build-vaihetta) |
| `canont/cli.py` | komentorivi |
| `tools/mock_camera.py` | valekamera kehitykseen ja testeihin |
| `tools/canon_ble.py` | kokeilu: kameran herätys Bluetoothilla (`pip install -e '.[ble]'`), ei toimi M50 II:lla |
