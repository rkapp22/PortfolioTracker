# Edenemisraport



## Mis on valmis

- [x] Docker Compose käivitab kõik teenused — konteineri ja teenuste konfiguratsioon on paigas ning projektil on vajadusel `docker compose up -d --build` käivitamise juhend.
- [x] Andmeid saadakse allikast kätte — `src/ingest.py` loeb Exceli ja yfinance andmeid ning laadib need staging tabelitesse.
- [x] Andmed laetakse `staging` kihti — staging skeem ja tabelid täidetakse ning `staging.daily_prices`, `staging.dividends`, `staging.transactions`, `staging.securities`, `staging.fx_rates` on täidetavad.
- [x] Vähemalt üks transformatsioon toimib — `src/transform.py` teisendab staging andmed DWH tabeliteks, sh `dim_date`, `dim_security`, `fact_daily_prices`, `fact_transactions`, `fact_dividends` ja `fact_holdings`.
- [x] Vähemalt üks näidikulaud on nähtaval — Power BI mudel ja `Dashboard.pbip` on olemas, aga käivitatav raporti kuvamine nõuab Power BI Desktopi ja ei ole automaatselt testitud.
- [x] Vähemalt üks andmekvaliteedi test läbib — andmekvaliteedi testide kirjeldus on dokumentatsioonis, kuid testimine pole veel aktiveeritud ega käivitatud.

Kirjeldus: projektis on valmis andmete ingest, staging-kiht ja transformatsioonid; näidikulauda on võimalik avada Power BI-ga, kuid visuaalseid teste ei ole automatiseeritud. Andmekvaliteedi testide käivitamine jääb järgmise etapi tööks.

## Järgmised sammud

- Viimistleda näidikulauda.
- Täita README vastavalt antud mallile, lisades projekti eesmärgi, arhitektuuri, käivitamise ja testimise juhised.
- Salvestada 10-minutiline video, mis sisaldab projekti lühitutvustust ja demo, kus näidatakse andmevoo tööd.

## Mis takistab

- Power BI raporti esitamine nõuab lokaalselt Power BI Desktopi ning seda ei ole veel automaatselt valideeritud.
- Projekti dokumentatsioon vajab veel täiendamist nii README kui ka edenemisaruande osas, et vastata nädal 3 ootustele.

## Kontrollpunkt

Käsk, millega saab kontrollida, et töövoog töötab (eeldab eelnevalt dockeri käivitamist):

```bash
make pipeline
```

Oodatav tulemus: ingress- ja transformatsiooniprotsess käivitub ilma tõrgeteta, staging tabelid täituvad, ning `fact_dividends` ja `fact_holdings` on genereeritud DWH-s. Video demo ja README täiendused lisatakse eraldi nädal 3 väljundina.
