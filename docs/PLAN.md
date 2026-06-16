# HealthLog – Plan: Privacy-First Apple-Health-Analyse

> Self-hosted Analyse von Apple-Health-Daten mit Fokus auf Korrelationen,
> Anomalien und Trends — vollständig auf eigener Hardware, keine externen
> Anbieter.
>
> **Projektname:** HealthLog (Repo-Slug `healthlog`)
> **Status:** Plan-Entwurf, verfeinert. Noch nicht implementiert. Nächster
> Schritt ist Phase 0 (Daten-Audit) — erst nach Freigabe.

## 1. Grundentscheidungen

- **Datenexport:** Health Auto Export (iPhone) → REST-Automation an eigenen Endpoint
- **Topologie:** Always-On-Server (Ingestion + DB + Grafana + Routine-Analyse) ⟷ Mac (interaktive Exploration + LLM)
- **Analyse-Kern:** klassische Statistik/ML (Korrelationen, Anomalien, Trends) — **kein** LLM im kritischen Pfad
- **LLM:** Ollama auf dem Mac (32 GB Unified Memory) als **Ausbaustufe (Phase 4)** für Klartext-Reports; Zielklasse 8–14B (z. B. Qwen 2.5 14B)
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
┌─ Always-On-Server (Docker Compose) ─────────────────────┐
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
                            │  read (psql)
┌─ Mac (Apple Silicon, 32 GB) ───────────────────────────┐
│  Interaktive Exploration: Jupyter · pandas · statsmodels│
│  (Phase 4) Ollama → Wochen-Report aus `findings`        │
└────────────────────────────────────────────────────────┘
```

### Prozess- statt Container-Trennung (Begründung)

Ingest und Analyse leben **in einem Container**, aber als **getrennte OS-Prozesse**
unter `s6-overlay` (sauberes PID 1: Signal-Handling, Zombie-Reaping, Restart-Policy).
Die rechenintensive Analyse (pandas/numpy/statsmodels) darf **nicht** im uvicorn-Prozess
laufen — sie würde über GIL/CPU-Last den Event-Loop blockieren und HAE-POSTs verzögern.
Getrennte Prozesse = OS-Level-Isolation. Die Analyse wird vom Scheduler zusätzlich als
**kurzlebiger Subprozess** (`python -m healthlog.analysis`) gestartet, sodass selbst ein
harter Crash in einer C-Extension nur diesen Subprozess killt — Scheduler **und** uvicorn
überleben, die Datenannahme ist strukturell abgeschirmt.

| Komponente | Wo | Warum |
|---|---|---|
| Ingestion (uvicorn) | App-Container, Dauerprozess | 24/7 Annahme der HAE-POSTs |
| Scheduler (APScheduler) | App-Container, eigener Prozess | triggert nachts, Logs→stdout, Env/TZ sauber |
| Analyse | App-Container, Subprozess des Schedulers | fault-isoliert, gefährdet die Annahme nicht |
| TimescaleDB | eigener Container | Postgres ohnehin separat |
| Grafana | eigener Container | fertig, direkt auf Timescale |
| Interaktive Exploration (Jupyter) | Mac | bequem lokal während der Findungsphase |
| LLM-Reports (Phase 4) | Mac | Apple Silicon (Unified Memory) spielt hier seine Stärke aus |

## 3. Tech-Stack & Begründung

| Schicht | Wahl | Warum |
|---|---|---|
| Ingestion | Eigenes **FastAPI** (statt offiziellem HAE-Server) | HAE postet JSON an jeden Endpoint; konsistent mit SQL-Storage; bekannter Stack. Der offizielle Server setzt auf MongoDB — Bruch zum SQL-Analyseziel. |
| Storage | **TimescaleDB** (Postgres-Extension) | Zeitreihen-Hypertables, Continuous Aggregates für Tageswerte, SQL für Korrelationen, native Grafana-Anbindung. |
| Scheduler | **APScheduler** (eigener Prozess unter s6) | Zeitplan im Code (versioniert), Logs→stdout (Docker-nativ), Env/TZ sauber — vs. cron-im-Container-Reibung. |
| Analyse | **Python: pandas + statsmodels + scipy + scikit-learn** | Reifer, reproduzierbarer Standard für Korrelation/Trend/Anomalie. |
| Dashboards | **Grafana** | Fertig, minimaler Aufwand, direkt auf Timescale. |
| Container-Basis | **`python:3.12-slim` + s6-overlay v3** | Schlankes Image (relevant für Public), volle Kontrolle, PUID/PGID + `/config` wie bei PocketLog. |
| LLM (Phase 4) | **Ollama**, 8–14B (z. B. Qwen 2.5 14B) | Lokal auf dem 32-GB-Mac; erhält nur fertige Befunde, nicht die Rohdaten. |

## 4. Datenmodell (Skizze)

> **Wichtig:** Das endgültige Schema wird erst **nach Phase 0** fixiert — die
> reale HAE-Payload-Struktur (Aggregation, Felder, Einheiten) entscheidet
> die Details. Die folgende Skizze nimmt die bekannten HAE-Eigenheiten vorweg.

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
5. **Generische Tagesaggregate** (§4.6): das CA bucketet per `(day, metric)`, ganz ohne
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

**An echter Payload (v2, 7 Tage, 30 Metriken) verifiziert.** HAE liefert pro Metrik
ein `data`-Array von Buckets in genau **zwei Shapes**:
- **`{Min, Avg, Max}`** — in der Praxis **nur `heart_rate`**.
- **`{qty}`** — alle übrigen 29 Metriken (auch HRV, Ruhepuls, Atemfrequenz, SpO₂).

Daher **eine Zeile pro Metrik-Bucket** mit nullbaren Aggregat-Spalten (füllen, was HAE
liefert), **nicht** ein einzelnes `value`. Das Modell ist **generisch**: jede Metrik
landet hier, ohne Schema-Änderung (siehe Inventar §4.7) — wir ingesten **alle**
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
                UNIQUE (sleep_start, source))
```
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
`stepCount`, `heartRateRecovery`, …), die nur ins Roh-Archiv gehen.

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
sonst zerfasern Typen über Sprachwechsel. `duration` in Sekunden, Energie in `kJ`
(→ kcal normalisieren, §4.5).

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

### 4.7 Metrik-Inventar (aus echter Payload, Phase 0)

30 Metriken im Export. Vorläufige Tier-Einteilung (Registry-Seed, in Phase 0 finalisiert):

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

Da das Modell generisch ist, kostet das Mitnehmen aller 30 praktisch nichts — neue
Metriken in künftigen Exports landen automatisch im Roh-Archiv und in `metric_samples`
und werden nur per Registry-Zeile „adoptiert".

### 4.6 Tagesaggregate (Continuous Aggregate)

Ein einzelnes CA kann nicht "die richtige" Aggregation pro Metrik liefern, deshalb
berechnet es **alle** Aggregate pro `(day, metric)`; die Analyse pickt je Metrik die
laut `metric_registry.agg_default` passende Spalte:

```sql
daily_metrics (day, metric, avg, vmin, vmax, sum, n)
  -- time_bucket('1 day', time, 'Europe/Vienna')  ← lokale TZ, NICHT UTC!
```
**Caveat:** `avg` ist hier ein `avg(vavg)` (Mittel der Bucket-Mittel) ohne Gewichtung
— für Tagesgranularität ausreichend; bei Bedarf exakt über das Roh-Archiv nachrechenbar.

### 4.7 Befunde der Pipeline (reine Statistik, kein LLM)

```sql
findings (id, computed_at, kind TEXT,            -- 'correlation'|'anomaly'|'trend'
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

## 5. Ingestion-Vertrag

- **Idempotenz/Upsert:** HAE sendet bei nächtlichen Exporten **überlappende Fenster**.
  Ohne Dedup wachsen Dubletten und verfälschen Tagesaggregate. Daher
  `INSERT … ON CONFLICT (UNIQUE-Key) DO UPDATE` auf alle Zieltabellen.
  `raw_ingest.content_hash` verwirft identische Re-Posts früh.
- **Backfill vs. Delta:** Beim ersten manuellen HAE-Export die **gesamte
  Apple-Health-Historie** (Jahre) einmal als Bulk-Backfill einspielen → danach
  **nächtliche Deltas**. Damit sind Korrelationen ab Tag 1 belastbar (≥6–8 Wochen).
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
- **PUID/PGID + `/config`** wie PocketLog: Entrypoint chownt `/config`, dropt
  Privilegien. `/config` hält Persistenz, die nicht in der DB liegt: Ingest-Secret,
  Logs, evtl. DB-Backups/Export.
- **Env-getriebene Config:** `INGEST_SECRET`, `DATABASE_URL`, `TZ`,
  `ANALYSIS_CRON`/Uhrzeit, `LOG_LEVEL`, `LOG_FORMAT` (text/json) — analog PocketLog.
- **Compose:** `timescaledb` + `healthlog` + `grafana`; DB nicht öffentlich
  exponiert, Grafana hinter Auth, Reverse-Proxy/TLS vor dem Ingest.
- **Public-Tauglichkeit:** README + Beispiel-`docker-compose.yml`, sinnvolle Defaults,
  keine Telemetrie, Doku der HAE-Automation-Einrichtung.

## 7. Tests & Qualität

Übernimmt PocketLogs Philosophie: **grüne Suite ist Pflicht-Gate vor jedem Image-Push**
(§8). HealthLogs Kern-Risiko liegt nicht in CRUD, sondern in **Parser-Korrektheit**
und **Analyse-Mathematik** — genau dort liegt der Testfokus.

- **Backend-Lint:** `ruff check` + `ruff format --check` (wie PocketLog).
- **Parser-/Ingest-Tests (pytest):** echte HAE-Payload als Fixture (aus Phase 0) →
  erwartete Zeilen in `metric_samples`/`sleep_sessions`/`workouts`. Pinnt die
  Übersetzung der HAE-Eigenheiten (Min/Avg/Max-Buckets, Einheiten).
- **Idempotenz:** zweimaliges Posten derselben Payload → keine Dubletten, Upsert
  greift, `content_hash`-Dedup verwirft den Re-Post. (Das Kernrisiko aus §5.)
- **TZ-Bucketing:** Sample um Mitternacht (Europe/Vienna) landet im korrekten
  lokalen Tag; Schlafsession wird dem Aufwach-Tag zugeordnet (§4.3).
- **Analyse-Mathematik:** synthetische Reihen mit **bekannter** Lag-Korrelation →
  Pipeline findet sie beim richtigen Lag; injizierte Anomalie wird erkannt;
  FDR-Korrektur senkt Zufallstreffer. Reproduzierbar mit festem Seed.
- **Migrationen gegen echtes Postgres/TimescaleDB:** `service`-Container in CI,
  `alembic upgrade head` von leerem Schema (analog PocketLogs MariaDB-Job — die DDL
  läuft sonst nur beim Endnutzer das erste Mal).
- **Smoke:** Image bauen, mit `PUID/PGID` + `/config`-Mount + Timescale-Service
  booten, `/api/health` abfragen, prüfen dass der Entrypoint chownt und beide
  s6-Services hochkommen.
- Frontend-Tests (Vitest/Playwright) erst relevant, falls Phase 5 (eigene Web-App) kommt.

## 8. CI/CD – GitHub Workflows

Drei Workflows, gespiegelt von PocketLog, mit gepinnten Action-SHAs:

| Workflow | Trigger | Tut |
|---|---|---|
| `test.yml` | `pull_request` **+** `workflow_call` | Lint, pytest (inkl. Migrationen gegen Timescale-Service), Smoke. Reusable, damit Build-Workflows darauf gaten. |
| `dev.yml` | Push auf `dev` | `uses: test.yml` → nur bei grün: Build + Push `:dev` und `:dev-<sha>` nach **GHCR**. |
| `build.yml` | Push Tag `v*` (+ `workflow_dispatch`) | `uses: test.yml` → bei grün: Build + Push `:vX.Y.Z` und `:latest` nach **GHCR** + GitHub-Release (`generate_release_notes`). |

- **Registry: zunächst nur GHCR** (`ghcr.io/<owner>/healthlog`). Die Docker-Hub-
  `login-action`- und Mirror-`tags`-Zeilen werden **bewusst später ergänzt** — im Plan
  als TODO markiert, damit der Build sofort ohne `DOCKERHUB_*`-Secrets läuft.
- Least-Privilege: `test.yml` hat `contents: read`; nur `dev.yml`/`build.yml` fordern
  `packages: write` (bzw. `contents: write` für das Release) auf ihren eigenen Jobs an.
- Plattform vorerst `linux/amd64` (Unraid-Ziel); arm64 bei Bedarf nachrüstbar.
- **Tests blocken den Push:** ein roter Lauf verhindert `:dev`/`:vX.Y.Z` — ein kaputter
  Commit erreicht nie ein Image.

## 9. Branching & Release (CONTRIBUTING)

**Identisch zu PocketLog:**
- Entwicklung auf kurzlebigen `feature/*`-Branches, abgezweigt von `dev`.
- **PRs immer gegen `dev`**, nie direkt gegen `main`.
- `main` wird ausschließlich per PR `dev → main` aktualisiert; **Release = Tag-Push**
  `vX.Y.Z` auf `main` → triggert `build.yml` (versioniertes Image + Release).
- `:dev` = Maintainer-Staging-Kanal, `:vX.Y.Z`/`:latest` = Produktion.
- `main` und `dev` per Ruleset geschützt (PR nötig, grüne Checks, keine Force-Pushes).
- Sprache durchgängig Englisch (Code, Kommentare, Docs, Commits, PRs) — wie PocketLog.
- Ein `CONTRIBUTING.md` hält diese Regeln fest (in Phase 1 anzulegen).

## 10. Phasen-Fahrplan

### Phase 0 – Daten-Audit ✅ (Sample ausgewertet)
Reale HAE-Payload (v2, 7 Tage, 30 Metriken + 1 Workout) liegt vor und ist analysiert:
zwei Bucket-Shapes, Datums-/TZ-Format, Schlafstruktur (Aufwach-Tag-Zuordnung),
Workout-`id`, Einheiten-Realität (Energie in kJ) — alles in §4 eingearbeitet, Inventar
in §4.7. **Offen für den Abschluss:** finale `metric_registry`-Befüllung (Tier/Einheit/
agg pro Metrik) und der **Bulk-Backfill** (gesamte Historie) vor den nächtlichen Deltas.

### Phase 1 – Ingestion + Storage
Docker-Compose mit TimescaleDB + dem `healthlog`-Image (uvicorn + Scheduler-Skelett),
Roh-Archiv + Parser + Upsert, HAE-Automation (nächtlich, Secret-Header),
Reverse-Proxy-Route. Ziel: Daten landen zuverlässig und idempotent.
**Parallel:** Repo-Grundgerüst — `CONTRIBUTING.md`, die drei Workflows (§8, GHCR-only)
und die ersten Parser-/Idempotenz-/TZ-Tests (§7), damit das Gate von Anfang an grün ist.

### Phase 2 – Exploration (Jupyter auf dem Mac)
Daten verstehen, Tagesaggregate (lokale TZ) prüfen, erste manuelle Korrelations-
und Trend-Plots. Hier lernst du, was überhaupt aussagekräftig ist.

### Phase 3 – Automatische Pipeline (im App-Container)
APScheduler triggert nachts den Analyse-Subprozess: Lag-Korrelationen (Spearman,
Lags 0–3 Tage) + Anomalie-Erkennung (28-Tage rolling Median + MAD) + Trends (STL),
Befunde → `findings` (mit FDR-`p_value_adj`). Begleitend die Analyse-Mathematik-Tests
gegen synthetische Reihen (§7).

### Phase 4 – Visualisierung + optionales LLM
Grafana-Dashboards (Trainingslast vs. HRV/Ruhepuls, Schlaf-Trends, `findings` als
Annotationen). Danach Ollama-Narration aus `findings` (Mac, 8–14B). Modell erhält
nur strukturierte Befunde; Zahlen werden gegroundet, nicht halluziniert.

### Phase 5 (optional, später)
Eigene Web-App im PocketLog-Stil.

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
- LLM rein lokal (Ollama, kein API-Key, kein Netzwerk-Egress).
- Keine Telemetrie in den Komponenten aktivieren.

## 13. Offene Punkte

### Entschieden (diese Session)
- Roh-Payload wird **verbatim archiviert** (`raw_ingest`, JSONB) — Replay-fähig.
- Retention: **Rohdaten dauerhaft behalten** (Volumen winzig), CA zusätzlich; Policy später revisiten.
- Scheduler: **ein Container**, s6-overlay, uvicorn + APScheduler-Prozess, Analyse als Subprozess.
- Container-Basis: **`python:3.12-slim` + s6-overlay v3**, PUID/PGID + `/config`.
- LLM-Korridor: **8–14B** (32-GB-Mac), konkrete Wahl in Phase 4.
- CI: drei Workflows nach PocketLog-Muster; Release per Tag, Gate auf grüne Tests.
- CONTRIBUTING: `dev`-Entwicklung, PR gegen `dev`, Release per Tag auf `main`.

### TODO (bewusst aufgeschoben)
- **Docker Hub:** Workflows pushen vorerst nur nach GHCR; Docker-Hub-Login + Mirror-Tags
  (+ `DOCKERHUB_*`-Secrets) später in `dev.yml`/`build.yml` ergänzen.
- **arm64-Image** bei Bedarf (aktuell nur `linux/amd64`).

### Phase 0 – durch Sample geklärt ✅
- Bucket-Shapes, Spaltenbelegung `metric_samples`, Datums-/TZ-Format → §4.2.
- Schlaf- und Workout-Struktur (inkl. Workout-`id`, Aufwach-Tag-Zuordnung) → §4.3/§4.4.
- Metrik-Inventar + vorläufige Tier-Einteilung → §4.7.

### Noch offen (Abschluss Phase 0 / Start Phase 1)
- **Finale `metric_registry`-Befüllung:** Tier/Einheit/`agg_default` pro der 30 Metriken
  festzurren (Stub-Heuristik aus §5 als Startpunkt).
- **Bulk-Backfill:** einmaliger Voll-Export der gesamten Apple-Health-Historie vor den
  nächtlichen Deltas (HAE-Datumsbereich „Alle"/größtmöglich).
- **Workout-Typ-Normalisierung:** Mapping lokalisierter `name` → kanonischer Typ (§4.4).

### Optional „think bigger" — nicht aktivierte Health-Kategorien
Im Sample bewusst aus (ECG/GPX) bzw. ungenutzt. Falls du sie **trackst**, lohnt das
Mitnehmen für reichere Korrelationen — das Modell verträgt sie ohne Änderung (§4.0):
- **Gemütszustand / State of Mind** (iOS 17+): Stimmung ↔ Schlaf/Aktivität wäre eine der
  wertvollsten Korrelationen überhaupt — **nur sinnvoll, wenn du Mood regelmäßig loggst**.
- **Medikamente, Symptome:** als Event-Marker für Anomalie-Kontext denkbar.
- **ECG/GPX bleiben bewusst aus** (rohe Waveforms/Standortdaten, kein Analysenutzen,
  Payload-/Privacy-Last).

### Gated auf Phase 4
- Konkretes Ollama-Modell + Prompt-/Grounding-Design für die Report-Narration.
