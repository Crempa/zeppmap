# Technické detaily

Doplněk k [README](../README.md) pro ty, kdo chtějí vědět, jak nástroj pracuje, nebo ho upravovat. Poznatky o tom, jak hodinky ukládají a kreslí mapy, jsou v [samostatném dokumentu](poznatky-o-hodinkach.md).

Všechny příkazy se spouštějí ve složce projektu jako `.venv/bin/python -m zeppmap <příkaz>`. Výpisy programu jsou anglicky.

## Jak se nástroj dostane k hodinkám

1. **Bluetooth LE.** Najde hodinky (knihovna [bleak](https://github.com/hbldh/bleak)), přihlásí se auth klíčem a zjistí jejich služby. Protokol Zepp OS (rámcování zpráv, výměna klíčů na křivce B‑163, šifrování AES) je převzatý z Gadgetbridge.
2. **Wi‑Fi hotspot.** Požádá hodinky o zapnutí vlastního hotspotu s náhodným jménem a heslem a připojí k němu počítač: na macOS přes `networksetup`, na Linuxu přes `nmcli`, na Windows přes `netsh` s dočasným profilem sítě. S volbou `--manual-wifi` se nepřipojuje sám, jen vypíše jméno a heslo sítě a čeká, až se k ní připojíte.
3. **FTP.** Zapne FTP server hodinek a soubory přenáší obyčejným FTP. Každou nahranou dlaždici stáhne zpět a porovná; přepisovanou dlaždici napřed zálohuje.
4. **Úklid.** Na konci FTP server i hotspot vypne a počítač se vrátí na svou Wi‑Fi. Na Linuxu a Windows nástroj ještě smaže profil sítě hotspotu, který si pro připojení vytvořil.

## Příkaz sync

```bash
.venv/bin/python -m zeppmap sync
```

1. Připojí se k hodinkám a přečte, pro které dlaždice v nich je základní mapa a co je teď ve vrstvě vrstevnic. Původní dlaždice vrstevnic, které ještě nemá na disku, stáhne do složky `zepp-contours`.
2. Vypne hotspot, počká, až se Mac vrátí na internet, a stáhne pro ty dlaždice trasy a lanovky. Stahuje po blocích 4 × 4 dlaždice a výsledek si ukládá do `data/sync`; co není starší než 30 dní, znovu nestahuje.
3. Sestaví dlaždice do složky `build/contours`, blok po bloku a několik dlaždic najednou: značky složené s původními vrstevnicemi. Kde hodinky vrstevnice nemají, vyjde dlaždice jen se značkami. Dlaždici, jejíž obsah se nezměnil, nechá být.
4. Připojí se podruhé a nahraje jen dlaždice, které se liší od těch v hodinkách. Když se nic neliší, podruhé se nepřipojuje.

Co je v hodinkách, poznává podle velikostí dlaždic a vlastního záznamu o nahrání (`build/contours/uploaded.json`), takže nemusí číst obsah celé vrstvy. Seznam dlaždic přečtený z hodinek si ukládá do `data/sync/tiles.json`.

Počítá i s velkou mapou. Pro 2 048 dlaždic (Česko se širokým okolím) je to 128 bloků: stažení tras trvá kolem půl hodiny a zabere asi 0,9 GB na disku. První běh do vrstvy vrstevnic je navíc delší kvůli vrstevnicím samotným: pro tuto mapu se z hodinek četlo 468 MB (9 minut), sestavení trvalo 20 minut a nahrávalo se 396 MB (14 minut). Přerušený běh stačí pustit znovu; stažené bloky, uložené vrstevnice, sestavené dlaždice i záznam o nahraných zůstávají.

**Původní vrstevnice.** Sloučená dlaždice původní vrstevnice v hodinkách přepíše, proto je `sync` napřed ukládá do `zepp-contours` a všechny další dlaždice skládá z této uložené podoby. Ukládá do ní jen dlaždice v původním formátu, nikdy vlastní. Když v hodinkách najde vlastní dlaždici a původní na disku nemá, hledá ji v zálohách (`backups/*-ctl`); když ji nenajde ani tam, dlaždici nechá beze změny a vypíše ji. Dlaždici vrstevnic s jinou stavbou nebo obsahem, než jaké má Zepp, také nechá beze změny. Původní vrstevnice vrátíte do hodinek příkazem:

```bash
.venv/bin/python -m zeppmap push zepp-contours --layer ctl
```

| Volba | Význam |
|---|---|
| `--layer ski` | značky samotné do lyžařské vrstvy místo do vrstvy vrstevnic; sestavují se do složky `build/ski`. Lyžařskou vrstvu ale hodinky ukazují jen u lyžařských sportů a v oblastech se značkami z ní zmizí lyžařská mapa Zeppu. |
| `--contours SLOŽKA` | kam ukládat původní dlaždice vrstevnic (výchozí `zepp-contours`) |
| `--dry-run` | vše připravit a vypsat, co by se nahrálo, ale nenahrávat; původní vrstevnice se z hodinek stáhnou i tak |
| `--refresh` | stáhnout trasy znovu hned |
| `--max-age DNŮ` | po kolika dnech stahovat trasy znovu (výchozí 30) |
| `--like ZDROJ` | seznam dlaždic vzít ze složky nebo ze souboru `tiles.json` místo z hodinek a k hodinkám se před sestavením nepřipojovat; do vrstvy vrstevnic se pak nenahrává |
| `--jobs N` | kolik dlaždic překládat najednou (výchozí 4) |
| `--full-check` | porovnat s hodinkami obsah každé dlaždice, ne jen velikost; u velké mapy trvá dlouho |
| `--out`, `--cache`, `--backup-dir` | jiné složky pro dlaždice, mezipaměť a zálohy |

## Co se kreslí

| Značka | Typ čáry | Vzhled v hodinkách |
|---|---|---|
| KČT červená | 0x43 | plná červená |
| alpská červeno-bílo-červená | 0x43 | plná červená, v názvu číslo trasy |
| KČT modrá | 0x46 | plná modrá |
| KČT zelená | 0x49 | plná zelená do modra |
| KČT žlutá | 0x5c | oranžová se žlutým lemem (čistě žlutou vrstva nemá) |
| černá (polské značení) | 0x50 | černá se žlutým lemem |
| naučná stezka | 0x5a | zelená s bílými čárkami |
| cyklotrasa | 0x54 | červená s bílými čárkami, v názvu číslo trasy |
| lanovka | 0x64 | tenká okrová s fialovými příčnými čárkami, v názvu jméno |

Typy čar jsou z lyžařské vrstvy hodinek; jak je hodinky kreslí, popisuje [dokument o hodinkách](poznatky-o-hodinkach.md#jak-hodinky-kreslí-čáry-lyžařské-vrstvy). V tmavém režimu hodinky barvy samy přizpůsobí, červená vychází růžově a modrá světle modře. Přiřazení typů je v `zeppmap/routes.py` (`MARK_TYPES`, `LIFT_TYPE`).

### Které trasy se berou

- **Značky KČT.** Pásová značka a tvarové značky odboček v barvě trasy, naučné stezky zvlášť. Stejně vypadá značení v Polsku, které má navíc černou barvu (ověřeno na polské straně Krkonoš).
- **Alpské značení.** V Rakousku, Bavorsku a Itálii mají skoro všechny trasy stejnou červeno-bílo-červenou značku (u italského CAI červenou s bílým pruhem) a liší se číslem. Kreslí se proto červeně a číslo trasy je v názvu čáry. Víc tras po jedné cestě dá jednu čáru se všemi čísly, nejvýš čtyřmi; zbytek nahradí plus.
- **Cyklotrasy.** Všechny silniční i terénní, s číslem v názvu čáry.
- **Lanovky.** Kabinové, sedačkové a kombinované, na všech úrovních přiblížení.

Nekreslí se:

- pěší trasy s jiným symbolem, například Svatojakubská cesta s mušlí, místní okruhy nebo dálkové trasy s vlastním znakem (Via Alpina, Alta Via), i když mají v datech uvedenou barvu; většinou vedou po trasách, které se kreslí,
- trasy, které v datech žádný symbol nemají,
- vleky, pojízdné pásy, nákladní lanovky a stanice.

Na vzorku tří alpských dlaždic se tak nakreslí 67 z 85 pěších tras u Tre Cime, 56 z 91 pod Dachsteinem a 42 z 59 u Garmische. Značení v německých středohořích (kříže, tečky, kosočtverce, písmena) nástroj zatím neumí.

Názvy lanovek jsou bez diakritiky. Hodinky převádějí velká písmena na malá jen u písmen bez háčků a kroužků, takže by název jinak vyšel napůl velkými písmeny, jako „ŠpindlerŮv Mlýn“ v oficiální mapě. Která lanovka jezdí v létě, v datech skoro není, takže se to nerozlišuje.

### Souběžné značky

Vede-li po cestě víc značek, kreslí se vedle sebe v pořadí červená, modrá, zelená, žlutá, černá, naučná stezka, cyklotrasa. Čára má na displeji stálou tloušťku, takže při oddálení zabírá v mapě víc metrů a značky se stejným rozestupem by splynuly. Každá úroveň přiblížení v dlaždici má proto vlastní polohu čar (`ctlmerge.SPACING`, pro lyžařskou vrstvu `routes.LEVEL_SPACING`):

| Úroveň | Měřítka v hodinkách | Tloušťka čáry na displeji | Rozestup ve vrstvě vrstevnic | Rozestup v lyžařské vrstvě |
|---|---|---|---|---|
| 0 | 8 m až 100 m | 8 až 10 bodů | 6 m | 9 m |
| 1 | 200 m, 300 m, 500 m, 750 m | z 8 na 3 body | 28 m | 36 m |
| 2 | 1 km, 1,5 km | asi 2 body | 40 m | 40 m |
| 3 | hodinky nekreslí | | přehled, bez rozestupu | přehled, bez rozestupu |

Rozestupy pro vrstvu vrstevnic vycházejí z šířek čar změřených na vzorníku; tabulka šířek je u `ctlmerge.SPACING`. Jedna úroveň slouží několika měřítkům, takže rozestup je vždy kompromis: při 6 m se značky na 50 m právě dotýkají, na 100 m se z poloviny překrývají a na 8 m je mezi nimi vidět cesta.

Před posunem se navazující cesty se stejnými značkami spojí do souvislých tras. Ostré rohy se na vnější straně zaoblí, na vnitřní se vypustí body, které by dělaly smyčky, a tam, kde se počet souběžných značek mění, se konce čar téže značky propojí. Úsek kratší než dva rozestupy se nerozestupuje, značky na něm zůstanou přes sebe.

Úroveň 3 je přehled pro největší oddálení. Hodinky ji zatím nezobrazují, v dlaždicích je pro případ, že to firmware změní. Každou cestu v ní zastoupí jedna pěší značka (v pořadí červená, modrá, zelená, žlutá, černá), naučné stezky se nekreslí a z cyklotras zůstanou jen dálkové, a to tam, kde nevede pěší značka. Za dálkové se berou mezinárodní a národní trasy a regionální s nejvýše trojciferným číslem; čtyřciferné trasy jsou místní, i když je OpenStreetMap vede jako regionální.

### Odkud jsou data

Jediným zdrojem je [OpenStreetMap](https://www.openstreetmap.org). Každá značená trasa je tam zapsaná jako celek se seznamem cest, po kterých vede, a s popisem značky. Zapisují to dobrovolníci; značky KČT prošla a zakreslila česká komunita v terénu, v Itálii trasy zadává i sám alpský klub CAI.

Data se stahují přes veřejnou službu Overpass. Dotaz obsahuje jen hranice oblasti a typ hledaných objektů, nic o hodinkách ani o uživateli. Servery bývají přetížené, nástroj jich proto zkouší postupně několik: `overpass.openstreetmap.fr`, `overpass-api.de`, `overpass.private.coffee` a `overpass.kumi.systems`. Všechny vydávají tatáž data. Provozovatelé žádají, aby jeden uživatel nestahoval víc než zhruba 1 GB za den; celé Česko je asi 0,9 GB.

Data © přispěvatelé OpenStreetMap, licence ODbL. Pro vlastní použití v hodinkách z ní nic neplyne; řešila by se při šíření vyrobených dlaždic.

### Jednotlivé kroky zvlášť

Při ladění vzhledu se hodí pustit kroky po jednom, nad zálohou dlaždic místo nad hodinkami:

```bash
.venv/bin/python -m zeppmap fetch-routes from-watch/map/res/scl data/routes.json
```

```bash
.venv/bin/python -m zeppmap routes data/routes.json from-watch/map/res/scl build/ski
```

```bash
.venv/bin/python -m zeppmap push build/ski --layer ski
```

První příkaz stáhne trasy pro oblast dlaždic v dané složce, druhý z nich vyrobí dlaždice, třetí je nahraje. S `--only X/Y` se první dva omezí na vybrané dlaždice. Tyto kroky vyrábějí dlaždice samotných značek pro lyžařskou vrstvu; skládání s vrstevnicemi umí jen `sync`. Zálohu dlaždic z hodinek udělá `pull map/res/scl from-watch/map/res/scl`.

## Příkazy

| Příkaz | Co dělá |
|---|---|
| `check` | Ověří, že je nainstalované vše potřebné: Python, knihovny, Java, mkgmap, auth klíč a povolení Bluetooth. K hodinkám se nepřipojuje. |
| `scan` | Najde hodinky v dosahu. Nepotřebuje klíč. |
| `services` | Přihlásí se a vypíše služby hodinek. Ověří klíč a ukáže, jestli hodinky nabízejí hotspot, FTP a mapy. Wi‑Fi se nedotkne. |
| `ls [cesta]` | Vypíše složku v hodinkách (výchozí `map`); s `-r` i podsložky a velikosti. |
| `pull cesta [kam]` | Stáhne soubor nebo složku (výchozí do `from-watch/`). Přerušené stahování umí navázat. |
| `push zdroj` | Nahraje dlaždice ze zipu nebo složky se strukturou `11/<x>/<y>.img`. |
| `sync` | Doplní do hodinek značky ke všem dlaždicím základní mapy: do vrstvy vrstevnic, s `--layer ski` do lyžařské. |
| `rm cesta` | Smaže zkušební dlaždice pod `map/res/<vrstva>`. Ve vrstvách `scl`, `ctl` a `ski` smaže jen jednotlivou dlaždici. |
| `info soubor.img` | Vypíše údaje o dlaždici: části, hranice, úrovně a typy objektů. Hodinky nepotřebuje. |
| `fetch-routes složka soubor` | Stáhne z OpenStreetMap značené trasy a lanovky pro oblast dlaždic ve složce. Hodinky nepotřebuje. |
| `routes data složka výstup` | Vyrobí z nich dlaždice pro lyžařskou vrstvu. Potřebuje Javu a mkgmap. |
| `swatch složka výstup` | Vyrobí vzorník pro dlaždice nalezené ve složce. Potřebuje Javu a mkgmap. |

Cesty v hodinkách jsou relativní k datovému oddílu `/mnt/data`.

Užitečné přepínače:

- `-v` vypíše komunikaci s hodinkami (pište ho před příkaz); `-vv` přidá i výpisy knihovny Bluetooth. Obsah zpráv, které hodinky posílají jiným službám (například žádost o počasí s tokenem k Zepp účtu a polohou), se nevypisuje.
- `--no-hotspot` u příkazů pracujících se soubory nezapne hotspot a zkusí FTP přes síť, na které už hodinky i Mac jsou. Na Balance 3 Ti to nefunguje, FTP server běží jen nad hotspotem hodinek.
- `--manual-wifi` (před příkazem i za ním) nepřipojí počítač k hotspotu hodinek sám. Vypíše jméno a heslo sítě, vy se k ní připojíte a nástroj pokračuje, jakmile hodinky uvidí; čeká až 5 minut. `sync` během jednoho běhu zapíná hotspot dvakrát, pokaždé se stejným jménem i heslem. Hodí se tam, kde automatické připojení selže.
- `push --dry-run` jen vypíše, co by se nahrálo.
- `push --verify full|size|none` určuje kontrolu po nahrání. Výchozí `full` stáhne každou dlaždici zpět a porovná ji; poškozená dlaždice umí hodinky při otevření mapy shodit.
- `push --layer JMÉNO` nahraje do `map/res/<jméno>`: `scl` je základní mapa (výchozí), `ctl` vrstevnice, `ski` lyžařská vrstva.
- `push --no-backup` vypne zálohu. Jinak se každá přepisovaná dlaždice nejdřív uloží do `backups/<čas>/`.

Auth klíč se čte ze souboru `~/.zeppmap-key`, z proměnné `ZEPPMAP_KEY` nebo ze souboru zadaného volbou `--key-file`. Jinou cestu k Javě nebo mkgmap určí proměnné `ZEPPMAP_JAVA` a `ZEPPMAP_MKGMAP`.

## Řešení potíží

| Co se děje | Co s tím |
|---|---|
| `scan` hodinky nenajde | Hodinky jsou nejspíš připojené k telefonu; vypněte na něm Bluetooth. Pomůže i probudit displej hodinek. Všechna zařízení v dosahu vypíše `scan --all`. |
| Hlášení, že Bluetooth není dostupný | Zkontrolujte povolení Bluetooth pro aplikaci, ze které příkaz spouštíte (Nastavení systému › Soukromí a zabezpečení › Bluetooth). |
| Chyba přihlášení, špatný klíč | Hodinky byly v Zeppu odpárovány a spárovány znovu; klíč je potřeba získat znovu. |
| Počítač se nepřipojí k hotspotu hodinek, „could not join“ nebo „is not reachable“ | Spusťte příkaz znovu s `--manual-wifi` a připojte se k vypsané síti sami. |
| Hodinky se po skončení nevrátí k telefonu | Drží je macOS. Vypněte Bluetooth na Macu, zapněte na telefonu a po připojení hodinek ho na Macu zase zapněte. |
| Chyba 451 nebo přerušený přenos | Spusťte příkaz znovu. Stahování naváže, nahrávání přepíše jen to, co je potřeba. |
| „was not uploaded correctly“ | Nástroj vrátil původní dlaždici, nebo novou odstranil; v hodinkách nezůstalo nic poškozeného. Spusťte příkaz znovu. |
| `sync` hlásí, že počítač není online | Po vypnutí hotspotu se Mac nevrátil na svou Wi‑Fi. Připojte ho a spusťte `sync` znovu; co už stáhl, má v mezipaměti. |
| Výpis příkazu skončí dřív, než příkaz doběhne | Stává se při spuštění z aplikace, která na výstup čeká jen chvíli. Jestli nahrávání proběhlo, je poznat podle záznamu `build/contours/uploaded.json` nebo nové složky v `backups`. |
| Mapa v hodinkách ukazuje starý stav | Zavřete a znovu otevřete mapu; dlaždice si načítá při otevření. |
| Značky jsou nakreslené dvakrát | Je zapnutá i lyžařská vrstva se značkami z dřívějška. Vypněte ji. |

## Složky projektu

V repozitáři:

| Složka nebo soubor | Obsah |
|---|---|
| `zeppmap/ble.py`, `btstate.py` | hledání hodinek a spojení přes Bluetooth LE |
| `zeppmap/session.py`, `chunked.py`, `crypto.py`, `ecdh_b163.py` | protokol Zepp OS: rámcování, šifrování, přihlášení, služby |
| `zeppmap/wifi.py`, `ftpops.py` | připojení k hotspotu a práce se soubory přes FTP |
| `zeppmap/imginfo.py`, `skitile.py`, `swatch.py` | čtení dlaždic, úprava na lyžařskou dlaždici, vzorníky a volání mkgmap |
| `zeppmap/routes.py` | výběr tras, geometrie souběžných značek, lanovky |
| `zeppmap/ctlmerge.py` | skládání značek s vrstevnicemi Zeppu, zkouška úrovní a vzorník šířek |
| `zeppmap/sync.py` | příkaz `sync`: mezipaměť tras, úložiště původních vrstevnic, sestavení a porovnání s hodinkami |
| `zeppmap/cli.py` | příkazová řádka |
| `tests/` | testy bez hodinek, včetně simulovaných hodinek |
| `docs/` | tato dokumentace a fotky |

Jen na disku, mimo repozitář (viz `.gitignore`):

| Složka | Obsah |
|---|---|
| `zepp-contours/` | původní dlaždice vrstevnic stažené z hodinek; skládají se z nich sloučené dlaždice, nemazat |
| `backups/` | dlaždice uložené před každým přepsáním |
| `build/contours/` | sestavené dlaždice pro vrstvu vrstevnic (značky s vrstevnicemi) |
| `build/ski/` | sestavené dlaždice značek pro lyžařskou vrstvu |
| `data/` | stažené trasy a mezipaměť příkazu `sync` |
| `from-watch/` | soubory stažené příkazem `pull` |
| `tools/` | mkgmap |
| `.venv/` | prostředí Pythonu |

## Známá omezení a nápady

- Jen Bluetooth LE. Pokud firmware vyžaduje Bluetooth Classic, `scan` hodinky nenajde nebo `services` nahlásí chybějící datový kanál.
- Windows a Linux jsou nevyzkoušené. S hodinkami je nástroj vyzkoušený jen na macOS. Připojení k hotspotu přes `nmcli` (Linux) a `netsh wlan` (Windows) je ověřené jen testy s napodobenými systémovými příkazy, ne na skutečném počítači s hodinkami. Profil sítě pro Windows předpokládá, že hotspot hodinek používá WPA2 s heslem. Kontrola povolení Bluetooth v příkazu `check` a hledání hodinek, které už systém drží připojené, fungují jen na macOS. Když automatické připojení selže, zbývá `--manual-wifi`.
- Základní mapu z dat OpenStreetMap nástroj nevyrábí, jen vrstvu značek nad oficiální mapou.
- Značky nejsou vidět od měřítka 2 km; to určuje firmware.
- Sloučené dlaždice se skládají z vrstevnic uložených při prvním běhu; novější vrstevnice od Zeppu `sync` zatím sám nepřevezme.
- Nápad: opravit velikost písmen v názvech oficiální mapy. Písmena s háčkem a kroužkem by šla uvnitř slov přepsat na malá přímo v dlaždici, beze změny její velikosti. Nevyzkoušeno.

## Testy

```bash
.venv/bin/python -m pip install pyftpdlib
```

```bash
.venv/bin/python -m unittest discover -s tests
```

Bez hodinek ověřují výměnu klíčů proti OpenSSL, rámcování paketů podle Gadgetbridge, čtení a stavbu dlaždic (s mkgmap) a celý postup příkazů proti simulovaným hodinkám a místnímu FTP serveru. Testy s mkgmap a FTP se bez Javy nebo knihovny pyftpdlib přeskočí.
