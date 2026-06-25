# Workout-Analyse & strukturierte Konfiguration

> Design und Methodik der Trainingslast-Analyse — ergänzt
> [`ARCHITECTURE.md`](ARCHITECTURE.md) um den Workout-Teil der Pipeline.
> Workouts werden zu **Tagesserien** verdichtet und laufen durch dieselbe
> Maschinerie (Lag-Korrelationen, Anomalien, Trends). Kernbausteine:
> typ-agnostische Tageslast (`workout_trimp` + `workout_load`, ACWR, Profil via
> `config.yaml`), typ-getrennte Last pro Sportart über `workouts.type_map`
> (`workout_trimp_running` …) und zonenbasiertes Edwards-TRIMP
> (`workout_edwards`, parallel zu Banister) aus der Intra-Workout-HR-Zeitreihe
> (siehe §9).

## 1. Motivation

Workouts werden bereits vollständig eingelesen und in der `workouts`-Tabelle
gespeichert (`app/ingest.py`), aber die Analyse-Pipeline (`app/analysis.py`)
liest sie nie: `build_series()` kennt nur `metric_samples` (Registry-`core`)
und `sleep_sessions`. Die spannenden Zusammenhänge — Training heute →
Erholung/Schlaf morgen — bleiben damit ungenutzt.

Ziel: Workouts zu **Tagesserien** verdichten und durch die **vorhandene**
Maschinerie (Lag-Korrelationen, Anomalien, Trends, Recovery) laufen lassen.

## 2. Leitidee: aus Events Tagesserien machen

Sobald eine Workout-Größe als tägliche Serie im `series`-Dict liegt, testet
`_correlation_findings()` automatisch beide Richtungen und Lags 0–3. Der
Großteil der Arbeit ist also ein Loader + wenige Zeilen in `build_series()`,
keine neue Analyse-Engine — analog zu den heutigen Schlaf-Serien.

### 2.1 Tages-Features (neuer Loader `load_workout_frame(db, tz)`)

Gruppierung nach lokalem Tag (`start_time AT TIME ZONE :tz)::date`), analog zu
`load_sleep_frame()`:

| Serie | Ableitung | agg |
|---|---|---|
| `workout_trimp` | Tagessumme Banister-TRIMP (Sessions **mit** `avg_hr`) | sum |
| `workout_load` | Tagessumme `active_energy_kcal` (Fallback, deckt **alle** Sessions) | sum |
| `workout_duration` | Tagessumme `duration_s` → h | sum |
| `workout_count` | Sessions/Tag | sum |
| `workout_intensity` | last-gewichteter Mittelwert `intensity` (sofern vorhanden) | avg |

`workout_trimp` und `workout_load` laufen **parallel** (verschiedene Einheiten
nicht mischen). Übereinstimmung → robustes Signal; Divergenz → informativ
(„viel Energie, wenig HR-Last" = lange lockere Einheit).

### 2.2 Zentrale Nuance: **0 statt NaN**

Bei Metriken bedeutet ein fehlender Tag NaN (keine Messung). Bei Workouts ist
ein trainingsfreier Tag eine **echte 0**. Die Workout-Serien werden daher über
`[erster … letzter Workout-Tag]` reindiziert und Lücken **mit 0 gefüllt**
(nicht interpoliert), nur **innerhalb** der beobachteten Spanne. Ergebnis: eine
dichte Serie, ideal für Korrelation/Anomalie.

## 3. HR-basiertes TRIMP (Banister)

Mit nur **einem** Ø-Puls pro Session (Apple Watch liefert `avg_hr`/`max_hr`)
plus Dauer und Ruhe-/Max-HR ist Banister-TRIMP berechenbar — **ohne**
Intra-Workout-Zeitreihe. Zonenbasierte Varianten (Edwards/Lucia) brauchen die
Sekunden-HR und scheiden vorerst aus (liegt nur im Raw-Archiv).

```
d     = duration_s / 60                              # Minuten
HRr   = clamp((avg_hr − HR_rest) / (HR_max − HR_rest), 0, 1)   # HR-Reserve-Anteil
y     = sex == "female" ? 0.86·e^(1.67·HRr) : 0.64·e^(1.92·HRr)
TRIMP = d · HRr · y
```

`workout_trimp` (Tag) = Σ TRIMP über die Sessions des Tages. Sessions ohne
`avg_hr` (oft Kraft) tragen nicht bei → dafür die kcal-Fallback-Serie.

### 3.1 HR_max / HR_rest — Fallback-Ketten

```
HR_max  =  profile.hr_max                          # expliziter Override (Max-Test) gewinnt
        ?: 208 − 0.7 · (Jahr − profile.birth_year) # Tanaka, genauer als 220−Alter
        ?: clamp(max(beobachtete max_hr), 160, 210) # datengetrieben, immer verfügbar

HR_rest =  28-Tage-Median(resting_heart_rate)      # gemessen, personalisiert, zeitvariabel
        ?: profile.hr_rest                          # Escape-Hatch
        ?: 60                                        # letzter Fallback
```

Aus **`birth_year`** (nicht `age`) wird das Alter pro Lauf neu gerechnet, damit
HR_max korrekt mitdriftet (~0,7/Jahr). Die Pipeline läuft **auch ganz ohne
Profil** (datengetriebenes HR_max, ♂-Gewicht als dokumentierter Default) — die
Profilwerte sind **optionale Präzisierung**, keine Voraussetzung.

## 4. Konfiguration: `config.yaml`

Bisher ist alle Konfiguration ENV-basiert (`app/config.py:Settings`). Für
strukturierte Werte (Profil, Typ-Mapping, Tunables) führen wir — wie im
`pocketlog-importer` — eine `config.yaml` ein. Klare Trennung:

```
ENV   = Secrets + Infrastruktur   (INGEST_SECRET, DATABASE_URL, TZ, PUID/PGID,
                                    LOG_*, ANALYSIS_CRON, NOTIFY_TOKEN)
YAML  = Verhalten + Profil        (profile, workouts, analysis-Tunables, und
                                    die nicht-geheimen Notify-Felder)
```

### 4.1 Datei-Layout (`/config/config.yaml`, gemountet)

```yaml
profile:
  birth_year: 1990        # optional → altersbasiertes HR_max (Tanaka)
  sex: male               # male | female | unspecified → TRIMP-Gewicht
  hr_max:                 # optional, expliziter Override (sonst abgeleitet)
  hr_rest:                # optional, sonst aus resting_heart_rate gemessen

workouts:
  load_metric: both       # trimp | energy | both
  type_map:               # lokalisierter HAE-Name → kanonischer Typ
    Laufen: running        # (für späteres typ-getrenntes TRIMP, Iteration 2)
    Radfahren: cycling
    Krafttraining: strength

analysis:                 # heute Modul-Konstanten in analysis.py
  max_lag: 3
  min_overlap: 42
  corr_keep_alpha: 0.10
  fdr_alpha: 0.05
  anomaly_window: 28
  anomaly_threshold: 3.5
  anomaly_recent_days: 14
  trend_strength_min: 0.30
  seasonality_strength_min: 0.20
  recovery_recent_days: 14
  recovery_z: 1.5

notify:                   # Token bleibt ENV (NOTIFY_TOKEN)
  url:
  events: [analysis, findings]   # ingest | analysis | findings
  level: problems                # problems | always
  verify_tls: true
```

### 4.2 Lade-Modell

- `Settings` (pydantic-settings, ENV) bleibt für **Secrets + Infrastruktur** und
  treibt weiterhin die s6-Services (uvicorn, scheduler, migrate).
- Neu: `AppConfig` (pydantic `BaseModel`), geladen aus `config.yaml` via
  `load_config()` + `validate_config()` — exakt das Importer-Muster. ENV
  überschreibt nur Secrets (`NOTIFY_TOKEN`). Fehlt die Datei, gelten valide
  Defaults (Verhalten bleibt wie heute).
- Die Analyse (`app/analysis.py`, Subprozess) lädt `AppConfig` und zieht
  Tunables + Profil daraus statt aus Modul-Konstanten. Ingest/uvicorn brauchen
  weiterhin primär ENV.
- `config.example.yaml` mitliefern; echte `config.yaml` ist gemountet und
  gitignored (wie beim Importer).

### 4.3 Aufteilung im Code

- `AppConfig` + `load_config`/`validate_config` in `app/appconfig.py`,
  `config.example.yaml` mitgeliefert; die echte `config.yaml` ist gemountet und
  gitignored.
- Analyse-Tunables liegen im `analysis:`-Block (Modul-Konstanten als Defaults,
  wenn die Datei fehlt).
- `profile`/`workouts` treiben TRIMP-/HR_max-Logik als pure Helfer
  (unit-testbar, `data-in`).
- Notify-Felder (außer dem Token, das ENV bleibt) liegen im `notify:`-Block; die
  Notify-Logik selbst ist davon unberührt.

## 5. Neue Befunde aus Trainingslast

### 5.1 ACWR (Acute:Chronic Workload Ratio)

Sportwissenschaftlicher Standard, ergänzt die generischen Anomalien:

```
ACWR = Ø_7d(workout_trimp) / Ø_28d(workout_trimp)
```

- `> ~1.5` = Lastspitze (Überlastungs-/Verletzungsrisiko), `< 0.8` = Detraining.
- Als `kind = "training_load"` in `findings` — passt in `String(16)`, **keine
  Migration** nötig; `severity = ACWR`, `details = {acute, chronic, ratio}`.
- Auf `workout_trimp` gerechnet (aussagekräftiger als auf Energie). Guter
  Kandidat, ihn auch in die Notify-`findings` aufzunehmen (neben `anomalies` +
  `recovery_alerts`).

## 6. Was es freischaltet (Lag-Korrelationen)

Sobald die Serien im Dict liegen, fallen u. a. heraus:

- `workout_trimp[t]` → **HRV[t+1]** (harter Tag drückt die HRV am Folgetag)
- `workout_trimp[t]` → **resting_heart_rate[t+1]**
- `workout_trimp[t]` → **sleep_deep_h / sleep_total_h / sleep_efficiency**
- `workout_trimp[t]` → **respiratory_rate**, **cardio_recovery**

## 7. Touchpoints (Aufwand)

1. `app/analysis.py`: `load_workout_frame()` + ~6 Zeilen in `build_series()`
   (Serien anhängen, 0-Fill), TRIMP/HR_max als pure Helfer.
2. `app/analysis.py`: `_training_load_findings()` (ACWR).
3. `AnalysisResult`: Zähler `training_load` ergänzen (sonst landen
   Workout-Korrelationen im `correlations`-Zähler).
4. Config: `AppConfig` + `config.yaml` (Abschnitt 4).
5. **Keine** Registry-Zeile (Workout-Serien werden wie Schlaf hand-verdrahtet).
6. **Keine** Schema-Migration.
7. Tests: `load_workout_frame` (Aggregation + 0-Fill), Banister-TRIMP,
   HR_max-Fallback-Kette, ACWR — alle als Pure-Functions gegen synthetische
   Daten mit festem Seed.

## 8. Caveats & Grenzen

- **Energie ist ein unvollkommener Last-Proxy** (langer Spaziergang vs. kurze
  Intervalle) → deshalb TRIMP als HR-basierte Zweitserie.
- **Banister glättet Intervalle** (ein Ø-HR pro Session): 4×4-Intervalle und
  gleichmäßiger Dauerlauf mit gleichem Ø-HR/gleicher Dauer ergeben denselben
  TRIMP. Zonenbasiert würde das auflösen — braucht aber die Sekunden-HR.
- **HR_max-Schätzung** ist der größte Unsicherheitsfaktor; datengetrieben
  unterschätzt eher. Für **relative** Vergleiche (Trends/Anomalien/ACWR)
  unkritisch, für absolute Zahlen weniger belastbar → `profile.birth_year`
  bzw. `hr_max` verbessern das.
- **Überlappung mit `active_energy`**: dessen Tagessumme enthält die
  Workout-Energie bereits → eine starke `workout_load`↔`active_energy`-
  Korrelation ist erwartbar/trivial und sollte als solche eingeordnet (oder das
  Paar ausgeschlossen) werden.
- **Null-Inflation** bei selten Trainierenden → schwächere Statistik; der
  `min_overlap`-Schutz greift sinnvoll.
- **Korrelation ≠ Kausalität** (ARCHITECTURE §11) — die Lag-Richtung „Training →
  Erholung" ist physiologisch plausibel, der Test misst aber nur Ko-Bewegung.

## 9. Last-Metriken im Detail

- **Typ-agnostisch:** alle Workouts in eine Tageslast
  (`workout_trimp` + `workout_load`), ACWR, Profil via `config.yaml`. Umgeht das
  `name`-Typ-Mapping komplett.
- **Typ-getrennt:** `workouts.type_map` (lokalisierter
  `name` → kanonischer Typ, case-insensitiv) erzeugt **zusätzlich** je Sportart
  eine Lastserie (`workout_trimp_<typ>` / `workout_load_<typ>`, gated über
  `load_metric`). Nicht gemappte Workouts speisen weiterhin nur das Aggregat.
  Korrelationen zwischen einem Aggregat und seiner eigenen Sport-Komponente sind
  mechanisch und werden ausgeschlossen (`_is_workout_aggregate_child`);
  Sport↔Sport und Sport↔andere Metrik bleiben.
- **ACWR pro Sportart:** ACWR läuft auf dem Aggregat **und** je
  Sportart (`_training_load_targets`, TRIMP bevorzugt). Schutz gegen Fehlalarme
  bei selten betriebenen Sportarten: eine Serie mit weniger als
  `analysis.acwr_min_active_days` (Default 8) Trainingstagen in den 28 Chronic-
  Tagen wird übersprungen.
- **Zonenbasiertes Edwards-TRIMP:** aus der Intra-Workout-HR-Serie
  (`heartRateData`, HAE liefert ~Minuten-Buckets `{Min, Avg, Max}` mit Zeitstempel)
  entsteht **zusätzlich** zu Banister eine parallele Tageslast `workout_edwards`
  (+ je Sportart `workout_edwards_<typ>`), gated über `workouts.edwards`
  (Default `true`, **selbst-gated**: ohne gespeicherte Samples wird nichts
  emittiert). Edwards summiert `Σ Minuten-in-Zone · Zonen-Gewicht` über fünf
  Zonen (50–60–70–80–90–100 % HR_max, Gewichte 1–5; darunter Gewicht 0). Jede
  zwei aufeinanderfolgenden Samples bilden ein Intervall, dessen Zeit der Zone
  des Start-HR zugerechnet wird; die Intervallzeiten werden auf `duration_s`
  reskaliert, damit Aufzeichnungslücken den Wert nicht verzerren. So löst
  Edwards Intervalle auf, die der eine Ø-Puls von Banister glättet (§8).
  Zonengrenzen hängen von HR_max ab → **zur Analysezeit** gerechnet, nie beim
  Ingest eingefroren.
  - **Architektur (Option B):** die Serie wird **beim Ingest** in eine eigene
    Tabelle `workout_hr_samples` geparst (`(workout_hae_id, ts)` als
    Idempotenz-Schlüssel, CASCADE am Workout) — nicht aus dem Roh-Archiv zur
    Analysezeit gelesen, damit „Raw = Cold Storage" gewahrt bleibt. Migration
    `0004_workout_hr_samples`.
  - **Historie:** der Content-Hash-Dedup verhindert, dass bereits archivierte
    Payloads beim erneuten Posten neu geparst werden → einmalig
    `healthlog rederive-workout-hr` spielt die HR-Samples aus dem `raw_ingest`-
    Archiv nach (idempotent; die Workouts existieren bereits). Vorab prüfbar mit
    `healthlog check-workout-hr`.
  - **ACWR bleibt auf Banister/kcal** (`_training_load_targets`): Edwards ist
    eine **parallele** Last-Serie für Korrelationen/Anomalien/Trends, kein
    zusätzliches ACWR-Ziel (dieselbe Trainingslast dreifach zu bewerten wäre
    redundant). Aggregat↔eigene Sport-Komponente derselben Metrik bleibt
    ausgeschlossen (`_is_workout_aggregate_child`, jetzt inkl. `workout_edwards`);
    Quervergleiche zwischen den Metriken (trimp/load/edwards) bleiben — ihre
    Übereinstimmung/Divergenz ist selbst aussagekräftig.
