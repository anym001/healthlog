# HealthLog – Architektur & Design

> Self-hosted Analyse von Apple-Health-Daten mit Fokus auf Korrelationen,
> Anomalien und Trends — vollständig auf eigener Hardware, keine externen
> Anbieter.
>
> Dieses Dokument hält die **Architektur und die Design-Entscheidungen** fest:
> das *Warum* hinter dem Code (Datenmodell, Ingestion-Vertrag, Analyse-Methodik,
> Privacy-Grenzen). Was implementiert ist und wie es genutzt wird, steht im
> README und im Code; hier steht, warum es so gebaut ist.

## 1. Grundentscheidungen

- **Datenexport:** Health Auto Export (iPhone) → REST-Automation an eigenen Endpoint
- **Topologie:** Always-On-Server trägt **alles Statistische** (Ingestion + DB + automatische Analyse + Grafana, optional interaktive Exploration) ⟷ Mac **nur** für die LLM-Narration; allein dort zahlt sich Apple Silicon (Unified Memory) aus
- **Analyse-Kern:** klassische Statistik/ML (Korrelationen, Anomalien, Trends) — **kein** LLM im kritischen Pfad
- **LLM:** Ollama auf dem Mac (32 GB Unified Memory) als **optionale Ausbaustufe** für Klartext-Reports; Zielklasse 8–14B (z. B. Qwen 2.5 14B)
- **Datenfokus:** Aktivität & Training, Schlaf & Erholung, Vitalwerte
- **Privacy:** 100 % eigene Hardware, keine externen Calls — auch das LLM bleibt lokal
- **Deployment-Ziel:** Unraid; das App-Image soll **public-fähig** sein
  (LinuxServer-Tugenden: PUID/PGID, `/config`, Env-Config) — siehe §6

## 2. Zielarchitektur

```
┌─ iPhone ──────────────────────────────────────────────┐
│  Health Auto Export  →  Automation "REST API"          │
│  (nächtlich, JSON POST, mit Secret-Header über TLS)    │
└───────────────────────────┬───────────────────────────┘
                            │  HTTPS (Reverse Proxy)
┌─ Always-On-Server (Docker / Unraid) ────────────────────┐
│  healthlog  (EIN App-Container, PID 1 = s6-overlay)     │
│    ├─ service: uvicorn   → FastAPI-Ingest, 24/7         │
│    │     validiert, archiviert Roh-JSON, schreibt (Upsert)│
│    └─ service: scheduler → APScheduler-Prozess          │
│          nachts → startet Analyse als SUBPROZESS:       │
│          · Lag-Korrelationen · STL-Trends · Anomalien   │
│          → Befunde in Tabelle `findings`                │
│  TimescaleDB  (Postgres + Hypertables)  ← Single Source │
│  Grafana  → Dashboards / Trends                          │
└───────────────────────────┬───────────────────────────┘
                            │  read-only (psql)
┌─ Mac (Apple Silicon, 32 GB) ───────────────────────────┐
│  NUR LLM-Narration: Ollama → Report aus `findings`      │
│  Apple Silicon / Unified Memory — der einzige Mac-Vorteil│
└────────────────────────────────────────────────────────┘
(Interaktive Exploration läuft, falls gewünscht, ebenfalls am Server.)
```

### Prozess- statt Container-Trennung (Begründung)

Ingest und Analyse leben **in einem Container**, aber als **getrennte OS-Prozesse**
unter `s6-overlay` (sauberes PID 1: Signal-Handling, Zombie-Reaping, Restart-Policy).
Die rechenintensive Analyse (pandas/numpy/statsmodels) darf **nicht** im uvicorn-Prozess
laufen — sie würde über GIL/CPU-Last den Event-Loop blockieren und HAE-POSTs verzögern.
Getrennte Prozesse = OS-Level-Isolation. Die Analyse wird vom Scheduler zusätzlich als
**kurzlebiger Subprozess** (`python -m app.analysis`) gestartet, sodass selbst ein
harter Crash in einer C-Extension nur diesen Subprozess killt — Scheduler **und** uvicorn
überleben, die Datenannahme ist strukturell abgeschirmt.

| Komponente | Wo | Warum |
|---|---|---|
| Ingestion (uvicorn) | App-Container, Dauerprozess | 24/7 Annahme der HAE-POSTs |
| Scheduler (APScheduler) | App-Container, eigener Prozess | triggert nachts, Logs→stdout, Env/TZ sauber |
| Analyse | App-Container, Subprozess des Schedulers | fault-isoliert, gefährdet die Annahme nicht |
| TimescaleDB | eigener Container | Postgres ohnehin separat |
| Grafana | eigener Container | direkt auf Timescale |
| Interaktive Exploration (optional, Jupyter) | Server | nur Ad-hoc-Analyse; Pipeline + Grafana decken den Normalfall, Daten bleiben am Server |
| LLM-Reports | Mac | **einziger** Mac-Grund: Apple Silicon (Unified Memory) für lokale 8–14B-Modelle |

## 3. Tech-Stack & Begründung

| Schicht | Wahl | Warum |
|---|---|---|
| Ingestion | Eigenes **FastAPI** (statt offiziellem HAE-Server) | HAE postet JSON an jeden Endpoint; konsistent mit SQL-Storage; bekannter Stack. Der offizielle Server setzt auf MongoDB — Bruch zum SQL-Analyseziel. |
| Storage | **TimescaleDB** (Postgres-Extension) | Zeitreihen-Hypertables, Continuous Aggregates für Tageswerte, SQL für Korrelationen, native Grafana-Anbindung. |
| Scheduler | **APScheduler** (eigener Prozess unter s6) | Zeitplan im Code (versioniert), Logs→stdout (Docker-nativ), Env/TZ sauber — vs. cron-im-Container-Reibung. |
| Analyse | **Python: pandas + statsmodels + scipy + scikit-learn** | Reifer, reproduzierbarer Standard für Korrelation/Trend/Anomalie. |
| Dashboards | **Grafana** | Minimaler Aufwand, direkt auf Timescale. |
| Container-Basis | **`python:3.12-slim` + s6-overlay v3** | Schlankes Image (relevant für Public), volle Kontrolle, PUID/PGID + `/config`. |
| LLM (optional) | **Ollama**, 8–14B (z. B. Qwen 2.5 14B) | Lokal auf dem 32-GB-Mac; erhält nur fertige Befunde, nicht die Rohdaten. |

## 4. Datenmodell

> Das Schema folgt der realen HAE-Payload-Struktur (Aggregation, Felder,
> Einheiten), an einem echten Export (v2, 7 Tage, 30 Metriken + 1 Workout)
> verifiziert.

### 4.0 Leitprinzip: Metriken jederzeit erweiterbar

Das Datenmodell ist bewusst **metrik-agnostisch** — eine neue Metrik (heute unbekannt,
in einem künftigen iOS/HAE-Update oder bei geändertem Tracking) erfordert **keine
Schema-Änderung und keine Migration**. Getragen wird das von fünf Bausteinen:

1. **Generische Werte-Tabelle** (`metric_samples`, §4.2): `metric` ist eine Spalte,
   **keine** Metrik bekommt eigene Tabellen-Spalten. `qty`/`vmin`/`vavg`/`vmax` decken
   beide HAE-Shapes ab.
2. **Roh-Archiv** (§4.1): nimmt jede Payload verbatim — auch Felder, die der Parser
   (noch) nicht kennt, gehen nie verloren und sind später nachparsbar.
3. **Toleranter Ingest** (§5): unbekannte Metriken werden **angenommen, nicht
   abgelehnt** — sie landen in `metric_samples` und legen automatisch einen
   **Registry-Stub** an (`tier='secondary'`, Einheit aus der Payload), der nur noch
   menschlich klassifiziert werden muss. Kein POST scheitert an einer neuen Metrik.
4. **Registry statt Code** (§4.5): Verhalten einer Metrik (kanonische Einheit,
   Tagesaggregat, Tier, Kategorie) ist **Daten, kein Code** — „adoptieren" = eine Zeile.
5. **Generische Tagesaggregate** (§4.7): die View aggregiert per `(day, metric)`, ganz ohne
   Metrik-Namen im Code — neue Metriken erscheinen automatisch.

Konsequenz: „eine weitere Metrik mitnehmen" heißt im Normalfall **nur** den HAE-Export
erweitern + ggf. eine Registry-Zeile pflegen. Spezialfälle mit eigener Struktur
(Schlaf §4.3, Workouts §4.4) bleiben die einzigen Tabellen mit dediziertem Schema.

### 4.1 Roh-Archiv (Replay-fähig)

```sql
-- Jede eingehende HAE-Payload verbatim, vor dem Parsen.
raw_ingest (id BIGSERIAL, received_at TIMESTAMPTZ DEFAULT now(),
            payload JSONB, source_ip INET, content_hash BYTEA)
            -- content_hash UNIQUE → identische Re-Posts werden verworfen.
```
Volle Fidelity: Bei Parser-/Schema-Fehlern lässt sich die Historie **neu parsen**,
ohne Datenverlust. Volumen lokal vernachlässigbar.

### 4.2 Geparste Messwerte (Hypertable)

HAE liefert pro Metrik ein `data`-Array von Buckets in genau **zwei Shapes**:
- **`{Min, Avg, Max}`** — in der Praxis **nur `heart_rate`**.
- **`{qty}`** — alle übrigen 29 Metriken (auch HRV, Ruhepuls, Atemfrequenz, SpO₂).

Daher **eine Zeile pro Metrik-Bucket** mit nullbaren Aggregat-Spalten (füllen, was HAE
liefert), **nicht** ein einzelnes `value`. Das Modell ist **generisch**: jede Metrik
landet hier, ohne Schema-Änderung (siehe Inventar §4.6) — wir ingesten **alle**
Metriken, die Registry klassifiziert sie:

```sql
metric_samples (time TIMESTAMPTZ, metric TEXT, source TEXT, unit TEXT,
                qty   DOUBLE PRECISION,   -- Punktwert/Summe (29 von 30 Metriken)
                vmin  DOUBLE PRECISION,   -- HAE "Min" (real nur heart_rate)
                vavg  DOUBLE PRECISION,   -- HAE "Avg"
                vmax  DOUBLE PRECISION,   -- HAE "Max"
                n     INTEGER,            -- Sample-Count im Bucket, falls vorhanden
                UNIQUE (metric, time, source))   -- Idempotenz, siehe §5
                -- Hypertable auf time
```
**Bestätigte Eigenheiten:** `date` = `'YYYY-MM-DD HH:MM:SS +0200'` (Leerzeichen,
expliziter TZ-Offset pro Wert → sauber als `TIMESTAMPTZ`). `source` kann **leer
(`''`)**, ein einzelnes Gerät oder **pipe-verkettet** sein
(`'Apple Watch …|iPhone …'`) und enthält teils ein No-Break-Space — der
Idempotenz-Schlüssel muss das vertragen (`source` nie NULL-only annehmen).

### 4.3 Schlaf (eigene Tabelle, intervallbasiert)

Schlaf passt nicht in `metric_samples` — er ist ein Intervall mit Phasen:

```sql
sleep_sessions (sleep_start TIMESTAMPTZ, sleep_end TIMESTAMPTZ,        -- sleepStart/End
                in_bed_start TIMESTAMPTZ, in_bed_end TIMESTAMPTZ,      -- inBedStart/End
                source TEXT,
                sleep_date DATE,         -- HAE-`date` = Mitternacht des Aufwach-Tags
                total_sleep_h, deep_h, core_h, rem_h, awake_h,         -- Stunden, dezimal
                asleep_h, in_bed_h DOUBLE PRECISION,
                UNIQUE NULLS NOT DISTINCT (sleep_end, source))
```
**Natürlicher Schlüssel = `(sleep_end, source)` (Aufwach-Identität, Migration 0011).**
Der HAE-REST-API-Push erfasst dieselbe Nacht mehrfach — jeweils mit späterem
`sleepStart`, aber identischem `sleepEnd`. Auf `sleep_end` zu schlüsseln lässt diese
Re-Captures beim Upsert kollabieren (die vollständigste Aufzeichnung mit dem größten
`total_sleep_h` gewinnt), während echte getrennte Perioden (z. B. ein Nickerchen mit
anderem Ende) erhalten bleiben. `NULLS NOT DISTINCT` hält den Replay auch bei seltenem
NULL-`sleep_end` idempotent. Die View `sleep_nightly` (Migration 0010) reduziert
zusätzlich auf eine Zeile je Kalendernacht (`sleep_date`) für Dashboards/Analyse.

**An echter Payload bestätigt:** HAE liefert pro Nacht ein Objekt mit `sleepStart`/
`sleepEnd`/`inBedStart`/`inBedEnd`, den Phasen-**Stunden** (dezimal) `deep`/`core`/
`rem`/`awake` und `totalSleep` (= `deep+core+rem`, verifiziert). `asleep`/`inBed` sind
in dieser Payload `0` (Phasen separat aufgeschlüsselt) → nullable/0 tolerieren.
**Tageszuordnung ist bereits HAEs Verhalten:** das Feld `date` steht auf
**Mitternacht des Aufwach-Tags** (z. B. `date=06-09`, `sleepStart=06-08 20:56`,
`sleepEnd=06-09 05:56`) → `sleep_date` 1:1 übernehmbar, deckt sich exakt mit unserer
Korrelations-Konvention. Mitternachtsübergreifender Schlaf bleibt eine Zeile.

### 4.4 Workouts

HAE liefert pro Workout ein **stabiles `id` (UUID)** — der bessere Idempotenz-Schlüssel
als `(start, type, source)`. Skalare kommen als `{qty, units}`-Objekte, dazu eine
`heartRate`-Summary `{min, avg, max}` und Intra-Workout-Zeitreihen (`heartRateData`,
`stepCount`, `heartRateRecovery`, …). Die HR-Zeitreihe wird in `workout_hr_samples`
geparst (für zonenbasiertes Edwards-TRIMP, siehe [`workout-analysis.md`](workout-analysis.md));
die übrigen Zeitreihen bleiben im Roh-Archiv.

```sql
workouts (hae_id UUID PRIMARY KEY,        -- HAE `id`, stabil → Idempotenz
          start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
          name TEXT,                       -- HAE `name` (LOKALISIERT, s.u.)
          location TEXT, is_indoor BOOL,
          duration_s, total_energy_kcal, active_energy_kcal,
          distance_km, avg_hr, max_hr, hr_recovery,   -- Erholungsindikator
          intensity, elevation_up_m,
          temperature_c, humidity_pct DOUBLE PRECISION,  -- Umgebungskontext
          source TEXT)
```
**Achtung Lokalisierung:** `name` ist sprachabhängig (`'Outdoor Spaziergang'`) — wie bei
Einheiten brauchen Workout-Typen eine Normalisierung (Mapping lokalisiert→kanonisch),
sonst zerfasern Typen über Sprachwechsel. Das übernimmt `app/workout_types.py`
(eingebaute DE+EN-Map → kanonischer Slug, erweiterbar via `workouts.type_map` in
`config.yaml`). `duration` in Sekunden, Energie in `kJ` (→ kcal normalisieren, §4.5).

### 4.5 Metrik-Registry (Normalisierung)

```sql
metric_registry (metric TEXT PRIMARY KEY, display_name TEXT,
                 unit_canonical TEXT,
                 agg_default TEXT,   -- 'avg'|'sum'|'min'|'max': welcher Tageswert zählt
                 category TEXT,      -- 'activity'|'sleep'|'vital'|'mobility'|'environment'
                 tier TEXT)          -- 'core' (Korrelations-/Trend-Fokus) | 'secondary'
```
Verhindert, dass dieselbe physiologische Größe unter mehreren Namen/Einheiten
zerfasert (kcal vs. kJ, `count/min`), und sagt der Analyse, **welcher** Tagesaggregat
pro Metrik sinnvoll ist (Steps→Summe, RestingHR→Min, HRV→Avg). Das **`tier`** trennt
Analyse-Fokus (`core`) von „mitgenommen, aber sekundär" (`secondary`): wir ingesten
**alles**, die Korrelations-/Anomalie-Pipeline läuft per Default nur über `core`
(begrenzt die Multiple-Testing-Last, §11), `secondary` bleibt jederzeit abfragbar.

**Einheiten-Wächter:** HAE liefert die Einheit pro Wert mit und kann sie lokalisieren.
**Real bestätigt:** Energie kommt als **`kJ`** (nicht kcal), dazu `kcal/hr·kg`,
`km/hr`, `m/s`, `degC`, `ml/(kg·min)`. `metric_registry.unit_canonical` ist die
Soll-Einheit; beim Ingest wird die eingehende `unit` dagegen geprüft → bei Abweichung
**konvertieren** (bekannter Faktor, kJ→kcal ×0.239006) **oder flaggen**, nie still
übernehmen. Genau dieser Fall trat im echten Export auf — der Wächter ist kein
Theoriekonstrukt. Ein Test pinnt das.

### 4.6 Metrik-Inventar (aus echter Payload)

30 Metriken im Export, kuratiert (Tier/Einheit/`agg_default` pro Metrik, durch
`test_registry.py` festgezurrt):

- **core – activity:** `step_count`, `active_energy` (kJ), `apple_exercise_time`,
  `walking_running_distance`, `flights_climbed`, `physical_effort`, `apple_stand_time`
- **core – sleep/recovery:** `sleep_analysis` (→ §4.3), `heart_rate_variability`,
  `resting_heart_rate`, `respiratory_rate`, `apple_sleeping_wrist_temperature`,
  `breathing_disturbances`, `time_in_daylight`
- **core – vital:** `heart_rate` (Min/Avg/Max), `blood_oxygen_saturation`,
  `walking_heart_rate_average`, `vo2_max`, `weight_body_mass`
- **secondary – mobility:** `walking_speed`, `walking_step_length`,
  `walking_asymmetry_percentage`, `walking_double_support_percentage`,
  `stair_speed_up`, `stair_speed_down`, `six_minute_walking_test_distance`
- **secondary – activity/environment:** `basal_energy_burned`, `apple_stand_hour`,
  `environmental_audio_exposure`, `headphone_audio_exposure`

**Aus dem ersten Voll-Backfill nachkuratiert** (neun zunächst auto-registrierte Metriken,
Migration `0003_curate_metrics`): `cardio_recovery` als **core – vital**
(1-Minuten-Herzfrequenz-Erholung, Cardio-Fitness-Marker → in der Pipeline); sekundär
`waist_circumference`, `height`, `atrial_fibrillation_burden` (vital), `swimming_distance`,
`swimming_stroke_count`, `handwashing` (activity), `mindful_minutes` (mindfulness),
`dietary_water` (nutrition). Die Kategorien `mindfulness`/`nutrition` kamen damit neu hinzu.

Da das Modell generisch ist, kostet das Mitnehmen aller 30 praktisch nichts — neue
Metriken in künftigen Exports landen automatisch im Roh-Archiv und in `metric_samples`
und werden nur per Registry-Zeile „adoptiert".

### 4.7 Tagesaggregate (View)

Aktuell eine **einfache SQL-View** (kein Timescale Continuous Aggregate — die Datenmenge
erfordert es nicht; ein CA kann sie später ohne Schema-Bruch ersetzen). Sie berechnet
**alle** Aggregate pro `(day, metric)`; die Analyse pickt je Metrik die laut
`metric_registry.agg_default` passende Spalte:

```sql
daily_metrics (day, metric, avg, vmin, vmax, sum, n)
  -- (time AT TIME ZONE 'Europe/Vienna')::date  ← lokaler Tag, NICHT UTC!
```
Die View nutzt `COALESCE(vavg, qty)` / `COALESCE(vmin, qty)` / `COALESCE(vmax, qty)`
(Migration `0005_daily_metrics_coalesce`), identisch zum Analyse-Loader
`load_daily_series` — Grafana und Pipeline sehen damit dieselben Tageswerte. `avg`
ist ein ungewichtetes Mittel der Bucket-Mittel — für Tagesgranularität ausreichend,
exakt über das Roh-Archiv nachrechenbar.

### 4.8 Befunde der Pipeline (reine Statistik, kein LLM)

```sql
findings (id, computed_at, kind TEXT,            -- correlation|anomaly|trend|seasonality|recovery_alert|consistency|training_load
          metric_a TEXT, metric_b TEXT,          -- metric_b nur bei correlation
          lag_days INT, coefficient DOUBLE PRECISION,
          p_value DOUBLE PRECISION, p_value_adj DOUBLE PRECISION,  -- FDR
          ref_date DATE,                          -- Bezugstag (Anomalie/Trendpunkt)
          window_start DATE, window_end DATE,     -- Analysefenster
          severity DOUBLE PRECISION,
          details JSONB,                          -- kind-spezifische Extras
          note TEXT)
```
`ref_date`/`window_*` machen einen Befund in Grafana **markierbar** (Annotation auf
dem Tag der Anomalie). Nicht zutreffende Felder bleiben NULL.

**Befund-Typen** (Snapshot pro Lauf, `app/analysis.py`):
- **correlation** — Spearman auf **trendbereinigten** Reihen (Trendkomponente
  subtrahiert, damit gegenläufige Langzeit-Trends keine Schein-Korrelation
  erzeugen), Lags 0–3 Tage (beide Richtungen), FDR-`p_value_adj`; pro Metrik-Paar
  nur der **stärkste** Lag/Richtung (Dedup).
- **anomaly** — 28-Tage trailing Median + MAD (robuster z), nur letzte 14 Tage.
- **trend** — STL-Trendkomponente (Slope + Trendstärke).
- **seasonality** — MSTL(7, 365): Jahresmuster (Amplitude + Hoch-/Tief-Monat), ab ≥2 Jahren;
  liegen Hoch/Tief <2 Monate auseinander, ist die Phase als unsicher geflaggt (`phase_confident`).
- **recovery_alert** — kombiniert: HRV auffällig niedrig **und** Ruhepuls hoch (+ optional kurzer Schlaf).
- **consistency** — rollende Streuung von Schlafdauer und Zubettgeh-Zeit (Mitternachts-Wrap behandelt).
- **training_load** — ACWR (akut 7-Tage / chronisch 28-Tage) auf der Tages-Trainingslast
  (`workout_trimp`, HR-basiert via Banister; sonst `workout_load` in kcal); nur geflaggt bei
  Lastspitze (Überlastung) oder Detraining. Details siehe [`workout-analysis.md`](workout-analysis.md).

Die reine Analyse-Mathematik ist DB-frei und gegen synthetische Reihen (bekannter
Lag/Anomalie/Trend/Jahres-Saison) mit festem Seed getestet (§7); dazu ein DB-End-to-End-Test.

## 5. Ingestion-Vertrag

- **Idempotenz/Upsert:** HAE sendet bei nächtlichen Exporten **überlappende Fenster**.
  Ohne Dedup wachsen Dubletten und verfälschen Tagesaggregate. Daher
  `INSERT … ON CONFLICT (UNIQUE-Key) DO UPDATE` auf alle Zieltabellen.
  `raw_ingest.content_hash` verwirft identische Re-Posts früh.
- **Backfill vs. Delta:** Beim ersten manuellen HAE-Export die **gesamte
  Apple-Health-Historie** (Jahre) einmal als Bulk-Backfill einspielen → danach
  **nächtliche Deltas**. Damit sind Korrelationen ab Tag 1 belastbar (≥6–8 Wochen).
  Der Voll-Export sprengt das HTTP-Limit (`MAX_PAYLOAD_BYTES`) und den Proxy-Timeout;
  deshalb läuft er **datei-basiert** über das CLI `healthlog backfill <pfad>`
  (Datei oder Verzeichnis, `--dry-run` zum Prüfen) — dieselbe `archive_raw → parse →
  store`-Pipeline wie der Endpoint, pro Datei committet, idempotent (Re-Run = No-Op
  dank `content_hash`-Dedup + Upsert).
- **Zeitzone:** Speicherung in `TIMESTAMPTZ`; alle Tages-Buckets in **lokaler TZ
  (Europe/Vienna)**, da das Tagesraster die Basis aller Analysen ist.
- **Robustheit & Erweiterbarkeit:** Payload-Größenlimit, Secret-Header in konstanter
  Zeit prüfen. **Unbekannte Metriken werden angenommen, nie abgelehnt** (§4.0): sie
  landen im Roh-Archiv **und** in `metric_samples` und legen automatisch einen
  Registry-Stub an (`tier='secondary'`, Einheit aus der Payload, `agg_default` heuristisch
  aus dem Shape: `qty`→`sum`, `Min/Avg/Max`→`avg`). Ein POST scheitert nie an einer
  neuen Metrik; die Klassifizierung kann jederzeit nachgezogen werden.
- **Einheiten stabil halten:** HAE-seitig *„Use Localized Units" = OFF* und feste
  Unit Preferences pro Metrik (metrisch: kcal/km/kg/°C, HR `count/min`, HRV `ms`,
  SpO₂ `%`). Serverseitig greift zusätzlich der Einheiten-Wächter der Registry
  (§4.5) — die App-Einstellung ist Vorsorge, die Registry ist die Absicherung.

## 6. Container-Topologie & Deployment

- **Ein App-Image** `healthlog` (`python:3.12-slim` + s6-overlay v3), zwei s6-Services
  (uvicorn, APScheduler), Analyse als Subprozess (§2).
- **PUID/PGID + `/config`:** Entrypoint chownt `/config`, dropt
  Privilegien. `/config` hält Persistenz, die nicht in der DB liegt: Ingest-Secret,
  `config.yaml`, Narration-Output, Logs, evtl. DB-Backups/Export.
- **Env-getriebene Config:** `INGEST_SECRET`, `DATABASE_URL`, `TZ`,
  `ANALYSIS_CRON` (5-Feld-Cron), `LOG_LEVEL`, `LOG_FORMAT` (text/json) — Secrets +
  Infrastruktur über ENV, Verhalten + Profil über `config.yaml` (siehe
  [`workout-analysis.md`](workout-analysis.md) §4).
- **Compose:** `timescaledb` + `healthlog` + `grafana`; DB nicht öffentlich
  exponiert, Grafana hinter Auth, Reverse-Proxy/TLS vor dem Ingest.
- **Public-Tauglichkeit:** README mit eingebettetem Compose-Beispiel (keine
  separate `docker-compose.yml`- oder `.env`-Datei im Repo), sinnvolle Defaults,
  keine Telemetrie, Doku der HAE-Automation-Einrichtung.

## 7. Tests & Qualität

**Grüne Suite ist Pflicht-Gate vor jedem Image-Push** (§8). Das Kern-Risiko liegt
nicht in CRUD, sondern in **Parser-Korrektheit** und **Analyse-Mathematik** — genau
dort liegt der Testfokus.

- **Backend-Lint:** `ruff check` + `ruff format --check`.
- **Parser-/Ingest-Tests (pytest):** echte HAE-Payload als Fixture → erwartete Zeilen
  in `metric_samples`/`sleep_sessions`/`workouts`. Pinnt die Übersetzung der
  HAE-Eigenheiten (Min/Avg/Max-Buckets, Einheiten).
- **Idempotenz:** zweimaliges Posten derselben Payload → keine Dubletten, Upsert
  greift, `content_hash`-Dedup verwirft den Re-Post. (Das Kernrisiko aus §5.)
- **TZ-Bucketing:** Sample um Mitternacht (Europe/Vienna) landet im korrekten
  lokalen Tag; Schlafsession wird dem Aufwach-Tag zugeordnet (§4.3).
- **Analyse-Mathematik:** synthetische Reihen mit **bekannter** Lag-Korrelation →
  Pipeline findet sie beim richtigen Lag; injizierte Anomalie wird erkannt;
  FDR-Korrektur senkt Zufallstreffer. Reproduzierbar mit festem Seed.
- **Migrationen gegen echtes Postgres/TimescaleDB:** `service`-Container in CI,
  `alembic upgrade head` von leerem Schema — die DDL läuft sonst nur beim Endnutzer
  das erste Mal.
- **Smoke:** Image bauen, mit `PUID/PGID` + `/config`-Mount + Timescale-Service
  booten, `/api/health` abfragen, prüfen dass der Entrypoint chownt und beide
  s6-Services hochkommen.

## 8. CI/CD – GitHub Workflows

Drei Workflows mit gepinnten Action-SHAs:

| Workflow | Trigger | Tut |
|---|---|---|
| `test.yml` | `pull_request` **+** `workflow_call` | Lint, pytest (inkl. Migrationen gegen Timescale-Service), Smoke. Reusable, damit Build-Workflows darauf gaten. |
| `dev.yml` | Push auf `dev` | `uses: test.yml` → nur bei grün: Build + Push `:dev` und `:dev-<sha>` nach **GHCR**. |
| `build.yml` | Push Tag `v*` (+ `workflow_dispatch`) | `uses: test.yml` → bei grün: Build + Push `:vX.Y.Z` und `:latest` nach **GHCR** + GitHub-Release (`generate_release_notes`). |

- **Registry: GHCR** (`ghcr.io/<owner>/healthlog`). Docker-Hub-Mirror ist bewusst
  out of scope (siehe §10).
- Least-Privilege: `test.yml` hat `contents: read`; nur `dev.yml`/`build.yml` fordern
  `packages: write` (bzw. `contents: write` für das Release) auf ihren eigenen Jobs an.
- Plattform `linux/amd64` (Unraid-Ziel); arm64 bei Bedarf nachrüstbar (§10).
- **Tests blocken den Push:** ein roter Lauf verhindert `:dev`/`:vX.Y.Z` — ein kaputter
  Commit erreicht nie ein Image.

**Dependabot** (`.github/dependabot.yml`) hält Abhängigkeiten aktuell — drei Ökosysteme,
**wöchentlich**, PRs gegen **`dev`** (nie `main`, §9), je gruppiert (ein PR statt vieler,
vermeidet gegenseitige Merge-Konflikte an den gepinnten SHA-Zeilen):
- `github-actions` (`/`) — hält die SHA-Pins der drei Workflows frisch.
- `pip` (`/backend`) — `requirements.txt` + `requirements-dev.txt`.
- `docker` (`/backend`) — Base-Image-Bumps (`python:3.12-slim`, s6-overlay).

Dependabot-PRs durchlaufen dasselbe `test.yml`-Gate wie jeder andere PR.

## 9. Branching & Release

- Entwicklung auf kurzlebigen `feature/*`-Branches, abgezweigt von `dev`.
- **PRs immer gegen `dev`**, nie direkt gegen `main`.
- `main` wird ausschließlich per PR `dev → main` aktualisiert; **Release = Tag-Push**
  `vX.Y.Z` auf `main` → triggert `build.yml` (versioniertes Image + Release).
- `:dev` = Maintainer-Staging-Kanal, `:vX.Y.Z`/`:latest` = Produktion.
- `main` und `dev` per Ruleset geschützt (PR nötig, grüne Checks, keine Force-Pushes).
- Sprache durchgängig Englisch (Code, Kommentare, Commits, PRs).
- Die verbindlichen Regeln stehen in `CONTRIBUTING.md`.

## 10. Scope & bewusste Grenzen

- **Single-tenant** (keine `user_id`/`subject_id`). Die Analyse ist pro Person; ein
  späterer Multi-User-/Subjekt-Ausbau ist eine saubere Migration (Spalte nullable +
  Default 1 + Backfill), da die Idempotenz-Keys schon stabil sind.
- **Roh-Payload verbatim** archiviert (`raw_ingest`, JSONB), Retention dauerhaft
  (Volumen winzig); ein CA kann die Tagesaggregat-View später ohne Schema-Bruch ersetzen.
- **ECG/GPX bleiben bewusst aus** (rohe Waveforms/Standortdaten, kein Analysenutzen,
  Payload-/Privacy-Last). Das generische Modell (§4.0) verträgt weitere Health-Kategorien
  (Medikamente, Symptome als Event-Marker) jederzeit ohne Schema-Änderung, falls relevant.
- **Optional / am Server, nicht eingebaut:** interaktive Jupyter-Exploration deckt nur
  den Ad-hoc-Fall ab, den Pipeline + Grafana schon abdecken.
- **Out of scope (bei Bedarf nachrüstbar):** Docker-Hub-Mirror der Images (aktuell nur
  GHCR), arm64-Image (aktuell nur `linux/amd64`), eine eigene Web-App.

## 11. Methodische Stolperfallen

- **Tagesraster:** Alles auf Kalendertage (lokale TZ) resamplen, sonst sind Metriken nicht vergleichbar.
- **Zeitversatz:** Effekte wirken verzögert (Training heute → HRV morgen) — Lag-Korrelationen, nicht nur Lag 0.
- **Spearman statt Pearson:** physiologische Daten sind oft nicht-linear/nicht-normalverteilt.
- **Multiple Testing:** viele Metrik-Paare → Zufallstreffer. FDR-Korrektur (`p_value_adj`), Befunde als Hinweis behandeln. Korrelation ≠ Kausalität.
- **Genug Historie:** vor ~6–8 Wochen sind Korrelationen wenig belastbar (→ Bulk-Backfill, §5).
- **Saisonalität:** Wochentag-Effekte (Wochenende ≠ Werktag) bei Anomalien berücksichtigen.
- **Aggregat-Semantik:** je Metrik den richtigen Tageswert nehmen (Registry) — Steps summieren, RestingHR minimieren, kein "avg über alles".

## 12. Privacy-Checkliste

- Ingest-Endpoint nur über TLS + Secret-Header/Token erreichbar (HAE unterstützt Custom Headers).
- DB nicht öffentlich exponiert; Grafana hinter Auth.
- LLM rein lokal (Ollama, kein API-Key, kein Netzwerk-Egress); die Narration erhält nur
  statistische Befund-Größen, nie Rohwerte (`scrub_details()`).
- Keine Telemetrie in den Komponenten aktivieren.
