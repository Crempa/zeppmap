# Co víme o hodinkách

Poznatky o tom, jak hodinky Amazfit se Zepp OS ukládají a kreslí offline mapy. Zjištěno v listopadu 2026 na Amazfit Balance 3 Ti: rozborem dlaždic stažených z hodinek, zkušebními dlaždicemi (vzorníky) a fotkami displeje. Na jiných modelech a verzích firmwaru se může chování lišit.

## Uložení map

- **Umístění:** základní mapa je v `map/res/scl/11/<x>/<y>.img`, vrstevnice v `map/res/ctl/11/<x>/<y>.img`, lyžařská vrstva v `map/res/ski/11/<x>/<y>.img`, evidence stažených map v `map/config/`. Čísla dlaždic odpovídají běžnému dělení webových map při přiblížení 11; jedna dlaždice má ve střední Evropě zhruba 12,5 × 12,5 km.
- **Velikost:** dlaždice základní mapy má kolem 0,5 MB, vrstevnice průměrně 0,23 MB (v horách až 1 MB), značky 0,03 MB. Celé Česko s okolím je 2 048 dlaždic. Hodinky mají podle výrobce úložiště 64 GB.
- **Evidence:** `map_config.json` drží přepínače vrstev a součty velikostí. `mapfile_downloaded_files.txt` je binární záznam stažené oblasti: hranice, velikosti v jednotkách 256 B, verze a čas. Rozpracované stahování je v `mapfile_resume_task.txt` a `resume_tasks/`. Vlastní dlaždice se zobrazí, i když v evidenci nejsou.

## Stavba dlaždic

- **Původ:** Zepp dlaždice vyrábí nástrojem mkgmap. Mají popis „OSM street map“, úrovně 0:24, 1:22, 2:20, 3:18 a přehledovou 4:17, popisky v UTF‑8, ID mapy složené z čísel dlaždice (`11130692` pro x 1113, y 692) a hranice přesně podle dlaždice.
- **Obsah:** základní dlaždice nesou i podklady pro navigaci (části NET a NOD). Dlaždice vrstevnic mají šest úrovní (24, 22, 20, 18, 16, 15) a obsahují jen čáry typů 0x20 až 0x23 a podklad; výšky jsou v popiscích ve stopách a hodinky je převádějí na metry.
- **Styl:** žádná dlaždice neobsahuje definici stylu (TYP). Vzhled určuje firmware podle kódu typu. Zepp používá vlastní sadu typů: běžné čáry 0x01 až 0x3b a řadu rozšířených, hlavně 0x10d01 až 0x10d1b.
- **Lyžařská vrstva:** dlaždice mají stejnou stavbu a úrovně jako základní mapa, ale v hlavičce značky `HMSNOW`/`HMSKIM` místo `DSKIMG`/`GARMIN`. Čáry mají vlastní typy 0x40 až 0x64, do dat uložené jako typ 0x00 až 0x3f s bitem „jednosměrná“. mkgmap takové typy neumí, proto se dlaždice sestaví s náhradními a pak upraví (`zeppmap/skitile.py`).
- **Lyžařská mapa Zeppu** podle názvů v dlaždicích Krkonoš: 0x40 černá sjezdovka, 0x43 červená, 0x46 modrá, 0x49 dětské a cvičné, 0x51 až 0x60 běžecké tratě, 0x63 vleky a lanovky dohromady; plocha 0x71 je lyžařský areál.

## Zobrazení

- **Měřítka:** 8 m, 15 m, 25 m, 50 m, 75 m, 100 m, 200 m, 300 m, 500 m, 750 m, 1 km, 1,5 km, 2 km, 3 km a dál.
- **Výběr úrovně dlaždice:** hodinky vybírají úroveň podle pořadí od přehledové úrovně, ne podle počtu bitů. Na měřítkách 8 až 100 m kreslí úroveň čtyři místa pod přehledovou, na 200 až 750 m tři místa pod ní, na 1 a 1,5 km dvě. V dlaždici s pěti úrovněmi (základní mapa) je to na 8 až 100 m ta nejpodrobnější, v dlaždici se šesti úrovněmi (vrstevnice) až druhá. Ověřeno zkušební dlaždicí se sedmi úrovněmi (`ctlmerge.probe_tile`): na 8 až 100 m se kreslila třetí nejpodrobnější úroveň, na 200 až 750 m čtvrtá a dvě nejpodrobnější nikdy.
- **Zjednodušení čar:** mkgmap čáry v úrovních pod 24 bitů zjednodušuje (z 6 561 bodů značek nechal v úrovni s 23 bity 1 503). Dlaždice se značkami proto musí mít na 8 až 100 m plných 24 bitů, jinak jsou trasy zblízka hranaté.
- **Lyžařská vrstva** se kreslí nad základní mapou i tam, kde žádná oficiální lyžařská mapa stažená není, a u čar zobrazuje názvy. Od měřítka 2 km se nekreslí.
- **Vrstvy při aktivitě:** běžná aktivita kreslí jen základní mapu a vrstevnice. Lyžařskou vrstvu firmware povoluje podle skupiny sportu, ne podle nastavení: podle firmwaru simulátoru Zepp OS pro Balance 2 se zobrazí jen u sportů ze skupin lyžování, běžecké lyžování a skialpinismus. Do skupiny běžeckého lyžování ale patří i nesněhové sporty:

  | Skupina | Sporty |
  |---|---|
  | lyžování | lyžování, sjezdové lyžování, snowboard |
  | běžecké lyžování | běžecké lyžování, biatlon, sněžnice, orientační běh, lov, venkovní bruslení, skateboard, sněžný skútr, sáňky |
  | skialpinismus | skialpinismus |

  Žádný cyklistický sport v těchto skupinách není. Přepínače „Zobrazit lyžařskou mapu“ a „Používat mapu při tréninku“ v nastavení hodinek jsou podmínka nutná, ne postačující. Na Balance 3 je to ověřené jen tím, že lyžařská vrstva při běžné aktivitě opravdu chybí.
- **Lyžařský formát ve vrstvě vrstevnic:** dlaždice lyžařské vrstvy (značky `HMSNOW`/`HMSKIM`) nahraná do `map/res/ctl` se vykreslí stejně jako v lyžařské vrstvě, a to i při běžné aktivitě. Hodinky tedy čtou typy čar podle hlavičky dlaždice, ne podle vrstvy. Na tom stojí celý nástroj (`zeppmap/ctlmerge.py`): skládá dlaždici z oficiálních vrstevnic a ze značek. Vrstevnice (typy 0x20 až 0x23) v ní zůstávají bez příznaku směru a hodinky je kreslí stejně jako z původní dlaždice. Lanovky mají typ 0x64, který se na rozdíl od 0x63 neukládá stejně jako vrstevnice 0x23.
- **Vrstva vrstevnic od 2 km:** dlaždici v lyžařském formátu hodinky od měřítka 2 km nekreslí ani ve vrstvě vrstevnic. Značky jsou tedy vidět na 8 m až 1,5 km.
- **Původní vrstevnice jen zblízka:** vrstevnice z původních dlaždic Zeppu hodinky kreslí jen při velkém přiblížení: na 25 a 50 m ano, na 100 m už ne. Úrovně pro větší měřítka v dlaždicích Zeppu jsou, ale nekreslí se.
- **Šířky čar:** čára má na displeji stálou tloušťku, v mapě tedy zabírá na každém měřítku jinak. Změřeno ze vzorníku šířek (`ctlmerge.width_tile`: vodorovné čáry všech typů přesně na mřížce, v názvu kód typu): plná značka má 0,8 m na 8 m, 5 m na 50 m, 9 m na 100 m a 19 m na 200 m; cyklotrasa, naučná stezka, žlutá a černá jsou na 8 až 100 m asi o pětinu širší. Celá tabulka je u `ctlmerge.SPACING`.
- **Názvy** jsou v dlaždicích velkými písmeny a hodinky je samy převádějí na malá. Umějí to u písmen bez diakritiky a u písmen s čárkou; písmena s háčkem a kroužkem nechávají velká, proto „KunĚtická“ nebo „BŘehy“. V dlaždici u Pardubic se to týká 856 z 3 725 různých názvů.
- **Čísla v základní mapě:** osmimístná čísla na mapě (například 59136382) jsou názvy ploch v základní mapě Zeppu, které místo jména nesou číslo. Hodinky je ukazují jen při velkém přiblížení.
- **Simulátor Zepp OS** pouští v emulátoru QEMU firmware sestavený pro jednotlivé modely. Obsahuje sportovní služby a prostředky uživatelského rozhraní, ale ne mapové jádro, takže vykreslování dlaždic se v něm zkoušet nedá.

## FTP server

- Běží jen nad hotspotem hodinek, na adrese 192.168.43.1:5210 s uživatelem anonymous. Přes domácí Wi‑Fi ho hodinky nespustí.
- Je postavený na FreeRTOS+FAT, umí jen základní příkazy a občas přeruší přenos větší dlaždice (chyba 451). Nástroj proto přenos opakuje a stahování umí navázat.
- Při zápisu existující soubor nezkracuje: po nahrání menší dlaždice přes větší za ní zůstal konec té původní. Nástroj proto starý soubor před zápisem maže a obsah po nahrání kontroluje.
- Na čtení neexistujícího souboru odpoví stejně jako na výpadek přenosu a nechá po něm prázdný soubor.
- Rychlost: čtení i zápis kolem 0,5 až 0,9 MB/s.

## Jak hodinky kreslí čáry základní mapy

Zjištěno vzorníkem při měřítku 50 až 100 m.

| Typ | Vzhled |
|---|---|
| 0x01 | silná červená |
| 0x02 | silná oranžová |
| 0x03, 0x04 | silná žlutá (obě stejně) |
| 0x05 | silná tmavě šedá |
| 0x06, 0x09 | střední tmavě šedá |
| 0x08 | tenká tmavě šedá |
| 0x0a | čárkovaná tmavě šedá |
| 0x14 | tenká modrá |
| 0x16 | tenká tmavá, do zelena |
| 0x18 | střední světle modrá |
| 0x1f | silná světle modrá |

Všechny ostatní běžné typy (0x07, 0x0b až 0x13, 0x15, 0x17, 0x19 až 0x1e, 0x20 až 0x3f) se při těchto měřítkách nekreslí, stejně jako rozšířené 0x10d05 až 0x10d1b. Část z nich přitom Zepp ve svých dlaždicích používá; proč je hodinky nekreslí, nevíme.

Zelenou čáru základní mapa nemá. Tence zeleně se kreslí jen vrstevnice z vrstvy `ctl`, a to i přes vlastní dlaždici. Proto značky nejdou udělat úpravou základní mapy.

## Jak hodinky kreslí čáry lyžařské vrstvy

Zjištěno vzorníkem při měřítku 50 m. Pět základních barev, každá v několika provedeních:

| Barva | Plná | Se žlutým lemem | S bílým středem | S bílými čárkami |
|---|---|---|---|---|
| černá | 0x40, 0x5d | 0x41, 0x50 | 0x4f | 0x51 |
| červená | 0x43, 0x5e, tmavší 0x45 | 0x44, 0x53 | 0x52 | 0x54 |
| modrá | 0x46, 0x5f | 0x47, 0x56 | 0x55 | 0x57 |
| zelená (do modra) | 0x49, tmavší 0x60 | 0x4a, 0x59 | 0x58 | 0x5a |
| lososová až oranžová | 0x4c, 0x4e, 0x61 | 0x4d, 0x5c | 0x5b | |

Další: 0x42 šedá s černými čárkami, 0x48 světle modrá čárkovaná, 0x4b tmavě zelená se světlými čárkami, 0x62 až 0x64 tenká okrová s fialovými příčnými čárkami (všechny tři stejně). Typy 0x65 až 0x7e se nekreslí.

Čistě žlutou čáru lyžařská vrstva nemá. Nejblíž je oranžová se žlutým lemem (0x5c).

## Vzorníky

Jak který typ vypadá, ukáže vzorník: zkušební dlaždice pokrytá opakující se stránkou, na které je každý zkoušený typ jednou a s popiskem.

| Stránka | Co zkouší | Vrstva |
|---|---|---|
| `lines` (výchozí) | běžné typy čar 0x01 až 0x3f a rozšířené typy, které používá Zepp | `scl` |
| `areas` | plochy 0x01 až 0x5f a rozšířené, překryvy čar, rozšířené typy čar | `scl` |
| `contours` | všechny běžné typy čar ve vrstvě vrstevnic, tedy jako překryv nad základní mapou; patří ke stránce `areas` | `ctl` |
| `overlay` | stejné čáry jako `lines`, ale se stavbou dlaždice vrstevnic | `ctl` nebo zkoušená |
| `ski` | typy čar 0x40 až 0x7e a plochy 0x50 až 0x7f lyžařské vrstvy, nad původní základní mapou | `ski` |
| `ctl` | všechny běžné typy čar ve vrstvě vrstevnic, v rámu z vrstevnic; názvy jsou čísla, protože ta vrstva ukazuje jen výšky | `ctl` |
| `zepp` | jen typy, které Zepp používá ve svých dlaždicích: plochy, body (s ikonou) a čáry, pravou polovinou přes les; typy se čtou z oficiálních dlaždic ve zdrojové složce a legenda k nim uvádí význam podle stylu mkgmap a ukázky názvů | `scl` |

```bash
.venv/bin/python -m zeppmap swatch from-watch/map/res/scl swatch-ski --only 1113/694 --page ski
```

```bash
.venv/bin/python -m zeppmap push swatch-ski --layer ski
```

Bez `--only` se vzorník vyrobí pro všechny dlaždice ve složce. Jak ho číst, popisuje `legend.txt` ve výstupní složce.

Vzorník dočasně nahradí dlaždici, která v hodinkách na tom místě je. Původní se při nahrání uloží do `backups/<čas>/` a vrátí se příkazem `push` s touto složkou a stejnou vrstvou.
